"""Exact content transaction primer tests using the real guarded driver and fake wire."""
import ast
import hashlib
import inspect
import unittest
from unittest.mock import patch

from evolvmem.library_deadline import driver, timeout_reuse
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import _PsycopgConnection
from tests.library_deadline import test_timeout_reuse as fixtures


def fixed_statement(method, digest):
    adapter = ast.parse(inspect.getsource(_PsycopgConnection)).body[0]
    function, = (node for node in adapter.body if isinstance(node, ast.FunctionDef)
                 and node.name == method.__name__)
    matches = [node.value for node in ast.walk(function)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and hashlib.sha256(node.value.encode()).hexdigest() == digest]
    if len(matches) != 1:
        raise AssertionError('reviewed fixed SQL is missing from the actual adapter')
    return matches[0]


STATIC_SQL = fixed_statement(
    _PsycopgConnection.begin, '6ddc30498bc92ff0d63e5fe9b919c5f34d1d3320c83c25f22f513b341f2fa6eb')
ROLE_SQL = fixed_statement(
    _PsycopgConnection.inspect_role, 'd5004d0a5cae5796dbb13f6a49933823a3a79fa91554a3916df1cc10296aeffa')


def dynamic_setters(raw):
    return [params for sql, params in raw.calls if sql == driver._SET_TIMEOUTS]


