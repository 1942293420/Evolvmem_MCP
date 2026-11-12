"""Transaction outcomes using an external-driver double; no real PostgreSQL."""
from contextlib import contextmanager
import dataclasses
import unittest
from tests.library_foundation import test_foundation as base
from tests.library_foundation.test_foundation import A, B, LIBRARY, CREDENTIAL, DEVICE


class Authority:
    def __init__(self):
        self.held = False

    @contextmanager
    def hold(self, route):
        if self.held:
            raise AssertionError('shared/reentrant request lease')
        self.held = True
        try:
            yield
        finally:
            self.held = False


class Connection:
    def __init__(self, api, authority):
        self.authority = authority
        self.role = api.DatabaseRole('runtime_a', 'runtime_a', False, False, False, True)
        self.state = api.ContentSessionState(A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready')
        self.credential = api.DatabaseCredential(CREDENTIAL, DEVICE, 3)
        self.staged = []
        self.persisted = []
        self.closed = False
        self.read_only = None
        self.commit_count = 0
        self.rollback_count = 0
        self.unknown_commit = False

    def begin(self, *, read_only):
        assert self.authority.held
        self.read_only = read_only

    def inspect_role(self):
        return self.role

    def session_context(self):
        return self.state

    def pin_session_context(self, policy_epoch, route_epoch):
        # Synthetic port only: actual row-lock behavior is covered in real PG.
        assert self.authority.held and self.read_only is False
        if (policy_epoch, route_epoch) != (self.state.policy_epoch, self.state.route_epoch):
            return None
        return self.state

    def session_credential(self):
        return self.credential

    def execute(self, sql, params=()):
        if self.read_only:
            raise RuntimeError('test driver refuses writes in READ ONLY')
        self.staged.append(params[0])

    def commit(self):
        assert self.authority.held
        self.persisted.extend(self.staged)
        self.staged.clear()
        self.commit_count += 1
        if self.unknown_commit:
            raise RuntimeError('private driver transport detail')

    def rollback(self):
        self.rollback_count += 1
        self.staged.clear()

    def close(self):
        self.closed = True


class Provider:
    def __init__(self, connection):
        self.connection = connection
        self.open_count = 0

    def open(self, binding):
        self.open_count += 1
        return self.connection


class SessionTests(unittest.TestCase):
    error = base.FoundationTests.error
    plan = base.FoundationTests.plan

    def setUp(self):
        base.FoundationTests.setUp(self)
        self.assertTrue(hasattr(self.api, 'SessionManager'), 'session lifecycle is not implemented')
        self.catalog.permission = 'admin'
        binding = self.api.ConnectionBinding('pg.synthetic', LIBRARY, A, CREDENTIAL, DEVICE,
                                            '127.0.0.1', 55439, 'synthetic_a', 'runtime_a', 'secrets.synthetic.a')
        self.registry = self.api.ConnectionRegistry([binding])
        self.authority = Authority()
        self.connection = Connection(self.api, self.authority)
        self.provider = Provider(self.connection)
        self.manager = self.api.SessionManager(self.router, self.registry, self.provider, self.authority)

    def test_write_commits_only_after_exit_and_connection_is_closed(self):
        with self.manager.open(self.plan('memory_add')) as session:
            session.execute('synthetic operation', ('new record',))
            self.assertEqual([], self.connection.persisted)
        self.assertEqual(['new record'], self.connection.persisted)
        self.assertTrue(self.connection.closed)
        self.assertFalse(self.authority.held)
        self.error('session_closed', lambda: session.execute('synthetic operation'))

    def test_read_transaction_is_read_only(self):
        with self.manager.open(self.plan()):
            self.assertTrue(self.connection.read_only)

    def test_operation_failure_rolls_back_without_losing_original_error(self):
        with self.assertRaisesRegex(ValueError, 'domain error'):
            with self.manager.open(self.plan('memory_add')) as session:
                session.execute('synthetic operation', ('must not commit',))
                raise ValueError('domain error')
        self.assertEqual([], self.connection.persisted)
        self.assertEqual(1, self.connection.rollback_count)
        self.assertTrue(self.connection.closed)
        self.assertFalse(self.authority.held)

    def test_epoch_change_during_operation_refuses_commit(self):
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan('memory_add')) as session:
                session.execute('synthetic operation', ('must not commit',))
                self.catalog.library = dataclasses.replace(self.library, policy_epoch=8)
        self.assertEqual('policy_epoch_changed', cm.exception.code)
        self.assertEqual([], self.connection.persisted)
        self.assertTrue(self.connection.closed)

    def test_changed_credential_is_rechecked_before_connect(self):
        route = self.plan()
        self.catalog.binding = dataclasses.replace(self.binding, active=False)
        self.error('unauthorized', lambda: self._enter(route))
        self.assertEqual(0, self.provider.open_count)

    def _enter(self, route):
        with self.manager.open(route):
            pass

    def test_mismatched_database_or_projection_never_runs_business(self):
        for changes in ({'principal_id': B}, {'library_id': B}, {'policy_epoch': 6},
                        {'route_epoch': 10}, {'acl_state': 'updating'}, {'authority_state': 'pg_staging'},
                        {'permission': None}):
            self.connection.state = dataclasses.replace(
                self.api.ContentSessionState(A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready'), **changes)
            self.error('database_authorization_mismatch', lambda: self._enter(self.plan()))
        self.assertEqual([], self.connection.persisted)

    def test_superuser_owner_bypass_or_role_switch_is_rejected(self):
        for changes in ({'superuser': True}, {'bypass_rls': True}, {'owns_protected_objects': True},
                        {'current_user': 'other_role'}, {'session_user': 'other_role'}, {'row_security': False}):
            self.connection.role = dataclasses.replace(
                self.api.DatabaseRole('runtime_a', 'runtime_a', False, False, False, True), **changes)
            self.error('unsafe_database_role', lambda: self._enter(self.plan()))

    def test_unknown_commit_is_not_retried_or_reported_as_rollback_success(self):
        self.connection.unknown_commit = True
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan('memory_add')) as session:
                session.execute('synthetic operation', ('possibly committed',))
        self.assertEqual('commit_result_unknown', cm.exception.code)
        self.assertEqual(['possibly committed'], self.connection.persisted)
        self.assertEqual(1, self.connection.commit_count)
        self.assertEqual(1, self.provider.open_count)
        self.assertTrue(self.connection.closed)
        self.assertNotIn('private', str(cm.exception))

    def test_publisher_runs_inside_authority_lease_after_commit(self):
        published = []
        def operation(session):
            session.execute('synthetic operation', ('record',))
            return {'id': 9}
        def publish(value):
            self.assertTrue(self.authority.held)
            self.assertEqual(['record'], self.connection.persisted)
            published.append(value)
        self.manager.run(self.plan('memory_add'), operation, publish=publish)
        self.assertEqual([{'id': 9}], published)

    def test_driver_connection_failure_is_redacted(self):
        def connector(**kwargs):
            raise RuntimeError('password=' + kwargs['password'])
        provider = self.api.PsycopgDirectProvider(connector, lambda ref: 'synthetic-only-secret')
        err = self.error('database_connection_failed', lambda: provider.open(self.registry.resolve(self.plan())))
        self.assertNotIn('synthetic-only-secret', str(err))


if __name__ == '__main__':
    unittest.main()
