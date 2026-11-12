"""Focused preflight tests: one read-only control session for a bounded batch."""
import hashlib
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from evolvmem.library_foundation import AuthorityRouter, FoundationError, RequestIdentity
from evolvmem.library_catalog import (ControlAuthorityLease, ControlBinding, ControlCatalog,
                                      ControlProvider)
from evolvmem.library_catalog.adapter import _HELD, _Held

A = '00000000-0000-0000-0000-000000000001'
D = '00000000-0000-0000-0000-000000000010'
C = '00000000-0000-0000-0000-000000000020'
P = '00000000-0000-0000-0000-000000000200'
L1 = '00000000-0000-0000-0000-000000000101'
L2 = '00000000-0000-0000-0000-000000000102'
L3 = '00000000-0000-0000-0000-000000000103'
DIGEST = hashlib.sha256(b'synthetic-token').hexdigest()


class DriverError(RuntimeError):
    def __init__(self, state):
        self.sqlstate = state
        super().__init__('private driver diagnostic')


def _row(library_id, permission='read'):
    return dict(
        credential_id=C, principal_id=A, device_id=D, credential_digest=DIGEST,
        credential_epoch=3, principal_epoch=2, device_epoch=4,
        credential_active=True, principal_active=True, device_active=True, expires_at=None,
        library_id=library_id, kind='project', backend='postgres', connection_ref='pg.synthetic',
        policy_epoch=7, route_epoch=11, authority_state='pg_active', acl_state='ready',
        write_backend='postgres', project_id=P, library_device_id=None,
        owner_principal_id=None, library_active=True, permission=permission)


class _Status:
    def __init__(self, name='IDLE'):
        self.name = name


class _Raw:
    def __init__(self, engine):
        self.engine = engine
        self.autocommit = True
        self.info = SimpleNamespace(transaction_status=_Status())
        self.closed = False
        self.pinned = False
        self.rollbacks = 0

    def protect_authority(self):
        self.pinned = True

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1
        self.info.transaction_status.name = 'IDLE'

    def close(self):
        self.closed = True
        self.info.transaction_status.name = 'IDLE'
        if self.engine.cleanup_error:
            raise DriverError('08006')


class _Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        engine = self.raw.engine
        engine.calls.append(sql)
        engine.params.append(params)
        if sql.startswith('BEGIN'):
            self.raw.info.transaction_status.name = 'INTRANS'
            return
        if 'pg_catalog.set_config' in sql:
            result = [{'configured': params[0]}]
        elif 'pg_catalog.pg_roles' in sql:
            result = [dict(session_user='catalog_reader', current_user='catalog_reader',
                           superuser=False, bypass_rls=False, create_role=False,
                           create_db=False, memberships=False, owns_protected=False,
                           direct_access=False)]
        elif 'em_control.resolve_library' in sql:
            library_id = params[6]
            if engine.fail_library == library_id:
                raise DriverError('08006')
            result = [engine.rows[library_id]] if library_id in engine.rows else []
        elif 'em_control.pin_library' in sql:
            self.raw.pinned = True
            result = [engine.rows[params[6]]]
        elif sql.strip() == 'SELECT 1 AS alive':
            result = [{'alive': 1}]
        else:
            raise AssertionError('unexpected SQL: ' + sql)
        if result:
            names = list(result[0])
            self.description = [SimpleNamespace(name=name) for name in names]
            self.rows = [tuple(row[name] for name in names) for row in result]
        else:
            self.description = []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Engine:
    def __init__(self, *library_ids):
        self.rows = {library_id: _row(library_id) for library_id in library_ids}
        self.connections = []
        self.calls = []
        self.params = []
        self.fail_library = None
        self.cleanup_error = False

    def connect(self, **kwargs):
        raw = _Raw(self)
        raw.kwargs = kwargs
        self.connections.append(raw)
        return raw


