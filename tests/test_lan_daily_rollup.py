"""Isolated contracts for the LAN capture worker's daily project rollup.

Every test keeps credentials, model calls, clock, and state inside a per-test
``tmp_path``: no real provider, no production database, no scheduling surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from evolvmem import kimi_hooks
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
)
from evolvmem.lan_config import LanSettings
from evolvmem.lan_daily_rollup import LanDailyRollup
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_tools import LanTools


class _Engine:
    """Deterministic in-memory embedding engine; never loads a model file."""

    is_loaded = False

    def initialize(self):
        self.is_loaded = True

    def encode_document(self, _text):
        return [1.0] + [0.0] * 767

    encode_query = encode_document

    def close(self):
        pass


class _Llm:
    """Fake provider adapter recording prompts; optional blocking gate."""

    def __init__(self, responses=None):
        self.responses = list(responses) if responses is not None else []
        self.prompts: list[str] = []
        self.gate = None

    def __call__(self, prompt, _config, **_kwargs):
        self.prompts.append(prompt)
        if self.gate is not None:
            self.gate(prompt)
        if self.responses:
            return self.responses.pop(0)
        return _ok_response()


class _Clock:
    def __init__(self, timestamp):
        self.ts = float(timestamp)

    def __call__(self):
        return self.ts


def _settings(tmp_path, *, embedding_enabled=True) -> LanSettings:
    return LanSettings(
        data_dir=tmp_path / "lan",
        owner_data_dir=tmp_path / "owner",
        token_hashes={
            user: hashlib.sha256((user + "-token").encode()).hexdigest()
            for user in ("jiangli", "kane")
        },
        embedding_enabled=embedding_enabled,
    )


def _start_runtime(tmp_path, *, embedding_enabled=True) -> LanRuntime:
    runtime = LanRuntime(
        _settings(tmp_path, embedding_enabled=embedding_enabled), _Engine()
    )
    runtime.initialize()
    return runtime


def _write_credentials(runtime) -> None:
    path = runtime.settings.owner_data_dir / "llm_credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"provider": "deepseek", "api_key": "fixture-only"}),
        encoding="utf-8",
    )


def _ok_response() -> str:
    return json.dumps(
        {
            "l0": "项目当前聚焦日更摘要链路。",
            "l1": "进展：已完成日更检查接线；待办：核验开场注入读取。",
            "l2": "完整细节：来源为项目会话摘要，摘要按源集合滚动更新。",
        },
        ensure_ascii=False,
    )


def _local(hour, minute, *, days=0):
    moment = datetime.now() + timedelta(days=days)
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _date_of(timestamp) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _state_path(runtime):
    return runtime.settings.data_dir / "lan_daily_rollup.json"


def _read_state(runtime) -> dict:
    return json.loads(_state_path(runtime).read_text(encoding="utf-8"))


def _write_state(runtime, payload: dict) -> None:
    _state_path(runtime).parent.mkdir(parents=True, exist_ok=True)
    _state_path(runtime).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _seed(runtime, user: str, project: str, tag: str) -> int:
    store = runtime.server_for(user).context_service.store
    item = store.create_item(
        ContextItemDraft(
            identity_key=f"project:{project}:progress:log:{tag}",
            content_type=ContextContentType.SESSION_SUMMARY,
            layers=ContextLayers(
                l0=f"会话摘要 {tag} 要点。",
                l1=f"细节：{tag} 的进展与决定。",
                l2=f"完整正文：{tag} 的依据与验证。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
        )
    )
    return item.id


@pytest.fixture
def daily_env(tmp_path, monkeypatch):
    runtime = _start_runtime(tmp_path)
    adapter = LanTools(runtime)
    _write_credentials(runtime)
    yield runtime, adapter, monkeypatch
    runtime.close()


def test_first_due_check_generates_and_records_the_day(daily_env):
    """A missing state file after 04:17 local must roll today's projects once."""
    runtime, adapter, monkeypatch = daily_env
    llm = _Llm()
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))

    assert LanDailyRollup(adapter, now=clock).check() is True

    state = _read_state(runtime)
    assert state["date"] == _date_of(clock.ts)
    assert state["last_check_at"] == pytest.approx(clock.ts)
    assert state["status"] == "completed"
    entry = state["users"]["jiangli"]["projects"]["eva"]
    assert entry["status"] == "ready"
    assert entry["reason"] == ""
    assert entry["context_id"] > 0
    assert isinstance(entry["covered_through"], str) and entry["covered_through"]
    assert "kane" not in state["users"]
    assert len(llm.prompts) == 1


