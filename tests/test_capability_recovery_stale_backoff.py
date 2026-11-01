"""Deterministic scheduling tests; no model, file index, or production database."""
import copy
from types import SimpleNamespace
import unittest

from evolvmem.capability_recovery import CapabilityVectorRecoveryLoop


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Index:
    def __init__(self, report, clock=None):
        self.report = report
        self.clock = clock
        self.calls = []
        self.closed = 0
        self.vector_index = SimpleNamespace(close=self.close)

    def close(self):
        self.closed += 1

    def reconcile(self, *, limit):
        self.calls.append(limit)
        if self.clock is not None:
            self.clock.now += 7.0
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def stale_report():
    return dict(status="partial", stale=1, processed=32, updated=30,
                remaining=None, dirty=True,
                errors=[{"reason": "eligibility_snapshot_changed"}])


class RecoveryStaleBackoffTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = CapabilityVectorRecoveryLoop(
            clock=self.clock, batch_size=16, index_factory=lambda index: index)

    def tick(self, *indices):
        self.clock.now = max(self.clock.now, self.loop.next_attempt_at)
        reports = self.loop.tick(indices)
        return self.loop.next_attempt_at - self.clock.now, reports

    def test_consecutive_stale_grows_and_caps_without_changing_reports(self):
        index = Index(stale_report())
        before = copy.deepcopy(index.report)
        for delay in (60, 120, 240, 480, 900, 900, 900):
            actual, reports = self.tick(index)
            self.assertEqual(actual, delay)
            self.assertIs(reports[0], index.report)
            self.assertEqual(reports[0], before)
            self.assertEqual(self.loop.failures, 0)
        self.assertEqual(index.calls, [16] * 7)
        self.assertEqual(index.closed, 7)

    def test_no_stale_success_resets_stale_sequence(self):
        index = Index(stale_report())
        for delay in (60, 120, 240):
            self.assertEqual(self.tick(index)[0], delay)
        index.report = dict(status="ready", stale=0, dirty=False, remaining=0)
        self.assertEqual(self.tick(index)[0], 60)
        index.report = stale_report()
        self.assertEqual(self.tick(index)[0], 60)
        self.assertEqual(self.tick(index)[0], 120)

    def test_other_partial_reasons_are_not_stale_and_break_the_streak(self):
        index = Index(stale_report())
        self.tick(index)
        self.tick(index)
        for report in (
            dict(status="partial", stale=0, remaining=40, dirty=True),
            dict(status="partial", remaining=40, dirty=True),
            dict(status="partial", stale=False, remaining=40, dirty=True),
            dict(status="partial", stale=-1, remaining=40, dirty=True),
        ):
            index.report = report
            for _ in range(3):
                self.assertEqual(self.tick(index)[0], 60)
        index.report = stale_report()
        self.assertEqual(self.tick(index)[0], 60)

    def test_ready_namespace_does_not_erase_another_namespaces_stale(self):
        stale = Index(stale_report())
        ready = Index(dict(status="ready", stale=0))
        for delay in (60, 120, 240):
            actual, reports = self.tick(stale, ready)
            self.assertEqual(actual, delay)
            self.assertIs(reports[0], stale.report)
            self.assertIs(reports[1], ready.report)
        self.assertEqual(stale.closed, 3)
        self.assertEqual(ready.closed, 3)

    def test_original_failed_status_backoff_and_success_reset_unchanged(self):
        failed = Index(dict(status="failed"))
        ready = Index(dict(status="ready"))
        for count, delay in enumerate((60, 120, 240, 480, 900, 900), 1):
            failed.report = dict(status=("failed", "unavailable", "incompatible")[(count-1) % 3])
            actual, reports = self.tick(failed, ready)
            self.assertEqual(actual, delay)
            self.assertEqual(self.loop.failures, count)
            self.assertIs(reports[0], failed.report)
        self.assertEqual(self.tick(ready)[0], 60)
        self.assertEqual(self.loop.failures, 0)

    def test_failure_and_stale_streaks_are_independent(self):
        index = Index(stale_report())
        for _ in range(3):
            self.tick(index)
        index.report = dict(status="failed", stale=0)
        self.assertEqual(self.tick(index)[0], 60)
        self.assertEqual(self.tick(index)[0], 120)
        index.report = stale_report()
        self.assertEqual(self.tick(index)[0], 60)
        self.assertEqual(self.loop.failures, 0)

    def test_mixed_round_preserves_longer_failure_backoff(self):
        fail = Index(dict(status="failed"))
        for _ in range(3):
            self.tick(fail)
        stale = Index(stale_report())
        self.assertEqual(self.tick(fail, stale)[0], 480)
        self.assertEqual(self.loop.failures, 4)
        # Failure streak ends, but the stale streak continues on its own.
        self.assertEqual(self.tick(stale)[0], 120)

    def test_not_due_poll_does_not_run_or_reset_stale_streak(self):
        index = Index(stale_report())
        self.tick(index)
        self.clock.now += 59
        self.assertFalse(self.loop.due())
        self.assertEqual(self.loop.tick((index,)), ())
        self.assertEqual(index.calls, [16])
        self.clock.now += 1
        self.assertTrue(self.loop.due())
        self.assertEqual(self.tick(index)[0], 120)

    def test_schedule_starts_after_work_and_uses_configured_interval(self):
        self.loop = CapabilityVectorRecoveryLoop(
            clock=self.clock, interval_seconds=120, index_factory=lambda i: i)
        index = Index(stale_report(), self.clock)
        for delay in (120, 240, 480, 900, 900):
            self.assertEqual(self.tick(index)[0], delay)

    def test_exception_does_not_skip_next_namespace_or_resource_close(self):
        bad = Index(OSError("synthetic-private-path"))
        good = Index(dict(status="ready"))
        for delay in (60, 120):
            actual, reports = self.tick(bad, good)
            self.assertEqual(actual, delay)
            self.assertEqual(reports[0], dict(status="failed", processed=0, error="OSError"))
            self.assertIs(reports[1], good.report)
        self.assertEqual((bad.closed, good.closed), (2, 2))


if __name__ == "__main__":
    unittest.main()
