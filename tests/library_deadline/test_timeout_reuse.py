"""Driver-port tests for the default-off fixed timeout-reuse candidate.

These are typed fake-driver tests, not live PostgreSQL evidence.  The trusted
root fixture still has to verify actual GUC values, locks and connection
metadata.  Every assertion about reuse is about setter SQL count on the fake
raw connection.
"""
import hashlib
import inspect
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys

from evolvmem.library_deadline import timeout_reuse
from evolvmem.library_deadline.driver import connect
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import FoundationError

CONTENT_SQL = ('SELECT principal_id, library_id, policy_epoch, route_epoch, permission, '
               'authority_state, acl_state FROM em_security.session_context()')
assert hashlib.sha256(CONTENT_SQL.encode()).hexdigest() == \
    '83b71cd0fc803ea352f4459ba94c84ea53b2068e3f7d65fb82c7a9cc9ddcf9c3'
NATIVE_MODE_SQL = "SELECT to_regclass('em_security.history_readiness') IS NOT NULL"
assert hashlib.sha256(NATIVE_MODE_SQL.encode()).hexdigest() == \
    '2a8da2fe6082b4cf3b42950a0aca1e0b6e9461f4e3691ea7f37b6da2be3e015a'
UPSERT_SQL = ('INSERT INTO em_content.vector_projections\n'
    '            (library_id,project_id,index_kind,entity_id,model_contract,dimension,entity_revision,content_hash,projection_state,embedding,vector_hash)\n'
    '            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::public.vector,%s)\n'
    '            ON CONFLICT(library_id,index_kind,entity_id,model_contract) DO UPDATE SET\n'
    '             entity_revision=excluded.entity_revision,content_hash=excluded.content_hash,\n'
    '             projection_state=excluded.projection_state,embedding=excluded.embedding,\n'
    '             vector_hash=excluded.vector_hash,updated_at=clock_timestamp()\n'
    '            WHERE vector_projections.project_id=excluded.project_id\n'
    '             AND vector_projections.entity_revision<=excluded.entity_revision\n'
    '             AND (vector_projections.entity_revision<excluded.entity_revision OR vector_projections.content_hash=excluded.content_hash)\n'
    '            RETURNING entity_id')
assert hashlib.sha256(UPSERT_SQL.encode()).hexdigest() == \
    '03740220957bfa66ddcc376ac4acbdff3b79ddc4d7467c9674231b6fffeda5b6'
SETTER = "SELECT pg_catalog.set_config('statement_timeout'"


class Status:
    def __init__(self, name): self.name = name
    def __int__(self): return {'IDLE': 0, 'INTRANS': 2, 'INERROR': 3}[self.name]


class Cursor:
    def __init__(self, raw): self.raw = raw; self.description = None
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def execute(self, sql, params=()):
        raw = self.raw
        raw.calls.append((sql, params))
        if SETTER in sql:
            raw.setters += 1
        if raw.fail_execute is not None and sql == raw.fail_execute:
            raw.fail_execute = None
            raise RuntimeError('private execute failure')
        if sql.upper().startswith('BEGIN'):
            raw.info.transaction_status = Status('INTRANS')
        elif sql.strip().upper() in ('COMMIT', 'ROLLBACK'):
            raw.info.transaction_status = Status('IDLE')
        return self
    def fetchall(self):
        if self.raw.fail_fetch:
            self.raw.fail_fetch = False
            raise RuntimeError('private fetch failure')
        return ()
    def fetchone(self): return self.fetchall() and None
    def close(self):
        if self.raw.fail_close:
            self.raw.fail_close = False
            raise RuntimeError('private close failure')


def tls_parameters(**over):
    values = {'sslmode': 'verify-full', 'sslrootcert': '/ca.pem'}
    values.update(over)
    return values


