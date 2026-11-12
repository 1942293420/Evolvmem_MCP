"""Driver-port tests; the synthetic driver is not a PostgreSQL/RLS emulator."""
import hashlib
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

from evolvmem.library_foundation import Authenticator, AuthorityRouter, FoundationError
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from unittest.mock import patch
import psycopg

A = '00000000-0000-0000-0000-000000000001'
D = '00000000-0000-0000-0000-000000000010'
C = '00000000-0000-0000-0000-000000000020'
L = '00000000-0000-0000-0000-000000000100'
P = '00000000-0000-0000-0000-000000000200'
DIGEST = hashlib.sha256(b'synthetic-token').digest()


class DriverError(RuntimeError):
    def __init__(self, state):
        self.sqlstate = state
        super().__init__('private connection/credential diagnostic')


class Engine:
    def __init__(self):
        self.connections = []
        self.calls = []
        self.fail_function = None
        self.failure_state = '55P03'
        self.bad_role = False
        self.direct_access = False
        self.role_name = 'catalog_reader'
        self.commit_error = False
        self.acl_state = 'pending'
        self.row = dict(credential_id=C, principal_id=A, device_id=D,
            credential_digest=DIGEST, credential_epoch=3, principal_epoch=2, device_epoch=4,
            credential_active=True, principal_active=True, device_active=True, expires_at=None,
            library_id=L, kind='project', backend='postgres', connection_ref='pg.synthetic',
            policy_epoch=7, route_epoch=11, authority_state='pg_active', acl_state='ready',
            write_backend='postgres', project_id=P, library_device_id=None,
            owner_principal_id=A, library_active=True, permission='admin')

    def connect(self, **kwargs):
        raw = Raw(self, kwargs)
        self.connections.append(raw)
        return raw


class TransactionStatus(SimpleNamespace):
    def __int__(self): return {'IDLE': 0, 'INTRANS': 2}[self.name]


class Raw:
    def __init__(self, engine, kwargs):
        self.engine, self.kwargs = engine, kwargs
        self.autocommit = kwargs['autocommit']
        self.info = SimpleNamespace(transaction_status=TransactionStatus(name='IDLE'))
        self.closed = False
        self.pinned = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1
        if self.engine.commit_error:
            raise DriverError('08006')
        self.info.transaction_status.name = 'IDLE'
        self.pinned = False

    def rollback(self):
        self.rollbacks += 1
        self.info.transaction_status.name = 'IDLE'
        self.pinned = False

    def cancel_safe(self, *, timeout):
        # Explicit driver-port double: no SQL or commit is run by cancellation.
        self.cancel_requested = True

    def close(self):
        self.closed = True
        self.pinned = False
        self.info.transaction_status.name = 'IDLE'


class Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rows = []

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def execute(self, sql, params=()):
        e = self.raw.engine
        e.calls.append((id(self.raw), sql, params))
        if sql.startswith('BEGIN'):
            self.raw.info.transaction_status.name = 'INTRANS'
            return
        if e.fail_function and e.fail_function in sql:
            raise DriverError(e.failure_state)
        if 'pg_catalog.set_config' in sql:
            result = [{'configured': params[0] if params else 'ok'}]
        elif 'pg_catalog.pg_roles' in sql:
            result = [dict(session_user=e.role_name, current_user=e.role_name,
                superuser=False, bypass_rls=False, create_role=False, create_db=False,
                memberships=e.bad_role, owns_protected=False, direct_access=e.direct_access)]
        elif 'em_control.auth_binding' in sql:
            result = [e.row] if bytes(params[0]) == DIGEST else []
        elif 'em_control.resolve_library' in sql:
            result = [e.row] if params[6] == L else []
        elif 'em_control.list_libraries' in sql:
            result = [e.row]
        elif 'em_control.resolve_external_device' in sql:
            result = [dict(device_id=D, external_device_id='win-synthetic-original')] if params[6] == 'win-synthetic-original' else []
        elif 'em_control.pin_library' in sql:
            self.raw.pinned = True
            result = [e.row]
        elif any(name in sql for name in ('begin_acl_change', 'confirm_acl_projection',
                                           'finish_acl_change', 'fail_acl_change', 'lookup_acl_operation')):
            if 'confirm_acl_projection' in sql: e.acl_state = 'applying'
            if 'finish_acl_change' in sql: e.acl_state = 'complete'
            if 'fail_acl_change' in sql: e.acl_state = 'failed'
            result = [dict(operation_id=O, library_id=L, target_policy_epoch=8,
                           operation_state=e.acl_state, expected_route_epoch=11, connection_ref='pg.synthetic')]
        elif 'em_control.enroll_external_device' in sql:
            result = [dict(enroll_external_device=None)]
        elif sql.strip() == 'SELECT 1 AS alive':
            result = [dict(alive=1)]
        else:
            raise AssertionError('unexpected SQL boundary: ' + sql)
        if result:
            keys = list(result[0])
            self.description = [SimpleNamespace(name=k) for k in keys]
            self.rows = [tuple(row[k] for k in keys) for row in result]
        else:
            self.description = []

    def fetchall(self): return self.rows
    def close(self): pass


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('evolvmem.library_catalog'), 'real control adapter is absent')
        self.api = importlib.import_module('evolvmem.library_catalog')
        self.engine = Engine()
        self.binding = self.api.ControlBinding('127.0.0.1', 55439, 'synthetic_control',
                                               'catalog_reader', 'secrets.synthetic.control')
        self.deadline = SyncRequestDeadlineGuard(10)
        # Only this test's external driver ports are doubled. Runtime guard,
        # catalog, pin validation and cleanup are actual candidate code.
        for target, name, replacement in (
                (psycopg, 'Connection', Raw),
                (psycopg, 'capabilities', SimpleNamespace(has_cancel_safe=lambda **kwargs: True))):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request_scope = self.deadline.enforce()
        self.request_scope.__enter__()
        self.addCleanup(self.finish_request_scope)
        self.provider = self.api.ControlProvider(self.binding, self.engine.connect,
            lambda ref: 'synthetic-secret', deadline_guard=self.deadline)
        self.catalog = self.api.ControlCatalog(self.provider)
        self.identity = Authenticator(self.catalog).authenticate('synthetic-token')
        self.router = AuthorityRouter(self.catalog, implemented_tools={'postgres': {'context_read'}})
        self.route = self.router.plan(self.identity, 'context_read', library_id=L)
        self.lease = self.api.ControlAuthorityLease(self.catalog, deadline_guard=self.deadline)

    def finish_request_scope(self):
        # Expected failing-lease tests may already have cancelled this request.
        # Supply that already-observed exception to contextlib for cleanup;
        # never treat cancellation as a successful request exit.
        if self.deadline._state().cancelled:
            error = FoundationError('request_deadline_exceeded')
            self.request_scope.__exit__(type(error), error, None)
        else:
            self.request_scope.__exit__(None, None, None)

    def error(self, code, operation):
        with self.assertRaises(FoundationError) as cm:
            operation()
        self.assertEqual(code, cm.exception.code)
        self.assertNotIn('private', str(cm.exception))

    def test_auth_and_resolve_decode_distinct_identity_epochs(self):
        self.assertEqual((2, 4, 3), (self.identity.principal_epoch, self.identity.device_epoch, self.identity.credential_epoch))
        self.assertTrue(self.route.library.active)
        self.assertEqual('pg_active', self.route.library.authority_state)
        self.assertTrue(all(c.closed for c in self.engine.connections))

    def test_control_connections_require_verified_server_identity(self):
        self.assertTrue(self.engine.connections)
        self.assertTrue(all(c.kwargs['sslmode'] == 'verify-full' for c in self.engine.connections))

    def test_default_foundation_still_refuses_unported_business(self):
        router = AuthorityRouter(self.catalog)
        for tool in ('memory_add', 'memory_replace', 'memory_remove', 'memory_search', 'context_read'):
            self.error('library_backend_unsupported', lambda: router.plan(self.identity, tool, library_id=L))

    def test_no_lease_without_explicit_complete_request_deadline_adapter(self):
        with self.assertRaises((TypeError, ValueError)):
            self.api.ControlAuthorityLease(self.catalog)
        with self.assertRaises((TypeError, ValueError)):
            self.api.ControlAuthorityLease(self.catalog, deadline_guard=object())

    def test_quiescence_happens_before_authority_locks_are_released(self):
        seen = []
        original = self.deadline.quiesce
        def quiesce(*, cancel):
            self.assertTrue(self.engine.connections[-1].pinned)
            seen.append(cancel)
            return original(cancel=cancel)
        with patch.object(self.deadline, 'quiesce', side_effect=quiesce):
            with self.lease.hold(self.route): pass
            self.assertEqual([False], seen)
            with self.assertRaises(RuntimeError):
                with self.lease.hold(self.route): raise RuntimeError('synthetic cancellation')
            self.assertEqual([False, True], seen)

    def test_external_hook_identifier_is_matched_and_preserved(self):
        binding = self.catalog.resolve_external_device(self.identity, 'win-synthetic-original')
        self.assertEqual('win-synthetic-original', binding.external_device_id)
        self.assertEqual(D, binding.device_id)
        self.error('device_binding_mismatch', lambda: self.catalog.resolve_external_device(self.identity, D))

    def test_list_is_bounded_and_does_not_serialize_connection_or_credential(self):
        result = self.catalog.list_libraries(self.identity, limit=4)
        self.assertEqual(L, result[0].library_id)
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('connection_ref', repr(result))
        self.assertNotIn('credential_digest', repr(result))
        self.error('invalid_arguments', lambda: self.catalog.list_libraries(self.identity, limit=10000))

    def test_pin_holds_a_real_transaction_and_revalidation_uses_it(self):
        count = len(self.engine.connections)
        with self.lease.hold(self.route):
            raw = self.engine.connections[-1]
            self.assertTrue(raw.pinned)
            self.assertEqual('INTRANS', raw.info.transaction_status.name)
            self.router.revalidate(self.route)
            self.assertEqual(count + 1, len(self.engine.connections))
            self.assertEqual(id(raw), self.engine.calls[-1][0])
        self.assertTrue(raw.closed)
        self.assertFalse(raw.pinned)
        self.assertEqual(1, raw.rollbacks)

    def test_pin_lock_wait_is_bounded_and_busy_is_safe(self):
        self.engine.fail_function = 'em_control.pin_library'
        self.error('authority_lease_busy', lambda: self._enter())
        raw = self.engine.connections[-1]
        self.assertTrue(raw.closed)
        timeouts = []
        for _, sql, params in self.engine.calls:
            if 'lock_timeout' not in sql:
                continue
            # Candidate setup batches four fixed local set_config calls into one
            # SELECT; lock_timeout is the first placeholder. The baseline issues
            # one single-placeholder statement, so keep both shapes validated.
            if sql.count('pg_catalog.set_config(') == 4:
                self.assertIn("set_config('lock_timeout', %s, true)", sql)
                timeouts.append(params[0])
            else:
                timeouts.append(params[-1])
        self.assertTrue(timeouts)
        self.assertTrue(all(isinstance(v, str) and v.endswith('ms') and 0 < int(v[:-2]) <= 500
                            for v in timeouts))

    def _enter(self):
        with self.lease.hold(self.route): pass

    def test_lost_pin_never_falls_back_to_an_unlocked_connection(self):
        with self.assertRaises(FoundationError):
            with self.lease.hold(self.route):
                count = len(self.engine.connections)
                self.engine.connections[-1].info.transaction_status.name = 'IDLE'
                self.error('authority_lease_lost', lambda: self.catalog.authorization_snapshot(self.identity, L))
                self.assertEqual(count, len(self.engine.connections))

    def test_nested_lease_is_rejected_and_outer_lease_survives(self):
        with self.lease.hold(self.route):
            self.error('deadline_previous_scope_active', lambda: self._enter())
            self.assertTrue(self.engine.connections[-1].pinned)

    def test_service_membership_is_rejected_on_each_connection(self):
        self.engine.bad_role = True
        self.error('unsafe_control_role', lambda: self.catalog.list_libraries(self.identity))
        self.assertTrue(self.engine.connections[-1].closed)

    def test_preexisting_direct_control_table_grant_is_rejected(self):
        self.engine.direct_access = True
        self.error('unsafe_control_role', lambda: self.catalog.list_libraries(self.identity))
        self.assertTrue(self.engine.connections[-1].closed)

    def test_driver_failure_does_not_leak_or_leave_connection_open(self):
        self.engine.fail_function = 'em_control.resolve_library'
        self.engine.failure_state = '08006'
        self.error('catalog_unavailable', lambda: self.catalog.authorization_snapshot(self.identity, L))
        self.assertTrue(self.engine.connections[-1].closed)

    def test_pin_checks_returned_epoch_before_yielding(self):
        self.engine.row['policy_epoch'] = 8
        self.error('authority_changed', lambda: self._enter())

    def test_pin_rejects_wrong_identity_or_insufficient_permission(self):
        for field, bad in (('principal_id', P), ('device_id', P), ('credential_active', False)):
            original = self.engine.row[field]
            self.engine.row[field] = bad
            self.error('authority_changed', self._enter)
            self.engine.row[field] = original
        from dataclasses import replace
        self.route = replace(self.route, action='write')
        self.engine.row['permission'] = 'read'
        self.error('authority_changed', self._enter)


