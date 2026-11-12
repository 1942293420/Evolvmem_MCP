"""Focused tests for the fused per-segment control role check.

The reused-service LOGIN/expiry attestation is part of the one bounded role
SELECT already executed by ``ControlSession.begin`` for every segment.  These
tests use a tiny SQL-port double so the exact statement string, row shape and
fail-closed decisions are observable without a database.  They do not claim
PostgreSQL/RLS/TLS execution.
"""
import hashlib
import unittest
from types import SimpleNamespace as NS

from evolvmem.library_catalog.adapter import (ControlBinding, ControlProvider,
                                              ControlSession)
from evolvmem.library_deadline import timeout_reuse
from evolvmem.library_foundation import FoundationError

BINDING = ControlBinding('127.0.0.1', 5432, 'synthetic', 'catalog_reader', 'secret')

OLD_REUSE_VALIDATION_SQL = (
    "SELECT r.rolcanlogin AS enabled, "
    "(r.rolvaliduntil IS NULL OR r.rolvaliduntil > pg_catalog.now()) AS nonexpired, "
    "(r.rolname = current_user) AS identity_ok "
    "FROM pg_catalog.pg_roles r WHERE r.rolname = session_user")
OLD_ROLE_SQL = "\n            SELECT session_user, current_user, r.rolsuper AS superuser,\n                   r.rolbypassrls AS bypass_rls, r.rolcreaterole AS create_role,\n                   r.rolcreatedb AS create_db,\n                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS memberships,\n                   EXISTS (SELECT 1 FROM pg_catalog.pg_class c\n                       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace\n                       WHERE n.nspname='em_control' AND c.relkind IN ('r','p','v','m','f')\n                       AND pg_catalog.has_table_privilege(r.oid,c.oid,\n                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) AS direct_access,\n                   (EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname='em_control'\n                              AND pg_catalog.pg_has_role(session_user,n.nspowner,'MEMBER'))\n                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c\n                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace\n                        WHERE n.nspname='em_control'\n                          AND pg_catalog.pg_has_role(session_user,c.relowner,'MEMBER'))) AS owns_protected\n            FROM pg_catalog.pg_roles r WHERE r.rolname=session_user\n        "
OLD_ROLE_DIGEST = '1dc0f430e5c05ed5297952240403281a81abe8553bddc7705c2c473138a344f7'
NEW_ROLE_DIGEST = '18713999bf3ddf09e27146955b5b35719796eb4a91a87f38469b1b05d87b43af'


def role_row(**overrides):
    row = dict(session_user='catalog_reader', current_user='catalog_reader',
               superuser=False, bypass_rls=False, create_role=False,
               create_db=False, login_enabled=True, nonexpired=True,
               memberships=False, direct_access=False, owns_protected=False)
    row.update(overrides)
    return row


class _Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=()):
        self.raw.executed.append(sql)
        if sql.startswith('BEGIN'):
            self.raw.info.transaction_status.name = 'INTRANS'
            self.description = None
            self._rows = []
            return self
        if 'pg_catalog.set_config' in sql:
            self.description = [NS(name='configured')]
            self._rows = [('ok',)]
            return self
        if 'pg_catalog.pg_roles' in sql:
            self.raw.role_sql = sql
            rows = self.raw.role_rows
            if rows:
                names = list(rows[0])
                self.description = [NS(name=name) for name in names]
                self._rows = [tuple(row[name] for name in names) for row in rows]
            else:
                self.description = []
                self._rows = []
            return self
        raise AssertionError('unexpected SQL boundary: ' + sql)

    def fetchall(self):
        return self._rows


class _Raw:
    def __init__(self, role_rows):
        self.role_rows = role_rows
        self.autocommit = True
        self.closed = False
        self.info = NS(transaction_status=NS(name='IDLE'))
        self.executed = []
        self.role_sql = None

    def cursor(self):
        return _Cursor(self)


