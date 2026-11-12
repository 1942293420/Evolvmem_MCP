"""Serialization deferral keeps the existing pacing and fresh-request contract."""
from evolvmem.pg_vector_port.runtime import (
    ProjectionDeferred, ProjectionResult, ProjectionSerializationDeferred)
from tests.pg_history_vectors.test_worker_idle_probe import scheduler


def test_serialization_deferred_waits_full_interval_and_does_not_complete_a_job(monkeypatch):
    worker, script, clock, event = scheduler(monkeypatch,
        [ProjectionSerializationDeferred()], max_runtime_seconds=1,
        max_jobs=256, idle_probe_budget=8, stop_after_wait=1)

    worker._epoch()

    assert event.waits == [.5]
    assert clock.elapsed == .5
    assert len(script.opened) == 1 and script.closed == script.opened
    assert not script.active
    assert worker.status()['last_outcome'] == 'serialization_deferred'
    assert worker.status()['completed_jobs'] == 0
    assert worker.status()['epoch_jobs'] == 0


def test_next_pass_after_serialization_deferral_uses_a_fresh_context(monkeypatch):
    worker, script, _, event = scheduler(monkeypatch,
        [ProjectionSerializationDeferred(), ProjectionResult(1, 'upsert', (1,))],
        max_runtime_seconds=10, max_jobs=256, idle_probe_budget=8,
        stop_after_wait=2)

    worker._epoch()

    assert event.waits == [.5, .01]
    assert len(script.opened) == 2
    assert script.opened[0] is not script.opened[1]
    assert script.closed == script.opened
    assert worker.status()['last_outcome'] == 'upsert'
    assert worker.status()['completed_jobs'] == 1
    assert worker.status()['epoch_jobs'] == 1


def test_authority_and_serialization_deferrals_have_distinct_status(monkeypatch):
    for expected, result in (('authority_deferred', ProjectionDeferred()),
                             ('serialization_deferred', ProjectionSerializationDeferred())):
        worker, _, _, _ = scheduler(monkeypatch, [result], max_runtime_seconds=1,
            max_jobs=256, idle_probe_budget=8, stop_after_wait=1)
        worker._epoch()
        assert worker.status()['last_outcome'] == expected
        assert worker.status()['completed_jobs'] == 0