O = '00000000-0000-0000-0000-000000000300'


class CoordinatorTests(unittest.TestCase):
    error = CatalogTests.error
    finish_request_scope = CatalogTests.finish_request_scope

    def setUp(self):
        CatalogTests.setUp(self)
        self.assertTrue(hasattr(self.api, 'ControlAclCoordinator'), 'real ACL coordinator absent')
        self.coordinator = self.api.ControlAclCoordinator(self.provider)
        self.intent = self.api.AclIntent(self.identity, L, P, 'read', 7, O, 'synthetic-acl-1')

    def test_intent_is_deterministic_and_cannot_choose_hash(self):
        self.assertEqual(self.intent.payload_hash, self.api.AclIntent(
            self.identity, L, P, 'read', 7, O, 'synthetic-acl-1').payload_hash)
        self.assertEqual(32, len(self.intent.payload_hash))
        with self.assertRaises((ValueError, FoundationError)):
            self.api.AclIntent(self.identity, L, P, 'root', 7, O, 'synthetic-acl-1')

    def test_begin_is_committed_before_projection_and_each_phase_is_independent(self):
        calls = []
        def projector(intent, operation):
            calls.append('projected')
            self.assertEqual('pending', operation.operation_state)
            self.assertEqual(1, self.engine.connections[-1].commits)
            self.assertTrue(self.engine.connections[-1].closed)
            return self.api.VerifiedProjection(O, L, 8, 11, 'pg.synthetic', b'v' * 32)
        result = self.coordinator.apply(self.intent, projector)
        self.assertEqual(['projected'], calls)
        self.assertEqual('complete', result.operation_state)
        phases = [c for c in self.engine.connections if c.commits]
        self.assertEqual(3, len(phases))
        self.assertTrue(all(c.closed for c in phases))

    def test_commit_unknown_does_not_project_or_retry(self):
        self.engine.commit_error = True
        self.error('control_commit_result_unknown', lambda: self.coordinator.apply(
            self.intent, lambda *_: self.fail('must not project uncertain begin')))
        self.assertEqual(1, sum(c.commits for c in self.engine.connections))
        self.assertEqual(1, sum('begin_acl_change' in sql for _, sql, _ in self.engine.calls))
        self.assertTrue(self.engine.connections[-1].closed)

    def test_reconciliation_lookup_reads_stable_intent_without_committing(self):
        self.engine.acl_state = 'applying'
        result = self.coordinator.lookup(self.intent)
        self.assertEqual('applying', result.operation_state)
        self.assertEqual(0, self.engine.connections[-1].commits)
        params = [p for _, sql, p in self.engine.calls if 'lookup_acl_operation' in sql][-1]
        self.assertEqual(self.intent.payload_hash, params[-1])

    def test_projection_failure_blocks_library_and_never_marks_ready(self):
        def projector(*_): raise RuntimeError('private content diagnostics')
        self.error('content_projection_failed', lambda: self.coordinator.apply(self.intent, projector))
        self.assertEqual('failed', self.engine.acl_state)
        self.assertFalse(any('finish_acl_change' in sql for _, sql, _ in self.engine.calls))

    def test_foreign_projection_receipt_is_not_confirmed(self):
        self.error('content_projection_failed', lambda: self.coordinator.apply(self.intent,
            lambda *_: self.api.VerifiedProjection(O, P, 8, 11, 'pg.synthetic', b'v' * 32)))
        self.assertFalse(any('confirm_acl_projection' in sql for _, sql, _ in self.engine.calls))

    def test_route_changed_projection_is_not_confirmed(self):
        for epoch, ref in ((12, 'pg.synthetic'), (11, 'pg.other')):
            self.error('content_projection_failed', lambda: self.coordinator.apply(self.intent,
                lambda *_: self.api.VerifiedProjection(O, L, 8, epoch, ref, b'v' * 32)))
        self.assertFalse(any('confirm_acl_projection' in sql for _, sql, _ in self.engine.calls))

    def test_completed_idempotent_operation_does_not_project_twice(self):
        self.engine.acl_state = 'complete'
        result = self.coordinator.apply(self.intent, lambda *_: self.fail('already complete'))
        self.assertEqual('complete', result.operation_state)

    def test_coordinator_cannot_run_inside_request_lease(self):
        with self.lease.hold(self.route):
            self.error('authority_lease_scope_mismatch', lambda: self.coordinator.begin(self.intent))

    def test_external_enrollment_preserves_original_identifier(self):
        self.coordinator.enroll_external_device(A, D, 'win-synthetic-original')
        params = [p for _, sql, p in self.engine.calls if 'enroll_external_device' in sql][-1]
        self.assertEqual((A, D, 'win-synthetic-original'), params)
        self.assertEqual(1, self.engine.connections[-1].commits)


if __name__ == '__main__': unittest.main()