class CountingProvider(ControlProvider):
    """The actual ControlProvider.open context, with its entries counted."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opens = 0

    @contextmanager
    def open(self, *, read_only=True):
        self.opens += 1
        with super().open(read_only=read_only) as session:
            yield session


class LegacyCatalog:
    """Adapter that only implements the original single-snapshot port."""
    def __init__(self, engine):
        self.engine, self.lookups = engine, 0

    def authorization_snapshot(self, identity, library_id):
        self.lookups += 1
        row = self.engine.rows.get(library_id)
        if row is None:
            return None
        from evolvmem.library_foundation import AuthorizationSnapshot
        from evolvmem.library_catalog.adapter import _snapshot
        return _snapshot(row)



def error(test, code, operation):
    with test.assertRaises(FoundationError) as caught:
        operation()
    test.assertEqual(code, caught.exception.code)
    return caught.exception


class PreflightBatchTests(unittest.TestCase):
    def setUp(self):
        self.identity = RequestIdentity(A, C, D, 3, 2, 4)
        self.engine = Engine(L1, L2)
        self.binding = ControlBinding('127.0.0.1', 55439, 'synthetic_control',
                                      'catalog_reader', 'secrets.synthetic.control')
        self.provider = CountingProvider(self.binding, self.engine.connect,
                                         lambda ref: 'synthetic-secret')
        self.catalog = ControlCatalog(self.provider)
        self.router = AuthorityRouter(self.catalog, implemented_tools={
            'postgres': frozenset({'experience_recall'})})

    def test_dual_library_batch_uses_one_control_open_and_keeps_order(self):
        with self.catalog.authorization_preflight(self.identity) as resolve:
            snapshots = (resolve(L2), resolve(L1))
        self.assertEqual([L2, L1], [snapshot.library.library_id for snapshot in snapshots])
        self.assertEqual(1, self.provider.opens)
        self.assertEqual(1, len(self.engine.connections))
        self.assertTrue(self.engine.connections[0].closed)
        self.assertEqual(1, self.engine.connections[0].rollbacks)

        routes = self.router.plan_many(self.identity, 'experience_recall', [L2, L1])
        self.assertEqual([L2, L1], [route.library.library_id for route in routes])
        self.assertEqual(2, self.provider.opens)
        self.assertEqual(2, len(self.engine.connections))

    def test_denial_is_all_or_none_and_still_closes_single_preflight(self):
        self.engine.rows.pop(L2)
        error(self, 'library_access_denied',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L2]))
        self.assertEqual(1, self.provider.opens)
        self.assertTrue(all(connection.closed for connection in self.engine.connections))

    def test_non_batch_adapter_falls_back_per_library_but_single_uses_plan(self):
        legacy = LegacyCatalog(self.engine)
        router = AuthorityRouter(legacy, implemented_tools={
            'postgres': frozenset({'experience_recall'})})
        self.assertEqual(1, len(router.plan_many(self.identity, 'experience_recall', [L1])))
        self.assertEqual(1, legacy.lookups)
        self.assertEqual(2, len(router.plan_many(self.identity, 'experience_recall', [L1, L2])))
        self.assertEqual(3, legacy.lookups)

    def test_held_batch_uses_existing_snapshot_path_and_scope_checks(self):
        routes = self.router.plan_many(self.identity, 'experience_recall', [L1, L2])
        class HeldSession:
            def __init__(self, rows):
                self.rows, self.calls, self.ensured = rows, [], 0
            def ensure_transaction(self):
                self.ensured += 1
            def execute(self, sql, params):
                self.calls.append(params)
                return [self.rows[params[6]]] if params[6] in self.rows else []
        session = HeldSession(self.engine.rows)
        token = _HELD.set(_Held(self.catalog, routes[0], session, routes))
        try:
            before = self.provider.opens
            with self.catalog.authorization_preflight(self.identity) as resolve:
                snapshots = (resolve(L1), resolve(L2))
            self.assertEqual([L1, L2], [snapshot.library.library_id for snapshot in snapshots])
            self.assertEqual(before, self.provider.opens)
            self.assertEqual(2, session.ensured)
            self.assertEqual(2, len(session.calls))
            calls = len(session.calls)
            def out_of_scope():
                with self.catalog.authorization_preflight(self.identity) as resolve:
                    resolve(L3)
            error(self, 'authority_lease_scope_mismatch', out_of_scope)
            self.assertEqual(calls, len(session.calls))
        finally:
            _HELD.reset(token)

    def test_first_denial_wins_over_later_backend_failure(self):
        self.engine.rows[L1]['permission'] = None
        self.engine.fail_library = L2
        error(self, 'library_access_denied',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L2]))
        resolved = [params[6] for params in self.engine.params
                    if len(params) == 7 and params[6] in (L1, L2)]
        self.assertEqual([L1], resolved)
        self.assertEqual(1, self.provider.opens)
        self.assertTrue(all(connection.closed for connection in self.engine.connections))

    def test_first_denial_wins_over_later_invalid_uuid(self):
        self.engine.rows[L1]['permission'] = None
        error(self, 'library_access_denied',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, 'not-a-uuid']))
        resolved = [params[6] for params in self.engine.params
                    if len(params) == 7 and params[6] == L1]
        self.assertEqual([L1], resolved)
        self.assertEqual(1, self.provider.opens)

    def test_later_invalid_uuid_keeps_sequential_error(self):
        error(self, 'invalid_library_selection',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, 'not-a-uuid']))
        self.assertEqual(1, self.provider.opens)

    def test_duplicate_selection_is_rejected_before_opening(self):
        error(self, 'invalid_library_selection',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L1]))
        self.assertEqual(0, self.provider.opens)
        self.assertEqual([], self.engine.connections)

    def test_provider_open_and_cleanup_failures_are_catalog_unavailable(self):
        with patch.object(self.provider, 'open', side_effect=FoundationError('control_busy')):
            error(self, 'catalog_unavailable',
                  lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L2]))
        self.assertEqual([], self.engine.connections)

        self.engine.cleanup_error = True
        error(self, 'catalog_unavailable',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L2]))
        self.assertTrue(self.engine.connections[-1].closed)

    def test_denial_is_not_remapped_by_cleanup_failure(self):
        self.engine.rows[L1]['permission'] = None
        self.engine.cleanup_error = True
        error(self, 'library_access_denied',
              lambda: self.router.plan_many(self.identity, 'experience_recall', [L1, L2]))
        self.assertEqual(1, self.provider.opens)

    def test_fresh_pin_still_rechecks_permission_and_epoch(self):
        routes = self.router.plan_many(self.identity, 'experience_recall', [L1, L2])
        class PinSession:
            def __init__(self, rows):
                self.rows = rows
            def execute(self, sql, params):
                return [self.rows[params[6]]]
        session = PinSession(self.engine.rows)
        ControlAuthorityLease._pin(session, routes[0])
        original = dict(self.engine.rows[L1])
        for field, value in (('permission', None), ('policy_epoch', 8)):
            self.engine.rows[L1] = dict(original, **{field: value})
            error(self, 'authority_changed',
                  lambda: ControlAuthorityLease._pin(session, routes[0]))
        self.assertEqual(1, self.provider.opens)

    def test_batch_driver_exception_closes_preflight(self):
        self.engine.fail_library = L2
        def operation():
            with self.catalog.authorization_preflight(self.identity) as resolve:
                resolve(L1)
                resolve(L2)
        error(self, 'catalog_unavailable', operation)
        self.assertEqual(1, self.provider.opens)
        self.assertEqual(1, len(self.engine.connections))
        self.assertTrue(self.engine.connections[0].closed)
        self.assertEqual(1, self.engine.connections[0].rollbacks)


if __name__ == '__main__':
    unittest.main()
