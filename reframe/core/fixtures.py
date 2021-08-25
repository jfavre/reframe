# Copyright 2016-2021 Swiss National Supercomputing Centre (CSCS/ETH Zurich)
# ReFrame Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: BSD-3-Clause

#
# Functionality to use fixtures in ReFrame tests.
#

import copy
import itertools

import reframe.core.namespaces as namespaces
import reframe.core.runtime as runtime
import reframe.utility.udeps as udeps
from reframe.core.exceptions import ReframeSyntaxError
from reframe.core.variables import Undefined


class FixtureRegistry:
    '''Registry to store multiple fixture variants from multiple classes.

    A given regression test class might lead to multiple fixtures. Hence,
    this registry stores the fixtures in key-value mappings, where the
    key is the class deriving from the
    :class:`reframe.pipeline.core.RegressionTest` class and the values are
    sub-mappings for all the different fixture variants arising from each
    class. These sub-mappings use the unique fixture ID (i.e. the ``name``)
    as keys, and a tuple with the fixture variant, valid systems and valid
    programming environments as values.

    This registry defines the naming convention to generate the unique IDs
    for each fixture variants and give support for the different scopes.
    This is resolved by the ``add`` method below.

    A test that modifies the ``name`` attribute will result into undefined
    behavior.

    :meta private:
    '''
    def __init__(self):
        self._reg = dict()

    def add(self, fixture, variant_num, branch, partitions, prog_envs):
        '''Add a fixture to the registry.

        This function handles the naming convention to avoid the clash when
        multiple tests use the same fixtures with the different scope levels.
        Fixtures steal the ``valid_systems`` and ``valid_prog_environs`` from
        the parent test. The nummber of env+partition combinations that get
        stolen from the parent test depends on the fixture's scope level:
         - session: Only one env+part combination per fixture.
         - partition: Only one environment per partition.
         - environment: One test per available part-env combination.
         - test: Use the ``valid_systems`` and ``valid_prog_environs`` from the
           root test without any modification. Fixtures with this scope are not
           shared with any other tests, so their name contains the full tree
           branch leading up to the fixture (this is to avoid collisions with
           any other branches).

        This method returns a list with the names of the newly registered
        fixtures.

        :param fixture: An instance of :class:`TestFixture`.
        :param variant_num: The variant index to instantiate the fixture with.
        :param branch: The branch the fixture belongs to.
        :param partitions: The system partitions supported by the root test.
        :param prog_envs: The valid programming environments from the root.
        '''

        cls = fixture.cls
        scope = fixture.scope
        fname = fixture.get_name(variant_num)
        reg_names = []
        self._reg.setdefault(cls, dict())
        if scope == 'session':
            # The name is just the class name
            name = fname
            self._reg[cls][name] = (variant_num, [prog_envs[0]], [partitions[0]])
            reg_names.append(name)
        elif scope == 'partition':
            for p in partitions:
                # The name contains the full partition name
                name = '_'.join([fname, p])
                self._reg[cls][name] = (variant_num, [prog_envs[0]], [p])
                reg_names.append(name)
        elif scope == 'environment':
            for p in partitions:
                for env in prog_envs:
                    # The name contains the full part and env names
                    name = '_'.join([fname, p, env])
                    self._reg[cls][name] = (variant_num, [env], [p])
                    reg_names.append(name)
        elif scope == 'test':
            # The name contains the full tree branch.
            name = '_'.join([fname, branch])
            self._reg[cls][name] = (variant_num, list(prog_envs), list(partitions))
            reg_names.append(name)

        return reg_names

    def update(self, other):
        '''Extend the current registry with the items from another registry.

        In the event of a clash, the elements from ``other`` take precedence.
        '''
        self._is_registry(other)
        for cls, variants in other._reg.items():
            self._reg.setdefault(cls, dict())
            for name, args in variants.items():
                self._reg[cls][name] = args

    def difference(self, other):
        '''Build a new registry taking the difference with another registry

        The resulting registry contains the elements from the current registry
        that are not present in ``other``.
        '''
        self._is_registry(other)
        ret = FixtureRegistry()
        for cls, variants in self._reg.items():
            if cls in other:
                other_variants = other._reg[cls]
                for name, args in variants.items():
                    if name not in other_variants:
                        ret._reg.setdefault(cls, dict())
                        ret._reg[cls][name] = args
            else:
                ret._reg[cls] = copy.deepcopy(variants)

        return ret

    def instantiate_all(self):
        '''Instantiate all the fixtures in the registry.'''
        ret = []
        for cls, variants in self._reg.items():
            for name, args in variants.items():
                varnum, penv, part = args

                # Set the fixture name and stolen env and part from the parent
                cls.name = name
                cls.valid_prog_environs = penv
                cls.valid_systems = part

                # Instantiate the fixture
                ret.append(cls(variant_num=varnum))

                # Reset cls defaults and append instance
                cls.name = Undefined
                cls.valid_prog_environs = Undefined
                cls.valid_systems = Undefined
        return ret

    def _is_registry(self, other):
        if not isinstance(other, FixtureRegistry):
            raise TypeError('argument is not a FixtureRegistry')

    def __getitem__(self, cls):
        '''Return the names of all registered fixtures from a given class.'''
        try:
            return self._reg[cls].keys()
        except KeyError:
            return []

    def __contains__(self, cls):
        return cls in self._reg


