"""Focused delete-locator reuse checks; synthetic driver only, no PostgreSQL."""
import json
import unittest
from contextlib import contextmanager
from uuid import UUID
import evolvmem.library_foundation.sessions as SESSIONS
import evolvmem.pg_business_core.executor as EXECUTOR

from evolvmem.library_foundation import (AuthorizedRoute, FoundationError, LibraryDescriptor,
                                         RequestIdentity)
from evolvmem.library_foundation.sessions import (_RemoveDenied, _RemoveReady, ContentSessionState,
                                                  DatabaseCredential, DatabaseRole, SessionManager)
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.library_project_acl.scope import resolve_project_scope
from evolvmem.pg_business_core.core import KeyLease
from evolvmem.pg_business_core.executor import PgBusinessExecutor
from evolvmem.pg_business_core.domain import UnsupportedBusiness


def uid(value):
    return str(UUID(int=value))


class Authority:
    def __init__(self):
        self.active = False
        self.entries = 0

    @contextmanager
    def hold(self, route):
        assert not self.active, 'shared/reentrant request lease'
        self.active = True
        self.entries += 1
        try:
            yield
        finally:
            self.active = False


class Connection:
    def __init__(self, authority, route, project_id, *, scope_ok=True, row=None,
                 receipt_rows=None, close_raises=False):
        self.authority = authority
        self.route = route
        self.project_id = project_id
        self.scope_ok = scope_ok
        self.row = row
        self.receipt_rows = list(receipt_rows or ())
        self.close_raises = close_raises
        self.events = []
        self.begin_modes = []
        self.read_only = None
        self.closed = 0
        self.locks = []
        self.rollbacks = 0
        self.credential_epoch = route.identity.credential_epoch
        self.unknown_commit_after = None
        self.change_credential_after = None
        self.after_lock = None
        self.commits = 0
        self.result = None
        self.role = DatabaseRole('runtime_a', 'runtime_a', False, False, False, True)
        self.state = ContentSessionState(route.identity.principal_id, route.library.library_id,
                                         route.library.policy_epoch, route.library.route_epoch,
                                         'write', 'pg_active', 'ready')

    def begin(self, *, read_only):
        assert self.authority.active
        self.read_only = read_only
        self.begin_modes.append(read_only)
        self.events.append(('begin', read_only))

    def inspect_role(self):
        return self.role

    def session_context(self):
        return self.state

    def pin_session_context(self, policy_epoch, route_epoch):
        assert self.authority.active and self.read_only is False
        self.events.append(('pin', policy_epoch, route_epoch))
        return self.state

    def session_credential(self):
        return DatabaseCredential(self.route.identity.credential_id, self.route.identity.device_id,
                                  self.credential_epoch)

    def execute(self, sql, params=()):
        flat = ' '.join(sql.split())
        if 'resolve_project_scope' in flat:
            self.events.append(('project_scope',))
            if not self.scope_ok:
                return ()
            return ((self.project_id, 'project-b', self.route.library.policy_epoch,
                     self.route.library.route_epoch),)
        if 'lan_write_requests' in flat:
            if flat.startswith('SELECT payload_hash,result_json'):
                self.events.append(('receipt_read',))
                return tuple(self.receipt_rows)
            if flat.startswith('INSERT INTO'):
                self.events.append(('receipt_insert',))
                self.receipt_rows.append((params[5], None))
                return ()
            if flat.startswith('UPDATE em_content.lan_write_requests'):
                self.events.append(('receipt_complete',))
                self.result = json.loads(params[0])
                return (('r',),)
            if flat.startswith('SELECT result_json'):
                self.events.append(('receipt_confirm',))
                return ((self.result,),) if self.result is not None else ()
        if 'em_content.memories' in flat:
            self.events.append(('memories_update',))
        else:
            self.events.append(('sql', flat.split()[0]))
        return ()

    def acquire_session_locks(self, keys):
        assert self.authority.active
        self.events.append(('lock', keys))
        self.locks.append(keys)
        if self.after_lock is not None:
            self.after_lock()

    def commit(self):
        assert self.authority.active
        self.commits += 1
        self.events.append(('commit',))
        if self.change_credential_after == self.commits:
            self.credential_epoch = self.route.identity.credential_epoch + 1
        if self.unknown_commit_after == self.commits:
            raise RuntimeError('transport lost the COMMIT reply')

    def rollback(self):
        self.rollbacks += 1
        self.events.append(('rollback',))

    def close(self):
        self.events.append(('close',))
        self.closed += 1
        if self.close_raises:
            raise RuntimeError('close transport failure')