class Raw:
    def __init__(self, *, host='db.example', hostaddr='10.0.0.5', port=5432,
                 dbname='content', user='runtime', ssl_in_use=True,
                 parameters=None, **over):
        self.autocommit = True; self.closed = False
        # Mirror Psycopg 3.3: ConnectionInfo has no ssl_in_use/sslrootcert;
        # TLS is on pgconn and resolved parameters are get_parameters().
        self.pgconn = NS(ssl_in_use=ssl_in_use)
        self.parameters = tls_parameters() if parameters is None else dict(parameters)
        self.info = NS(transaction_status=Status('IDLE'), host=host,
                       hostaddr=hostaddr, port=port, dbname=dbname,
                       user=user, get_parameters=lambda: dict(self.parameters))
        for key, value in over.items(): setattr(self.info, key, value)
        self.calls = []; self.setters = 0
        self.fail_execute = None; self.fail_fetch = False; self.fail_close = False
        self.commit_error = False; self.rollbacks = 0; self.commits = 0
        self.raise_cursor = False
    def cursor(self):
        if self.raise_cursor:
            self.raise_cursor = False
            raise RuntimeError('private cursor failure')
        return Cursor(self)
    def commit(self):
        self.commits += 1
        if self.commit_error: raise RuntimeError('private unknown COMMIT detail')
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True
    def cancel_safe(self, *, timeout): pass


def fake_psycopg():
    return NS(Connection=Raw, capabilities=NS(has_cancel_safe=lambda **kw: True))


