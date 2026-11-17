"""Driver-port tests, explicitly not live Psycopg or PostgreSQL evidence."""
from contextlib import contextmanager
from types import SimpleNamespace as NS
import sys
import threading
import time
import unittest
from unittest.mock import patch

from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import PsycopgDirectProvider


class Cursor:
    def __init__(self, raw): self.raw = raw; self.description = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def close(self): pass
    def execute(self, sql, params=()):
        self.raw.calls.append((threading.get_ident(), sql, params))
        if sql == 'BLOCK':
            self.raw.started.set()
            if not self.raw.cancelled.wait(.5): raise RuntimeError('test latch timed out')
            raise RuntimeError('private cancelled SQL')
        return self
    def fetchall(self): return ()
    def fetchone(self): return None


class Raw:
    def __init__(self):
        self.autocommit = True; self.closed = False
        self.info = NS(transaction_status=0)
        self.calls = []; self.started = threading.Event(); self.cancelled = threading.Event()
        self.commits = 0; self.rollbacks = 0; self.close_thread = None
        self.commit_error = False; self.cancel_thread = None
    def cursor(self): return Cursor(self)
    def commit(self):
        self.commits += 1
        if self.commit_error: raise RuntimeError('private unknown COMMIT detail')
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True; self.close_thread = threading.get_ident()
    def cancel_safe(self, *, timeout):
        self.cancel_thread = threading.get_ident(); self.cancelled.set()


def fake_driver(capable=True):
    def capability(*, check=False):
        if not capable: raise RuntimeError('unsupported libpq')
        return True
    return NS(Connection=Raw, capabilities=NS(has_cancel_safe=capability))


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.binding = NS(host='127.0.0.1', port=5432, database='synthetic', role='runtime', password_ref='secret')
        self.guard = SyncRequestDeadlineGuard(1)
        self.raw = Raw()
        self.driver_patch = patch.dict(sys.modules, {'psycopg': fake_driver()})
        self.driver_patch.start(); self.addCleanup(self.driver_patch.stop)
    def provider(self, connector=None):
        self.assertIn('deadline_guard', __import__('inspect').signature(PsycopgDirectProvider).parameters,
                      'provider does not register actual resources')
        return PsycopgDirectProvider(connector or (lambda **kw: self.raw), lambda ref:'synthetic-secret', deadline_guard=self.guard)
    def test_guarded_provider_rejects_use_without_request_scope(self):
        called = []
        provider = self.provider(lambda **kw: called.append(kw))
        with self.assertRaises(FoundationError): provider.open(self.binding)
        self.assertEqual([], called)
    def test_late_connect_is_closed_before_any_sql(self):
        self.guard = SyncRequestDeadlineGuard(.04)
        def connect(**kwargs): time.sleep(.07); return self.raw
        with self.assertRaises(FoundationError):
            with self.guard.enforce(): self.provider(connect).open(self.binding)
        self.assertTrue(self.raw.closed); self.assertEqual([], self.raw.calls)
    def test_active_sql_cancelled_and_no_later_commit(self):
        self.guard = SyncRequestDeadlineGuard(.05)
        with self.assertRaises(FoundationError):
            with self.guard.enforce():
                connection = self.provider().open(self.binding)
                try: connection.execute('BLOCK')
                finally: connection.close()
        self.assertEqual(0, self.raw.commits)
        self.assertNotEqual(threading.get_ident(), self.raw.cancel_thread)
        self.assertEqual(threading.get_ident(), self.raw.close_thread)
    def test_cancel_safe_legacy_fallback_is_rejected(self):
        sys.modules['psycopg'] = fake_driver(False)
        with self.assertRaises(FoundationError):
            with self.guard.enforce(): self.provider().open(self.binding)
        self.assertTrue(self.raw.closed)
    def test_pooled_connection_is_closed_and_rejected(self):
        self.raw._pool = object()
        with self.assertRaises(FoundationError):
            with self.guard.enforce(): self.provider().open(self.binding)
        self.assertEqual([], self.raw.calls)
    def test_unknown_commit_error_is_not_reported_as_success(self):
        self.raw.commit_error = True
        with self.guard.enforce():
            connection = self.provider().open(self.binding)
            with self.assertRaises(FoundationError) as caught: connection.commit()
            self.assertEqual('commit_result_unknown',caught.exception.code)
            self.assertNotIn('private',str(caught.exception))
            connection.close()
        self.assertEqual(1, self.raw.commits)
        self.assertTrue(self.raw.closed)
    def test_retired_resource_and_other_thread_cannot_execute(self):
        errors = []
        with self.guard.enforce():
            connection = self.provider().open(self.binding)
            def other():
                try: connection.execute('UNAUTHORIZED')
                except FoundationError: errors.append('refused')
            worker = threading.Thread(target=other); worker.start(); worker.join(.5)
            connection.close()
            with self.assertRaises(FoundationError): connection.execute('LATE')
        self.assertEqual(['refused'], errors)
        self.assertFalse(any(call[1] in ('UNAUTHORIZED', 'LATE') for call in self.raw.calls))

    def test_resource_limit_prevents_opening_an_extra_connection(self):
        opened=[]
        def connect(**kwargs):
            raw=Raw();opened.append(raw);return raw
        with self.guard.enforce():
            provider=self.provider(connect)
            for _ in range(4): provider.open(self.binding)
            with self.assertRaises(FoundationError): provider.open(self.binding)
        self.assertEqual(4,len(opened))
        self.assertTrue(all(raw.closed for raw in opened))

    def test_cancellation_during_timeout_setup_cannot_send_business_sql(self):
        with self.assertRaises(FoundationError):
            with self.guard.enforce():
                connection=self.provider().open(self.binding)
                with patch.object(connection._raw,'_timeouts',side_effect=self.guard.cancel):
                    with self.assertRaises(FoundationError): connection.execute('WRITE')
        self.assertFalse(any(call[1]=='WRITE' for call in self.raw.calls))

    def test_cancellation_during_timeout_setup_cannot_send_commit(self):
        with self.assertRaises(FoundationError):
            with self.guard.enforce():
                connection=self.provider().open(self.binding)
                with patch.object(connection._raw,'_timeouts',side_effect=self.guard.cancel):
                    with self.assertRaises(FoundationError): connection.commit()
        self.assertEqual(0,self.raw.commits)