class TestFixture:
    '''Regression test fixture class.

    A fixture is a regression test that generates a resource that must exist
    before the parent test is executed.
    A fixture is a class that derives from the
    :class:`reframe.core.pipeline.RegressionTest` class and serves as a
    building block to compose a more complex test structure.
    Since fixtures are full ReFrame tests on their own, a fixture can have
    multiple fixtures, and so on; building a tree-like structure.

    However, a given fixture may be shared by multiple regression tests that
    need the same resource. This can be achieved by setting the appropriate
    scope level on which the fixture should be shared.
    By default, fixtures are registered with the ``'test'`` scope, which makes
    each fixture "private" to each of the parent tests. Hence, if all fixtures
    use this scope, the resulting fixture hierarchy can be thought of multiple
    independent trees that emanate from each root regression test. On the other
    hand, setting a more relaxed scope that allows resource sharing across
    different regression tests will effectively interconnect the fixture trees
    that share a resource.

    From a more to less restrictive scope, the valid scopes are ``'test'``,
    ``'environment'``, ``'partition'`` and ``'session'``. Fixtures with
    a scope set to either ``'partition'`` or ``'session'`` must derive from
    the :class:`reframe.core.pipeline.RunOnlyRegressionTest` class, since the
    generated resource must not depend on the programming environment.

    Fixtures may be parameterized, where a regression test that uses a
    parameterized fixture is by extension a parameterized test. Hence, the
    number of test variants of a test will depend on the test parameters and
    the parameters of each of the fixtures that compose the parent test. Each
    possible parameter-fixture combination has a unique ``variant_num``, which
    is an index in the range from ``[0, cls.num_variants)``.

    :meta private:
    '''

    def __init__(self, cls, *, scope='test'):
        # Can't use isinstance here because of circular deps.
        rfm_kind = getattr(cls, '_rfm_regression_class_kind', 0)
        if rfm_kind==0:
            raise ReframeSyntaxError(
                f"{cls.__qualname__!r} must be a derived class from "
                f"'RegressionTest'"
            )
        elif rfm_kind & 1:
            if scope in {'session', 'partition'}:
                raise ReframeSyntaxError(
                    f'incompatible scope for fixture {cls.__qualname__}; '
                    f'scope {scope!r} only supports run-only fixtures.'
                )

        if scope not in {'session', 'partition', 'environment', 'test'}:
            raise ReframeSyntaxError(
                f'invalid scope for fixture {cls.__qualname__} ({scope!r})'
            )

        self._cls = cls
        self._scope = scope

    @property
    def cls(self):
        return self._cls

    @property
    def scope(self):
        return self._scope

    def get_name(self, variant_num=None):
        return self.cls.fullname(variant_num)


