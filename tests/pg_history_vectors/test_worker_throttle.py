"""Offline scheduler checks: no database, encoder, or host is started."""
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
    def __init__(self, clock, results, *, latency=0):
        self.clock, self.results, self.latency = clock, iter(results), latency
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
                self.clock.elapsed += self.latency
                if isinstance(result, BaseException):
                    raise result
                return result

            port.project_one = project_one
            yield port, request
        finally:
            assert self.active.pop() is request
            self.closed.append(request)


def scheduler(monkeypatch, results, *, latency=0, stop_after_wait=None, **options):
    clock = Clock()
    script = ProjectionScript(clock, results, latency=latency)
    worker = module.ProjectionWorker(script.context, enabled=True, interval_seconds=.5,
        max_jobs_per_pass=1, **options)
    event = StopEvent(clock, script.active, stop_after_wait=stop_after_wait)
    worker._stop = event
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock.monotonic))
    return worker, script, clock, event


def test_completed_jobs_progress_without_an_idle_interval_per_job(monkeypatch):
    # Regression: an unconditional .5 s wait cannot finish this backlog in 1 s.
    worker, script, clock, event = scheduler(monkeypatch,
        [ProjectionResult(job, 'upsert', (job,)) for job in range(1, 13)],
        latency=.02, max_runtime_seconds=1, max_jobs=12)

    worker._epoch()

    assert worker.status()['completed_jobs'] == 12
    assert worker.status()['epoch_jobs'] == 12
    assert clock.elapsed < .5
    assert len(event.waits) == 11
    assert all(0 < delay <= .02 for delay in event.waits)
    assert len(set(script.opened)) == 12
    assert script.closed == script.opened
    assert not script.active


@pytest.mark.parametrize('result,outcome', [
    (None, 'idle'),
    (ProjectionResult(None, 'no_pending_for_model', ()), 'no_pending_for_model'),
    (ProjectionDeferred(), 'authority_deferred'),
])
def test_idle_and_authority_deferred_passes_keep_the_configured_interval(monkeypatch, result, outcome):
    worker, script, clock, event = scheduler(monkeypatch, [result, result],
        max_runtime_seconds=1, max_jobs=256)

    worker._epoch()

    assert event.waits == [.5, .5]
    assert clock.elapsed == 1
    assert len(script.opened) == 2
    assert worker.status()['last_outcome'] == outcome
    assert worker.status()['completed_jobs'] == 0
    assert script.closed == script.opened


@pytest.mark.parametrize('result', [None, ProjectionResult(None, 'no_pending_for_model', ()), ProjectionDeferred()])
def test_a_pass_ending_idle_is_paced_even_if_it_completed_an_earlier_job(monkeypatch, result):
    worker, script, _, event = scheduler(monkeypatch,
        [ProjectionResult(1, 'remove', (1,)), result], stop_after_wait=1,
        max_runtime_seconds=300, max_jobs=256)
    worker.per_pass = 2

    worker._epoch()

    assert event.waits == [.5]
    assert worker.status()['completed_jobs'] == 1
    assert len(script.opened) == 2 and script.closed == script.opened


def test_fast_success_still_stops_at_the_epoch_job_budget(monkeypatch):
    worker, script, _, event = scheduler(monkeypatch,
        [ProjectionResult(job, 'upsert', (job,)) for job in range(1, 258)],
        max_runtime_seconds=300, max_jobs=256)

    worker._epoch()

    assert worker.status()['completed_jobs'] == 256
    assert worker.status()['epoch_jobs'] == 256
    assert len(script.opened) == 256
    assert len(event.waits) == 255
    assert script.closed == script.opened


def test_fast_success_does_not_start_a_request_after_the_epoch_deadline(monkeypatch):
    worker, script, clock, _ = scheduler(monkeypatch,
        [ProjectionResult(job, 'upsert', (job,)) for job in range(1, 257)],
        latency=.3, max_runtime_seconds=1, max_jobs=256)

    worker._epoch()

    assert len(script.opened) == 4
    assert all(start < 1 for start in script.started_at)
    assert clock.elapsed >= 1
    assert script.closed == script.opened


@pytest.mark.parametrize('code', [
    'write_commit_unknown', 'database_operation_failed', 'library_host_cleanup_incomplete',
    'request_deadline_expired',
])
def test_failure_stops_continuous_service_without_retry_or_restart(monkeypatch, code):
    worker, script, _, event = scheduler(monkeypatch,
        [ProjectionResult(1, 'upsert', (1,)), FoundationError(code), ProjectionResult(2, 'upsert', (2,))],
        max_runtime_seconds=300, max_jobs=256)
    worker._continuous = True

    worker._run()

    assert worker.status()['state'] == 'failed'
    assert worker.status()['error_code'] == code
    assert worker.status()['completed_jobs'] == 1
    assert worker.status()['completed_epochs'] == 0
    assert event.is_set()
    assert len(script.opened) == 2 and script.closed == script.opened
    with pytest.raises(VectorError, match='projection_worker_already_started'):
        worker.start()
    assert len(script.opened) == 2


def test_close_interrupts_the_owned_threads_long_idle_wait():
    cleaned = threading.Event()
    threads = []

    @contextmanager
    def context():
        threads.append(threading.get_ident())
        try:
            yield None
        finally:
            cleaned.set()

    worker = module.ProjectionWorker(context, enabled=True, interval_seconds=60,
        max_runtime_seconds=300, max_jobs_per_pass=1, max_jobs=256)
    try:
        worker.start()
        assert cleaned.wait(timeout=1), 'the worker did not exit its idle request'
        status = worker.close(timeout=1)
        assert status['state'] == 'stopped'
        assert not status['active']
        assert threads == [worker._thread.ident]
    finally:
        worker.close(timeout=1)
