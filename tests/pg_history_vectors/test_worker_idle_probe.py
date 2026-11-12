"""Offline idle-probe schedule checks: no database, encoder or host is started."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from evolvmem.library_foundation import FoundationError
from evolvmem.pg_vector_port import worker as module
from evolvmem.pg_vector_port.model import VectorError
from evolvmem.pg_vector_port.runtime import ProjectionDeferred, ProjectionResult, VectorPort


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed


class StopEvent:
    """An interruptible scheduler clock; waits never consume wall-clock time."""
    def __init__(self, clock, active, *, stop_after_wait=None):
        self.clock, self.active = clock, active
        self.stop_after_wait = stop_after_wait
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        assert not self.active, 'a request must exit before scheduling another pass'
        if self.stopped:
            return True
        self.waits.append(seconds)
        self.clock.elapsed += seconds
        if self.stop_after_wait == len(self.waits):
            self.set()
        return self.stopped


class ProjectionScript:
    """Model/SQL results at the actual worker's per-job context boundary."""
    def __init__(self, clock, results):
        self.clock, self.results = clock, iter(results)
        self.active = []
        self.opened = []
        self.closed = []
        self.started_at = []

    @contextmanager
    def context(self):
        assert not self.active, 'jobs must use serial, fresh requests'
        result = next(self.results)
        request = object()
        self.active.append(request)
        self.opened.append(request)
        self.started_at.append(self.clock.elapsed)
        try:
            if isinstance(result, ProjectionDeferred):
                raise result
            if result is None:
                yield None
                return
            port = object.__new__(VectorPort)
            port.guard = SimpleNamespace(timeout_seconds=30)

            def project_one(route, *, completion_budget=1):
                assert 1 <= completion_budget <= 2
                assert route is request and self.active == [request]
                if isinstance(result, BaseException):
                    raise result
                return result

            port.project_one = project_one
            yield port, request
        finally:
            assert self.active.pop() is request
            self.closed.append(request)


def scheduler(monkeypatch, results, *, stop_after_wait=None, **options):
    clock = Clock()
    script = ProjectionScript(clock, results)
    worker = module.ProjectionWorker(script.context, enabled=True, interval_seconds=.5,
        max_jobs_per_pass=1, **options)
    event = StopEvent(clock, script.active, stop_after_wait=stop_after_wait)
    worker._stop = event
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock.monotonic))
    return worker, script, clock, event


@pytest.mark.parametrize('budget', [0, 17, True, 1.5, '8', None])
def test_idle_probe_budget_is_trusted_and_bounded(budget):
    with pytest.raises(VectorError, match='invalid_projection_idle_budget'):
        module.ProjectionWorker(lambda: None, enabled=True, idle_probe_budget=budget)


@pytest.mark.parametrize('result', [
    None,
    ProjectionResult(None, 'no_pending_for_model', ()),
])
def test_empty_results_use_a_bounded_burst_of_short_probes_then_full_interval(monkeypatch, result):
    # Shared+hidden libraries can need several empty probes before the next
    # native project; each burst is bounded and the configured interval follows.
    worker, script, clock, event = scheduler(monkeypatch, [result] * 12,
        idle_probe_budget=3, max_runtime_seconds=10, max_jobs=256,
        stop_after_wait=5)

    worker._epoch()

    assert event.waits == [.01, .01, .5, .01, .01]
    assert clock.elapsed == .54
    assert len(script.opened) == 5
    assert worker.status()['completed_jobs'] == 0
    assert worker.status()['last_outcome'] in ('idle', 'no_pending_for_model')
    assert script.closed == script.opened
    assert not script.active


def test_all_empty_probes_with_budget_eight_end_in_one_full_interval(monkeypatch):
    # The all-empty burst is kept separate from the reset tests: eight empty
    # results must produce seven short probes and exactly one full interval.
    worker, script, clock, event = scheduler(monkeypatch, [None] * 8,
        idle_probe_budget=8, max_runtime_seconds=10, max_jobs=256,
        stop_after_wait=8)

    worker._epoch()

    assert event.waits == [.01] * 7 + [.5]
    assert clock.elapsed == pytest.approx(.57)
    assert len(script.opened) == 8
    assert script.closed == script.opened


def test_successful_job_resets_the_empty_probe_budget(monkeypatch):
    # An empty probe before the productive job proves the job itself reset the
    # counter: without that reset the third empty probe would hit budget 2.
    worker, script, clock, event = scheduler(monkeypatch,
        [None, ProjectionResult(1, 'upsert', (1,)), None, None],
        idle_probe_budget=2, max_runtime_seconds=10, max_jobs=256,
        stop_after_wait=4)

    worker._epoch()

    assert event.waits == [.01, .01, .01, .5]
    assert clock.elapsed == .53
    assert worker.status()['completed_jobs'] == 1
    assert len(script.opened) == 4
    assert script.closed == script.opened


def test_projection_deferred_always_waits_the_full_interval_and_resets_the_budget(monkeypatch):
    # The empty probe after the deferral must start a new burst; without the
    # deferred reset it would immediately hit budget 2 and wait .5 early.
    worker, script, clock, event = scheduler(monkeypatch,
        [None, ProjectionDeferred(), None, None],
        idle_probe_budget=2, max_runtime_seconds=10, max_jobs=256,
        stop_after_wait=4)

    worker._epoch()

    assert event.waits == [.01, .5, .01, .5]
    assert clock.elapsed == 1.02
    assert len(script.opened) == 4
    assert script.closed == script.opened


@pytest.mark.parametrize('code', ['commit_result_unknown', 'write_committed_delivery_failed'])
def test_commit_error_stops_after_one_attempt_with_budget_eight(monkeypatch, code):
    # Unknown commit and unconfirmed delivery must never be retried as idle
    # probes; the next scripted result would be visible if a retry happened.
    worker, script, _, event = scheduler(monkeypatch,
        [FoundationError(code), ProjectionResult(2, 'upsert', (2,))],
        idle_probe_budget=8, max_runtime_seconds=10, max_jobs=256)
    worker._continuous = True

    worker._run()

    assert worker.status()['state'] == 'failed'
    assert worker.status()['error_code'] == code
    assert worker.status()['completed_jobs'] == 0
    assert worker.status()['completed_epochs'] == 0
    assert event.is_set()
    assert len(script.opened) == 1
    assert script.closed == script.opened


def test_close_stops_a_thread_with_the_trusted_probe_budget():
    opened = []
    cleaned = threading.Event()
    enough_probes = threading.Event()

    @contextmanager
    def context():
        opened.append(threading.get_ident())
        if len(opened) >= 16:
            enough_probes.set()
        try:
            yield None
        finally:
            cleaned.set()

    worker = module.ProjectionWorker(context, enabled=True, interval_seconds=60,
        max_runtime_seconds=300, max_jobs_per_pass=1, max_jobs=256,
        idle_probe_budget=16)
    try:
        worker.start()
        assert enough_probes.wait(timeout=1), 'the worker did not run its bounded probes'
        status = worker.close(timeout=1)
        assert status['state'] == 'stopped'
        assert not status['active']
        assert opened == [worker._thread.ident] * len(opened)
    finally:
        worker.close(timeout=1)