def test_completed_day_is_not_checked_again(daily_env):
    """A second check on the same local day must neither run nor re-record."""
    runtime, adapter, monkeypatch = daily_env
    llm = _Llm()
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)
    assert daily.check() is True
    recorded_at = _read_state(runtime)["last_check_at"]

    clock.ts = _local(23, 0)
    assert daily.check() is False

    assert len(llm.prompts) == 1
    assert _read_state(runtime)["last_check_at"] == recorded_at


def test_before_the_daily_time_nothing_runs(daily_env):
    """04:16 local is not due; 04:17 is the boundary."""
    runtime, adapter, monkeypatch = daily_env
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", _Llm())
    clock = _Clock(_local(4, 16))
    daily = LanDailyRollup(adapter, now=clock)

    assert daily.check() is False
    assert not _state_path(runtime).exists()

    clock.ts = _local(4, 17)
    assert daily.check() is True
    assert _read_state(runtime)["date"] == _date_of(clock.ts)


def test_startup_after_the_daily_time_catches_up_the_same_day(daily_env):
    """Yesterday's completed state must not suppress today's startup catch-up."""
    runtime, adapter, monkeypatch = daily_env
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", _Llm())
    _seed(runtime, "jiangli", "eva", "a")
    yesterday = _local(5, 0, days=-1)
    _write_state(
        runtime,
        {
            "version": 1,
            "date": _date_of(yesterday),
            "last_check_at": yesterday,
            "status": "completed",
            "reason": "",
            "users": {},
        },
    )
    clock = _Clock(_local(4, 30))

    assert LanDailyRollup(adapter, now=clock).check() is True

    state = _read_state(runtime)
    assert state["date"] == _date_of(clock.ts)
    assert state["users"]["jiangli"]["projects"]["eva"]["status"] == "ready"


def test_next_day_recheck_marks_unchanged_without_an_llm_call(daily_env):
    """A new local day rechecks, yet an unchanged source set never calls the model."""
    runtime, adapter, monkeypatch = daily_env
    llm = _Llm()
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)
    assert daily.check() is True
    first = _read_state(runtime)["users"]["jiangli"]["projects"]["eva"]

    clock.ts = _local(5, 0, days=1)
    assert daily.check() is True

    state = _read_state(runtime)
    assert state["date"] == _date_of(clock.ts)
    assert state["status"] == "completed"
    entry = state["users"]["jiangli"]["projects"]["eva"]
    assert entry["status"] == "skipped"
    assert entry["reason"] == "unchanged"
    assert entry["context_id"] == first["context_id"]
    assert entry["covered_through"] == first["covered_through"]
    assert len(llm.prompts) == 1


def test_new_sources_on_a_later_day_refresh_the_summary(daily_env):
    """The next day's check must pick up sources added after the first rollup."""
    runtime, adapter, monkeypatch = daily_env
    llm = _Llm()
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)
    assert daily.check() is True
    first_id = _read_state(runtime)["users"]["jiangli"]["projects"]["eva"]["context_id"]

    _seed(runtime, "jiangli", "eva", "b")
    clock.ts = _local(5, 0, days=1)
    assert daily.check() is True

    entry = _read_state(runtime)["users"]["jiangli"]["projects"]["eva"]
    assert entry["status"] == "ready"
    assert entry["context_id"] != first_id
    assert len(llm.prompts) == 2


