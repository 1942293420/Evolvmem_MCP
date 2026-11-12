"""Execute our psycopg boundary using a narrow cursor double, not a DB emulator.

These tests validate the adapter's arguments/lifecycle, not PostgreSQL SQL or RLS.
"""
import unittest
from tests.library_foundation import test_sessions as base
from tests.library_foundation.test_foundation import A, LIBRARY, CREDENTIAL, DEVICE


class Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rows = ()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.closed_cursors += 1

    def execute(self, sql, params):
        self.raw.queries.append((sql, params))
        if self.raw.fail_query:
            raise RuntimeError('private SQL diagnostic and password')
        if 'FROM pg_catalog.pg_roles r WHERE' in sql:
            # NOINHERIT owner membership is not visible through a USAGE check.
            owner = self.raw.noinherit_owner and "'MEMBER'" in sql
            membership = self.raw.noinherit_owner and 'pg_auth_members' in sql
            self.rows = (('runtime_a', 'runtime_a', False, False, owner, True, False, False, membership),)
        elif 'FROM em_security.session_context()' in sql:
            self.rows = ((A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready'),)
        elif 'FROM em_security.session_credential()' in sql:
            self.rows = ((CREDENTIAL, DEVICE, 3),)
        elif sql == 'SELECT %s':
            self.rows = ((params[0],),)
        elif sql.startswith('SELECT pg_catalog.set_config'):
            self.rows = (('configured',),)
        elif sql.startswith('BEGIN TRANSACTION'):
            return
        else:
            raise AssertionError('unexpected SQL boundary in test double')
        self.description = ('synthetic description',)

    def fetchall(self):
        return self.rows


class RawConnection:
    def __init__(self):
        self.queries = []
        self.closed = False
        self.closed_cursors = 0
        self.commits = 0
        self.rollbacks = 0
        self.noinherit_owner = False
        self.fail_query = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class DriverAdapterTests(unittest.TestCase):
    error = base.SessionTests.error
    plan = base.SessionTests.plan

    def setUp(self):
        base.SessionTests.setUp(self)
        self.raw = RawConnection()
        self.connect_args = None
        self.resolved_refs = []
        def connect(**kwargs):
            self.connect_args = kwargs
            return self.raw
        def secret(reference):
            self.resolved_refs.append(reference)
            return 'synthetic-test-password'
        self.driver = self.api.PsycopgDirectProvider(connect, secret)
        self.manager = self.api.SessionManager(self.router, self.registry, self.driver, self.authority)

    def test_driver_boundary_uses_trusted_arguments_and_closes_every_cursor(self):
        with self.manager.open(self.plan()) as session:
            self.assertEqual((('hello',),), session.execute('SELECT %s', ('hello',)))
        self.assertEqual('runtime_a', self.connect_args['user'])
        self.assertEqual('synthetic_a', self.connect_args['dbname'])
        self.assertEqual('verify-full', self.connect_args['sslmode'])
        self.assertNotIn('dsn', self.connect_args)
        self.assertEqual(['secrets.synthetic.a'], self.resolved_refs)
        self.assertEqual('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY', self.raw.queries[0][0])
        self.assertEqual(len(self.raw.queries), self.raw.closed_cursors)
        self.assertEqual(1, self.raw.commits)
        self.assertTrue(self.raw.closed)

    def test_noinherit_owner_membership_cannot_pass_adapter_introspection(self):
        self.raw.noinherit_owner = True
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan()):
                self.fail('SET ROLE capable runtime admitted')
        self.assertEqual('unsafe_database_role', cm.exception.code)
        self.assertEqual(0, self.raw.commits)
        self.assertTrue(self.raw.closed)

    def test_cursor_failure_is_redacted_and_connection_is_discarded(self):
        self.raw.fail_query = True
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan()):
                self.fail('failed BEGIN must not yield a session')
        self.assertEqual('database_operation_failed', cm.exception.code)
        self.assertNotIn('password', str(cm.exception))
        self.assertEqual(1, self.raw.rollbacks)
        self.assertTrue(self.raw.closed)


if __name__ == '__main__':
    unittest.main()