class Provider:
    def __init__(self, connection):
        self.connection = connection
        self.open_count = 0

    def open(self, binding):
        self.open_count += 1
        return self.connection


class Core:
    def __init__(self, session, owner, lease, scope):
        self.session, self.owner, self.lease = session, owner, lease
        self.library_id, self.project_id = scope.library_id, scope.project_id

    def get_by_id(self, legacy_id):
        self.owner.events.append(('lookup', legacy_id))
        return self.owner.rows.get(legacy_id)

    def legacy_remove(self, args):
        row = self.get_by_id(args['id'])
        if row is None:
            return {'status': 'deleted', 'id': args['id'], 'effect': 'none'}
        self.lease.require(self.library_id, row['key'], self.project_id)
        self.session.execute("UPDATE em_content.memories SET status='deleted'")
        return {'status': 'deleted', 'id': args['id']}


class Wire:
    def __init__(self, core, *, semantic_merge_enabled):
        self.core = core

    def memory_remove(self, args):
        return self.core.legacy_remove(args)


class Executor(PgBusinessExecutor):
    def __init__(self, *args, rows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = {31: {'key': 'alpha'}} if rows is None else rows
        self.events = []

    def _core(self, session, lease=None, *, project_scope=None):
        if project_scope is None:
            project_scope = resolve_project_scope(session, self.project_key)
        return Core(session, self, lease, project_scope)


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.authority = Authority()
        self.identity = RequestIdentity(uid(1), uid(2), uid(3), 1)
        self.library = LibraryDescriptor(uid(4), 'device', 'postgres', 'target', 7, 1,
                                         'pg_active', 'ready', 'postgres',
                                         device_id=uid(9), owner_principal_id=uid(10), active=True)
        self.project_id = project_uuid('project-b')
        self._old_wire = EXECUTOR.LegacyWire
        EXECUTOR.LegacyWire = Wire

    def route(self, tool='memory_remove', action='write'):
        return AuthorizedRoute(self.identity, self.library, tool, action)

    def manager(self, connection, route):
        binding = type('Binding', (), {'role': 'runtime_a'})()
        self.registry = type('Registry', (), {'resolve': lambda registry, candidate: binding})()
        self.provider = Provider(connection)
        router = type('Router', (), {'revalidate': lambda router, candidate: None})()
        return SessionManager(router, self.registry, self.provider, self.authority)

    def test_remove_locator_uses_one_authority_one_connection_and_two_preflights(self):
        self.assertIs(SessionManager, SESSIONS.SessionManager)
        self.assertIs(SESSIONS.SessionManager.keyed.__wrapped__.__globals__['_KeyedTransactions'],
                      SESSIONS._KeyedTransactions)
        route = self.route()
        connection = Connection(self.authority, route, self.project_id)
        manager = self.manager(connection, route)
        def prepare(session):
            return _RemoveReady((11, 22), 'alpha')
        with manager.keyed(route, None, _prepare_remove=prepare) as scope:
            self.assertIs(type(scope), SESSIONS._KeyedTransactions)
            self.assertIsNone(scope.early_denial)
            self.assertEqual('alpha', scope.canonical_identity)
            with scope.transaction() as session:
                session.execute('business effect')
        self.assertEqual(1, self.authority.entries)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(1, connection.closed)
        self.assertEqual([False, True, False], connection.begin_modes)
        self.assertEqual([(11, 22)], connection.locks)
        events = connection.events
        first_commit = events.index(('commit',))
        second_commit = events.index(('commit',), first_commit + 1)
        self.assertLess(events.index(('pin', 7, 1)), first_commit)
        lock = events.index(('lock', (11, 22)))
        self.assertLess(second_commit, lock)
        last_begin = max(i for i, event in enumerate(events) if event == ('begin', False))
        self.assertLess(lock, last_begin)

    def test_normal_keyed_add_path_is_unchanged(self):
        route = self.route('memory_add')
        connection = Connection(self.authority, route, self.project_id)
        manager = self.manager(connection, route)
        with manager.keyed(route, (5,)) as scope:
            self.assertIsNone(scope.early_denial)
            self.assertIsNone(scope.canonical_identity)
            with scope.transaction() as session:
                session.execute('business effect')
        self.assertEqual([True, False], connection.begin_modes)
        self.assertEqual([(5,)], connection.locks)
        self.assertEqual(1, sum(1 for e in connection.events if e[0] == 'pin'))
        self.assertLess(connection.events.index(('lock', (5,))),
                        connection.events.index(('pin', 7, 1)))
        self.assertEqual(1, connection.closed)

    def test_credential_change_between_locator_and_readonly_preflight_blocks_locks(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id)
        connection.change_credential_after = 1
        manager = self.manager(connection, route)
        with self.assertRaises(FoundationError) as caught:
            with manager.keyed(route, None,
                               _prepare_remove=lambda session: _RemoveReady((7,), 'alpha')):
                pass
        self.assertEqual('database_credential_mismatch', caught.exception.code)
        self.assertEqual([False, True], connection.begin_modes)
        self.assertEqual([], connection.locks)
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(1, connection.closed)

    def test_locator_unknown_commit_never_locks_or_publishes(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id)
        connection.unknown_commit_after = 1
        manager = self.manager(connection, route)
        with self.assertRaises(FoundationError) as caught:
            with manager.keyed(route, None,
                               _prepare_remove=lambda session: _RemoveReady((7,), 'alpha')):
                pass
        self.assertEqual('commit_result_unknown', caught.exception.code)
        self.assertEqual([False], connection.begin_modes)
        self.assertEqual([], connection.locks)
        self.assertEqual(1, connection.closed)
        self.assertEqual(1, connection.rollbacks)

    def _executor(self, connection, route):
        manager = self.manager(connection, route)
        executor = Executor(manager, object(), model_contract='synthetic',
                            semantic_merge_enabled=False, project_key='project-b')
        executor.events = connection.events
        return executor

    def tearDown(self):
        EXECUTOR.LegacyWire = self._old_wire

    def test_early_denial_closes_before_one_publication_and_has_no_locks(self):
        for flag, has_publisher in ((True, True), (True, False)):
            with self.subTest(flag=flag, has_publisher=has_publisher):
                self.authority = Authority()
                route = self.route()
                connection = Connection(self.authority, route, self.project_id, scope_ok=False)
                executor = self._executor(connection, route)
                outputs = []
                def publish(value):
                    self.assertTrue(self.authority.active)
                    self.assertEqual(1, connection.closed)
                    self.assertEqual([], connection.locks)
                    outputs.append(value)
                result = executor.mutate(route, {'id': 31}, request_id='deny-1',
                                         publish=publish if has_publisher else None,
                                         preflight_errors_as_results=True)
                self.assertEqual({'error': 'project_scope_unavailable'}, result)
                self.assertEqual(1 if has_publisher else 0, len(outputs))
                self.assertEqual(1, connection.closed)
                self.assertEqual(1, self.authority.entries)
                self.assertEqual(1, self.provider.open_count)
                self.assertEqual([], connection.locks)
                self.assertFalse(any(e[0] == 'receipt_insert' for e in connection.events))
                self.assertFalse(any(e[0] == 'memories_update' for e in connection.events))
                self.assertGreater(connection.events.index(('close',)),
                                   connection.events.index(('commit',)))
                self.assertEqual(1, connection.commits)
                self.assertGreater(connection.events.index(('close',)),
                                   connection.events.index(('commit',)))

    def test_early_denial_opt_out_preserves_original_error(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id, scope_ok=False)
        executor = self._executor(connection, route)
        outputs = []
        with self.assertRaises(FoundationError) as caught:
            executor.mutate(route, {'id': 31}, request_id='deny-2', publish=outputs.append,
                            preflight_errors_as_results=False)
        self.assertEqual('project_scope_unavailable', caught.exception.code)
        self.assertEqual([], outputs)
        self.assertEqual([], connection.locks)
        self.assertEqual(1, connection.closed)

    def test_early_denial_close_failure_is_delivery_failed_and_not_retried(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id, scope_ok=False,
                                close_raises=True)
        executor = self._executor(connection, route)
        outputs = []
        with self.assertRaises(FoundationError) as caught:
            executor.mutate(route, {'id': 31}, request_id='deny-3', publish=outputs.append,
                            preflight_errors_as_results=True)
        self.assertEqual('write_committed_delivery_failed', caught.exception.code)
        self.assertEqual([], outputs)
        self.assertEqual(1, connection.closed)
        self.assertEqual([], connection.locks)
        self.assertFalse(any(e[0] == 'receipt_insert' for e in connection.events))

    def test_successful_delete_flow_has_one_connection_and_ordered_transactions(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id)
        executor = self._executor(connection, route)
        outputs = []
        result = executor.mutate(route, {'id': 31}, request_id='ok-1', publish=outputs.append)
        self.assertEqual({'status': 'deleted', 'id': 31}, result)
        self.assertEqual([result], outputs)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(1, self.authority.entries)
        self.assertEqual(1, connection.closed)
        self.assertEqual([False, True, False, False, True], connection.begin_modes)
        self.assertEqual(1, len(connection.locks))
        events = connection.events
        commits = [i for i, event in enumerate(events) if event[0] == 'commit']
        self.assertEqual(5, len(commits))
        self.assertLess(events.index(('lookup', 31)), commits[0])
        lock_index = next(i for i, event in enumerate(events) if event[0] == 'lock')
        self.assertLess(commits[1], lock_index)
        self.assertLess(events.index(('receipt_insert',)), events.index(('memories_update',)))
        self.assertLess(events.index(('memories_update',)), events.index(('receipt_complete',)))
        self.assertLess(events.index(('receipt_complete',)), events.index(('receipt_confirm',)))
        self.assertLess(events.index(('receipt_confirm',)), commits[4])
        self.assertEqual('deleted', connection.result['status'])

    def test_effect_key_mismatch_is_rejected_before_data_change(self):
        route = self.route()
        connection = Connection(self.authority, route, self.project_id)
        executor = self._executor(connection, route)
        connection.after_lock = lambda: executor.rows.__setitem__(31, {'key': 'beta'})
        with self.assertRaises(UnsupportedBusiness) as caught:
            executor.mutate(route, {'id': 31}, request_id='mismatch-1')
        self.assertEqual('key_lease_required', caught.exception.code)
        self.assertFalse(any(e[0] == 'memories_update' for e in connection.events))
        self.assertFalse(any(e[0] == 'receipt_complete' for e in connection.events))
        self.assertTrue(any(e[0] == 'receipt_insert' for e in connection.events))
        self.assertEqual(1, connection.closed)


    def _digest(self, args, request_id):
        import hashlib
        payload = json.dumps(dict(version=2, project_key='project-b', project_id=self.project_id,
                                  arguments=args), ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def test_none_key_and_missing_row_keep_original_lock_material(self):
        from evolvmem.context_migration import LegacyMemoryMigrator
        for rows, key in ((({31: {'key': None}}), None), ({}, None)):
            with self.subTest(rows=rows):
                route = self.route()
                connection = Connection(self.authority, route, self.project_id)
                executor = self._executor(connection, route)
                executor.rows = rows
                result = executor.mutate(route, {'id': 31}, request_id='edge-1')
                identity = LegacyMemoryMigrator.identity_key_for(
                    key if rows else 'missing-legacy-id:31', 1)
                expected = tuple(sorted((
                    EXECUTOR.lock_number('evolvmem-business-key-v2', self.library.library_id,
                                         self.project_id, identity),
                    EXECUTOR.lock_number('evolvmem-business-request-v1', self.library.library_id,
                                         self.identity.principal_id, 'memory_remove', 'edge-1'))))
                self.assertEqual([expected], connection.locks)
                self.assertEqual('deleted', result['status'])

    def test_receipt_conflict_and_indeterminate_precede_effect(self):
        route = self.route()
        args = {'id': 31}
        digest = self._digest(args, 'receipt-1')
        cases = ((digest + 'x', None, 'request_id_conflict'),
                 (digest, None, 'request_indeterminate'))
        for stored_hash, result_json, expected in cases:
            with self.subTest(expected=expected):
                connection = Connection(self.authority, route, self.project_id,
                                        receipt_rows=[(stored_hash, result_json)])
                executor = self._executor(connection, route)
                outputs = []
                result = executor.mutate(route, args, request_id='receipt-1',
                                         publish=outputs.append, preflight_errors_as_results=True)
                self.assertEqual({'error': expected}, result)
                self.assertEqual([result], outputs)
                self.assertFalse(any(e[0] == 'memories_update' for e in connection.events))
                self.assertFalse(any(e[0] == 'receipt_insert' for e in connection.events))
                self.assertEqual(1, connection.closed)

    def test_completed_receipt_replays_without_effect(self):
        route = self.route()
        payload = json.dumps(dict(version=2, project_key='project-b', project_id=self.project_id,
                                  arguments={'id': 31}), ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False)
        import hashlib
        digest = hashlib.sha256(payload.encode()).hexdigest()
        stored = {'status': 'deleted', 'id': 31}
        connection = Connection(self.authority, route, self.project_id,
                                receipt_rows=[(digest, stored)])
        executor = self._executor(connection, route)
        outputs = []
        result = executor.mutate(route, {'id': 31}, request_id='replay-1', publish=outputs.append)
        self.assertEqual(stored, result)
        self.assertEqual([stored], outputs)
        self.assertFalse(any(e[0] == 'memories_update' for e in connection.events))
        self.assertFalse(any(e[0] == 'receipt_insert' for e in connection.events))


if __name__ == '__main__':
    unittest.main()