def test_failed_day_stays_incomplete_and_retries_after_an_hour(daily_env):
    """A failed generation is recorded, not completed, and waits one hour."""
    runtime, adapter, monkeypatch = daily_env
    llm = _Llm(["这不是 JSON。"])
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)

    assert daily.check() is True
    state = _read_state(runtime)
    assert state["status"] == "failed"
    entry = state["users"]["jiangli"]["projects"]["eva"]
    assert entry["status"] == "failed"
    assert entry["reason"] == "invalid_json"
    assert len(llm.prompts) == 1

    clock.ts = _local(5, 30)
    assert daily.check() is False
    assert len(llm.prompts) == 1
    assert _read_state(runtime)["status"] == "failed"

    clock.ts = _local(6, 1)
    assert daily.check() is True
    assert len(llm.prompts) == 2
    assert _read_state(runtime)["status"] == "completed"


def test_missing_credentials_fail_the_day_without_a_model_call(daily_env):
    """The per-round credential file gates every model call; retries re-read it."""
    runtime, adapter, monkeypatch = daily_env
    called = []
    llm = _Llm()
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    called_before = len(llm.prompts)
    (runtime.settings.owner_data_dir / "llm_credentials.json").unlink()
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)

    assert daily.check() is True
    state = _read_state(runtime)
    assert state["status"] == "failed"
    assert state["reason"] == "provider_unavailable"
    assert state["users"] == {}
    assert len(llm.prompts) == called_before

    _write_credentials(runtime)
    clock.ts = _local(6, 30)
    assert daily.check() is True
    assert len(llm.prompts) == called_before + 1
    assert _read_state(runtime)["status"] == "completed"


def test_daily_llm_call_does_not_block_a_reader(daily_env):
    """The dispatch lock must be released while the model works."""
    runtime, adapter, monkeypatch = daily_env
    started = threading.Event()
    release = threading.Event()
    llm = _Llm()
    llm.gate = lambda _prompt: (started.set(), release.wait(5))
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    daily = LanDailyRollup(adapter, now=_Clock(_local(5, 0)))
    outcome = {}

    def run_check():
        outcome["checked"] = daily.check()

    worker = threading.Thread(target=run_check)
    worker.start()
    assert started.wait(5), "the fake provider was never called"

    reader_result = {}

    def read():
        reader_result.update(adapter.call_tool("jiangli", "memory_status", {}))

    reader = threading.Thread(target=read)
    reader.start()
    reader.join(2)
    blocked = reader.is_alive()
    release.set()
    worker.join(10)
    reader.join(10)

    assert not blocked, "a reader was blocked while the daily model call ran"
    assert reader_result.get("authenticated_user") == "jiangli"
    assert outcome.get("checked") is True


def test_memory_write_during_the_model_call_discards_the_rollup(daily_env):
    """A revision change while the model works must not persist a stale summary."""
    runtime, adapter, monkeypatch = daily_env
    started = threading.Event()
    release = threading.Event()
    llm = _Llm()
    llm.gate = lambda _prompt: (started.set(), release.wait(5))
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", llm)
    _seed(runtime, "jiangli", "eva", "a")
    daily = LanDailyRollup(adapter, now=_Clock(_local(5, 0)))
    outcome = {}

    def run_check():
        outcome["checked"] = daily.check()

    worker = threading.Thread(target=run_check)
    worker.start()
    assert started.wait(5), "the fake provider was never called"
    runtime.server_for("jiangli").handle_tool_call(
        "memory_add",
        {
            "key": "project:eva:decision:concurrent",
            "value": "并发写入使本次生成的摘要基于过期来源集，必须整体丢弃。",
        },
    )
    release.set()
    worker.join(10)

    assert outcome.get("checked") is True
    state = _read_state(runtime)
    assert state["status"] == "failed"
    entry = state["users"]["jiangli"]["projects"]["eva"]
    assert entry["status"] == "failed"
    assert entry["reason"] == "llm_no_response"


