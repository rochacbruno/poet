# -*- coding: utf-8 -*-

import tempfile

import os
import re
import shutil
import subprocess
import logging

from packaging.utils import canonicalize_name
from pip.download import unpack_url
from pip.index import Link
from piptools.resolver import Resolver
from piptools.repositories import PyPIRepository
from piptools.scripts.compile import get_pip_command
from piptools.cache import DependencyCache
from piptools.utils import is_pinned_requirement, key_from_req

from .locations import CACHE_DIR
from .package.pip_dependency import PipDependency
from .utils.helpers import call, template

# Silencing pip loggers
pip_git_logger = logging.getLogger('pip.vcs.git')
pip_git_logger.setLevel(logging.ERROR)


class Installer(object):

    UNSAFE = ['setuptools']

    def __init__(self, command, repository, with_progress=False, dry_run=False):
        self._command = command
        self._poet = command.poet
        self._repository = repository
        self._with_progress = with_progress
        self._dry_run = dry_run

    def install(self, features=None, dev=True):
        """
        Install packages defined in configuration files.
        
        If a lock file does not exist, it will lock dependencies
        before installing them.
        
        :param features: Features to install
        :type features: list or None
        
        :param dev: Whether to install dev dependencies or not
        :type dev: bool
        
        :rtype: None 
        """
        if not os.path.exists(self._poet.lock_file):
            if features:
                for feature in features:
                    if feature not in self._poet.features:
                        raise ValueError(
                            'Feature [{}] does not exist'
                            .format(feature)
                        )

            self.lock(dev=dev)

            return self.install(features=features, dev=dev)

        lock = self._poet.lock
        if features:
            for feature in features:
                if feature not in lock.features:
                    raise ValueError(
                        'Feature [{}] does not exist'
                        .format(feature)
                    )

        self._command.line('')
        self._command.line('<info>Installing dependencies</>')
        self._command.line('')

        deps = lock.pip_dependencies

        if dev:
            deps += lock.pip_dev_dependencies

        featured_packages = set()
        for feature, packages in lock.features.items():
            if feature in features:
                for package in packages:
                    featured_packages.add(canonicalize_name(package))

        # Getting already installed packages
        installed = {}
        freezed = self._call([self._command.pip(), 'freeze'])
        for req_line in freezed.split(os.linesep):
            if not req_line.startswith('-e '):
                if '==' not in req_line:
                    continue

                name, version = req_line.split('==')
            else:
                # Finding revision
                m = re.match('@(?P<revision>.+)#egg=(?P<name>.+)$', req_line)
                if not m:
                    # Malformed string?
                    # We do not consider it installed to make a clean install
                    continue

                name = m.group('name')
                version = m.group('revision')

            installed[canonicalize_name(name)] = version

        skipped = 0
        total = len(deps)
        for dep in deps:
            name = dep.name

            # Package is optional but is not featured
            if dep.optional and name not in featured_packages:
                skipped += 1

                continue

            # Checking if the package is already installed
            if name in installed:
                if not dep.is_vcs_dependency():
                    version = dep.constraint.replace('==', '')
                else:
                    version = dep.pretty_constraint.split(' ')[-1]

                if version == installed[name]:
                    if self._command.output.is_verbose():
                        self._command.line(
                            ' - Skipping <info>{}</> '
                            '(Version <comment>{}</> is already installed)'
                                .format(name, version)
                        )

                    skipped += 1

                    continue

            # Checking Python version
            if dep.is_python_restricted():
                python_version = self._command.python_version
                if not any([python_version in python for python in dep.python]):
                    # If the package is not compatible
                    # with the current Python version
                    # we do not install
                    if self._command.output.is_verbose():
                        self._command.line(
                            ' - Skipping <info>{}</> '
                            '(Specifies Python <comment>{}</> and current Python is <comment>{}</>)'
                            .format(name, ','.join([str(p) for p in dep.python]), python_version)
                        )

                    skipped += 1

                    continue


            cmd = [self._command.pip(), 'install', dep.normalized_name]

            if dep.is_vcs_dependency():
                constraint = dep.pretty_constraint

                # VCS must be updated to be installed
                cmd.append('-U')
            else:
                constraint = dep.constraint.replace('==', '')

            message = (
                ' - Installing <info>{}</> (<comment>{}</>)'
                .format(name, constraint)
            )
            end_message  = (
                'Installed <info>{}</> (<comment>{}</>)'
                .format(name, constraint)
            )
            error_message = 'Error while installing [{}]'.format(name)

            self._progress(cmd, message[3:], end_message, message, error_message)

        if skipped == total:
            self._command.line('<info>All dependencies already installed</>')

    def update(self, packages=None, features=None, dev=True):
        if self._poet.is_lock():
            raise Exception('Update is only available with a poetry.toml file.')

        if packages and features:
            raise Exception('Cannot specify packages and features when updating.')

        self._command.line('')
        self._command.line(
            '<info>Updating dependencies</>{}'
            .format('' if not self._dry_run else ' (<comment>Dry run</>)')
        )
        self._command.line('')

        # Reading current lock
        lock = self._poet.lock
        current_deps = lock.pip_dependencies
        if dev:
            current_deps += lock.pip_dev_dependencies

        # Resolving new dependencies and locking them
        deps = self._poet.pip_dependencies
        if dev:
            deps += self._poet.pip_dev_dependencies

        featured_packages = set()
        for feature, _packages in self._poet.features.items():
            if feature in features:
                for package in _packages:
                    featured_packages.add(canonicalize_name(package))

        # Removing optional packages unless they are featured packages
        deps = [
            dep
            for dep in deps
            if not dep.optional
               or dep.optional and dep.name in featured_packages
        ]

        if packages:
            packages = [canonicalize_name(name) for name in packages]
            to_resolve_deps = [dep for dep in deps if dep.name in packages]

            packages = self.resolve_and_compare(to_resolve_deps, deps)
        else:
            packages = self.resolve(deps)

        deps = [PipDependency(p['name'], p['version'], p['category']) for p in packages]

        delete = not packages and not features
        actions = self._resolve_update_actions(deps, current_deps, delete=delete)

        if not actions:
            self._command.line('<info>Dependencies already up-to-date!</info>')

            return

        installs = len([a for a in actions if a[0] == 'install'])
        updates = len([a for a in actions if a[0] == 'update'])
        uninstalls = len([a for a in actions if a[0] == 'remove'])

        self._command.line(
            '<info>Package operations: '
            '<comment>{}</> install{}, <comment>{}</> update{} and <comment>{}</> uninstall{}'
            '</info>'
            .format(
                installs, 's' if installs != 1 else '',
                updates, 's' if updates != 1 else '',
                uninstalls, 's' if uninstalls != 1 else '',
            )
        )

        self._perform_update_actions(actions)

        if self._dry_run:
            return

        # If everything went well, we write down the lock file
        features = {}
        for name, featured_packages in self._poet.features.items():
            name = canonicalize_name(name)
            features[name] = [canonicalize_name(p) for p in featured_packages]

        self._write_lock(packages, features)

    def lock(self, dev=True):
        if self._poet.is_lock():
            return

        self._command.line('')
        self._command.line('<info>Locking dependencies to <comment>poetry.lock</></>')
        self._command.line('')

        deps = self._poet.pip_dependencies

        if dev:
            deps += self._poet.pip_dev_dependencies

        packages = self.resolve(deps)
        features = {}
        for name, featured_packages in self._poet.features.items():
            name = canonicalize_name(name)
            features[name] = [canonicalize_name(p) for p in featured_packages]

        self._write_lock(packages, features)

    def resolve(self, deps):
        if not self._with_progress:
            self._command.line('<info>Resolving dependencies</info>')

            return self._resolve(deps)

        with self._spin(
            '<info>Resolving dependencies</info>',
            '<info>Resolving dependencies</info>',
            fmt='%message%%indicator%',
            values=('   ', '.  ', '.. ', '...', ' ..', '  .'),
            interval=100
        ):
            return self._resolve(deps)

    def resolve_and_compare(self, deps, all_deps):
        if not self._with_progress:
            self._command.line('<info>Resolving dependencies</info>')

            return self._resolve_and_compare(deps, all_deps)

        with self._spin(
            '<info>Resolving dependencies</info>',
            '<info>Resolving dependencies</info>',
            fmt='%message%%indicator%',
            values=('   ', '.  ', '.. ', '...', ' ..', '  .'),
            interval=100
        ):
            return self._resolve_and_compare(deps, all_deps)

    def _resolve_and_compare(self, deps, all_deps):
        # Resolving for specific packages
        new_packages = self._resolve(deps)

        # Resolving all
        all_packages = self._resolve(all_deps)

        # Checking compatibility
        packages = []
        for pkg in all_packages:
            package = pkg
            for new_pkg in new_packages:
                if pkg['name'] == new_pkg['name']:
                    # If the versions of the package are different
                    # we have an incompatibility
                    if pkg['version'] != pkg['version']:
                        raise Exception(
                            'Incompatibility detected for package [{}] '
                            '(Version {} incompatible with version {})'
                                .format(
                                pkg['name'],
                                pkg['version'],
                                new_pkg['version']
                            )
                        )

                    package = pkg

                    break

            packages.append(package)

        return packages

    def _resolve(self, deps):
        # Checking if we should active prereleases
        prereleases = False
        for dep in deps:
            if dep.accepts_prereleases():
                prereleases = True
                break

        constraints = [dep.as_requirement() for dep in deps]

        command = get_pip_command()
        opts, _ = command.parse_args([])

        resolver = Resolver(
            constraints, PyPIRepository(opts, command._build_session(opts)),
            cache=DependencyCache(CACHE_DIR),
            prereleases=prereleases
        )
        matches = resolver.resolve()
        pinned = [m for m in matches if not m.editable and is_pinned_requirement(m)]
        unpinned = [m for m in matches if m.editable or not is_pinned_requirement(m)]
        reversed_dependencies = resolver.reverse_dependencies(matches)

        # Complete reversed dependencies with cache
        cache = resolver.dependency_cache.cache
        for m in unpinned:
            name = key_from_req(m.req)
            if name not in cache:
                continue

            dependencies = cache[name][list(cache[name].keys())[0]]
            for dep in dependencies:
                dep = canonicalize_name(dep)
                if dep not in reversed_dependencies:
                    reversed_dependencies[dep] = set()

                reversed_dependencies[dep].add(canonicalize_name(name))

        hashes = resolver.resolve_hashes(pinned)
        packages = []
        for m in matches:
            name = key_from_req(m.req)
            if name in self.UNSAFE:
                continue

            version = str(m.req.specifier)
            if m in unpinned:
                m = re.match('(?P<url>.+)@(?P<revision>.+?)#egg=(?P<name>.+)$', m.link.url)
                url = m.group('url')
                rev = m.group('revision')

                version = self._get_vcs_version(url, rev)
                checksum = 'sha1:{}'.format(version['rev'])
            else:
                version = version.replace('==', '')
                checksum = list(sorted(hashes[m]))

            # Figuring out category and optionality
            category = None
            optional = False

            # Checking if it's a main dependency
            for dep in deps:
                if dep.name == name:
                    category = dep.category
                    optional = dep.optional
                    break

            if not category:
                def _category(child):
                    opt = False
                    cat = None
                    parents = reversed_dependencies.get(child, set())
                    for parent in parents:
                        for dep in deps:
                            if dep.name != parent:
                                continue

                            opt = dep.optional

                            if dep.category == 'main':
                                # Dependency is given by at least one main package
                                # We flag it as main
                                return 'main', opt

                            return 'dev', opt

                        cat, op = _category(parent)

                        if cat is not None:
                            return cat, opt

                    return cat, opt

                category, optional = _category(name)

            # If category is still None at this point
            # The dependency must have come from a VCS
            # dependency. To avoid missing packages
            # we assume "main" category and not optional
            if category is None:
                category = 'main'
                optional = False

            if not isinstance(checksum, list):
                checksum = [checksum]

            # Retrieving Python restriction if any
            python = self._get_pythons_for_package(
                name, reversed_dependencies, deps
            )
            python = list(python)

            if '*' in python:
                # If at least one parent gave a wildcard
                # Then it should be installed for any Python version
                python = ['*']

            package = {
                'name': name,
                'version': version,
                'checksum': checksum,
                'category': category,
                'optional': optional,
                'python': python
            }

            packages.append(package)

        return sorted(packages, key=lambda p: p['name'].lower())

    def _resolve_update_actions(self, deps, current_deps, delete=True):
        """
        Determine actions on depenncies.
        
        :param deps: New dependencies
        :type deps: list[poet.package.PipDependency]
        
        :param current_deps: Current dependencies
        :type current_deps: list[poet.package.PipDependency]
        
        :param delete: Whether to add delete actions or not
        :type delete: bool
        
        :return: List of actions to execute
        :type: list[tuple]
        """
        actions = []
        for dep in deps:
            action = None
            from_ = None
            found = False

            for current_dep in current_deps:
                name = dep.name
                current_name = current_dep.name
                version = dep.normalized_constraint
                current_version = current_dep.normalized_constraint

                if name == current_name:
                    # Existing dependency
                    found = True

                    if version == current_version:
                        break

                    # If version is different we mark it
                    # as to be updated
                    action = 'update'
                    from_ = current_dep
                    break

            if not found:
                # New dependency. We mark it as to be installed.
                action = 'install'

            if action:
                actions.append((action, from_, dep))

        if not delete:
            return actions

        # We need to check if we have to remove
        # any dependency
        for dep in current_deps:
            found = False

            for new_dep in deps:
                if dep.name == new_dep.name:
                    found = True

                    break

            if not found:
                actions.append(('remove', None, dep))

        return actions

    def _perform_update_actions(self, actions):
        for action, from_, dep in actions:
            cmd = [self._command.pip()]
            description = 'Installing'

            if action == 'remove':
                description = 'Removing'
                cmd += ['uninstall', dep.normalized_name, '-y']
            elif action == 'update':
                description = 'Updating'
                cmd += ['install', dep.normalized_name, '-U']
            else:
                cmd += ['install', dep.normalized_name]

            name = dep.name

            if dep.is_vcs_dependency():
                constraint = dep.pretty_constraint
            else:
                constraint = dep.constraint.replace('==', '')

            version = '<comment>{}</>'.format(constraint)

            if from_:
                if from_.is_vcs_dependency():
                    constraint = from_.pretty_constraint
                else:
                    constraint = from_.constraint.replace('==', '')

                version = '<comment>{}</> -> '.format(constraint) + version

            message = ' - {} <info>{}</> ({})'.format(description, name, version)
            start_message = message[3:]
            end_message = '{} <info>{}</> ({})'.format(description.replace('ing', 'ed'), name, version)
            error_message = 'Error while {} [{}]'.format(description.lower(), name)

            if self._dry_run:
                self._command.line(message)

                continue

            self._progress(cmd, start_message, end_message, message, error_message)

    def _get_vcs_version(self, url, rev):
        tmp_dir = tempfile.mkdtemp()
        current_dir = self._poet.base_dir

        try:
            unpack_url(Link(url), tmp_dir, download_dir=tmp_dir, only_download=True)

            os.chdir(tmp_dir)
            call(['git', 'checkout', rev])

            revision = call(['git', 'rev-parse', rev])
            # Getting info
            revision = revision.strip()
            version = {
                'git': url,
                'rev': revision
            }
        except Exception:
            raise
        finally:
            shutil.rmtree(tmp_dir)
            # Going back to current directory
            os.chdir(current_dir)

        return version

    def _write_lock(self, packages, features):
        self._command.line('<info>Writing dependencies</>')

        content = self._generate_lock_content(packages, features)

        with open(self._poet.lock_file, 'w') as f:
            f.write(content)

    def _generate_lock_content(self, packages, features):
        lock_template = template('poetry.lock')

        return lock_template.render(
            name=self._poet.name,
            version=self._poet.version,
            packages=packages,
            features=features
        )

    def _get_pythons_for_package(self, name, reversed_dependencies, deps):
        pythons = set()
        if name not in reversed_dependencies:
            # Main dependency
            for dep in deps:
                if name == dep.name:
                    for p in dep.python:
                        pythons.add(str(p))

                    break

            if not len(pythons):
                pythons.add('*')

            return pythons

        parents = reversed_dependencies[name]

        for parent in parents:
            parent_pythons = self._get_pythons_for_package(
                parent, reversed_dependencies, deps
            )

            pythons = pythons.union(parent_pythons)

        if not len(pythons):
            pythons.add('*')

        return pythons

    def _call(self, cmd, error_message=None):
        try:
            return call(cmd)
        except subprocess.CalledProcessError as e:
            if error_message:
                raise Exception(error_message + ' ({})'.format(str(e)))

            raise

    def _progress(self, cmd, start_message, end_message, default_message, error_message):
        if not self._with_progress:
            self._command.line(default_message)

            return self._call(cmd, error_message)

        with self._spin(start_message, end_message):
            return self._call(cmd, error_message)

    def _spin(self, start_message, end_message, fmt=None, values=None, interval=100):
        indicator = self._command.progress_indicator(indicator_values=values, indicator_change_interval=interval)
        if fmt is not None:
            if fmt in indicator.formats:
                fmt = indicator.formats[fmt]

            indicator.format = fmt

        return indicator.auto(start_message, end_message)
