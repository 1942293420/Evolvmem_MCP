"""Whole-batch rollback at the real SessionManager/authority boundary.

Only the content driver and the authority coordinator are doubles: these tests
exercise the actual SessionManager._transaction/run state machine, including the
new private batch operation and the actual psycopg IDLE verification helper.
No database or network is opened.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

import psycopg.pq

from evolvmem.library_foundation import models
from evolvmem.library_foundation._projection_conflict import (
    ProjectionBatchConflict, ProjectionBatchDeferred)
from evolvmem.library_foundation.sessions import (ContentSessionState, DatabaseCredential,
                                                  DatabaseRole, SessionManager,
                                                  _PsycopgConnection)

A = '00000000-0000-0000-0000-000000000001'
DEVICE = '00000000-0000-0000-0000-000000000010'
CREDENTIAL = '00000000-0000-0000-0000-000000000020'
LIBRARY = '00000000-0000-0000-0000-000000000100'
PROJECT = '00000000-0000-0000-0000-000000000200'
ROLE = 'runtime_a'


class Authority:
    """Post-yield validation/cleanup runs only on a normal scope exit."""
    def __init__(self):
        self.held = False
        self.events = []
        self.post_failure = False
        self.cleanup_failure = False

    @contextmanager
    def hold(self, route):
        self.held = True
        self.events.append('enter')
        failed = False
        try:
            yield
            self.events.append('post_check')
            if self.post_failure:
                raise models.FoundationError('control_post_check_failed')
        except BaseException:
            failed = True
            raise
        finally:
            self.events.append('cleanup')
            self.held = False
            if self.cleanup_failure and not failed:
                raise models.FoundationError('control_cleanup_failed')


class Connection:
    """Models transaction status, not merely a rollback method call."""
    def __init__(self, authority, *, conf=None):
        self.authority = authority
        self.role = DatabaseRole(ROLE, ROLE, False, False, False, True)
        self.state = ContentSessionState(A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready')
        self.credential = DatabaseCredential(CREDENTIAL, DEVICE, 3)
        self.pending = []
        self.persisted = []
        self.events = []
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE
        self.closed = False
        self.close_count = 0
        self.fail_close = False
        self.fail_batch_rollback = False
        self.non_idle = False

    def begin(self, *, read_only):
        assert self.authority.held
        assert read_only is False
        self.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        self.events.append('begin')

    def inspect_role(self):
        return self.role

    def session_context(self):
        return self.state

    def pin_session_context(self, policy_epoch, route_epoch):
        return self.state

    def session_credential(self):
        return self.credential

    def execute(self, sql, params=()):
        self.events.append(('sql', sql))
        self.pending.append(params)

    def commit(self):
        self.events.append('commit')
        self.persisted.extend(self.pending)
        self.pending.clear()
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def rollback(self):
        self.events.append('rollback')
        self.pending.clear()
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def _rollback_projection_batch(self):
        self.events.append('batch_rollback')
        if self.fail_batch_rollback:
            raise RuntimeError('private rollback failure')
        self.pending.clear()
        self.transaction_status = (psycopg.pq.TransactionStatus.INTRANS if self.non_idle
                                   else psycopg.pq.TransactionStatus.IDLE)
        if self.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise models.FoundationError('projection_batch_rollback_not_idle')

    def close(self):
        self.events.append('close')
        self.close_count += 1
        self.closed = True
        if self.fail_close:
            raise RuntimeError('private close failure')


def make_route(action='write'):
    identity = models.RequestIdentity(A, CREDENTIAL, DEVICE, 3)
    library = models.LibraryDescriptor(
        LIBRARY, 'project', 'postgres', 'pg.synthetic', 7, 11, 'pg_active', 'ready',
        'postgres', project_id=PROJECT, active=True)
    return models.AuthorizedRoute(identity, library, 'context_write', action)


BINDING = models.ConnectionBinding('pg.synthetic', LIBRARY, A, CREDENTIAL, DEVICE,
                                   'localhost', 5432, 'synthetic_content', ROLE, 'ref.runtime_a')


def manager_for(connection, authority):
    router = SimpleNamespace(revalidate=lambda route: None)
    registry = SimpleNamespace(resolve=lambda route: BINDING)
    provider = SimpleNamespace(open=lambda binding: connection)
    return SessionManager(router, registry, provider, authority)


class BatchOperationTests(unittest.TestCase):
    def setUp(self):
        self.authority = Authority()
        self.route = make_route()

    def _run(self, callback, *, route=None):
        connection = Connection(self.authority)
        manager = manager_for(connection, self.authority)
        operation = manager._projection_batch_apply(callback)
        with self.assertRaises(ProjectionBatchDeferred):
            manager.run(route or self.route, operation)
        return connection

    def test_batch_conflict_rolls_back_verifies_idle_closes_then_defers(self):
        def callback(session):
            session.execute('uncommitted vector write')
            raise ProjectionBatchConflict()
        connection = self._run(callback)
        self.assertEqual('begin', connection.events[0])
        self.assertIn('batch_rollback', connection.events)
        self.assertEqual('close', connection.events[-1])
        self.assertLess(connection.events.index('batch_rollback'),
                        connection.events.index('close'))
        self.assertEqual(['post_check', 'cleanup'], self.authority.events[1:])
        self.assertIs(connection.transaction_status, psycopg.pq.TransactionStatus.IDLE)
        self.assertEqual([], connection.persisted)
        self.assertEqual(0, connection.events.count('commit'))
        self.assertEqual(1, connection.close_count)
        self.assertTrue(connection.closed)

    def test_rollback_failure_is_fatal_and_never_defers(self):
        def callback(session):
            session.execute('uncommitted vector write')
            raise ProjectionBatchConflict()
        connection = Connection(self.authority)
        connection.fail_batch_rollback = True
        manager = manager_for(connection, self.authority)
        with self.assertRaises(models.FoundationError) as raised:
            manager.run(self.route, manager._projection_batch_apply(callback))
        self.assertEqual('projection_batch_rollback_failed', raised.exception.code)
        self.assertEqual([], connection.persisted)
        self.assertEqual(0, connection.events.count('commit'))

    def test_non_idle_rollback_is_fatal_and_never_defers(self):
        def callback(session):
            session.execute('uncommitted vector write')
            raise ProjectionBatchConflict()
        connection = Connection(self.authority)
        connection.non_idle = True
        manager = manager_for(connection, self.authority)
        with self.assertRaises(models.FoundationError) as raised:
            manager.run(self.route, manager._projection_batch_apply(callback))
        self.assertEqual('projection_batch_rollback_failed', raised.exception.code)

    def test_close_failure_after_rollback_is_fatal_and_never_defers(self):
        def callback(session):
            session.execute('uncommitted vector write')
            raise ProjectionBatchConflict()
        connection = Connection(self.authority)
        connection.fail_close = True
        manager = manager_for(connection, self.authority)
        with self.assertRaises(models.FoundationError) as raised:
            manager.run(self.route, manager._projection_batch_apply(callback))
        self.assertEqual('projection_batch_close_failed', raised.exception.code)

    def test_authority_cleanup_failure_after_rollback_is_fatal_and_never_defers(self):
        def callback(session):
            session.execute('uncommitted vector write')
            raise ProjectionBatchConflict()
        self.authority.cleanup_failure = True
        connection = Connection(self.authority)
        manager = manager_for(connection, self.authority)
        with self.assertRaises(models.FoundationError) as raised:
            manager.run(self.route, manager._projection_batch_apply(callback))
        self.assertEqual('control_cleanup_failed', raised.exception.code)
        self.assertIn('post_check', self.authority.events)

    def test_plain_callable_with_batch_marker_is_fatal_not_deferred(self):
        connection = Connection(self.authority)
        manager = manager_for(connection, self.authority)
        with self.assertRaises(models.FoundationError):
            manager.run(self.route, lambda session: (_ for _ in ()).throw(
                ProjectionBatchConflict()))
        self.assertEqual(0, connection.events.count('commit'))

    def test_batch_operation_requires_write_admin_and_no_publisher(self):
        connection = Connection(self.authority)
        manager = manager_for(connection, self.authority)
        operation = manager._projection_batch_apply(lambda session: ())
        with self.assertRaises(ValueError):
            manager.run(make_route('read'), operation)
        with self.assertRaises(ValueError):
            manager.run(self.route, operation, publish=lambda result: None)


class ActualIdleVerificationTests(unittest.TestCase):
    def test_actual_psycopg_helper_requires_real_idle_status(self):
        for status, okay in ((psycopg.pq.TransactionStatus.IDLE, True),
                             (psycopg.pq.TransactionStatus.INTRANS, False),
                             (psycopg.pq.TransactionStatus.ACTIVE, False)):
            with self.subTest(status=status):
                class Raw:
                    def __init__(self):
                        self.info = SimpleNamespace(transaction_status=status)
                        self.rollbacks = 0
                    def rollback(self):
                        self.rollbacks += 1
                raw = Raw()
                connection = _PsycopgConnection(raw)
                if okay:
                    connection._rollback_projection_batch()
                    self.assertEqual(1, raw.rollbacks)
                else:
                    with self.assertRaises(models.FoundationError) as raised:
                        connection._rollback_projection_batch()
                    self.assertEqual('projection_batch_rollback_not_idle',
                                     raised.exception.code)


class BaseExceptionCleanupTests(unittest.TestCase):
    """Interrupted cleanup keeps the original fault and never defers/succeeds."""
    def test_interrupted_raw_rollback_preserves_fault_and_close_once(self):
        for fault in (KeyboardInterrupt('synthetic interrupt'), SystemExit(17)):
            with self.subTest(fault=type(fault).__name__):
                authority = Authority()
                connection = Connection(authority)
                fault_holder = fault
                def raw_rollback():
                    raise fault_holder
                actual = SimpleNamespace(_raw=SimpleNamespace(rollback=raw_rollback))
                connection._rollback_projection_batch = (
                    lambda: _PsycopgConnection._rollback_projection_batch(actual))
                manager = manager_for(connection, authority)
                def operation(session):
                    session.execute('synthetic staged vector and ACK')
                    raise ProjectionBatchConflict()
                with self.assertRaises(type(fault)) as raised:
                    manager.run(self_route(), manager._projection_batch_apply(operation))
                self.assertIs(fault, raised.exception)
                self.assertEqual(1, connection.close_count)
                self.assertTrue(connection.closed)
                self.assertEqual([], connection.persisted)
                self.assertNotIn('commit', connection.events)
                self.assertFalse(authority.held)

    def test_interrupted_info_after_rollback_preserves_fault_and_close_once(self):
        for fault in (KeyboardInterrupt('synthetic interrupt'), SystemExit(17)):
            with self.subTest(fault=type(fault).__name__):
                class Info:
                    @property
                    def transaction_status(self):
                        raise fault
                class Raw:
                    info = Info()
                    def rollback(self):
                        self.rolled = True
                raw = Raw()
                connection = _PsycopgConnection(raw)
                with self.assertRaises(type(fault)) as raised:
                    connection._rollback_projection_batch()
                self.assertIs(fault, raised.exception)
                self.assertTrue(raw.rolled)

    def test_explicit_close_baseexception_is_fatal_with_one_attempt(self):
        for fault in (KeyboardInterrupt('synthetic interrupt'), SystemExit(17)):
            with self.subTest(fault=type(fault).__name__):
                authority = Authority()
                connection = Connection(authority)
                def close():
                    connection.close_count += 1
                    connection.closed = True
                    raise fault
                connection.close = close
                manager = manager_for(connection, authority)
                def operation(session):
                    session.execute('synthetic staged vector and ACK')
                    raise ProjectionBatchConflict()
                with self.assertRaises(type(fault)) as raised:
                    manager.run(self_route(), manager._projection_batch_apply(operation))
                self.assertIs(fault, raised.exception)
                self.assertEqual(1, connection.close_count)
                self.assertTrue(connection.closed)
                self.assertEqual([], connection.persisted)


def self_route():
    return make_route()


if __name__ == '__main__':
    unittest.main()
