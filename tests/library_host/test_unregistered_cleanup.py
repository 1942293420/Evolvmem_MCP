"""Actual Host/guard/driver; explicit external raw-connection close ports."""
import unittest
from tests.library_host.support import Port, TOKEN, platform_shim

platform_shim()
from evolvmem.library_deadline.driver import connect
from evolvmem.library_foundation import FoundationError


class UnregisteredCleanupTests(unittest.TestCase):
    def setUp(self):
        self.port = Port()
        self.addCleanup(self.port.close)
        patcher = self.port.driver_patch()
        patcher.start()
        self.addCleanup(patcher.stop)

    def late_return(self, *, close_fails, kind):
        host = self.port.options(enabled=True)
        self.addCleanup(host.close)
        closes = []
        with self.assertRaisesRegex(FoundationError, '^request_deadline_exceeded$'):
            with host.request_scope() as guard:
                def connector(**kwargs):
                    raw = self.port.connect(**kwargs)
                    original = raw.close
                    def close():
                        closes.append(True)
                        if close_fails:
                            raise OSError('synthetic private close failure')
                        original()
                    raw.close = close
                    guard.cancel()  # connector returned; wrapper not registered
                    return raw
                try:
                    connect(guard, connector, {'dbname': 'control', 'user': 'reader'},
                            kind=kind, statement_ms=1000, lock_ms=500)
                except FoundationError as exc:
                    # The original primary must survive cleanup, not be replaced
                    # by the raw close exception or a new cleanup error.
                    self.assertEqual('request_deadline_exceeded', exc.code)
                    self.assertEqual([], guard._state().resources)
                    raise
        self.assertEqual([True], closes)
        self.assertEqual(not close_fails, self.port.connections[-1].closed)
        self.assertEqual(0, host.active_requests)
        if close_fails:
            count = len(self.port.connections)
            with self.assertRaisesRegex(FoundationError, '^library_host_cleanup_incomplete$'):
                with host.request_scope():
                    self.fail('unconfirmed raw close admitted another request')
            self.assertEqual(count, len(self.port.connections))
        else:
            with host.request_scope():
                self.assertIsNotNone(host.authenticate(TOKEN))
            self.assertTrue(all(raw.closed for raw in self.port.connections))

    def test_late_raw_close_failure_poison_survives_unregistered_resource(self):
        for kind in ('control', 'content'):
            with self.subTest(kind=kind):
                self.late_return(close_fails=True, kind=kind)

    def test_late_raw_successful_close_allows_a_new_request(self):
        for kind in ('control', 'content'):
            with self.subTest(kind=kind):
                self.late_return(close_fails=False, kind=kind)

    def test_unsupported_driver_primary_is_preserved_when_raw_close_fails(self):
        host = self.port.options(enabled=True)
        self.addCleanup(host.close)
        closes = []
        with self.assertRaisesRegex(FoundationError, '^deadline_driver_unsupported$'):
            with host.request_scope() as guard:
                def connector(**kwargs):
                    raw = self.port.connect(**kwargs)
                    raw.autocommit = False
                    def close():
                        closes.append(True)
                        raise OSError('synthetic private driver close failure')
                    raw.close = close
                    return raw
                connect(guard, connector, {'dbname': 'control', 'user': 'reader'},
                        kind='control', statement_ms=1000, lock_ms=500)
        self.assertEqual([True], closes)
        self.assertFalse(self.port.connections[-1].closed)
        with self.assertRaisesRegex(FoundationError, '^library_host_cleanup_incomplete$'):
            with host.request_scope():
                self.fail('failed driver admission poisoned no host state')


if __name__ == '__main__':
    unittest.main()
