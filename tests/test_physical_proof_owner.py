"""Pure physical-state contracts; scanner substitutes contain no SQL/model."""
import threading
import unittest

from evolvmem.physical_integrity import PhysicalProofOwner


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.identity = ('synthetic-path', 1, 2, 3)
        self.contract = 'fixed-config'
        self.refreshes = []
        self.owner = PhysicalProofOwner(self.identity, self.contract,
            refresh=lambda: self.refreshes.append(True), clock=lambda: self.clock[0])

    def view(self, ttl=300):
        return self.owner.observe(self.identity, self.contract, ttl)

    def check(self, scanner=lambda: (), ttl=300, config=None):
        return self.owner.check(self.identity, self.contract, ttl, scanner,
                                current_config=config or (lambda: self.contract))

    def test_no_proof_until_actual_completion_and_cache_hit_does_not_renew(self):
        self.assertEqual(self.view().state, 'pending')
        def slow():
            self.assertEqual(self.view().state, 'pending')
            self.clock[0] += 170
            return ()
        self.assertEqual(self.check(slow), ((), 270))
        self.clock[0] += 40
        self.assertEqual(self.check(lambda: self.fail('unexpected scan')), ((), 270))
        self.assertEqual(self.view().checked_at, 270)

    def test_hard_ttl_clock_reversal_and_disabled_cache_reject(self):
        self.check()
        self.clock[0] = 399.9
        self.assertEqual(self.view().state, 'ok')
        self.clock[0] = 400
        self.assertEqual(self.view().state, 'expired')
        self.clock[0] = 99
        self.assertEqual(self.view().state, 'expired')
        self.clock[0] = 100
        self.assertEqual(self.view(0).state, 'expired')

    def test_failure_immediately_revokes_live_success_without_semantic_work(self):
        self.check()
        self.clock[0] = 281
        before = self.owner.token(self.identity, self.contract, 300)
        self.assertEqual(self.view().state, 'ok')
        self.assertEqual(self.check(lambda: ('quick_check_failed',)), (('quick_check_failed',), 281))
        self.assertEqual(self.view().state, 'failed')
        self.assertNotEqual(self.owner.token(self.identity, self.contract, 300), before)

    def test_foreground_request_is_only_a_coalescible_notification(self):
        self.owner.request_refresh()
        self.assertEqual(self.refreshes, [True])
        self.assertEqual(self.view().state, 'pending')

    def test_predecessor_must_actually_exit_before_new_scan(self):
        entered, release = threading.Event(), threading.Event()
        def previous():
            entered.set()
            release.wait(2)
        thread = threading.Thread(target=previous)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.owner.bind_predecessor(thread)
            with self.assertRaisesRegex(RuntimeError, 'physical_handoff_pending'):
                self.check(lambda: self.fail('overlapping scanner'))
            self.assertEqual(self.view().state, 'pending')
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.check()[0], ())

    def test_concurrent_check_never_opens_second_scanner(self):
        def scanner():
            with self.assertRaisesRegex(RuntimeError, 'physical_scan_running'):
                self.check(lambda: self.fail('second scanner'))
            return ()
        self.check(scanner)

    def test_close_during_scan_discards_late_success_and_never_refreshes(self):
        def scanner():
            self.owner.close()
            return ()
        with self.assertRaisesRegex(RuntimeError, 'physical_owner_closed'):
            self.check(scanner)
        self.assertEqual(self.view().state, 'closed')
        self.owner.request_refresh()
        self.assertEqual(self.refreshes, [])

    def test_config_change_during_scan_cannot_publish(self):
        with self.assertRaisesRegex(RuntimeError, 'physical_boundary_changed'):
            self.check(config=lambda: 'changed')
        self.assertEqual(self.view().state, 'boundary')

    def test_identity_or_config_drift_invalidates_even_if_later_restored(self):
        for identity, config in ((('synthetic-path', 1, 99, 3), self.contract),
                                 (self.identity, 'changed-config')):
            with self.subTest(identity=identity, config=config):
                self.setUp()
                self.check()
                self.assertEqual(self.owner.observe(identity, config, 300).state, 'boundary')
                self.assertEqual(self.view().state, 'boundary')

    def test_scanner_exception_is_fixed_failure_not_exception_text(self):
        def scanner():
            raise RuntimeError('PRIVATE CONTENT /private/path')
        self.assertEqual(self.check(scanner)[0], ('quick_check_failed',))
        self.assertEqual(self.view().state, 'failed')

    def test_refresh_at_existing_sixty_percent_and_natural_expiry_changes_token(self):
        self.check()
        self.clock[0] = 279
        self.assertFalse(self.view().needs_refresh)
        self.clock[0] = 280
        self.assertTrue(self.view().needs_refresh)
        before = self.owner.token(self.identity, self.contract, 300)
        self.clock[0] = 400
        self.assertNotEqual(self.owner.token(self.identity, self.contract, 300), before)


if __name__ == '__main__':
    unittest.main()