class ReuseTests(unittest.TestCase):
    def setUp(self):
        timeout_reuse.clear_attested_profiles()
        self.guard = SyncRequestDeadlineGuard(12)
        self.raw = Raw()
        patcher = patch.dict(sys.modules, {'psycopg': fake_psycopg()})
        patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(timeout_reuse.clear_attested_profiles)

    def attest(self, **over):
        values = dict(kind='content', host='db.example', hostaddr='10.0.0.5',
                      port=5432, database='content', user='runtime',
                      sslmode='verify-full', sslrootcert='/ca.pem',
                      ca_sha256='a' * 64, statement_ms=10000, lock_ms=10000,
                      source_sha256='b' * 64, proof_sha256='c' * 64)
        values.update(over)
        return timeout_reuse.attest(**values)

    def kwargs(self, **over):
        values = dict(host='db.example', hostaddr='10.0.0.5', port=5432,
                      dbname='content', user='runtime', password='secret',
                      sslmode='verify-full', sslrootcert='/ca.pem',
                      connect_timeout=3, autocommit=True)
        values.update(over)
        return values

    def open(self, profile=None, raw=None, **kw):
        raw = raw or self.raw
        return connect(self.guard, lambda **kwargs: raw, self.kwargs(**kw),
                       kind='content', statement_ms=10000, lock_ms=10000,
                       profile=profile)

    def begin(self, conn):
        conn.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ WRITE')

    def test_timeouts_signature_preserved_for_observer(self):
        self.assertEqual(list(inspect.signature(
            __import__('evolvmem.library_deadline.driver', fromlist=['x']).GuardedConnection._timeouts
        ).parameters), ['self'])

    def test_equal_content_caps_are_valid(self):
        profile = self.attest()
        self.assertEqual((10000, 10000), (profile.statement_ms, profile.lock_ms))

    def test_default_off_sends_setter_every_time(self):
        with self.guard.enforce():
            conn = self.open()
            self.begin(conn)
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 2, self.raw.setters)
            self.assertEqual(0, conn.timeout_reuse_hits)

    def test_same_connection_intrans_and_saturated_reuses_after_one_setter(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            baseline = self.raw.setters
            for _ in range(4):
                conn.execute(CONTENT_SQL)
            self.assertEqual(baseline + 1, self.raw.setters)
            self.assertEqual(3, conn.timeout_reuse_hits)
            # Every application statement itself still executed unchanged.
            self.assertEqual(4, sum(1 for sql, _ in self.raw.calls if sql == CONTENT_SQL))

    def test_actual_resolved_connection_mismatch_keeps_original_path(self):
        raw = Raw(hostaddr='10.0.0.9')
        with self.guard.enforce():
            conn = self.open(self.attest(), raw=raw)
            self.begin(conn)
            before = raw.setters
            conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
            self.assertEqual(before + 2, raw.setters)
            self.assertEqual(0, conn.timeout_reuse_hits)

    def test_missing_ca_attestation_is_refused(self):
        with self.assertRaises(ValueError):
            self.attest(sslrootcert=None)
        with self.assertRaises(ValueError):
            self.attest(ca_sha256='')

    def test_omitted_hostaddr_or_ca_kwargs_disables(self):
        with self.guard.enforce():
            raw2 = Raw()
            # Remove CA from the exact connect kwargs: no profile match.
            conn2 = connect(self.guard, lambda **kw: raw2,
                            {k: v for k, v in self.kwargs().items() if k not in ('sslrootcert', 'hostaddr')},
                            kind='content', statement_ms=10000, lock_ms=10000,
                            profile=self.attest())
            self.begin(conn2)
            before = raw2.setters
            conn2.execute(CONTENT_SQL); conn2.execute(CONTENT_SQL)
            self.assertEqual(before + 2, raw2.setters)
            conn2.close()

    def test_str_subclass_encode_error_cannot_keep_record(self):
        class Bad(str):
            def encode(self, *args, **kwargs): raise UnicodeError('private')
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)  # prime
            conn.execute(Bad(CONTENT_SQL))  # exact type is str only: fallback
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)
            self.assertEqual(0, conn.timeout_reuse_hits)

    def test_cursor_creation_error_invalidates(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)  # prime
            self.raw.raise_cursor = True
            with self.assertRaises(RuntimeError):
                conn.execute(CONTENT_SQL)
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)

    def test_unknown_sql_and_implicit_guc_definer_restore_setter(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            conn.execute(CONTENT_SQL)  # reuse
            conn.execute("SELECT pg_catalog.set_config('statement_timeout', '1', true)")
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            conn.execute(CONTENT_SQL)  # new prime then reuse
            self.assertEqual(before + 1, self.raw.setters)

    def test_begin_commit_rollback_savepoint_all_invalidate(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            for boundary in ('SAVEPOINT s', 'ROLLBACK TO SAVEPOINT s',
                             'RELEASE SAVEPOINT s', 'SET statement_timeout = 0',
                             'COMMIT'):
                conn.execute(boundary)
                before = self.raw.setters
                conn.execute(CONTENT_SQL)
                self.assertEqual(before + 1, self.raw.setters, boundary)
                if boundary == 'COMMIT':
                    self.begin(conn)
                conn.execute(CONTENT_SQL)  # prime for next boundary

    def test_new_transaction_on_same_connection_does_not_reuse(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
            conn.execute('COMMIT')
            self.begin(conn)
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)
            # The first approved statement in the new transaction primed;
            # the next one may reuse.
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)

    def test_other_connection_has_no_shared_record(self):
        other = Raw()
        with self.guard.enforce():
            first = self.open(self.attest())
            second = self.open(self.attest(), raw=other)
            self.begin(first); self.begin(second)
            first.execute(CONTENT_SQL); first.execute(CONTENT_SQL)
            before = other.setters
            second.execute(CONTENT_SQL); second.execute(CONTENT_SQL)
            self.assertEqual(before + 1, other.setters)

    def test_mismatched_profile_caps_or_binding_stays_off(self):
        with self.guard.enforce():
            for profile, kwargs in (
                (self.attest(statement_ms=2000, lock_ms=500), self.kwargs()),
                (self.attest(host='other.example'), self.kwargs()),
                (self.attest(database='other'), self.kwargs()),
                (self.attest(user='other'), self.kwargs()),
                (self.attest(hostaddr='10.0.0.9'), self.kwargs()),
                (self.attest(sslrootcert='/other.pem'), self.kwargs()),
            ):
                raw = Raw(); conn = self.open(profile, raw=raw, **{})
                # For the binding-only mismatches, connect kwargs still match
                # the original request; no reuse may be enabled.
                self.begin(conn)
                before = raw.setters
                conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
                self.assertEqual(before + 2, raw.setters, profile)
                conn.close()

    def test_near_deadline_below_either_cap_uses_original_setter_each_time(self):
        self.guard = SyncRequestDeadlineGuard(0.5)
        with self.guard.enforce():
            conn = self.open(self.attest(statement_ms=10000, lock_ms=10000))
            self.begin(conn)
            before = self.raw.setters
            conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
            self.assertEqual(before + 2, self.raw.setters)
            self.assertEqual(0, conn.timeout_reuse_hits)
            values = [params for sql, params in self.raw.calls if SETTER in sql]
            self.assertTrue(all(int(value[0][:-2]) < 10000 for value in values))

    def test_failed_fetch_close_cancel_and_execute_restore_setter(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            cur = conn.cursor(); cur.execute(CONTENT_SQL)
            self.raw.fail_fetch = True
            with self.assertRaises(RuntimeError): cur.fetchall()
            before = self.raw.setters; conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)
            cur = conn.cursor(); cur.execute(CONTENT_SQL)
            self.raw.fail_close = True
            with self.assertRaises(RuntimeError): cur.close()
            before = self.raw.setters; conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)
            conn.execute(CONTENT_SQL)
            conn.cancel(1)
            before = self.raw.setters; conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)

    def test_ordinary_successful_cursor_close_retains_record(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            cur = conn.cursor(); cur.execute(CONTENT_SQL); cur.close()
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before, self.raw.setters)

    def test_raw_failure_then_next_approved_primes(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            self.raw.fail_execute = CONTENT_SQL
            with self.assertRaises(RuntimeError):
                conn.execute(CONTENT_SQL)
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)

    def test_commit_unknown_and_no_recovery(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            self.raw.commit_error = True
            with self.assertRaises(FoundationError) as caught:
                conn.commit()
            self.assertEqual('commit_result_unknown', caught.exception.code)
            self.assertEqual(1, self.raw.commits)
            self.assertFalse(any('COMMIT' in sql and sql != 'COMMIT' for sql, _ in self.raw.calls))

    def test_raw_rollback_and_retire_clear_record(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            conn.rollback()
            before = self.raw.setters
            conn.execute(CONTENT_SQL)  # IDLE: setter, no prime
            self.assertEqual(before + 1, self.raw.setters)
            self.begin(conn)
            conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
            conn.retire()
            self.assertEqual(1, self.raw.rollbacks)

    def test_no_free_boolean_accepts_unreviewed_sql(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            conn.execute(CONTENT_SQL)
            conn.execute('SELECT 1')
            before = self.raw.setters
            conn.execute(CONTENT_SQL)
            self.assertEqual(before + 1, self.raw.setters)
            with self.assertRaises(TypeError):
                conn.execute(CONTENT_SQL, (), True)

    def test_tls_false_or_gss_disables(self):
        for ssl_in_use in (False, 'GSS'):
            raw = Raw(ssl_in_use=ssl_in_use)
            with self.guard.enforce():
                conn = self.open(self.attest(), raw=raw)
                self.begin(conn)
                before = raw.setters
                conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
                self.assertEqual(before + 2, raw.setters, ssl_in_use)
                self.assertEqual(0, conn.timeout_reuse_hits)
                conn.close()

    def test_missing_or_wrong_tls_parameters_disable(self):
        cases = (
            {'sslrootcert': '/ca.pem'},                 # missing sslmode
            {'sslmode': 'verify-full'},                 # missing CA
            tls_parameters(sslmode='require'),          # wrong mode
            tls_parameters(sslrootcert='/other.pem'),   # wrong CA
        )
        for parameters in cases:
            raw = Raw(parameters=parameters)
            with self.guard.enforce():
                conn = self.open(self.attest(), raw=raw)
                self.begin(conn)
                before = raw.setters
                conn.execute(CONTENT_SQL); conn.execute(CONTENT_SQL)
                self.assertEqual(before + 2, raw.setters, parameters)
                conn.close()

    def test_base_profile_excludes_native_background(self):
        profile = self.attest()
        self.assertFalse(timeout_reuse.approved_sql('content', NATIVE_MODE_SQL, profile))
        with self.guard.enforce():
            conn = self.open(profile)
            self.begin(conn)
            before = self.raw.setters
            conn.execute(NATIVE_MODE_SQL); conn.execute(NATIVE_MODE_SQL)
            self.assertEqual(before + 2, self.raw.setters)

    def test_native_background_profile_permits_audited_extra(self):
        profile = self.attest(capability='native_background',
                              secondary_proof_sha256='d' * 64)
        self.assertTrue(timeout_reuse.approved_sql('content', NATIVE_MODE_SQL, profile))
        with self.guard.enforce():
            conn = self.open(profile)
            self.begin(conn)
            baseline = self.raw.setters
            conn.execute(CONTENT_SQL)       # first approved primes
            conn.execute(NATIVE_MODE_SQL)   # audited extra reuses
            conn.execute(CONTENT_SQL)       # base reuses
            self.assertEqual(baseline + 1, self.raw.setters)
            self.assertEqual(2, conn.timeout_reuse_hits)

    def test_vector_upsert_blocked_even_with_secondary(self):
        profile = self.attest(capability='native_background',
                              secondary_proof_sha256='d' * 64)
        self.assertFalse(timeout_reuse.approved_sql('content', UPSERT_SQL, profile))
        with self.guard.enforce():
            conn = self.open(profile)
            self.begin(conn)
            before = self.raw.setters
            conn.execute(UPSERT_SQL); conn.execute(UPSERT_SQL)
            self.assertEqual(before + 2, self.raw.setters)

    def test_secondary_attestation_requires_content_and_separate_proof(self):
        with self.assertRaises(ValueError):
            self.attest(capability='native_background')  # missing secondary proof
        with self.assertRaises(ValueError):
            self.attest(kind='control', capability='native_background',
                        secondary_proof_sha256='d' * 64)
        with self.assertRaises(ValueError):
            self.attest(secondary_proof_sha256='d' * 64)  # base cannot carry it
        with self.assertRaises(ValueError):
            self.attest(capability='unknown', secondary_proof_sha256='d' * 64)

    def test_other_binding_secondary_disabled(self):
        raw = Raw(hostaddr='10.0.0.9')
        profile = self.attest(capability='native_background',
                              secondary_proof_sha256='d' * 64)
        with self.guard.enforce():
            conn = self.open(profile, raw=raw)
            self.begin(conn)
            before = raw.setters
            conn.execute(NATIVE_MODE_SQL); conn.execute(NATIVE_MODE_SQL)
            self.assertEqual(before + 2, raw.setters)

    def test_inventory_is_exactly_38_base_plus_14_native(self):
        base = set().union(*timeout_reuse.APPROVED_SQL_SHA256.values())
        native = set(timeout_reuse.NATIVE_BACKGROUND_SQL_SHA256)
        self.assertEqual(38, len(base))
        self.assertEqual(4, len(timeout_reuse.APPROVED_SQL_SHA256['control']))
        self.assertEqual(34, len(timeout_reuse.APPROVED_SQL_SHA256['content']))
        self.assertEqual(14, len(native))
        self.assertEqual(52, len(base | native))
        self.assertNotIn('03740220957bfa66ddcc376ac4acbdff3b79ddc4d7467c9674231b6fffeda5b6',
                         base | native)
        self.assertTrue(all(re.fullmatch(r'[0-9a-f]{64}', d) for d in base | native))
        self.assertTrue(timeout_reuse.approved_sql('content', CONTENT_SQL))
        self.assertFalse(timeout_reuse.approved_sql('content', CONTENT_SQL + ' '))

    def test_inventory_is_exactly_the_38_reviewed_digests(self):
        all_digests = set().union(*timeout_reuse.APPROVED_SQL_SHA256.values())
        self.assertEqual(38, len(all_digests))
        self.assertEqual(4, len(timeout_reuse.APPROVED_SQL_SHA256['control']))
        self.assertEqual(34, len(timeout_reuse.APPROVED_SQL_SHA256['content']))
        self.assertTrue(all(re.fullmatch(r'[0-9a-f]{64}', d) for d in all_digests))
        self.assertTrue(timeout_reuse.approved_sql('content', CONTENT_SQL))
        self.assertFalse(timeout_reuse.approved_sql('content', CONTENT_SQL + ' '))


if __name__ == '__main__':
    unittest.main()