class FixtureSpace(namespaces.Namespace):
    ''' Regression test fixture space.

    The fixture space is first built by joining the available fixture spaces
    in the base classes, and later extended by the locally defined fixtures
    that are expected in the local fixture space. Defining fixtures with the
    same name in more than one of the base classes is disallowed. However,
    a fixture defined in a base class can be overridden bya fixture defined
    in the derived class under the same name.

    The fixture injection occurs on an instance of the target class. The
    fixtures are first grouped in a fixture registry, which is then injected
    into the target instance under the ``_rfm_fixture_registry`` attribute.
    '''

    @property
    def local_namespace_name(self):
        return '_rfm_local_fixture_space'

    @property
    def namespace_name(self):
        return '_rfm_fixture_space'

    def __init__(self, target_cls=None, target_namespace=None):
        super().__init__(target_cls, target_namespace)

        self.__random_access_iter = tuple(x for x in iter(self))

    def join(self, other, cls):
        '''Join other fixture spaces into the current one.

        :param other: instance of the FixtureSpace class.
        :param cls: the target class.
        '''
        for key, value in other.fixtures.items():
            if key in self.fixtures:
                raise ReframeSyntaxError(
                    f'fixture space conflict: '
                    f'fixture {key!r} is defined in more than '
                    f'one base class of class {cls.__qualname__!r}'
                )

            self.fixtures[key] = value

    def extend(self, cls):
        '''Extend the inherited fixture space with the local fixture space.'''
        local_fixture_space = getattr(cls, self.local_namespace_name)
        while local_fixture_space:
            name, fixture = local_fixture_space.popitem()
            self.fixtures[name] = fixture

        # If any previously declared fixture was defined in the class body
        # by directly assigning it a value, raise an error. Fixtures must be
        # changed using the `x = fixture(...)` syntax.
        for key in cls.__dict__:
            if key in self.fixtures:
                raise ReframeSyntaxError(
                    f'fixture {key!r} must be modified through the built-in '
                    f'fixture type'
                )

    def inject(self, obj, cls=None, fixture_variant=None):
        '''Build a fixture registry and inject it in the parent's test instance.

        A fixture steals the valid_systems and valid_prog_environments from the
        parent tests, and these attributes could be set during the parent
        test's instantiation. Similarly, the fixture registry requires of the
        parent test's full name to build unique IDs for fixtures with the
        ``'test'`` scope (i.e. fixtures private to a parent test).

        :param obj: Parent test's instance.
        :param cls: Parent test's class.
        :param fixture_variant: Index representing a point in the fixture space.

        .. note::
           This function is aware of the implementation of the
           :class:`reframe.core.pipeline.RegressionTest` class.
        '''

        # Nothing to do if the fixture space is empty
        if not self.fixtures or fixture_variant is None:
            return

        # Create the fixture registry
        obj._rfm_fixture_registry = FixtureRegistry()

        # Prepare the partitions and prog_envs
        part, prog_envs = self._get_partitions_and_prog_envs(obj)

        # Get the variant numbers for each of the fixtures (as a k-v map) for
        # the given point in the fixture space.
        fixture_variant_num_map = self[fixture_variant]

        # Register the fixtures
        for name, fixture in self.fixtures.items():
            var_num = fixture_variant_num_map[name]

            # The fixture registry returns the fixture names added to the registry
            dep_names = obj._rfm_fixture_registry.add(fixture, var_num, obj.name,
                                                      part, prog_envs)

            # Add dependencies
            if fixture.scope == 'session':
                dep_mode = udeps.fully
            elif fixture.scope == 'partition':
                dep_mode = udeps.by_part
            elif fixture.scope == 'environment':
                dep_mode = udeps.by_env
            else:
                dep_mode = udeps.by_case

            # Inject the dependency
            for name in dep_names:
                obj.depends_on(name, dep_mode)

    def _get_partitions_and_prog_envs(self, obj):
        '''Process the partitions and programming environs of the parent.'''
        try:
            part = tuple(obj.valid_systems)
        except AttributeError:
            raise ReframeSyntaxError(
                f'valid_systems is undefined in test {obj.name}'
            )
        else:
            rt = runtime.runtime()
            if '*' in part or rt.system.name in part:
                part = tuple(p.fullname for p in rt.system.partitions)

        try:
            prog_envs = tuple(obj.valid_prog_environs)
        except AttributeError:
            raise ReframeSyntaxError(
                f'valid_prog_environs is undefined in test {obj.name}'
            )
        else:
            if '*' in prog_envs:
                all_pes = set()
                for p in runtime.runtime().system.partitions:
                    for e in p.environs:
                        all_pes.add(e.name)
                prog_envs = tuple(all_pes)
        return part, prog_envs

    def __iter__(self):
        '''Walk through all index combinations for all fixtures.'''
        yield from itertools.product(
            *(list(range(f.cls.num_variants)
            for f in self.fixtures.values()))
        )

    def __len__(self):
        if not self.__random_access_iter:
            return 1

        return len(self.__random_access_iter)

    def __getitem__(self, key):
        '''Access an element in the fixture space.

        If the key is an integer, this function will return a mapping with the
        variant numbers of each of the fixtures for the provided point in the
        fixture space. In this case, the fixture must be an index in the range
        of ``[0, len(self))``.
        If the key is just a fixture name, this function will return the
        underlying fixture object with that name.
        '''
        if isinstance(key, int):
            ret = dict()
            f_ids = self.__random_access_iter[key]
            for i,f in enumerate(self.fixtures):
                ret[f] = f_ids[i]

            return ret

        return self.fixtures[key]

    @property
    def fixtures(self):
        return self._namespace
