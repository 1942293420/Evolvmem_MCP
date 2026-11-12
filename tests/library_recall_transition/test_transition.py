"""Focused standard-import tests for the read-only recall transition repair.

No database, service, credential or network access. Driver ports are fake; the
real candidate adapter/router/host code is imported through OUT/stage.
"""
import hashlib
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from evolvmem.library_foundation import (Authenticator, AuthorityRouter, AuthorizedRoute, CredentialBinding,
    FoundationError, LibraryDescriptor, RequestIdentity)
from evolvmem.library_catalog import ControlBinding, ControlCatalog, ControlProvider, ControlAuthorityLease
from evolvmem.library_catalog.adapter import RecallPinRefusal
from evolvmem.library_foundation.routing import RecallTransientWitness, RecallWitness
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.pg_vector_port.model import ModelContract

A = '00000000-0000-0000-0000-000000000001'
D = '00000000-0000-0000-0000-000000000010'
C = '00000000-0000-0000-0000-000000000020'
L = '00000000-0000-0000-0000-000000000100'
L2 = '00000000-0000-0000-0000-000000000101'
P = '00000000-0000-0000-0000-000000000200'
P2 = '00000000-0000-0000-0000-000000000201'
DIGEST = hashlib.sha256(b'synthetic-token').digest()

CONTRACT = ModelContract('synthetic-model', 768, '', '', 1024)



class DriverError(RuntimeError):
    def __init__(self, state):
        self.sqlstate = state
        super().__init__('private driver diagnostic')


class TxStatus(SimpleNamespace):
    def __int__(self):
        return {'IDLE': 0, 'INTRANS': 2}[self.name]


class Engine:
    def __init__(self):
        self.connections = []
        self.calls = []
        self.pin_state = None
        self.pin_rows = None
        self.row = dict(credential_id=C, principal_id=A, device_id=D, credential_digest=DIGEST,
            credential_epoch=3, principal_epoch=2, device_epoch=4, credential_active=True,
            principal_active=True, device_active=True, expires_at=None, library_id=L, kind='project',
            backend='postgres', connection_ref='pg.synthetic', policy_epoch=7, route_epoch=11,
            authority_state='pg_active', acl_state='ready', write_backend='postgres', project_id=P,
            library_device_id=None, owner_principal_id=A, library_active=True, permission='read')

    def connect(self, **kwargs):
        raw = Raw(self, kwargs)
        self.connections.append(raw)
        return raw


class Raw:
    def __init__(self, engine, kwargs):
        self.engine, self.kwargs = engine, kwargs
        self.autocommit = kwargs['autocommit']
        self.info = SimpleNamespace(transaction_status=TxStatus(name='IDLE'))
        self.closed = self.rollbacks = 0
        self.pinned = False

    def protect_authority(self):
        pass

    def cancel_safe(self, **kwargs):
        pass

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.rollbacks += 1
        self.info.transaction_status.name = 'IDLE'
        self.pinned = False

    def close(self):
        self.closed += 1
        self.info.transaction_status.name = 'IDLE'


