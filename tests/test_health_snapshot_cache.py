"""Bounded health reuse must never authorize a changed namespace."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.test_lan_sharing import lan, add
from evolvmem import request_health


def test_unchanged_requests_reuse_complete_snapshot(lan, monkeypatch):
    runtime, adapter = lan
    add(adapter, "kane")
    server = runtime.server_for("kane")
    request_health._entry(server).snapshot = None
    original = server._evaluate_live_status
    evaluated = []
    def evaluate():
        evaluated.append(True)
        return original()
    monkeypatch.setattr(server, "_evaluate_live_status", evaluate)

    first = server._live_status()
    first.status_counts["caller_damage"] = 999
    second = server._live_status()

    assert second.ready
    assert "caller_damage" not in second.status_counts
    assert len(evaluated) == 1


def test_complete_evaluation_does_not_scan_status_twice(lan, monkeypatch):
    runtime, _ = lan
    server = runtime.server_for("kane")
    original = server.context_service.status
    scans = []
    def status():
        scans.append(True)
        return original()
    monkeypatch.setattr(server.context_service, "status", status)

    assert server._evaluate_live_status().ready
    assert len(scans) == 1


def test_concurrent_unchanged_requests_share_one_evaluation(lan, monkeypatch):
    runtime, _ = lan
    server = runtime.server_for("kane")
    original = server._evaluate_live_status
    entered, release = threading.Event(), threading.Event()
    evaluations = []
    def evaluate():
        evaluations.append(True)
        entered.set()
        assert release.wait(3)
        return original()
    monkeypatch.setattr(server, "_evaluate_live_status", evaluate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(server._live_status)
        assert entered.wait(1)
        two = pool.submit(server._live_status)
        release.set()
        assert one.result(3).ready
        assert two.result(3).ready
    assert len(evaluations) == 1


def test_evaluation_that_changes_database_cannot_publish_ready(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, "kane")
    server = runtime.server_for("kane")
    request_health._entry(server).snapshot = None
    original = server._evaluate_live_status
    def evaluate():
        result = original()
        with server.context_service.store.transaction():
            server.context_service.store._connection().execute(
                "DELETE FROM context_layers WHERE item_id=? AND layer='l0'",
                (item["context_id"],))
        return result
    monkeypatch.setattr(server, "_evaluate_live_status", evaluate)

    assert server._context_gate_error() is not None


def test_evaluation_shares_projection_and_vector_observations(lan, monkeypatch):
    import evolvmem.context_service as service_module
    import evolvmem.context_vector_health as vector_module
    runtime, _ = lan
    server = runtime.server_for("kane")
    lag, vector = service_module.check_projection_lag, vector_module.inspect_context_vector
    scans = {"projection": 0, "vector": 0}
    def projection(*args, **kwargs):
        scans["projection"] += 1
        return lag(*args, **kwargs)
    def inspect(*args, **kwargs):
        scans["vector"] += 1
        return vector(*args, **kwargs)
    monkeypatch.setattr(service_module, "check_projection_lag", projection)
    monkeypatch.setattr(vector_module, "inspect_context_vector", inspect)

    assert server._evaluate_live_status().ready
    assert scans == {"projection": 1, "vector": 1}


def test_cache_expires_and_failure_never_keeps_old_ready(lan, monkeypatch):
    runtime, _ = lan
    server = runtime.server_for("kane")
    now = [100.]
    monkeypatch.setattr(request_health.time, "monotonic", lambda: now[0])
    assert server._live_status().ready
    now[0] += 31
    monkeypatch.setattr(server, "_evaluate_live_status", lambda: None)
    assert server._live_status() is None
    assert server._context_gate_error() is not None


def test_dirty_and_provenance_changes_invalidate_snapshot(lan):
    runtime, _ = lan
    server = runtime.server_for("kane")
    assert server._live_status().ready
    before = request_health._state(server)
    server.context_service.vector_index.mark_dirty()
    assert request_health._state(server) != before
    after = request_health._state(server)
    request_health.vector_provenance.path(server.config).write_text("{}")
    assert request_health._state(server) != after


def test_unknown_state_does_not_authorize_previous_snapshot(lan, monkeypatch):
    runtime, _ = lan
    server = runtime.server_for("kane")
    assert server._live_status().ready
    def broken(_):
        raise OSError("fixture")
    monkeypatch.setattr(request_health, "_state", broken)
    assert server._context_gate_error() is not None


def test_telemetry_write_reuses_integrity_but_rechecks_semantic_layers(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, "kane")
    server = runtime.server_for("kane")
    service = server.context_service
    original = service._quick_check_diagnostics
    checks = []
    def quick():
        checks.append(True)
        return original()
    monkeypatch.setattr(service, "_quick_check_diagnostics", quick)
    # Start a new integrity observation, independent of fixture initialization.
    service._integrity_snapshot = None
    assert server._evaluate_live_status().ready
    service._record_served_access([item["context_id"]])
    assert server._evaluate_live_status().ready
    assert len(checks) == 1
    with service.store.transaction():
        service.store._connection().execute(
            "DELETE FROM context_layers WHERE item_id=? AND layer='l1'",
            (item["context_id"],))
    assert not server._evaluate_live_status().ready
    assert len(checks) == 1


def test_integrity_expiry_and_schema_change_require_new_check(lan, monkeypatch):
    import time
    runtime, _ = lan
    server = runtime.server_for("kane")
    service = server.context_service
    service._integrity_snapshot = None
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    original = service._quick_check_diagnostics
    checks = []
    def quick():
        checks.append(True)
        return original()
    monkeypatch.setattr(service, "_quick_check_diagnostics", quick)
    assert server._evaluate_live_status().ready
    assert server._evaluate_live_status().ready
    assert len(checks) == 1
    with service.store.transaction():
        service.store._connection().execute("CREATE TABLE probe_integrity(id INTEGER)")
    assert server._evaluate_live_status().ready
    assert len(checks) == 2
    now[0] += 301
    monkeypatch.setattr(service, "_scan_integrity_readonly",
                        lambda *args: ("quick_check_failed",))
    assert not server._evaluate_live_status().ready
    service._integrity_worker.join(2)
    assert not server._evaluate_live_status().ready


def test_integrity_refresh_runs_off_request_and_stale_baseline_fails_closed(lan, monkeypatch):
    import time
    runtime, _ = lan
    server = runtime.server_for("kane")
    service = server.context_service
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    service._integrity_snapshot = None
    assert server._evaluate_live_status().ready
    entered, release = threading.Event(), threading.Event()
    checks = []
    def scan(path, expected):
        checks.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return ()
    monkeypatch.setattr(service, "_scan_integrity_readonly", scan, raising=False)
    now[0] += 181
    try:
        assert server._evaluate_live_status().ready
        assert entered.wait(1)
        assert checks == [service._integrity_worker.ident]
        assert checks[0] != threading.get_ident()
        worker = service._integrity_worker
        # Concurrent calls keep the current baseline and coalesce refresh.
        assert server._evaluate_live_status().ready
        now[0] += 118
        assert server._live_status().ready
        now[0] += 2
        assert not server._live_status().ready
        assert len(checks) == 1
    finally:
        release.set()
        if entered.is_set():
            worker.join(2)
    assert server._evaluate_live_status().ready


def test_failed_background_integrity_invalidates_previously_ready_cache(lan, monkeypatch):
    import time
    runtime, _ = lan
    server = runtime.server_for("kane")
    service = server.context_service
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    service._integrity_snapshot = None
    assert server._live_status().ready
    entered, release = threading.Event(), threading.Event()
    def scan(path, expected):
        entered.set()
        assert release.wait(3)
        return ("quick_check_failed",)
    monkeypatch.setattr(service, "_scan_integrity_readonly", scan, raising=False)
    now[0] += 181
    try:
        assert server._live_status().ready
        assert entered.wait(1)
        worker = service._integrity_worker
    finally:
        release.set()
        if entered.is_set():
            worker.join(2)
    assert server._context_gate_error()["error"] == "degraded_legacy"


def test_idle_integrity_refresh_is_scheduled_and_close_cancels(lan, monkeypatch):
    import time
    import evolvmem.context_service as service_module
    runtime, _ = lan
    server = runtime.server_for("kane")
    service = server.context_service
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    timers = []
    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback, self.cancelled = delay, callback, False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(service_module.threading, "Timer", Timer)
    checks = []
    def scan(path, expected):
        checks.append(True)
        return ()
    monkeypatch.setattr(service, "_scan_integrity_readonly", scan)
    service._integrity_snapshot = None
    assert server._live_status().ready
    assert timers
    first = timers[-1]
    # No requests are made while the timer refreshes the independent baseline.
    now[0] += first.delay
    first.callback()
    service._integrity_worker.join(2)
    assert checks == [True]
    assert timers[-1] is not first
    now[0] += 170
    assert server._live_status().ready
    final_timer = timers[-1]
    service.close()
    assert final_timer.cancelled
    final_timer.callback()
    assert checks == [True]
