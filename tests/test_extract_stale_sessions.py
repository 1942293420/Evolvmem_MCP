"""Offline stale-session worker state progression tests."""

import os
import importlib.util
from types import SimpleNamespace

import pytest

from evolvmem import kimi_hooks
from scripts import extract_stale_sessions as stale


@pytest.fixture(autouse=True)
def isolated_worker_paths(tmp_path, monkeypatch):
    """Existing worker tests must never prune real heartbeat files."""
    monkeypatch.setattr(stale, "DATA_DIR", tmp_path)
    monkeypatch.setattr(stale, "STATE_PATH", tmp_path / ".extracted_sessions.json")
    monkeypatch.setattr(stale, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(stale, "SESSIONS_GLOB", str(tmp_path / "sessions" / "*"))


def test_worker_state_uses_configured_data_directory(tmp_path, monkeypatch):
    destination = tmp_path / "custom-data"
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(destination))
    spec = importlib.util.spec_from_file_location("isolated_stale", stale.__file__)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    assert worker.DATA_DIR == destination
    worker.save_state({"sample-session": {"mtime": 123}})

    assert worker.load_state() == {"sample-session": {"mtime": 123}}
    assert (destination / ".extracted_sessions.json").is_file()
    assert worker.LIVE_DIR == destination / "live"


class FakeKimiHooks:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def session_end(self, payload):
        session_id = payload["session_id"]
        self.calls.append(session_id)
        return self.outcomes[session_id]


def _result(status, *, persisted=0, reason="", rate_limited=False):
    return SimpleNamespace(
        status=status,
        persisted=persisted,
        reason=reason,
        rate_limited=rate_limited,
    )


def test_batch_advances_only_completed_or_skipped_sessions(monkeypatch):
    monkeypatch.setattr(stale, "log", lambda _message: None)
    state = {
        "session_retry": {
            "mtime": 5.0,
            "via": "offline-fallback",
            "status": "completed",
        },
    }
    hooks = FakeKimiHooks({
        "session_done": _result("completed", persisted=3),
        "session_short": _result("skipped", reason="conversation too short"),
        "session_retry": _result("retry", reason="read timed out"),
    })

    rate_limited = stale.process_batch(
        [
            (30.0, "session_done"),
            (20.0, "session_short"),
            (10.0, "session_retry"),
        ],
        state,
        hooks,
    )

    assert rate_limited is False
    assert state == {
        "session_done": {
            "mtime": 30.0,
            "via": "offline-fallback",
            "status": "completed",
        },
        "session_short": {
            "mtime": 20.0,
            "via": "offline-fallback",
            "status": "skipped",
        },
        "session_retry": {
            "mtime": 5.0,
            "via": "offline-fallback",
            "status": "completed",
        },
    }
    assert state["session_retry"]["mtime"] == 5.0
    assert state["session_done"]["mtime"] == 30.0


def test_rate_limit_stops_remaining_batch(monkeypatch):
    monkeypatch.setattr(stale, "log", lambda _message: None)
    state = {}
    hooks = FakeKimiHooks({
        "session_limited": _result(
            "retry", reason="HTTP 429", rate_limited=True
        ),
        "session_not_called": _result("completed", persisted=1),
    })

    rate_limited = stale.process_batch(
        [
            (20.0, "session_limited"),
            (10.0, "session_not_called"),
        ],
        state,
        hooks,
    )

    assert rate_limited is True
    assert hooks.calls == ["session_limited"]
    assert state == {}


def test_unexpected_failure_stays_pending_and_next_session_runs(monkeypatch):
    monkeypatch.setattr(stale, "log", lambda _message: None)
    state = {}

    class RaisingHooks(FakeKimiHooks):
        def session_end(self, payload):
            session_id = payload["session_id"]
            self.calls.append(session_id)
            if session_id == "session_broken":
                raise RuntimeError("database busy")
            return self.outcomes[session_id]

    hooks = RaisingHooks({
        "session_ok": _result("completed", persisted=1),
    })

    rate_limited = stale.process_batch(
        [(20.0, "session_broken"), (10.0, "session_ok")],
        state,
        hooks,
    )

    assert rate_limited is False
    assert hooks.calls == ["session_broken", "session_ok"]
    assert "session_broken" not in state
    assert state["session_ok"]["status"] == "completed"