class Cursor:
    def __init__(self, raw):
        self.raw, self.description, self.rows = raw, None, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=()):
        engine = self.raw.engine
        engine.calls.append((id(self.raw), sql, params))
        if sql.startswith('BEGIN'):
            self.raw.info.transaction_status.name = 'INTRANS'
            return
        if 'pg_catalog.set_config' in sql:
            result = [{'ok': True}]
        elif 'pg_catalog.pg_roles' in sql:
            result = [dict(session_user='catalog_reader', current_user='catalog_reader',
                superuser=False, bypass_rls=False, create_role=False, create_db=False,
                memberships=False, owns_protected=False, direct_access=False)]
        elif 'em_control.auth_binding' in sql:
            result = [engine.row]
        elif 'em_control.resolve_library' in sql:
            result = [engine.row]
        elif 'em_control.pin_library' in sql:
            if engine.pin_state is not None:
                raise DriverError(engine.pin_state)
            self.raw.pinned = True
            result = [engine.row] if engine.pin_rows is None else list(engine.pin_rows)
        elif sql.strip() == 'SELECT 1 AS alive':
            result = [{'alive': 1}]
        else:
            raise AssertionError('unexpected SQL: ' + sql)
        if result:
            self.description = [SimpleNamespace(name=key) for key in result[0]]
            self.rows = [tuple(row[key] for key in result[0]) for row in result]
        else:
            self.description = []
            self.rows = []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.engine = Engine()
        self.binding = ControlBinding('127.0.0.1', 55439, 'synthetic_control',
                                      'catalog_reader', 'secrets.synthetic.control')
        self.deadline = SyncRequestDeadlineGuard(10)
        self.scope = self.deadline.enforce()
        self.scope.__enter__()
        self.addCleanup(self.finish_scope)
        for target, name, replacement in (
                (psycopg, 'Connection', Raw),
                (psycopg, 'capabilities', SimpleNamespace(has_cancel_safe=lambda **kwargs: True))):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider = ControlProvider(self.binding, self.engine.connect,
            lambda ref: 'synthetic-secret', deadline_guard=self.deadline)
        self.catalog = ControlCatalog(self.provider)
        self.identity = Authenticator(self.catalog).authenticate('synthetic-token')
        self.router = AuthorityRouter(self.catalog,
            implemented_tools={'postgres': {'context_search', 'context_read'}})
        self.lease = ControlAuthorityLease(self.catalog, deadline_guard=self.deadline)

    def finish_scope(self):
        if self.deadline._state().cancelled:
            error = FoundationError('request_deadline_exceeded')
            self.scope.__exit__(type(error), error, None)
        else:
            self.scope.__exit__(None, None, None)

    def test_recall_prepin_42501_is_typed_and_cleanup_confirmed(self):
        self.engine.pin_state = '42501'
        route = self.router.plan(self.identity, 'context_search', library_id=L)
        before = len(self.engine.connections)
        with self.assertRaises(RecallPinRefusal) as caught:
            with self.lease.hold(route):
                self.fail('body must not start after a pre-pin refusal')
        self.assertEqual('authority_changed', caught.exception.code)
        self.assertEqual((route,), caught.exception.routes)
        self.assertEqual(before + 1, len(self.engine.connections))
        raw = self.engine.connections[-1]
        self.assertEqual(1, raw.rollbacks)
        self.assertEqual(1, raw.closed)

    def test_ordinary_read_42501_keeps_public_authority_changed(self):
        self.engine.pin_state = '42501'
        route = self.router.plan(self.identity, 'context_read', library_id=L)
        with self.assertRaises(FoundationError) as caught:
            with self.lease.hold(route):
                pass
        self.assertNotIsInstance(caught.exception, RecallPinRefusal)
        self.assertEqual('authority_changed', caught.exception.code)

    def test_pin_execute_refusal_with_cursor_close_42501_is_not_typed(self):
        route = self.router.plan(self.identity, 'context_search', library_id=L)
        for execute_state in ('42501', '55P03'):
            self.engine.pin_state = execute_state
            with patch.object(Cursor, '__exit__', side_effect=DriverError('42501')):
                with self.assertRaises(FoundationError) as caught:
                    with self.lease.hold(route):
                        pass
            self.assertNotIsInstance(caught.exception, RecallPinRefusal)
            self.assertEqual('authority_changed', caught.exception.code)
            raw = self.engine.connections[-1]
            self.assertEqual(1, raw.closed)

    def test_pin_no_rows_and_malformed_snapshot_are_not_typed(self):
        route = self.router.plan(self.identity, 'context_search', library_id=L)
        for rows in ([], [dict(engine_row=self.engine.row)]):
            self.engine.pin_rows = rows
            with self.assertRaises(FoundationError) as caught:
                with self.lease.hold(route):
                    pass
            self.assertNotIsInstance(caught.exception, RecallPinRefusal)
            self.assertEqual('authority_changed', caught.exception.code)
            raw = self.engine.connections[-1]
            self.assertEqual(1, raw.closed)
        self.engine.pin_rows = None

    def test_pin_protect_and_cursor_creation_failures_are_not_typed(self):
        route = self.router.plan(self.identity, 'context_search', library_id=L)
        from evolvmem.library_deadline.driver import GuardedConnection
        with patch.object(GuardedConnection, 'protect_authority', side_effect=RuntimeError('private protect')):
            with self.assertRaises(RuntimeError) as caught:
                with self.lease.hold(route):
                    pass
        self.assertNotIsInstance(caught.exception, RecallPinRefusal)
        with patch.object(Raw, 'cursor', side_effect=DriverError('42501')):
            with self.assertRaises(FoundationError) as caught:
                with self.lease.hold(route):
                    pass
        self.assertNotIsInstance(caught.exception, RecallPinRefusal)
        self.assertEqual('authority_changed', caught.exception.code)

    def test_generic_authority_changed_and_cleanup_failure_are_not_typed(self):
        route = self.router.plan(self.identity, 'context_search', library_id=L)
        for state in ('55P03', '08006'):
            self.engine.pin_state = state
            with self.assertRaises(FoundationError) as caught:
                with self.lease.hold(route):
                    pass
            self.assertNotIsInstance(caught.exception, RecallPinRefusal)
        self.engine.pin_state = '42501'
        original = self.engine.connections[-1] if self.engine.connections else None
        with patch.object(Raw, 'close', side_effect=RuntimeError('private close')):
            with self.assertRaises(FoundationError) as caught:
                with self.lease.hold(route):
                    pass
        self.assertNotIsInstance(caught.exception, RecallPinRefusal)
        self.assertEqual('control_cleanup_failed', caught.exception.code)