class FusedBeginTests(unittest.TestCase):
    def begin(self, rows, *, reused=True):
        session = ControlSession(_Raw(rows), BINDING)
        session.begin(read_only=True, lock_timeout_ms=500,
                      statement_timeout_ms=2000, reused=reused)
        return session

    def denied(self, rows, *, reused=True):
        session = ControlSession(_Raw(rows), BINDING)
        with self.assertRaises(FoundationError) as caught:
            session.begin(read_only=True, lock_timeout_ms=500,
                          statement_timeout_ms=2000, reused=reused)
        self.assertEqual('unsafe_control_role', caught.exception.code)
        return session

    def role_queries(self, raw):
        return [sql for sql in raw.executed if 'pg_catalog.pg_roles' in sql]

    # The combined result -------------------------------------------------
    def test_reused_combined_row_accepts_exact_true_booleans(self):
        session = self.begin([role_row()], reused=True)
        self.assertFalse(session.broken)
        self.assertEqual(1, len(self.role_queries(session.raw)))
        self.assertIn('r.rolcanlogin AS login_enabled', session.raw.role_sql)
        self.assertIn('r.rolvaliduntil', session.raw.role_sql)
        self.assertEqual(3, len(session.raw.executed), 'BEGIN, setter, role only')

    def test_defaultoff_legacy_row_without_optional_columns_accepted(self):
        legacy = role_row()
        legacy.pop('login_enabled')
        legacy.pop('nonexpired')
        session = self.begin([legacy], reused=False)
        self.assertFalse(session.broken)
        self.assertEqual(1, len(self.role_queries(session.raw)))

    def test_begin_defaults_to_fresh_contract(self):
        legacy = role_row()
        legacy.pop('login_enabled')
        legacy.pop('nonexpired')
        session = ControlSession(_Raw([legacy]), BINDING)
        session.begin(read_only=True, lock_timeout_ms=500,
                      statement_timeout_ms=2000)
        self.assertFalse(session.broken)

    def test_reused_legacy_row_missing_both_optional_columns_fails(self):
        legacy = role_row()
        legacy.pop('login_enabled')
        legacy.pop('nonexpired')
        session = self.denied([legacy])
        self.assertTrue(session.broken)

    def test_reused_missing_each_optional_field_fails(self):
        for field in ('login_enabled', 'nonexpired'):
            with self.subTest(field=field):
                row = role_row()
                row.pop(field)
                session = self.denied([row])
                self.assertTrue(session.broken)

    def test_reused_null_each_optional_field_fails(self):
        for field in ('login_enabled', 'nonexpired'):
            with self.subTest(field=field):
                session = self.denied([role_row(**{field: None})])
                self.assertTrue(session.broken)

    def test_reused_non_boolean_optional_field_fails(self):
        for value in (1, 0, 'true', False):
            for field in ('login_enabled', 'nonexpired'):
                with self.subTest(field=field, value=value):
                    self.denied([role_row(**{field: value})])

    def test_wrong_row_counts_fail_closed(self):
        for rows in ([], [role_row(), role_row()]):
            with self.subTest(count=len(rows)):
                session = self.denied(rows)
                self.assertFalse(session.broken,
                                 'the pre-existing row-count refusal is unchanged')

    def test_always_checked_identity_and_unsafe_flags(self):
        for field, value in (('session_user', 'other_reader'),
                             ('current_user', 'other_reader'),
                             ('superuser', True), ('bypass_rls', True),
                             ('create_role', True), ('create_db', True),
                             ('memberships', True), ('direct_access', True),
                             ('owns_protected', True)):
            for reused in (False, True):
                with self.subTest(field=field, reused=reused):
                    self.denied([role_row(**{field: value})], reused=reused)

    # One fresh query per segment, no cache --------------------------------
    def test_each_segment_executes_its_own_combined_role_query(self):
        raws = []
        for index in range(3):
            session = self.begin([role_row()], reused=index > 0)
            raws.append(session.raw)
        for raw in raws:
            queries = self.role_queries(raw)
            self.assertEqual(1, len(queries))
            self.assertEqual(queries[0], raws[0].role_sql,
                             'same reviewed SQL is re-executed for each segment')
            self.assertNotIn('AS identity_ok', queries[0])

    def test_no_standalone_reuse_validation_sql_executed(self):
        self.assertFalse(hasattr(ControlProvider, '_REUSE_VALIDATION_SQL'))
        self.assertFalse(hasattr(ControlProvider, '_validate_reused'))
        session = self.begin([role_row()], reused=True)
        self.assertNotIn(OLD_REUSE_VALIDATION_SQL, session.raw.executed)
        self.assertFalse(any('AS identity_ok' in sql for sql in session.raw.executed))

    # Exact timeout-reuse whitelist ---------------------------------------
    def test_digest_matches_changed_query_and_old_is_not_approved(self):
        session = self.begin([role_row()], reused=True)
        sql = session.raw.role_sql
        self.assertEqual(NEW_ROLE_DIGEST,
                         hashlib.sha256(sql.encode('utf-8')).hexdigest())
        self.assertTrue(timeout_reuse.approved_sql('control', sql))
        self.assertIn(NEW_ROLE_DIGEST, timeout_reuse.APPROVED_SQL_SHA256['control'])
        self.assertEqual(OLD_ROLE_DIGEST,
                         hashlib.sha256(OLD_ROLE_SQL.encode('utf-8')).hexdigest())
        self.assertFalse(timeout_reuse.approved_sql('control', OLD_ROLE_SQL))
        self.assertNotIn(OLD_ROLE_DIGEST, timeout_reuse.APPROVED_SQL_SHA256['control'])

    def test_control_whitelist_is_exactly_four_with_other_digests_unchanged(self):
        control = timeout_reuse.APPROVED_SQL_SHA256['control']
        self.assertEqual(4, len(control))
        self.assertEqual(frozenset({
            NEW_ROLE_DIGEST,
            'fd99fd744176a5ffc1160f67e78dcc6e567a38a8d09649ae17daa861f56dac90',
            'e4d6d4471d8530c13bb6981e58febf18d94e02e8db26e03e755a17614e57bd91',
            'de9fa89ae4782959536f4ed4938cbfb6715e411b4005eb376aa4724bc33b221b',
        }), control)
        self.assertEqual(34, len(timeout_reuse.APPROVED_SQL_SHA256['content']))
        self.assertEqual(14, len(timeout_reuse.NATIVE_BACKGROUND_SQL_SHA256))
        self.assertEqual(38, len(set().union(*timeout_reuse.APPROVED_SQL_SHA256.values())))


if __name__ == '__main__':
    unittest.main()