def test_absent_version_checkpoint_reprocesses_session_once(
        monkeypatch, tmp_path):
    wire = (tmp_path / "wd_project_hash" / "session_hook" / "agents"
            / "main" / "wire.jsonl")
    wire.parent.mkdir(parents=True)
    wire.write_text("x" * (stale.MIN_WIRE_BYTES + 1), encoding="utf-8")
    old_mtime = 1_700_000_000.0
    os.utime(wire, (old_mtime, old_mtime))
    monkeypatch.setattr(
        stale,
        "SESSIONS_GLOB",
        str(tmp_path / "*" / "session_*" / "agents" / "main" / "wire.jsonl"),
    )
    state = {}

    candidates = stale.find_candidates(
        now=old_mtime + stale.IDLE_MINUTES * 60 + 1,
        state=state,
    )

    assert candidates == [(old_mtime, "session_hook")]
    hooks = FakeKimiHooks({
        "session_hook": _result("completed", persisted=1),
    })
    assert stale.process_batch(candidates, state, hooks) is False
    assert state == {
        "session_hook": {
            "mtime": old_mtime,
            "via": "offline-fallback",
            "status": "completed",
        }
    }
    assert stale.find_candidates(
        now=old_mtime + stale.IDLE_MINUTES * 60 + 1,
        state=state,
    ) == []


def test_main_aborts_when_provider_config_is_unavailable(monkeypatch):
    monkeypatch.setattr(stale, "load_state", lambda: {})
    monkeypatch.setattr(
        stale,
        "find_candidates",
        lambda _now, _state: [(1.0, "session_pending")],
    )
    monkeypatch.setattr(stale, "save_state", lambda _state: None)
    monkeypatch.setattr(stale, "log", lambda _message: None)
    monkeypatch.setattr(kimi_hooks, "_load_llm_config", lambda: None)

    stale.main()


def test_live_session_with_quiet_wire_is_skipped(monkeypatch, tmp_path):
    wire = (tmp_path / "wd_project_hash" / "session_live" / "agents"
            / "main" / "wire.jsonl")
    wire.parent.mkdir(parents=True)
    wire.write_text("x" * (stale.MIN_WIRE_BYTES + 1), encoding="utf-8")
    old_mtime = 1_700_000_000.0
    os.utime(wire, (old_mtime, old_mtime))
    monkeypatch.setattr(
        stale,
        "SESSIONS_GLOB",
        str(tmp_path / "*" / "session_*" / "agents" / "main" / "wire.jsonl"),
    )
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    monkeypatch.setattr(stale, "LIVE_DIR", live_dir)
    now = old_mtime + stale.IDLE_MINUTES * 60 + 1

    # 无心跳：静默会话是候选
    assert stale.find_candidates(now=now, state={}) == [
        (old_mtime, "session_live")]

    # 新鲜心跳：会话仍打开，跳过
    marker = live_dir / "session_live"
    marker.touch()
    os.utime(marker, (now, now))
    assert stale.find_candidates(now=now, state={}) == []

    # 心跳过期后重新成为候选
    expired = now - stale.IDLE_MINUTES * 60 - 1
    os.utime(marker, (expired, expired))
    assert stale.find_candidates(now=now, state={}) == [
        (old_mtime, "session_live")]


def test_halt_run_stops_remaining_batch(monkeypatch):
    monkeypatch.setattr(stale, "log", lambda _message: None)
    state = {}
    hooks = FakeKimiHooks({
        "session_poor": SimpleNamespace(
            status="retry", persisted=0,
            reason="HTTP 402 credentials/quota unavailable",
            rate_limited=False, halt_run=True,
        ),
        "session_not_called": SimpleNamespace(
            status="completed", persisted=1, reason="",
            rate_limited=False, halt_run=False,
        ),
    })

    halted = stale.process_batch(
        [
            (20.0, "session_poor"),
            (10.0, "session_not_called"),
        ],
        state,
        hooks,
    )

    assert halted is True
    assert hooks.calls == ["session_poor"]
    assert state == {}


def test_prune_live_markers_removes_old_only(monkeypatch, tmp_path):
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    monkeypatch.setattr(stale, "LIVE_DIR", live_dir)
    now = 1_700_000_000.0
    fresh = live_dir / "session_fresh"
    fresh.touch()
    os.utime(fresh, (now, now))
    old = live_dir / "session_old"
    old.touch()
    old_ts = now - stale.LIVE_PRUNE_DAYS * 86400 - 1
    os.utime(old, (old_ts, old_ts))

    stale._prune_live_markers(now)

    assert fresh.exists()
    assert not old.exists()