class FakeCatalog:
    def __init__(self, snapshots):
        self.snapshots, self.resolved, self.exits = snapshots, [], 0

    @contextmanager
    def authorization_preflight(self, identity):
        try:
            def resolve(library_id):
                self.resolved.append(library_id)
                return self.snapshots[library_id]
            yield resolve
        finally:
            self.exits += 1

    def authorization_snapshot(self, identity, library_id):
        self.resolved.append(library_id)
        return self.snapshots[library_id]


def credential():
    return CredentialBinding(C, A, D, DIGEST.hex(), 3, True, True, True, None, 2, 4)


def library(library_id=L, *, acl='updating', backend='postgres', active=True,
            authority='pg_active', policy=7, route_epoch=11, project=P):
    return LibraryDescriptor(library_id, 'project', backend, 'pg.synthetic', policy,
        route_epoch, authority, acl, backend, project, None, A, active)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.identity = RequestIdentity(A, C, D, 3, 2, 4)
        self.router = AuthorityRouter(FakeCatalog({}), implemented_tools={'postgres':
            {'context_search', 'experience_recall', 'context_read'}})

    def router_with(self, *libraries, permission='read'):
        from evolvmem.library_foundation import AuthorizationSnapshot
        catalog = FakeCatalog({lib.library_id: AuthorizationSnapshot(credential(), lib, permission)
                               for lib in libraries})
        return AuthorityRouter(catalog, implemented_tools={'postgres':
            {'context_search', 'experience_recall', 'context_read'}}), catalog

    def test_updating_is_a_typed_witness_only_for_recall_and_all_libraries_are_checked(self):
        router, catalog = self.router_with(library(L), library(L2, project=P2))
        with self.assertRaises(RecallTransientWitness) as caught:
            router.plan_many(self.identity, 'context_search', [L, L2], recall_transition=True)
        self.assertEqual([L, L2], catalog.resolved)
        self.assertEqual(1, catalog.exits)
        self.assertEqual({'updating'}, {w.library.acl_state for w in caught.exception.witnesses})
        self.assertEqual({L, L2}, {w.library.library_id for w in caught.exception.witnesses})

    def test_ordinary_plan_still_immediately_rejects_updating(self):
        router, catalog = self.router_with(library(L))
        with self.assertRaises(FoundationError) as caught:
            router.plan_many(self.identity, 'context_search', [L])
        self.assertEqual('library_unavailable', caught.exception.code)
        with self.assertRaises(FoundationError) as caught:
            router.plan(self.identity, 'context_search', library_id=L)
        self.assertEqual('library_unavailable', caught.exception.code)

    def test_witness_requires_active_postgres_pg_active_read_permission(self):
        cases = [library(active=False), library(authority='pg_staging'),
                 library(backend='sqlite', authority='sqlite_active')]
        for value in cases:
            router, _ = self.router_with(value)
            with self.assertRaises(FoundationError) as caught:
                router.plan_many(self.identity, 'context_search', [L], recall_transition=True)
            self.assertNotIsInstance(caught.exception, RecallTransientWitness)
        router, _ = self.router_with(library())
        with self.assertRaises(RecallTransientWitness):
            router.plan_many(self.identity, 'context_search', [L], recall_transition=True)
        router, _ = self.router_with(library(acl='ready'))
        routes = router.plan_many(self.identity, 'context_search', [L], recall_transition=True)
        self.assertEqual(L, routes[0].library.library_id)

    def test_real_deny_is_not_masked_by_an_earlier_witness(self):
        from evolvmem.library_foundation import AuthorizationSnapshot
        catalog = FakeCatalog({L: AuthorizationSnapshot(credential(), library(L), 'read'),
                               L2: AuthorizationSnapshot(credential(), library(L2, project=P2), None)})
        router = AuthorityRouter(catalog, implemented_tools={'postgres': {'context_search'}})
        with self.assertRaises(FoundationError) as caught:
            router.plan_many(self.identity, 'context_search', [L, L2], recall_transition=True)
        self.assertEqual('library_access_denied', caught.exception.code)
        self.assertNotIsInstance(caught.exception, RecallTransientWitness)