def test_private_summaries_never_reach_the_public_library(daily_env):
    """Only the owner rolls up; the other user and public remain untouched."""
    runtime, adapter, monkeypatch = daily_env
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", _Llm())
    _seed(runtime, "jiangli", "eva", "a")
    _seed(runtime, "kane", "palm", "b")

    assert LanDailyRollup(adapter, now=_Clock(_local(5, 0))).check() is True

    public = runtime.server_for("jiangli", "public").context_service.store
    assert (
        public._connection()
        .execute("SELECT COUNT(*) FROM context_project_rollups")
        .fetchone()[0]
        == 0
    )
    assert (
        public._connection()
        .execute(
            "SELECT COUNT(*) FROM context_items WHERE content_type='project_summary'"
        )
        .fetchone()[0]
        == 0
    )
    for user, expected in (("jiangli", ["eva"]), ("kane", [])):
        rows = (
            runtime.server_for(user)
            .context_service.store._connection()
            .execute("SELECT project FROM context_project_rollups")
            .fetchall()
        )
        assert [row["project"] for row in rows] == expected


def test_state_file_is_single_private_and_content_free(daily_env):
    """The state must be one 0600 JSON with ids and codes only, written atomically."""
    runtime, adapter, monkeypatch = daily_env
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", _Llm())
    _seed(runtime, "jiangli", "eva", "a")

    assert LanDailyRollup(adapter, now=_Clock(_local(5, 0))).check() is True

    path = _state_path(runtime)
    text = path.read_text(encoding="utf-8")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "fixture-only" not in text
    assert "的进展与决定" not in text
    assert "会话摘要" not in text
    assert list(path.parent.glob(path.name + ".*")) == []
    payload = json.loads(text)
    assert set(payload) == {
        "version",
        "date",
        "last_check_at",
        "last_check_local",
        "status",
        "reason",
        "users",
    }
    assert set(payload["users"]["jiangli"]["projects"]["eva"]) == {
        "status",
        "reason",
        "context_id",
        "covered_through",
    }


def test_unavailable_vectors_do_not_trap_the_day_in_retry(tmp_path, monkeypatch):
    """Without a vector cache the committed summary is checked, not retried hourly."""
    runtime = _start_runtime(tmp_path, embedding_enabled=False)
    adapter = LanTools(runtime)
    _write_credentials(runtime)
    monkeypatch.setattr(kimi_hooks, "_call_llm_with_retry", _Llm())
    _seed(runtime, "jiangli", "eva", "a")
    clock = _Clock(_local(5, 0))
    daily = LanDailyRollup(adapter, now=clock)
    try:
        assert daily.check() is True
        state = _read_state(runtime)
        assert state["status"] == "completed"
        assert state["users"]["jiangli"]["projects"]["eva"]["status"] == "vector_dirty"

        clock.ts = _local(5, 30)
        assert daily.check() is False
    finally:
        runtime.close()


def test_capture_worker_runs_the_daily_check_after_extraction(tmp_path, monkeypatch):
    """The worker loop must call the daily check only after upload/extraction work."""
    import evolvmem.lan_runtime as lan_runtime

    observed = []

    class _DailyCheck:
        def __init__(self, adapter):
            self.adapter = adapter

        def check(self):
            observed.append(self.adapter.pending_count)

    class _Adapter:
        def __init__(self):
            self.backfill_count = 0
            self.pending_count = 0

        def process_backfills(self):
            self.backfill_count += 1

        def process_pending(self):
            self.pending_count += 1

    monkeypatch.setattr(lan_runtime, "LanDailyRollup", _DailyCheck)
    monkeypatch.setattr(lan_runtime, "_CAPTURE_POLL_SECONDS", 0.01)
    runtime = LanRuntime(_settings(tmp_path, embedding_enabled=False))
    runtime.initialize()
    adapter = _Adapter()
    try:
        runtime.start_capture_worker(adapter)
        deadline = time.time() + 3
        while not observed and time.time() < deadline:
            time.sleep(0.01)
        assert observed, "the capture worker never ran the daily check"
        assert observed[0] >= 1, "the daily check ran before uploads/extractions"
    finally:
        runtime.close()
