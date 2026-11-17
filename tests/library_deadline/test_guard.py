import socket
import threading
import time
import unittest
from unittest.mock import patch

from evolvmem.library_foundation import FoundationError
try:
    from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
except ModuleNotFoundError:
    SyncRequestDeadlineGuard = None


class CancelTarget:
    kind = 'content'
    protected = False
    retired = False

    def __init__(self):
        self.cancelled = threading.Event()
        self.cancel_thread = None
        self.retire_thread = None

    def cancel(self, timeout):
        self.cancel_thread = threading.get_ident()
        self.cancelled.set()

    def retire(self):
        self.retire_thread = threading.get_ident()
        self.retired = True


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SyncRequestDeadlineGuard, 'concrete guard missing')

    def test_nested_scope_does_not_restart_deadline(self):
        guard = SyncRequestDeadlineGuard(.06)
        with self.assertRaises(FoundationError):
            with guard.enforce():
                time.sleep(.04)
                with guard.enforce():
                    time.sleep(.04)
                    guard.check()

    def test_watchdog_cancels_but_cleanup_stays_on_request_thread(self):
        guard = SyncRequestDeadlineGuard(.04)
        target = CancelTarget()
        owner = threading.get_ident()
        with self.assertRaises(FoundationError):
            with guard.enforce():
                guard._register(target)
                with guard.operation(target):
                    self.assertTrue(target.cancelled.wait(.5))
                self.fail('late success must not advance')
        self.assertNotEqual(owner, target.cancel_thread)
        self.assertEqual(owner, target.retire_thread)

    def test_deadline_shuts_down_real_socket(self):
        guard = SyncRequestDeadlineGuard(.04)
        sender, peer = socket.socketpair()
        try:
            peer.settimeout(.5)  # a failed implementation cannot hang this test
            with self.assertRaises(FoundationError):
                with guard.enforce():
                    guard.bind_socket(sender)
                    with guard.operation(kind='output'):
                        self.assertEqual(b'', peer.recv(1))
        finally:
            sender.close()
            peer.close()

    def test_cancelled_scope_cannot_start_commit(self):
        guard = SyncRequestDeadlineGuard(1)
        effects = []
        with self.assertRaises(FoundationError):
            with guard.enforce():
                guard.cancel()
                with self.assertRaises(FoundationError):
                    with guard.operation(kind='commit'):
                        effects.append('commit')
        self.assertEqual([], effects)

    def test_pinned_control_is_not_cancelled_before_content_cleanup(self):
        guard = SyncRequestDeadlineGuard(.04)
        content, control = CancelTarget(), CancelTarget()
        control.kind, control.protected = 'control', True
        with self.assertRaises(FoundationError):
            with guard.enforce():
                guard._register(content)
                guard._register(control)
                with guard.operation(content):
                    self.assertTrue(content.cancelled.wait(.5))
        self.assertFalse(control.cancelled.is_set())
        self.assertIsNone(control.retire_thread)

    def test_shared_guard_keeps_concurrent_requests_separate(self):
        guard = SyncRequestDeadlineGuard(.09)
        barrier = threading.Barrier(2)
        outcomes = []
        def request(slow):
            target = CancelTarget()
            try:
                with guard.enforce():
                    guard._register(target)
                    barrier.wait(.5)
                    if slow:
                        with guard.operation(target):
                            target.cancelled.wait(.5)
                    else:
                        guard.check()
                    outcomes.append(('ok', slow))
            except FoundationError:
                outcomes.append(('cancelled', slow))
        threads = [threading.Thread(target=request, args=(slow,)) for slow in (False, True)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual([('ok', False), ('cancelled', True)], outcomes)

    def test_successful_quiesce_prevents_later_business_and_output(self):
        guard = SyncRequestDeadlineGuard(1)
        with guard.enforce():
            guard.quiesce(cancel=False)
            for kind in ('content', 'commit', 'output'):
                with self.assertRaises(FoundationError):
                    with guard.operation(kind=kind): pass

    def test_cancellation_between_precheck_and_operation_never_enters_body(self):
        guard = SyncRequestDeadlineGuard(1)
        entered = []
        with self.assertRaises(FoundationError):
            with guard.enforce():
                original = guard.check
                def checked_then_cancelled():
                    original()
                    guard.cancel()
                with patch.object(guard, 'check', side_effect=checked_then_cancelled):
                    with guard.operation(kind='commit'):
                        entered.append(True)
        self.assertEqual([], entered)

    def test_cancel_worker_fault_does_not_replace_unknown_commit(self):
        guard = SyncRequestDeadlineGuard(1)
        guard.CANCEL_SECONDS = .005
        guard.MAX_RESOURCES = 1
        release = threading.Event()
        target = CancelTarget()
        target.cancel = lambda timeout: release.wait(2)
        try:
            with self.assertRaises(FoundationError) as caught:
                with guard.enforce():
                    guard._register(target)
                    raise FoundationError('commit_result_unknown')
            self.assertEqual('commit_result_unknown', caught.exception.code)
            with self.assertRaises(FoundationError) as unhealthy:
                with guard.enforce(): pass
            self.assertEqual('deadline_cleanup_incomplete', unhealthy.exception.code)
        finally:
            release.set()

    def test_segment_rearm_refuses_residual_control_resource(self):
        guard=SyncRequestDeadlineGuard(1)
        self.assertTrue(callable(getattr(guard,'begin_authority',None)))
        with guard.enforce():
            control=CancelTarget();control.kind='control';control.protected=True
            guard._register(control)
            guard.quiesce(cancel=False)
            with self.assertRaises(FoundationError): guard.begin_authority()

    def test_segment_rearm_refuses_after_output_has_started(self):
        guard=SyncRequestDeadlineGuard(1)
        self.assertTrue(callable(getattr(guard,'begin_authority',None)))
        with guard.enforce():
            with guard.operation(kind='output'): pass
            guard.quiesce(cancel=False)
            with self.assertRaises(FoundationError): guard.begin_authority()

    def test_segment_rearm_cannot_revive_cancelled_request(self):
        guard=SyncRequestDeadlineGuard(1)
        self.assertTrue(callable(getattr(guard,'begin_authority',None)))
        with self.assertRaises(FoundationError):
            with guard.enforce():
                guard.cancel()
                with self.assertRaises(FoundationError): guard.begin_authority()