class FakeProvider:
    def __init__(self):
        self.contract = CONTRACT
        self.encodes = 0

    def encode_query(self, query, *, deadline):
        self.encodes += 1
        return tuple([0.01] * 768)


class FakeGuard:
    def __init__(self):
        self.deadline = time.monotonic() + 10

    def check(self):
        if time.monotonic() >= self.deadline:
            raise FoundationError('request_deadline_exceeded')

    def absolute_deadline(self):
        self.check()
        return self.deadline


class FakeState:
    def __init__(self, router):
        self.router, self.guard = router, FakeGuard()
        self.identity = RequestIdentity(A, C, D, 3, 2, 4)
        self.coordinator, self.published = object(), False


class HostTests(unittest.TestCase):
    def setUp(self):
        from evolvmem.library_host import LibraryHost
        self.host = object.__new__(LibraryHost)
        self.host._embedding_provider = FakeProvider()
        self.host._config = object()
        self.host._model_contract = CONTRACT.key

    def route(self, *, library_id=L, policy=7, tool='context_search'):
        return self.host_route(library_id, policy, tool)

    def host_route(self, library_id, policy, tool):
        from evolvmem.library_foundation import AuthorizedRoute
        return AuthorizedRoute(RequestIdentity(A, C, D, 3, 2, 4),
            library(library_id, acl='ready', policy=policy,
                    project=P if library_id == L else P2), tool, 'read')

    def args(self, **extra):
        value = dict(query='synthetic query', library_id=L, project_key=P)
        value.update(extra)
        return value

    def test_initial_updating_witness_does_not_encode_and_retries_once(self):
        witness = RecallWitness(RequestIdentity(A, C, D, 3, 2, 4), 'context_search',
                                library(L, acl='updating'), 'read')
        class Router:
            def __init__(self):
                self.calls = 0
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                self.calls += 1
                if self.calls == 1:
                    raise RecallTransientWitness((witness,), tool, tuple(library_ids))
                return (self_route(),)
        router = Router()
        state = FakeState(router)
        calls = []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                calls.append((routes, selections))
                kwargs['publish']('one-publication')
                return 'ok'
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            result = self.host._recall(state, 'context_search', self.args(), lambda value: None)
        self.assertEqual('ok', result)
        self.assertEqual(1, self.host._embedding_provider.encodes)
        self.assertEqual(1, len(calls))
        self.assertEqual(2, router.calls)

    def test_stale_pin_requires_epoch_advance_then_one_fresh_retry(self):
        old = self.route(policy=7)
        new = self.route(policy=8)
        class Router:
            def __init__(self):
                self.calls = 0
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                self.calls += 1
                return (old,) if self.calls == 1 else (new,)
        router = Router()
        state = FakeState(router)
        searches, publications = [], []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                if len(searches) == 1:
                    raise RecallPinRefusal((old,))
                kwargs['publish']('payload')
                return 'ok'
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            result = self.host._recall(state, 'context_search', self.args(), publications.append)
        self.assertEqual('ok', result)
        self.assertEqual([(old,), (new,)], searches)
        self.assertEqual(1, self.host._embedding_provider.encodes)
        self.assertEqual(['payload'], publications)
        self.assertEqual(2, router.calls)

    def test_unchanged_epoch_never_retries_content(self):
        old = self.route(policy=7)
        class Router:
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                return (old,)
        state = FakeState(Router())
        searches = []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                raise RecallPinRefusal((old,))
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', self.args(), lambda value: None)
        self.assertEqual('authority_changed', caught.exception.code)
        self.assertEqual(1, len(searches))

    def test_bounded_four_observations_check_both_libraries_without_retry(self):
        old = (self.route(library_id=L, policy=7), self.route(library_id=L2, policy=7))
        witnesses = (RecallWitness(RequestIdentity(A, C, D, 3, 2, 4), 'context_search',
                       library(L, acl='updating', policy=8), 'read'),
                     RecallWitness(RequestIdentity(A, C, D, 3, 2, 4), 'context_search',
                       library(L2, acl='updating', policy=8, project=P2), 'read'))
        class Router:
            def __init__(self):
                self.calls = []
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                self.calls.append(tuple(library_ids))
                if len(self.calls) == 1:
                    return old
                raise RecallTransientWitness(witnesses, tool, library_ids)
        router = Router()
        state = FakeState(router)
        searches = []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                raise RecallPinRefusal(old, failed_route=old[0])
        from evolvmem.library_host import LibraryHost
        clock = [time.monotonic()]
        def advance(seconds):
            clock[0] += seconds
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search), \
             patch.object(LibraryHost, '_TRANSITION_WINDOW', 10.0), \
             patch('evolvmem.library_host.time.monotonic', lambda: clock[0]), \
             patch('evolvmem.library_host.time.sleep', advance):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', dict(query='synthetic query', library_ids=[L, L2], project_key=P), lambda value: None)
        self.assertEqual('authority_changed', caught.exception.code)
        self.assertEqual(1, len(searches))
        self.assertEqual(5, len(router.calls))
        self.assertTrue(all(call == (L, L2) for call in router.calls[1:]))

    def test_cancel_before_observation_never_retries_or_encodes_again(self):
        old = (self.route(policy=7),)
        class Router:
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                return old
        state = FakeState(Router())
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                state.guard.deadline = time.monotonic() - 1
                raise RecallPinRefusal(old, failed_route=old[0])
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', self.args(), lambda value: None)
        self.assertEqual('request_deadline_exceeded', caught.exception.code)
        self.assertEqual(1, self.host._embedding_provider.encodes)

    def test_other_library_policy_advance_does_not_justify_retry(self):
        old = (self.route(library_id=L, policy=7), self.route(library_id=L2, policy=7))
        new = (self.route(library_id=L, policy=7), self.route(library_id=L2, policy=8))
        class Router:
            def __init__(self):
                self.calls = 0
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                self.calls += 1
                return old if self.calls == 1 else new
        state = FakeState(Router())
        searches = []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                raise RecallPinRefusal(old, failed_route=old[0])
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search), \
             patch('evolvmem.library_host.time.sleep'):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', dict(query='q', library_ids=[L, L2], project_key=P),
                                  lambda value: None)
        self.assertEqual('authority_changed', caught.exception.code)
        self.assertEqual(1, len(searches))

    def test_generic_authority_changed_or_denial_after_fresh_plan_is_not_retried(self):
        old = self.route(policy=7)
        for error in (FoundationError('authority_changed'), FoundationError('unauthorized'),
                      FoundationError('library_access_denied')):
            class Router:
                def __init__(self):
                    self.calls = 0
                def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                    self.calls += 1
                    return (old,) if self.calls == 1 else (_ for _ in ()).throw(error)
            state = FakeState(Router())
            searches = []
            class Search:
                def __init__(self, *args, **kwargs):
                    pass
                def search(self, routes, selections, request, **kwargs):
                    searches.append(routes)
                    raise RecallPinRefusal((old,))
            with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search), \
                 patch('evolvmem.library_host.time.sleep'):
                with self.assertRaises(FoundationError):
                    self.host._recall(state, 'context_search', self.args(), lambda value: None)
            self.assertEqual(1, len(searches))

    def test_published_or_content_failure_after_pin_is_not_retried(self):
        old = self.route(policy=7)
        class Router:
            def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
                return (old,)
        state = FakeState(Router())
        state.published = True
        searches = []
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                raise RecallPinRefusal((old,))
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', self.args(), lambda value: None)
        self.assertEqual('authority_changed', caught.exception.code)
        self.assertEqual(1, len(searches))
        state.published = False
        class Search2:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                searches.append(routes)
                raise FoundationError('recall_selected_changed')
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search2):
            with self.assertRaises(FoundationError) as caught:
                self.host._recall(state, 'context_search', self.args(), lambda value: None)
        self.assertEqual('recall_selected_changed', caught.exception.code)
        self.assertEqual(2, len(searches))

    def test_historical_as_of_uses_no_embedding(self):
        route = self.route(policy=7)
        class Router:
            def plan_many(self, identity, tool, library_ids, **options):
                return (route,)
        state = FakeState(Router())
        class Search:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
            def search(self, routes, selections, request, **kwargs):
                return 'ok'
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            result = self.host._recall(state, 'context_search',
                dict(query='q', library_id=L, project_key=P, as_of='2026-01-01T00:00:00'),
                lambda value: None)
        self.assertEqual('ok', result)
        self.assertEqual(0, self.host._embedding_provider.encodes)

    def test_memory_search_does_not_use_transition_planning(self):
        route = self.route(tool='memory_search')
        class Router:
            def __init__(self):
                self.options = []
            def plan_many(self, identity, tool, library_ids, **kwargs):
                self.options.append(kwargs)
                return (route,)
        router = Router()
        state = FakeState(router)
        class Search:
            def __init__(self, *args, **kwargs):
                pass
            def search(self, routes, selections, request, **kwargs):
                return 'ok'
        with patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
            self.assertEqual('ok', self.host._recall(state, 'memory_search', self.args(), lambda value: None))
        self.assertEqual([{}], router.options)


def self_route():
    return AuthorizedRoute(RequestIdentity(A, C, D, 3, 2, 4), library(L, acl='ready'),
        'context_search', 'read')


if __name__ == '__main__':
    unittest.main()
