"""Focused tests for the narrow per-request control raw reuse (no pool).

Only the ordinary driver/ControlProvider/host boundary is used.  The synthetic
SQL port is the frozen catalog double from tests/library_catalog/test_catalog.py,
extended here with DISCARD ALL, close-error, rollback and cancel gates.  It is
not a PostgreSQL/RLS/TLS emulator; physical handshake counts are connection
constructor calls on the doubled external driver port.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from evolvmem.library_catalog.adapter import (ControlProvider, ControlBinding,
    ControlCatalog, ControlAuthorityLease)
from evolvmem.library_deadline import timeout_reuse
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import Authenticator, AuthorityRouter, FoundationError

SOURCE = Path(os.environ.get('EVOLVMEM_SOURCE_ROOT', Path(__file__).resolve().parents[2])) / 'tests/library_catalog/test_catalog.py'
SPEC = importlib.util.spec_from_file_location('frozen_catalog_port_for_reuse', SOURCE)
FROZEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FROZEN)
DriverError = FROZEN.DriverError

OLD_REUSE_VALIDATION_SQL = (
    "SELECT r.rolcanlogin AS enabled, "
    "(r.rolvaliduntil IS NULL OR r.rolvaliduntil > pg_catalog.now()) AS nonexpired, "
    "(r.rolname = current_user) AS identity_ok "
    "FROM pg_catalog.pg_roles r WHERE r.rolname = session_user")
OLD_ROLE_SQL = "\n            SELECT session_user, current_user, r.rolsuper AS superuser,\n                   r.rolbypassrls AS bypass_rls, r.rolcreaterole AS create_role,\n                   r.rolcreatedb AS create_db,\n                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS memberships,\n                   EXISTS (SELECT 1 FROM pg_catalog.pg_class c\n                       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace\n                       WHERE n.nspname='em_control' AND c.relkind IN ('r','p','v','m','f')\n                       AND pg_catalog.has_table_privilege(r.oid,c.oid,\n                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) AS direct_access,\n                   (EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname='em_control'\n                              AND pg_catalog.pg_has_role(session_user,n.nspowner,'MEMBER'))\n                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c\n                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace\n                        WHERE n.nspname='em_control'\n                          AND pg_catalog.pg_has_role(session_user,c.relowner,'MEMBER'))) AS owns_protected\n            FROM pg_catalog.pg_roles r WHERE r.rolname=session_user\n        "
OLD_ROLE_DIGEST = '1dc0f430e5c05ed5297952240403281a81abe8553bddc7705c2c473138a344f7'
NEW_ROLE_DIGEST = '18713999bf3ddf09e27146955b5b35719796eb4a91a87f38469b1b05d87b43af'
OTHER_CONTROL_DIGESTS = frozenset({
    'fd99fd744176a5ffc1160f67e78dcc6e567a38a8d09649ae17daa861f56dac90',
    'e4d6d4471d8530c13bb6981e58febf18d94e02e8db26e03e755a17614e57bd91',
    'de9fa89ae4782959536f4ed4938cbfb6715e411b4005eb376aa4724bc33b221b',
})


class Status(NS):
    def __int__(self): return {'IDLE': 0, 'INTRANS': 2}[self.name]


class Cursor(FROZEN.Cursor):
    # The combined begin role SELECT, in the exact order selected by the
    # product SQL.  This double changes only the synthetic result; it still
    # records every statement so the tests can prove no extra reuse SELECT.
    ROLE_KEYS = ('session_user', 'current_user', 'superuser', 'bypass_rls',
                 'create_role', 'create_db', 'login_enabled', 'nonexpired',
                 'memberships', 'direct_access', 'owns_protected')

    def execute(self, sql, params=()):
        if sql.strip() == 'DISCARD ALL':
            raw = self.raw
            engine = raw.engine
            engine.calls.append((id(raw), sql, params))
            engine.resets.append(raw.info.transaction_status.name)
            raw.discards += 1
            raw.reset_cursors += 1
            self.is_reset = True
            if engine.reset_error:
                raise DriverError('08006')
            return self
        super().execute(sql, params)
        if 'pg_catalog.pg_roles' in sql:
            self._role_result(sql)
        return self

    def _role_result(self, sql):
        engine = self.raw.engine
        if not self.rows or 'AS login_enabled' not in sql:
            # An unexpected legacy reuse SELECT is intentionally left alone so
            # the "no standalone reuse validation SQL" assertions can see it.
            return
        row = dict(zip((column.name for column in self.description), self.rows[0]))
        if engine.role_current is not None:
            row['current_user'] = engine.role_current
        row['login_enabled'] = engine.role_canlogin
        row['nonexpired'] = engine.role_nonexpired
        mode = engine.role_fields
        if mode == 'legacy':
            row.pop('login_enabled', None)
            row.pop('nonexpired', None)
        elif mode == 'missing_login':
            row.pop('login_enabled', None)
        elif mode == 'missing_expiry':
            row.pop('nonexpired', None)
        elif mode == 'null_login':
            row['login_enabled'] = None
        elif mode == 'null_expiry':
            row['nonexpired'] = None
        else:
            assert mode == 'full', mode
        keys = [key for key in self.ROLE_KEYS if key in row]
        values = tuple(row[key] for key in keys)
        self.description = [NS(name=key) for key in keys]
        self.rows = [values] * engine.role_row_count

    def close(self):
        raw = self.raw
        if raw is not None and getattr(self, 'is_reset', False):
            raw.reset_cursor_closed = True
        if raw is not None and raw.open_cursors:
            raw.open_cursors -= 1
            raw.closed_cursors += 1
        if raw is not None and raw.engine.cursor_close_error:
            raise DriverError('08006')
        super().close()


class Raw(FROZEN.Raw):
    def __init__(self, engine, kwargs):
        super().__init__(engine, kwargs)
        self.info.transaction_status = Status(name='IDLE')
        self.cancels = 0
        self.discards = 0
        self.reset_cursors = 0
        self.reset_cursor_closed = False
        self.closed_cursors = 0
        self.open_cursors = 0
        # Proves the opt-in arm disarms client-side prepared statements.
        self.prepare_threshold = 99
        self.close_error = False
        self.closed_during_cancel = None

    def cursor(self):
        self.open_cursors += 1
        return Cursor(self)

    def cancel_safe(self, *, timeout):
        self.cancels += 1
        engine = self.engine
        if engine.cancel_gate is not None:
            engine.cancel_active = True
            engine.cancel_gate.wait(2)
            engine.cancel_active = False

    def rollback(self):
        if self.engine.rollback_error:
            raise DriverError('08006')
        super().rollback()
        if self.engine.rollback_leaves_intrans:
            self.info.transaction_status.name = 'INTRANS'

    def close(self):
        if self.close_error:
            raise DriverError('08006')
        self.closed_during_cancel = self.engine.cancel_active
        super().close()


class Engine(FROZEN.Engine):
    def __init__(self):
        super().__init__()
        self.resets = []
        self.reset_error = False
        self.cursor_close_error = False
        self.rollback_error = False
        self.rollback_leaves_intrans = False
        self.cancel_gate = None
        self.cancel_active = False
        # Synthetic combined role result controls (still exactly one row/type
        # per call unless a fail-closed case explicitly changes it).
        self.role_canlogin = True
        self.role_nonexpired = True
        self.role_fields = 'full'
        self.role_row_count = 1
        self.role_current = None

    def connect(self, **kwargs):
        raw = Raw(self, kwargs)
        self.connections.append(raw)
        return raw

    def begins(self):
        return sum(1 for _, sql, _ in self.calls if sql.startswith('BEGIN'))


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.guard = SyncRequestDeadlineGuard(5)
        self.engine = Engine()
        patcher = patch.dict(sys.modules, {'psycopg': NS(
            Connection=Raw, capabilities=NS(has_cancel_safe=lambda **kwargs: True))})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.binding = ControlBinding('127.0.0.1', 5432, 'synthetic',
                                      'catalog_reader', 'secret')

    def provider(self, *, reuse=True, secrets=None):
        box = {'value': 'synthetic'}
        secrets = box if secrets is None else secrets
        provider = ControlProvider(self.binding, self.engine.connect,
                                   lambda ref: secrets['value'],
                                   deadline_guard=self.guard,
                                   control_request_reuse=reuse)
        return provider

    def recording(self, provider, sink):
        original = provider.open

        @contextmanager
        def wrapper(**kwargs):
            with original(**kwargs) as session:
                sink.append(session)
                yield session

        provider.open = wrapper

    def three_opens(self, provider, sink):
        """authenticate + resolve + pin: the admitted-request control path."""
        self.recording(provider, sink)
        catalog = ControlCatalog(provider)
        identity = Authenticator(catalog).authenticate('synthetic-token')
        router = AuthorityRouter(catalog, implemented_tools={'postgres': {'context_read'}})
        route = router.plan(identity, 'context_read', library_id=FROZEN.L)
        lease = ControlAuthorityLease(catalog, deadline_guard=self.guard)
        with lease.hold(route):
            pass
        return catalog, route

    def run_scope(self, fn, **provider_kwargs):
        provider = None
        with self.guard.enforce():
            provider = self.provider(**provider_kwargs)
            try:
                return fn(provider)
            finally:
                provider.close_reuse()

    @staticmethod
    def role_sqls(engine):
        return [sql for _, sql, _ in engine.calls if 'pg_catalog.pg_roles' in sql]

    def open_once(self, provider):
        with provider.open() as session:
            session.execute('SELECT 1 AS alive')
        return session

    def reset_role_result(self):
        self.engine.role_canlogin = True
        self.engine.role_nonexpired = True
        self.engine.role_fields = 'full'
        self.engine.role_row_count = 1
        self.engine.role_current = None

    # 1 -------------------------------------------------------------------
    def test_default_off_keeps_three_physical_connections(self):
        sink = []
        with self.guard.enforce():
            provider = self.provider(reuse=False)
            self.three_opens(provider, sink)
            provider.close_reuse()
        self.assertEqual(3, len(self.engine.connections))
        self.assertEqual(3, len(sink))
        self.assertEqual(3, self.engine.begins())
        self.assertTrue(all(raw.closed for raw in self.engine.connections))
        # default-off never touches prepare_threshold
        self.assertTrue(all(raw.prepare_threshold == 99 for raw in self.engine.connections))

    def test_enabled_one_physical_three_fresh_sessions(self):
        sink = []
        with self.guard.enforce():
            provider = self.provider()
            self.three_opens(provider, sink)
            provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections), 'one physical connect per request')
        self.assertEqual(3, self.engine.connections[0].discards,
                         'one DISCARD ALL per successful borrow')
        self.assertEqual(3, len(sink))
        self.assertEqual(3, len({id(session) for session in sink}))
        self.assertEqual(3, len({id(session.raw) for session in sink}), 'new wrapper per borrow')
        self.assertEqual(3, self.engine.begins(), 'new BEGIN per borrow')
        self.assertEqual(['IDLE'] * 3, self.engine.resets)
        # role verification is re-executed, never cached
        self.assertEqual(3, sum(1 for _, sql, _ in self.engine.calls
                                if 'pg_catalog.pg_roles' in sql))
        self.assertEqual(1, sum(1 for _, sql, _ in self.engine.calls
                                if 'pin_library' in sql))
        raw = self.engine.connections[0]
        self.assertIsNone(raw.prepare_threshold)
        self.assertTrue(raw.closed, 'request finally physically closes the idle raw')

    def test_each_segment_executes_one_fresh_combined_role_query(self):
        with self.guard.enforce():
            provider = self.provider()
            try:
                for _ in range(3):
                    self.open_once(provider)
            finally:
                provider.close_reuse()
        chunks = []
        for _, sql, _ in self.engine.calls:
            if sql.startswith('BEGIN'):
                chunks.append([])
            if chunks:
                chunks[-1].append(sql)
        self.assertEqual(3, len(chunks), 'one BEGIN per segment')
        for chunk in chunks:
            role = [sql for sql in chunk if 'pg_catalog.pg_roles' in sql]
            self.assertEqual(1, len(role), 'one role query per segment, never two')
            self.assertIn('r.rolcanlogin AS login_enabled', role[0])
            self.assertIn('r.rolvaliduntil', role[0])
            self.assertIn('current_user', role[0])
            self.assertNotIn('AS identity_ok', role[0])
        self.assertEqual(1, len(self.engine.connections))

    def test_no_standalone_reuse_validation_sql_is_executed(self):
        self.assertFalse(hasattr(ControlProvider, '_REUSE_VALIDATION_SQL'))
        self.assertFalse(hasattr(ControlProvider, '_validate_reused'))
        with self.guard.enforce():
            provider = self.provider()
            try:
                for _ in range(3):
                    self.open_once(provider)
            finally:
                provider.close_reuse()
        executed = [sql for _, sql, _ in self.engine.calls]
        self.assertNotIn(OLD_REUSE_VALIDATION_SQL, executed)
        self.assertFalse(any('AS identity_ok' in sql for sql in executed))
        self.assertFalse(any('AS enabled' in sql for sql in executed))
        self.assertEqual(3, len(self.role_sqls(self.engine)))

    def test_reused_combined_role_state_denials(self):
        cases = (
            ('disabled', {'role_canlogin': False}),
            ('expired', {'role_nonexpired': False}),
            ('changed_current_role', {'role_current': 'other_reader'}),
            ('missing_login', {'role_fields': 'missing_login'}),
            ('missing_expiry', {'role_fields': 'missing_expiry'}),
            ('null_login', {'role_fields': 'null_login'}),
            ('null_expiry', {'role_fields': 'null_expiry'}),
            ('legacy_missing_both', {'role_fields': 'legacy'}),
            ('zero_rows', {'role_row_count': 0}),
            ('two_rows', {'role_row_count': 2}),
        )
        for name, change in cases:
            with self.subTest(case=name):
                self.engine = Engine()
                self.guard = SyncRequestDeadlineGuard(5)
                with self.guard.enforce():
                    provider = self.provider()
                    try:
                        self.open_once(provider)
                        raw = self.engine.connections[0]
                        self.assertFalse(raw.closed)
                        for key, value in change.items():
                            setattr(self.engine, key, value)
                        with self.assertRaises(FoundationError) as caught:
                            self.open_once(provider)
                        self.assertEqual('unsafe_control_role', caught.exception.code)
                        self.assertEqual(1, len(self.engine.connections),
                                         'denial happened on the reused physical raw')
                        self.assertTrue(raw.closed, 'a denied reused raw is never recycled')
                        self.assertEqual(1, raw.discards,
                                         'only the successful first borrow reset')
                        self.reset_role_result()
                        self.open_once(provider)
                        self.assertEqual(2, len(self.engine.connections),
                                         'the denied raw was physically discarded')
                        self.assertTrue(raw.closed)
                    finally:
                        provider.close_reuse()

    def test_defaultoff_frozen_legacy_role_row_is_accepted(self):
        # The frozen catalog double returns the previous role row, which has
        # no login_enabled/nonexpired columns.  Default-off must not reject it;
        # only the reused path requires the added optional columns.
        frozen = FROZEN.Engine()
        with patch.object(sys.modules['psycopg'], 'Connection', FROZEN.Raw):
            with self.guard.enforce():
                provider = ControlProvider(self.binding, frozen.connect,
                                           lambda ref: 'synthetic',
                                           deadline_guard=self.guard,
                                           control_request_reuse=False)
                try:
                    self.three_opens(provider, [])
                finally:
                    provider.close_reuse()
        self.assertEqual(3, len(frozen.connections))
        self.assertEqual(3, len(self.role_sqls(frozen)))
        self.assertTrue(all(raw.closed for raw in frozen.connections))

    def test_exception_cleanup_unchanged_with_fused_role_check(self):
        with self.guard.enforce():
            provider = self.provider()
            try:
                self.open_once(provider)
                raw = self.engine.connections[0]
                with self.assertRaises(RuntimeError):
                    with provider.open() as session:
                        session.execute('SELECT 1 AS alive')
                        raise RuntimeError('synthetic body failure')
                self.assertTrue(raw.closed)
                self.assertEqual(2, raw.rollbacks, 'rollback still runs for both borrows')
                self.assertEqual(1, raw.discards, 'a failing borrow is never recycled')
                self.assertEqual(2, len(self.role_sqls(self.engine)))
            finally:
                provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections))

    def test_exact_combined_role_digest_and_old_digest_not_approved(self):
        with self.guard.enforce():
            provider = self.provider()
            try:
                self.open_once(provider)
            finally:
                provider.close_reuse()
        sql = self.role_sqls(self.engine)[0]
        self.assertEqual(NEW_ROLE_DIGEST, hashlib.sha256(sql.encode('utf-8')).hexdigest())
        self.assertTrue(timeout_reuse.approved_sql('control', sql))
        self.assertIn(NEW_ROLE_DIGEST, timeout_reuse.APPROVED_SQL_SHA256['control'])
        self.assertEqual(OTHER_CONTROL_DIGESTS,
                         timeout_reuse.APPROVED_SQL_SHA256['control'] - {NEW_ROLE_DIGEST})
        self.assertEqual(OLD_ROLE_DIGEST,
                         hashlib.sha256(OLD_ROLE_SQL.encode('utf-8')).hexdigest())
        self.assertFalse(timeout_reuse.approved_sql('control', OLD_ROLE_SQL))
        self.assertNotIn(OLD_ROLE_DIGEST, timeout_reuse.APPROVED_SQL_SHA256['control'])
        self.assertEqual(4, len(timeout_reuse.APPROVED_SQL_SHA256['control']))
        self.assertEqual(34, len(timeout_reuse.APPROVED_SQL_SHA256['content']))
        self.assertEqual(14, len(timeout_reuse.NATIVE_BACKGROUND_SQL_SHA256))
        self.assertEqual(38, len(set().union(*timeout_reuse.APPROVED_SQL_SHA256.values())))

    def test_separate_request_never_reuses_physical_raw(self):
        sink = []
        with self.guard.enforce():
            provider = self.provider()
            self.three_opens(provider, sink)
            provider.close_reuse()
        first = self.engine.connections[0]
        second_guard = SyncRequestDeadlineGuard(5)
        self.guard = second_guard
        with self.guard.enforce():
            provider = self.provider()
            try:
                self.three_opens(provider, sink)
            finally:
                provider.close_reuse()
        self.assertEqual(2, len(self.engine.connections))
        self.assertTrue(first.closed)
        self.assertNotIn(self.engine.connections[0], self.engine.connections[1:])

    def test_default_provider_and_coordinator_are_excluded(self):
        # The ACL coordinator / content providers are built without the
        # keyword: the default must stay off even with a concrete guard.
        sink = []
        with self.guard.enforce():
            provider = self.provider(reuse=False)
            try:
                self.three_opens(provider, sink)
            finally:
                provider.close_reuse()
        self.assertEqual(3, len(self.engine.connections))
        with self.assertRaises(ValueError):
            ControlProvider(self.binding, self.engine.connect, lambda ref: 'synthetic',
                            control_request_reuse=True)
        with self.assertRaises(ValueError):
            ControlProvider(self.binding, self.engine.connect, lambda ref: 'synthetic',
                            deadline_guard=self.guard, control_request_reuse=1)

    # 2 -------------------------------------------------------------------
    def test_timeout_profile_path_unchanged_on_reborrow(self):
        sink = []
        wrappers = []
        with self.guard.enforce():
            provider = self.provider()
            self.recording(provider, sink)
            catalog = ControlCatalog(provider)
            identity = Authenticator(catalog).authenticate('synthetic-token')
            for _ in range(2):
                with provider.open() as session:
                    wrappers.append(session.raw)
                    session.execute('SELECT 1 AS alive')
                    self.assertGreater(session.raw.timeout_setter_calls, 0,
                                       'ordinary setter path on every new wrapper')
                    self.assertIsNone(session.raw._installed,
                                      'no timeout record is transferred')
                    self.assertIsNone(session.raw._reuse_profile,
                                      'default-off profile path is untouched')
            provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections))
        self.assertEqual(2, len(wrappers))
        self.assertIsNot(wrappers[0], wrappers[1])
        self.assertEqual(wrappers[0].timeout_setter_calls,
                         wrappers[1].timeout_setter_calls,
                         'same original timeout setter count per borrow')

    # 3 -------------------------------------------------------------------
    def test_caught_sql_exception_never_recycles(self):
        sink = []
        with self.guard.enforce():
            provider = self.provider()
            first = self.engine.connect
            with provider.open() as session:
                self.engine.fail_function = 'SELECT 1 AS alive'
                with self.assertRaises(FoundationError):
                    session.execute('SELECT 1 AS alive')
                self.engine.fail_function = None
            self.assertTrue(self.engine.connections[0].closed)
            # a later normal open must connect new, never reuse the failed raw
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            provider.close_reuse()
        self.assertEqual(2, len(self.engine.connections))

    def test_caught_fetch_and_cursor_close_exception_never_recycles(self):
        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                self.engine.cursor_close_error = True
                with self.assertRaises(DriverError):
                    with session.raw.cursor() as cursor:
                        cursor.execute('SELECT 1 AS alive')
                self.engine.cursor_close_error = False
            self.assertTrue(self.engine.connections[0].closed)
            provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections))

    # 4 -------------------------------------------------------------------
    def test_reset_cursor_closed_and_idle_verified(self):
        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            provider.close_reuse()
        raw = self.engine.connections[0]
        self.assertEqual(1, raw.discards)
        self.assertEqual(1, raw.reset_cursors)
        self.assertTrue(raw.reset_cursor_closed, 'reset cursor explicitly closed')
        self.assertEqual(['IDLE'], self.engine.resets)

    def test_reset_error_discards_physical_raw(self):
        with self.guard.enforce():
            provider = self.provider()
            with self.assertRaises(FoundationError) as caught:
                with provider.open() as session:
                    session.execute('SELECT 1 AS alive')
                    self.engine.reset_error = True
            self.assertEqual('control_cleanup_failed', caught.exception.code)
            self.assertTrue(self.engine.connections[0].closed)
            self.engine.reset_error = False
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            provider.close_reuse()
        self.assertEqual(2, len(self.engine.connections))

    def test_non_idle_after_rollback_discards_physical_raw(self):
        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
                self.engine.rollback_leaves_intrans = True
            raw = self.engine.connections[0]
            self.assertTrue(raw.closed)
            self.engine.rollback_leaves_intrans = False
            provider.close_reuse()

    def test_rollback_error_is_cleanup_failure_and_discards(self):
        with self.guard.enforce():
            provider = self.provider()
            with self.assertRaises(FoundationError) as caught:
                with provider.open() as session:
                    session.execute('SELECT 1 AS alive')
                    self.engine.rollback_error = True
            self.assertEqual('control_cleanup_failed', caught.exception.code)
            self.assertTrue(self.engine.connections[0].closed)
            provider.close_reuse()

    # 5 -------------------------------------------------------------------
    def test_direct_cancel_overlapping_detach_never_recycles(self):
        gate = threading.Event()
        self.engine.cancel_gate = gate
        raw = None

        class Probe(threading.Thread):
            def run(inner):
                wrapper.cancel(.25)

        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                wrapper = session.raw
                raw = wrapper._raw
                thread = Probe()
                thread.start()
                # let cancel_safe start and hold the wrapper cancel lock
                for _ in range(200):
                    if self.engine.cancel_active:
                        break
                    time.sleep(.005)
                self.assertTrue(self.engine.cancel_active)
            gate.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(raw.closed)
            self.assertTrue(raw.closed_during_cancel is False,
                            'raw close waited for the in-flight cancel_safe')
            provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections))

    def test_stale_old_wrapper_cancel_is_noop_after_handoff(self):
        sink = []
        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                stale = session.raw
            raw = self.engine.connections[0]
            before = raw.cancels
            stale.cancel(.25)  # retired wrapper: must never touch the stored raw
            self.assertEqual(before, raw.cancels)
            self.assertFalse(raw.closed)
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            provider.close_reuse()
        self.assertEqual(1, len(self.engine.connections))

    def test_late_cancel_after_detach_closes_physical_and_blocks_next_borrow(self):
        with self.assertRaises(FoundationError):
            with self.guard.enforce():
                provider = self.provider()
                with provider.open() as session:
                    session.execute('SELECT 1 AS alive')
                # cancel after the idle raw was detached: never reused
                self.guard.cancel()
                provider.close_reuse()
        self.assertTrue(self.engine.connections[0].closed)
        self.assertEqual(1, len(self.engine.connections))

    # 6 -------------------------------------------------------------------
    def test_base_exception_closes_raw(self):
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc_type.__name__):
                self.engine = Engine()
                self.guard = SyncRequestDeadlineGuard(5)
                with self.assertRaises(exc_type):
                    with self.guard.enforce():
                        provider = self.provider()
                        try:
                            with provider.open() as session:
                                session.execute('SELECT 1 AS alive')
                                raise exc_type()
                        finally:
                            provider.close_reuse()
                self.assertTrue(self.engine.connections[0].closed)
                self.assertEqual(0, self.engine.connections[0].discards,
                                 'a failing body is never recycled')

    def test_cleanup_failure_poisons_guard(self):
        with self.guard.enforce():
            provider = self.provider()
            with self.assertRaises(FoundationError):
                with provider.open() as session:
                    session.execute('SELECT 1 AS alive')
                    raw = session.raw._raw
                    self.engine.reset_error = True
                    raw.close_error = True
            self.assertTrue(self.guard._unhealthy.is_set())
            provider.close_reuse()
        self.assertTrue(raw.closed is False)

    # 7 -------------------------------------------------------------------
    def test_changed_secret_before_later_open_connects_new(self):
        secrets = {'value': 'synthetic'}
        with self.guard.enforce():
            provider = self.provider(secrets=secrets)
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            first = self.engine.connections[0]
            self.assertFalse(first.closed)
            secrets['value'] = 'rotated'
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
                self.assertIsNot(first, session.raw._raw)
            self.assertTrue(first.closed, 'old identity/target raw is discarded')
            provider.close_reuse()
        self.assertEqual(2, len(self.engine.connections))

    def test_other_thread_never_checkouts_idle_raw(self):
        with self.guard.enforce():
            provider = self.provider()
            with provider.open() as session:
                session.execute('SELECT 1 AS alive')
            idle = self.engine.connections[0]
            errors = []

            def other():
                try:
                    with provider.open() as session:
                        session.execute('SELECT 1 AS alive')
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], FoundationError)
            self.assertFalse(idle.closed)
            self.assertEqual(1, len(self.engine.connections))
            provider.close_reuse()
        self.assertTrue(idle.closed)


if __name__ == '__main__':
    unittest.main()