class ContentStaticTimeoutPrimerTests(unittest.TestCase):
    setUp = fixtures.ReuseTests.setUp
    attest = fixtures.ReuseTests.attest
    kwargs = fixtures.ReuseTests.kwargs
    open = fixtures.ReuseTests.open
    begin = fixtures.ReuseTests.begin

    def test_real_content_begin_primes_first_role_and_keeps_both_original_setters(self):
        self.assertFalse(timeout_reuse.approved_sql('content', STATIC_SQL))
        with self.guard.enforce():
            conn = self.open(self.attest())
            _PsycopgConnection(conn).begin(read_only=False)
            self.assertEqual([('10000ms', '10000ms')] * 2, dynamic_setters(self.raw))
            conn.execute(ROLE_SQL)
            self.assertEqual(2, len(dynamic_setters(self.raw)))
            self.assertEqual(1, conn.timeout_reuse_hits)
            self.assertEqual(1, sum(sql == ROLE_SQL for sql, _ in self.raw.calls))
            before = len(dynamic_setters(self.raw))
            conn.execute(STATIC_SQL)
            self.assertEqual(before + 1, len(dynamic_setters(self.raw)))

    def test_same_transaction_read_only_begin_primes(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            _PsycopgConnection(conn).begin(read_only=True)
            conn.execute(ROLE_SQL)
            self.assertEqual(2, len(dynamic_setters(self.raw)))

    def test_default_off_or_actual_connection_mismatch_cannot_seed(self):
        with self.guard.enforce():
            for profile, raw in ((None, fixtures.Raw()), (self.attest(), fixtures.Raw(user='other'))):
                with self.subTest(profile=profile is not None):
                    conn = self.open(profile, raw=raw)
                    self.assertIsNone(conn._reuse_profile)
                    _PsycopgConnection(conn).begin(read_only=True)
                    conn.execute(ROLE_SQL)
                    self.assertEqual(3, len(dynamic_setters(raw)))
                    conn.close()

    def test_other_exact_profile_caps_or_kind_cannot_seed(self):
        with self.guard.enforce():
            for kind, caps in (('content', (9000, 9000)), ('control', (10000, 10000))):
                with self.subTest(kind=kind, caps=caps):
                    raw = fixtures.Raw()
                    profile = self.attest(kind=kind, statement_ms=caps[0], lock_ms=caps[1])
                    conn = driver.connect(self.guard, lambda **_: raw, self.kwargs(),
                                          kind=kind, statement_ms=caps[0], lock_ms=caps[1], profile=profile)
                    self.assertIs(conn._reuse_profile, profile)
                    _PsycopgConnection(conn).begin(read_only=True)
                    self.assertIsNone(conn._installed)
                    conn.execute(ROLE_SQL)
                    self.assertEqual(3, len(dynamic_setters(raw)))
                    conn.close()

    def test_unknown_sql_or_nonempty_or_nonbuiltin_params_cannot_seed(self):
        class TupleAlias(tuple):
            pass

        class SqlAlias(str):
            pass

        with self.guard.enforce():
            for statement, params in ((STATIC_SQL + ' ', ()), (SqlAlias(STATIC_SQL), ()),
                                      (STATIC_SQL, (1,)), (STATIC_SQL, []), (STATIC_SQL, TupleAlias())):
                with self.subTest(sql_type=type(statement).__name__, params=type(params).__name__):
                    raw = fixtures.Raw()
                    conn = self.open(self.attest(), raw=raw)
                    self.begin(conn)
                    conn.execute(statement, params)
                    self.assertIsNone(conn._installed)
                    conn.execute(ROLE_SQL)
                    self.assertEqual(3, len(dynamic_setters(raw)))
                    conn.close()

    def test_below_cap_before_config_and_crossing_cap_after_config_cannot_seed(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            for before in (9.9, 10.1):
                with self.subTest(before=before):
                    with patch.object(self.guard, 'remaining_seconds', side_effect=(before, 9.9)):
                        conn.execute(STATIC_SQL)
                    self.assertIsNone(conn._installed)
                    self.assertEqual(f'{int(min(before, 10)*1000)}ms', dynamic_setters(self.raw)[-1][1])
                    count = len(dynamic_setters(self.raw))
                    conn.execute(ROLE_SQL)
                    self.assertEqual(count + 1, len(dynamic_setters(self.raw)))

    def test_failed_setter_or_failed_static_sql_leaves_no_stamp(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            self.begin(conn)
            for failure in (driver._SET_TIMEOUTS, STATIC_SQL):
                with self.subTest(failure=hashlib.sha256(failure.encode()).hexdigest()):
                    self.raw.fail_execute = failure
                    with self.assertRaises(RuntimeError):
                        conn.execute(STATIC_SQL)
                    self.assertIsNone(conn._installed)
                    count = len(dynamic_setters(self.raw))
                    conn.execute(ROLE_SQL)
                    self.assertEqual(count + 1, len(dynamic_setters(self.raw)))

    def test_cancel_after_config_sql_before_successful_exit_cannot_seed(self):
        original = fixtures.Cursor.execute
        with self.assertRaisesRegex(FoundationError, 'request_deadline_exceeded'):
            with self.guard.enforce():
                conn = self.open(self.attest())
                self.begin(conn)

                def cancel_after_sql(cursor, statement, params=()):
                    result = original(cursor, statement, params)
                    if statement == STATIC_SQL:
                        self.guard.cancel()
                    return result

                try:
                    with patch.object(fixtures.Cursor, 'execute', cancel_after_sql):
                        conn.execute(STATIC_SQL)
                finally:
                    self.assertIsNone(conn._installed)

    def test_after_static_config_unknown_command_and_commit_keep_original_boundaries(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            _PsycopgConnection(conn).begin(read_only=True)
            self.assertIsNotNone(conn._installed)
            conn.execute('SELECT 9')
            self.assertIsNone(conn._installed)
            conn.execute(ROLE_SQL)
            count = len(dynamic_setters(self.raw))
            conn.commit()
            self.assertIsNone(conn._installed)
            self.assertEqual(count + 1, len(dynamic_setters(self.raw)))

    def test_autocommit_and_rollback_do_not_leave_stamp(self):
        with self.guard.enforce():
            conn = self.open(self.attest())
            with conn.cursor() as cursor:
                cursor.execute(STATIC_SQL)
            self.assertIsNone(conn._installed)
            _PsycopgConnection(conn).begin(read_only=True)
            self.assertIsNotNone(conn._installed)
            conn.rollback()
            self.assertIsNone(conn._installed)
