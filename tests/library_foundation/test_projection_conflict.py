"""Pre-effect row-conflict recovery at the real transaction/authority boundary.

Only the driver and the authority coordinator are doubles: these tests exercise
the actual VectorRepository.document(), LibrarySession, SessionManager._transaction
and SessionManager.run state machines. No database, model or service is used.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

import psycopg.errors

from tests.library_foundation import test_foundation as base
from tests.library_foundation.test_foundation import A, B, LIBRARY, CREDENTIAL, DEVICE, PROJECT
from evolvmem.library_foundation import models
from evolvmem.library_foundation import _projection_conflict as conflict
from evolvmem.pg_vector_port.projection import Job
from evolvmem.pg_vector_port.repository import VectorRepository


class Authority:
    """Mirrors ControlAuthorityLease: post-yield checks run only on normal exit."""
    def __init__(self):
        self.held = False
        self.post_failure = False
        self.cleanup_failure = False
        self.events = []

    @contextmanager
    def hold(self, route):
        if self.held:
            raise AssertionError('shared/reentrant request lease')
        self.held = True
        self.events.append('authority_enter')
        failed = False
        try:
            yield
            self.events.append('authority_post_check')
            if self.post_failure:
                raise models.FoundationError('control_post_check_failed')
        except BaseException:
            failed = True
            raise
        finally:
            self.events.append('authority_cleanup')
            self.held = False
            if self.cleanup_failure and not failed:
                raise models.FoundationError('control_cleanup_failed')


class Connection:
    def __init__(self, api, authority):
        self.authority = authority
        self.role = api.DatabaseRole('runtime_a', 'runtime_a', False, False, False, True)
        self.state = api.ContentSessionState(A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready')
        self.credential = api.DatabaseCredential(CREDENTIAL, DEVICE, 3)
        self.staged = []
        self.persisted = []
        self.events = []
        self.closed = False
        self.read_only = None
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.fail_rollback = False
        self.fail_close = False
        self.unknown_commit = False
        self.raise_on = None
        self.actual_error = None

    def begin(self, *, read_only):
        assert self.authority.held
        self.read_only = read_only
        self.events.append('begin')

    def inspect_role(self):
        return self.role

    def session_context(self):
        return self.state

    def pin_session_context(self, policy_epoch, route_epoch):
        assert self.authority.held and self.read_only is False
        return self.state

    def session_credential(self):
        return self.credential

    def execute(self, sql, params=()):
        self.events.append(('sql', sql))
        if self.read_only:
            raise RuntimeError('test driver refuses writes in READ ONLY')
        if self.raise_on is not None and self.raise_on in sql:
            try:
                raise self.actual_error
            except BaseException:
                # Same shape as _PsycopgConnection.execute: redacted public
                # error with the actual typed exception only in __context__.
                raise models.FoundationError('database_operation_failed') from None
        self.staged.append(params[0] if params else sql)

    def commit(self):
        assert self.authority.held
        self.events.append('commit')
        self.persisted.extend(self.staged)
        self.staged.clear()
        self.commit_count += 1
        if self.unknown_commit:
            raise RuntimeError('private driver transport detail')

    def rollback(self):
        self.events.append('rollback')
        self.rollback_count += 1
        if self.fail_rollback:
            raise RuntimeError('private rollback failure')
        self.staged.clear()

    def close(self):
        self.events.append('close')
        self.close_count += 1
        self.closed = True
        if self.fail_close:
            raise RuntimeError('private close failure')


class Provider:
    def __init__(self, connection):
        self.connection = connection
        self.open_count = 0

    def open(self, binding):
        self.open_count += 1
        return self.connection


class ConflictTests(base.FoundationTests):
    def setUp(self):
        super().setUp()
        self.catalog.permission = 'admin'
        binding = self.api.ConnectionBinding('pg.synthetic', LIBRARY, A, CREDENTIAL, DEVICE,
                                             '127.0.0.1', 55439, 'synthetic_a', 'runtime_a', 'secrets.synthetic.a')
        self.registry = self.api.ConnectionRegistry([binding])
        self.authority = Authority()
        self.connection = Connection(self.api, self.authority)
        self.provider = Provider(self.connection)
        self.manager = self.api.SessionManager(self.router, self.registry, self.provider, self.authority)
        self.route = self.plan('memory_add')
        self.repository = object.__new__(VectorRepository)
        self.repository.session = self.api.LibrarySession(self.route, self.connection)
        self.repository.lib, self.repository.project = LIBRARY, PROJECT
        self.repository.contract = SimpleNamespace(key='trusted-model', dimension=768)
        self.repository.scope = SimpleNamespace(require=lambda session: None)
        self.job = Job(1, LIBRARY, 'context', 9, 'upsert', 1, 'digest', 'trusted-model', PROJECT)

    def operation(self, *, lock=True):
        return self.manager._projection_apply(
            lambda session: self.repository.document(self.job, lock=lock))

    def assert_fatal(self, expected):
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, self.operation())
        self.assertEqual(expected, cm.exception.code)
        self.assertEqual(0, self.connection.commit_count)
        self.assertEqual([], self.connection.persisted)
        self.assertFalse(self.authority.held)

    def test_confirmed_conflict_defers_only_after_rollback_close_and_normal_authority_exit(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        with self.assertRaises(conflict.ProjectionConflictDeferred) as cm:
            self.manager.run(self.route, self.operation())
        self.assertIs(type(cm.exception), conflict.ProjectionConflictDeferred)
        self.assertEqual(0, self.connection.commit_count)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)
        self.assertEqual([], self.connection.persisted)
        self.assertFalse(self.authority.held)
        # The sentinel is only visible after authority post-check and cleanup.
        self.assertEqual(['authority_enter', 'authority_post_check', 'authority_cleanup'],
                         self.authority.events)
        self.assertEqual(['begin', 'rollback', 'close'],
                         [event for event in self.connection.events if isinstance(event, str)])
        self.assertNotIn('commit', self.connection.events)
        self.assertEqual(1, self.provider.open_count)
        self.assertNotIn('40001', str(cm.exception))
        self.assertNotIn('synthetic', str(cm.exception))

    def test_rollback_failure_on_marker_is_fatal_not_deferred(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.connection.fail_rollback = True
        self.assert_fatal('projection_conflict_cleanup_failed')
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)

    def test_content_close_failure_on_marker_is_fatal_not_deferred(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.connection.fail_close = True
        self.assert_fatal('projection_conflict_cleanup_failed')
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)

    def test_authority_post_check_failure_stops_before_the_sentinel(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.authority.post_failure = True
        self.assert_fatal('control_post_check_failed')

    def test_authority_cleanup_failure_stops_before_the_sentinel(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.authority.cleanup_failure = True
        self.assert_fatal('control_cleanup_failed')

    def test_non_serialization_sqlstates_and_wrong_types_never_defer(self):
        cases = [
            psycopg.errors.DeadlockDetected('synthetic 40P01'),
            psycopg.errors.InFailedSqlTransaction('synthetic 25P02'),
            psycopg.errors.OperationalError('synthetic generic'),
        ]
        for actual in cases:
            with self.subTest(actual=type(actual).__name__):
                self.setUp()
                self.connection.raise_on = 'FOR SHARE OF i'
                self.connection.actual_error = actual
                with self.assertRaises(self.api.FoundationError) as cm:
                    self.manager.run(self.route, self.operation())
                self.assertEqual('database_operation_failed', cm.exception.code)
                self.assertNotIsInstance(cm.exception, conflict.ProjectionConflictDeferred)
                self.assertEqual(0, self.connection.commit_count)
                self.assertEqual(1, self.connection.rollback_count)
                self.assertEqual(1, self.connection.close_count)

    def test_sqlstate_only_forgery_is_not_a_typed_serialization_failure(self):
        class Forged(Exception):
            sqlstate = '40001'
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = Forged('typed 40001 is required')
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, self.operation())
        self.assertEqual('database_operation_failed', cm.exception.code)

    def test_serialization_failure_on_exists_elsewhere_never_defers(self):
        # The same 40001 on an unlocked context read is never eligible.
        self.connection.raise_on = 'context_items'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, self.operation(lock=False))
        self.assertEqual('database_operation_failed', cm.exception.code)
        self.assertNotIsInstance(cm.exception, conflict.ProjectionConflictDeferred)
        self.assertEqual(0, self.connection.commit_count)

    def test_legacy_locked_read_defers_only_after_rollback_close_and_normal_authority_exit(self):
        # The observed real race is the native locked legacy entity SELECT, the
        # same pre-effect shape as the context_items row.
        self.connection.raise_on = 'FOR SHARE OF m'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.job = Job(1, LIBRARY, 'legacy', 9, 'upsert', 1, 'digest', 'trusted-model', PROJECT)
        with self.assertRaises(conflict.ProjectionConflictDeferred) as cm:
            self.manager.run(self.route, self.operation())
        self.assertIs(type(cm.exception), conflict.ProjectionConflictDeferred)
        self.assertEqual(0, self.connection.commit_count)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)
        self.assertEqual([], self.connection.persisted)
        self.assertFalse(self.authority.held)
        self.assertEqual(['authority_enter', 'authority_post_check', 'authority_cleanup'],
                         self.authority.events)
        self.assertEqual(['begin', 'rollback', 'close'],
                         [event for event in self.connection.events if isinstance(event, str)])
        self.assertNotIn('commit', self.connection.events)
        self.assertNotIn('40001', str(cm.exception))
        self.assertNotIn('synthetic', str(cm.exception))

    def test_unlocked_legacy_read_remains_fatal(self):
        # Only the FOR SHARE form is eligible; the plain entity read keeps the
        # pre-existing redacted failure.
        self.connection.raise_on = 'em_content.memories'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        self.job = Job(1, LIBRARY, 'legacy', 9, 'upsert', 1, 'digest', 'trusted-model', PROJECT)
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, self.operation(lock=False))
        self.assertEqual('database_operation_failed', cm.exception.code)
        self.assertNotIsInstance(cm.exception, conflict.ProjectionConflictDeferred)
        self.assertEqual(0, self.connection.commit_count)
        self.assertEqual([], self.connection.persisted)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)
        self.assertFalse(self.authority.held)

    def test_plain_run_gets_no_automatic_recovery(self):
        self.connection.raise_on = 'FOR SHARE OF i'
        self.connection.actual_error = psycopg.errors.SerializationFailure('synthetic 40001')
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, lambda session: self.repository.document(self.job, lock=True))
        self.assertEqual('database_operation_failed', cm.exception.code)
        self.assertNotIsInstance(cm.exception, conflict.ProjectionConflictDeferred)
        self.assertEqual(0, self.connection.commit_count)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)

    def test_unknown_commit_with_opt_in_remains_fail_stop(self):
        callback = self.manager._projection_apply(lambda session: 'applied')
        self.connection.unknown_commit = True
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.route, callback)
        self.assertEqual('commit_result_unknown', cm.exception.code)
        self.assertEqual(1, self.connection.commit_count)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertEqual(1, self.connection.close_count)
        self.assertEqual(1, self.provider.open_count)
        self.assertNotIsInstance(cm.exception, conflict.ProjectionConflictDeferred)

    def test_opt_in_is_rejected_on_reads_and_with_publish(self):
        callback = self.manager._projection_apply(lambda session: None)
        with self.assertRaises(ValueError):
            self.manager.run(self.plan('context_read'), callback)
        with self.assertRaises(ValueError):
            self.manager.run(self.route, callback, publish=lambda value: None)
        self.assertEqual(0, self.provider.open_count)


if __name__ == '__main__':
    unittest.main()
