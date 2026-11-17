"""Offline V22 ACL plan 55P03 recovery boundary tests using fake transports."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import UUID

import psycopg

from evolvmem.library_foundation import ConnectionBinding, FoundationError, RequestIdentity
from evolvmem.library_catalog import AclIntent, AclOperation, ControlBinding, ControlProvider
from evolvmem.library_deadline import driver
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_project_acl.worker import AclProjector, ProjectionTarget

PLAN_SQL = 'SELECT * FROM em_control.get_acl_projection_plan(%s)'


def uid(n):
    return str(UUID(int=n))


IDENTITY = RequestIdentity(uid(1), uid(2), uid(3), 1)
LIB, DEVICE, PRINCIPAL, OPERATION_ID, CREDENTIAL = uid(4), uid(3), uid(5), uid(6), uid(7)
INTENT = AclIntent(IDENTITY, LIB, PRINCIPAL, 'read', 1, OPERATION_ID, 'synthetic-request')
OPERATION = AclOperation(OPERATION_ID, LIB, 2, 'pending', 1, 'device_one')


class FakeInfo:
    def __init__(self):
        self.transaction_status = type('Status', (), {'name': 'IDLE'})()


class FakeRaw:
    def __init__(self, engine, kind, tid):
        self.engine, self.kind, self.tid = engine, kind, tid
        self.closed = False
        self.autocommit = True
        self.info = FakeInfo()
        self.pending = None

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.engine.events.append(('rollback', self.tid))
        self.info.transaction_status.name = 'IDLE'

    def close(self):
        self.engine.events.append(('close', self.tid))
        self.closed = True

    def commit(self):
        self.engine.events.append(('commit', self.tid))
        if self.pending is not None:
            self.engine.state = self.pending
            self.pending = None
        self.info.transaction_status.name = 'IDLE'
        if self.engine.commit_unknown:
            raise RuntimeError('synthetic unknown commit')


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rows = []
        self.fetch_error = None
        self.close_error = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.close_error is not None:
            self.raw.engine.events.append(('cursor_close_error', self.raw.tid))
            raise self.close_error

    def execute(self, sql, params=()):
        raw = self.raw
        raw.engine.events.append(('sql', raw.tid, sql, params))
        if sql.startswith('BEGIN'):
            raw.info.transaction_status.name = 'INTRANS'
            return
        if 'set_config' in sql:
            self._set([{'set_config': True}])
        elif 'pg_catalog.pg_roles' in sql:
            self._role()
        elif 'get_acl_projection_plan' in sql:
            raw.engine.plan(self, params)
        elif 'inspect_acl_projection' in sql:
            self._set([{'snapshot_json': json.dumps(raw.engine.state, sort_keys=True)}])
        elif 'apply_acl_projection' in sql:
            raw.engine.apply(raw, params)
            raw.engine.applies += 1
            self._set([{'applied_new': raw.engine.applies == 1}])
        else:
            raise AssertionError('unexpected SQL boundary')

    def _set(self, output):
        names = list(output[0])
        self.description = [type('Column', (), {'name': n})() for n in names]
        self.rows = [tuple(row[n] for n in names) for row in output]

    def _role(self):
        role = 'projector'
        if self.raw.kind == 'control':
            self._set([dict(session_user=role, current_user=role, superuser=False, bypass_rls=False,
                            create_role=False, create_db=False, memberships=False,
                            direct_access=False, owns_protected=False)])
        else:
            self._set([dict(database='synthetic_content', session_user=role, current_user=role,
                            login=True, superuser=False, bypass_rls=False, create_role=False,
                            create_db=False, memberships=False, owns_protected=False,
                            direct_access=False)])

    def fetchall(self):
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.rows


class FakeGuardedCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = FakeCursor(connection.raw)

    @property
    def description(self):
        return self.cursor.description

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close_error = self.cursor.close_error
        if self.cursor.close_error is not None:
            self.connection.engine.events.append(('guarded_cursor_close', self.connection.raw.tid))
            raise self.cursor.close_error

    def execute(self, sql, params=()):
        self.connection.guard_cursor_calls += 1
        self.connection.engine.events.append(('guarded_gucs', self.connection.raw.tid))
        self.connection.guard.check()
        self.cursor.execute(sql, params)
        self.connection.raw.executed_sql.append(sql)
        return self

    def fetchall(self):
        return self.cursor.fetchall()


class FakeGuardedConnection:
    def __init__(self, engine, raw, guard):
        self.engine, self.raw, self.guard = engine, raw, guard
        self._raw = raw
        self.closed = False
        self.retired = False
        self.guard_cursor_calls = 0
        self.close_error = None

    @property
    def info(self):
        return self.raw.info

    @property
    def autocommit(self):
        return self.raw.autocommit

    def cursor(self):
        return FakeGuardedCursor(self)

    def rollback(self):
        self.guard.check()
        if self.raw.tid in self.engine.rollback_raises:
            self.engine.events.append(('rollback_error', self.raw.tid))
            raise FoundationError('rollback_result_unknown')
        self.raw.rollback()
        if self.raw.tid in self.engine.rollback_nonidle:
            self.raw.info.transaction_status.name = 'INTRANS'

    def close(self):
        self.retired = True
        if self.raw.tid in self.engine.close_raises:
            self.engine.events.append(('close_error', self.raw.tid))
            raise RuntimeError('synthetic close failure')
        self.raw.close()
        self.closed = True

    def commit(self):
        self.raw.commit()


class Engine:
    def __init__(self, plan):
        self.plan_text = json.dumps(plan, sort_keys=True)
        self.state = dict(database='synthetic_content', session_role='projector', applied=None, projects=[],
                          project_acls=[], acls=[])
        self.state['library'] = dict(library_id=LIB, kind='device', device_id=DEVICE, project_id=None,
                                     policy_epoch=1, route_epoch=1, authority_state='pg_active',
                                     acl_state='ready', owner_principal_id=uid(1))
        self.state['bindings'] = [dict(login_role='runtime_reader', principal_id=PRINCIPAL,
                                       credential_id=CREDENTIAL, device_id=DEVICE, credential_epoch=1,
                                       expires_at=None, enabled=True, role_safe=True)]
        self.events, self.plan_calls, self.plan_outcomes = [], [], []
        self.applies = 0
        self.commit_unknown = False
        self.close_raises = set()
        self.rollback_raises = set()
        self.rollback_nonidle = set()
        self.counter = 0
        self.transport = None

    def new_raw(self, kind):
        self.counter += 1
        tid = '%s-%d' % (kind, self.counter)
        raw = FakeRaw(self, kind, tid)
        raw.executed_sql = []
        self.events.append(('connect', tid))
        if kind == 'control':
            self.transport = raw
        return raw

    def connect(self, **kwargs):
        return self.new_raw('content')

    def plan(self, cursor, params):
        assert params == (OPERATION_ID,), 'exact operation identity required'
        tid = cursor.raw.tid
        self.plan_calls.append((tid, params))
        outcome = self.plan_outcomes.pop(0) if self.plan_outcomes else 'ok'
        self.events.append(('plan', tid, outcome))
        if outcome == 'lock':
            raise psycopg.errors.LockNotAvailable('synthetic 55P03')
        if outcome == 'lock_close_lock':
            cursor.close_error = psycopg.errors.LockNotAvailable('synthetic close 55P03')
            raise psycopg.errors.LockNotAvailable('synthetic execute 55P03')
        if outcome == 'other_close_lock':
            cursor.close_error = psycopg.errors.LockNotAvailable('synthetic close 55P03')
            raise RuntimeError('synthetic execute other')
        if outcome == 'wrong_state':
            exc = psycopg.errors.LockNotAvailable('synthetic forged state')
            exc.sqlstate = '40001'
            raise exc
        if outcome == 'fetch_fail':
            cursor.description = [type('Column', (), {'name': 'plan_text'})()]
            cursor.fetch_error = RuntimeError('synthetic fetch failure')
            return
        if outcome == 'base_exception':
            raise KeyboardInterrupt('synthetic cancellation')
        row = dict(plan_text=self.plan_text,
                   plan_hash_hex=hashlib.sha256(self.plan_text.encode()).hexdigest())
        cursor._set([row])

    def apply(self, raw, params):
        plan = json.loads(params[0])
        registry = json.loads(params[2])
        state = deepcopy(self.state)
        state['library'].update(policy_epoch=plan['target_policy_epoch'], acl_state='ready')
        state['acls'] = [dict(principal_id=a['principal_id'], permission=a['permission'],
                              active=a['active'], projection_epoch=plan['target_policy_epoch'])
                         for a in plan['acls']]
        state['bindings'] = [dict(row, role_safe=True) for row in registry]
        state['applied'] = dict(operation_id=OPERATION_ID, target_policy_epoch=plan['target_policy_epoch'],
                                expected_policy_epoch=plan['expected_policy_epoch'],
                                route_epoch=plan['route_epoch'], payload_hash=plan['payload_hash'],
                                plan_hash=params[1].hex(), registry_hash=params[3].hex())
        raw.pending = state


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(version=2, operation_id=OPERATION_ID, library_id=LIB, connection_ref='device_one',
                         expected_policy_epoch=1, target_policy_epoch=2, route_epoch=1, kind='device',
                         device_id=DEVICE, project_id=None, owner_principal_id=uid(1),
                         payload_hash=INTENT.payload_hash.hex(),
                         evaluation_at='2026-11-01T00:00:00.000000Z',
                         acls=[dict(principal_id=PRINCIPAL, permission='read', active=True,
                                    principal_active=True, principal_epoch=1)],
                         credentials=[dict(principal_id=PRINCIPAL, credential_id=CREDENTIAL,
                                           device_id=DEVICE, credential_epoch=1, principal_epoch=1,
                                           device_epoch=1, credential_active=True,
                                           principal_active=True, device_active=True, expires_at=None)],
                         projects=[], project_acls=[])
        self.engine = Engine(self.plan)
        self.binding = ControlBinding('localhost', 5432, 'synthetic_content', 'projector', 'ref.projector')
        self.targets = (ProjectionTarget(
            LIB, 'device_one', self.binding,
            (ConnectionBinding('device_one', LIB, PRINCIPAL, CREDENTIAL, DEVICE, 'localhost', 5432,
                               'synthetic_content', 'runtime_reader', 'ref.reader'),)),)
        self.guard = SyncRequestDeadlineGuard(30)

    def worker(self, guard=True, provider=None):
        if provider is None:
            connector = ((lambda **kwargs: self.engine.new_raw('control')) if not guard
                         else self.engine.connect)
            provider = ControlProvider(self.binding, connector,
                                       lambda _: 'synthetic-only-secret',
                                       deadline_guard=self.guard if guard else None)
        return AclProjector(provider, self.targets, self.engine.connect,
                            lambda _: 'synthetic-only-secret',
                            deadline_guard=self.guard if guard else None)

    def run_worker(self):
        with patch.object(driver, 'connect', side_effect=self.fake_connect), \
             patch.object(driver, 'GuardedConnection', FakeGuardedConnection), \
             patch.object(psycopg, 'Connection', FakeRaw):
            with self.guard.enforce() if self.worker_guard else _null():
                return self.worker_instance(INTENT, OPERATION)

    def fake_connect(self, guard, connector, kwargs, *, kind, statement_ms, lock_ms, profile=None):
        self.assertIs(guard, self.guard)
        return FakeGuardedConnection(self.engine, self.engine.new_raw(kind), guard)

    def error(self, action):
        with self.assertRaises(FoundationError) as caught:
            action()
        return caught.exception.code

    def recovery_case(self, outcomes, guard=True):
        self.engine.plan_outcomes = list(outcomes)
        self.worker_guard = guard
        self.worker_instance = self.worker(guard=guard)
        return self.run_worker()

    def test_first_plan_lock_recovers_with_ordered_cleanup(self):
        receipt = self.recovery_case(['lock', 'ok', 'ok'])
        self.assertEqual(OPERATION_ID, receipt.operation_id)
        self.assertEqual(1, self.engine.applies)
        self.assertEqual(3, sum(1 for e in self.engine.events if e[0] == 'connect' and e[1].startswith('control')))
        tid = next(e[1] for e in self.engine.events if e[0] == 'connect' and e[1].startswith('control'))
        rollback = next(i for i, e in enumerate(self.engine.events)
                        if e[0] == 'rollback' and e[1] == tid)
        close = next(i for i, e in enumerate(self.engine.events) if e[0] == 'close' and e[1] == tid)
        second = next(i for i, e in enumerate(self.engine.events)
                      if e[0] == 'connect' and e[1] != tid and e[1].startswith('control'))
        self.assertLess(rollback, close)
        self.assertLess(close, second)
        self.assertTrue(all(params == (OPERATION_ID,) for _, params in self.engine.plan_calls))
        self.assertTrue(all(sql == PLAN_SQL for e in self.engine.events if e[0] == 'sql'
                            and 'get_acl_projection_plan' in e[2] for sql in (e[2],)))

    def test_second_plan_lock_recovers_before_content_and_applies_once(self):
        receipt = self.recovery_case(['ok', 'lock', 'ok'])
        self.assertEqual(OPERATION_ID, receipt.operation_id)
        self.assertEqual(1, self.engine.applies)
        self.assertEqual(4, len(self.engine.plan_calls))
        self.assertEqual(3, sum(1 for e in self.engine.events if e[0] == 'connect' and e[1].startswith('control')))

    def test_shared_budget_allows_only_one_recovery_total(self):
        self.engine.plan_outcomes = ['lock', 'ok', 'lock', 'ok']
        self.worker_guard = True
        self.worker_instance = self.worker()
        self.assertEqual('control_busy', self.error(self.run_worker))
        self.assertEqual(3, len(self.engine.plan_calls))
        self.assertEqual(0, self.engine.applies)

    def test_third_plan_lock_is_terminal_without_content_replay(self):
        self.engine.plan_outcomes = ['ok', 'ok', 'lock']
        self.worker_guard = True
        self.worker_instance = self.worker()
        self.assertEqual('control_busy', self.error(self.run_worker))
        self.assertEqual(1, self.engine.applies)
        self.assertEqual(3, len(self.engine.plan_calls))
        self.assertEqual(2, sum(1 for e in self.engine.events if e[0] == 'connect' and e[1].startswith('control')))

    def test_cursor_close_55p03_dominates_execute_55p03(self):
        self.engine.plan_outcomes = ['lock_close_lock']
        self.worker_guard = True
        self.worker_instance = self.worker()
        self.assertEqual('control_busy', self.error(self.run_worker))
        self.assertEqual(1, len(self.engine.plan_calls))
        self.assertEqual(0, self.engine.applies)

    def test_first_commit_unknown_never_retries_or_opens_content(self):
        self.engine.commit_unknown = True
        self.worker_guard = True
        self.worker_instance = self.worker()
        self.assertEqual('control_commit_result_unknown', self.error(self.run_worker))
        self.assertEqual(1, len(self.engine.plan_calls))
        self.assertEqual(1, sum(1 for e in self.engine.events if e[0] == 'connect'))

    def test_rollback_failure_never_retries(self):
        self.engine.plan_outcomes = ['lock']
        self.worker_guard = True
        self.worker_instance = self.worker()
        tid = None
        original_connect = self.fake_connect

        def fail_rollback(guard, connector, kwargs, **kw):
            nonlocal tid
            connection = original_connect(guard, connector, kwargs, **kw)
            if tid is None:
                tid = connection.raw.tid
                self.engine.rollback_raises.add(tid)
            return connection

        with patch.object(driver, 'connect', side_effect=fail_rollback), \
             patch.object(driver, 'GuardedConnection', FakeGuardedConnection), \
             patch.object(psycopg, 'Connection', FakeRaw):
            with self.guard.enforce():
                with self.assertRaises(FoundationError):
                    self.worker_instance(INTENT, OPERATION)
        self.assertEqual(1, len(self.engine.plan_calls))
        self.assertEqual(0, self.engine.applies)

    def test_non_idle_rollback_never_retries(self):
        self.engine.plan_outcomes = ['lock']
        self.worker_guard = True
        self.worker_instance = self.worker()
        original_connect = self.fake_connect
        tid = [None]

        def nonidle(guard, connector, kwargs, **kw):
            connection = original_connect(guard, connector, kwargs, **kw)
            if tid[0] is None:
                tid[0] = connection.raw.tid
                self.engine.rollback_nonidle.add(tid[0])
            return connection

        with patch.object(driver, 'connect', side_effect=nonidle), \
             patch.object(driver, 'GuardedConnection', FakeGuardedConnection), \
             patch.object(psycopg, 'Connection', FakeRaw):
            with self.guard.enforce():
                self.assertEqual('control_cleanup_failed', self.error(lambda: self.worker_instance(INTENT, OPERATION)))
        self.assertEqual(1, len(self.engine.plan_calls))
        self.assertEqual(0, self.engine.applies)

    def test_unrelated_errors_and_base_exception_never_retry(self):
        for outcome in ('wrong_state', 'fetch_fail', 'base_exception', 'other_close_lock'):
            self.engine = Engine(self.plan)
            self.engine.plan_outcomes = [outcome]
            self.worker_guard = True
            self.worker_instance = self.worker()
            with self.subTest(outcome=outcome):
                with patch.object(driver, 'connect', side_effect=self.fake_connect), \
                     patch.object(driver, 'GuardedConnection', FakeGuardedConnection), \
                     patch.object(psycopg, 'Connection', FakeRaw):
                    with self.guard.enforce():
                        if outcome == 'base_exception':
                            with self.assertRaises(KeyboardInterrupt):
                                self.worker_instance(INTENT, OPERATION)
                        else:
                            with self.assertRaises(FoundationError):
                                self.worker_instance(INTENT, OPERATION)
                self.assertEqual(1, len(self.engine.plan_calls))
                self.assertEqual(0, self.engine.applies)

    def test_no_guard_path_keeps_original_single_terminal_attempt(self):
        self.engine.plan_outcomes = ['lock']
        self.worker_guard = False
        self.worker_instance = self.worker(guard=False)
        # No guarded transport in this test: use the plain connector path.
        self.error(lambda: self.worker_instance(INTENT, OPERATION))
        self.assertEqual(1, len(self.engine.plan_calls))
        self.assertEqual(0, self.engine.applies)

    def test_guarded_wrapper_is_used_not_low_level_raw(self):
        receipt = self.recovery_case(['ok', 'ok', 'ok'])
        self.assertEqual(OPERATION_ID, receipt.operation_id)
        guarded = [e for e in self.engine.events if e[0] == 'guarded_gucs']
        self.assertGreaterEqual(len(guarded), 3)
        self.assertEqual(1, self.engine.applies)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


if __name__ == '__main__':
    unittest.main()
