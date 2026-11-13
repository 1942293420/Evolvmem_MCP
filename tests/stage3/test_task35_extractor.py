"""TDD tests for Stage 3 Task 3.5: Metered KeyMoment experience extractor (§5.3 & R2-R4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolvmem.experience_v2.gate.rules import evaluate_l0_fields
from evolvmem.experience_v2.llm_meter import BudgetConfig, BudgetDeferred, estimate_tokens, scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
    scan_direct_sqlite_in_pipeline,
)
from evolvmem.experience_v2.pipeline.moments import KeyMoment
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes


def _sample_reader_and_moment() -> tuple[InMemoryHistoryReader, KeyMoment]:
    sess = HistorySession(
        summary=HistorySessionSummary(
            session_id="sess-35-01",
            adapter="codex",
            source_format="codex-jsonl",
            project="demo-app",
            source_ref="archive:17473",
            library="dev-lib",
            external_session_id="ext-35-01",
            created_at="2026-11-13T12:00:00Z",
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=1, role="user", content="Why did timer pause in background?"),
            HistoryRawRecord(event_index=2, role="tool", content="exit_code=1\nTimerStalledError"),
            HistoryRawRecord(event_index=3, role="assistant", content="Move wait loop to host runner."),
        ),
        raw_bytes=None,
    )
    reader = InMemoryHistoryReader([sess])
    moment = KeyMoment(
        moment_id="km_35_01",
        session_id="sess-35-01",
        kind="external_surprise",
        title="Background page throttles JS timer",
        event_range=(1, 3),
        snippet_text="user: Why did timer pause?\ntool: TimerStalledError\nassistant: Use host runner.",
        snippet_tokens=22,
        evidence_pointers=({"session_id": "sess-35-01", "source_ref": "archive:17473", "event_range": [1, 3], "content_hash": "sha256:abc"},),
        score=0.9,
    )
    return reader, moment


def test_extract_prompt_limits_valid_json_and_l0_pass(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.extract import (
        EXTRACT_SYSTEM_PROMPT,
        HistoryReaderEvidenceVerifier,
        extract_moment,
    )

    conn, *_ = pg_s3_migrated
    assert estimate_tokens(EXTRACT_SYSTEM_PROMPT) <= 600
    assert "值得记" in EXTRACT_SYSTEM_PROMPT
    assert "不值得记" in EXTRACT_SYSTEM_PROMPT
    assert "skip_reason" in EXTRACT_SYSTEM_PROMPT
    assert "tool_fumble" in EXTRACT_SYSTEM_PROMPT
    assert "apply_patch" in EXTRACT_SYSTEM_PROMPT
    assert "勿剧透" in EXTRACT_SYSTEM_PROMPT

    reader, moment = _sample_reader_and_moment()
    seen_msgs: list[list[dict]] = []

    def fake_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        seen_msgs.append(messages)
        payload = {
            "category": "系统环境经验",
            "problem": "浏览器后台标签页内定时等待会被挂起导致采集超时",
            "solution": "将长耗时等待计时逻辑移到宿主执行端进程而非页面脚本定时器",
            "reusable_experience": "后台页面计时不可靠，跨端调度应将阻塞等待放在宿主端",
            "kind": "env_fact",
            "misbelief": "误以为页面内 setTimeout 在切后台后仍按固定周期触发",
            "scope": {"system": "browser-automation", "os": "Windows"},
            "evidence": [{"library": "wrong-lib", "source": "archive:99999", "event": 99}],
            "skip_reason": "",
        }
        return {
            "text": "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
            "input_tokens": 220,
            "output_tokens": 110,
            "cost_estimate": 0.00033,
        }

    res = extract_moment(
        conn,
        moment,
        model="deepseek-v4-flash",
        transport=fake_transport,
        owner_id="owner-35",
        library="dev-lib",
        valid_record_indices=[1, 2, 3],
    )
    assert res.skipped is False
    assert res.tokens_used == 330
    assert estimate_tokens(seen_msgs[0][1]["content"]) <= 3000

    # Anchored evidence points strictly to verified session archive:17473 and valid event 1..3
    assert res.payload["evidence"] == [{"library": "dev-lib", "source": "archive:17473", "event": 1}]
    verifier = HistoryReaderEvidenceVerifier(reader)
    verdict, reason, detail = evaluate_l0_fields(res.payload, evidence_verifier=verifier, require_evidence=True)
    assert verdict == "pass", f"Expected L0 pass, got {reason}: {detail}"

    row = conn.execute("SELECT stage, model, input_tokens, output_tokens FROM llm_calls WHERE id = %s", (res.call_id,)).fetchone()
    assert row == ("extract", "deepseek-v4-flash", 220, 110)


def test_extract_skip_reason_zero_moments_and_budget_deferral(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.extract import extract_moment, extract_session_moments

    conn, *_ = pg_s3_migrated
    _, moment = _sample_reader_and_moment()

    def skip_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {
            "text": json.dumps({"skip_reason": "one_off_bug: 单次行号笔误且已提交git"}, ensure_ascii=False),
            "input_tokens": 180,
            "output_tokens": 30,
        }

    skipped_res = extract_moment(conn, moment, model="deepseek-v4-flash", transport=skip_transport, owner_id="owner-35")
    assert skipped_res.skipped is True
    assert "one_off_bug" in skipped_res.skip_reason

    # R2: 0 moments => 0 LLM calls
    called = {"n": 0}

    def never_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        called["n"] += 1
        return {"text": "{}"}

    assert extract_session_moments(conn, (), model="deepseek-v4-flash", transport=never_transport, owner_id="owner-35") == []
    assert called["n"] == 0

    # R7: session budget exceeded raises BudgetDeferred
    tight = BudgetConfig(live_daily_tokens=2_000_000, session_max_tokens=250)
    with pytest.raises(BudgetDeferred):
        extract_moment(conn, moment, model="deepseek-v4-flash", transport=skip_transport, owner_id="owner-35", budget=tight)

    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
    assert scan_unauthorized_experience_notes_writes(Path(".")) == []


def test_extract_preserves_relaxed_l0_sentences_without_clipping(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.extract import HistoryReaderEvidenceVerifier, extract_moment

    conn, *_ = pg_s3_migrated
    reader, moment = _sample_reader_and_moment()

    prob = "在EvolvMem迁移验证过程中执行PG隔离自动化测试全部通过但生产后台服务依然读取旧的SQLite数据文件导致新增经验未生效"
    sol = "迁移脚本仅在独立测试Schema与临时容器上跑通了写入回放测试，但线上Web与LAN MCP服务的systemd环境变量及配置文件仍指向本地SQLite路径；必须核对真实工作目录环境变量、切换连接配置并重启守护进程验证只读库切换状态。"
    reus = "数据迁移任务绝不能仅凭隔离测试容器通过就判定上线完成，必须核实生产常驻服务的实际连接指向与配置热加载并执行端到端探针校验。"
    mis = "误以为本地隔离集成测试套件全绿且脚本退出码为0即代表线上生产服务已自动切流到PostgreSQL数据库，忽略了后台常驻守护进程仍加载旧环境变量文件里的SQLite路径配置。"
    assert 55 <= len(prob) <= 120
    assert 105 <= len(sol) <= 240
    assert 55 <= len(reus) <= 120
    assert 85 <= len(mis) <= 160

    def rich_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        payload = {
            "category": "系统环境经验",
            "problem": prob,
            "solution": sol,
            "reusable_experience": reus,
            "kind": "pitfall",
            "misbelief": mis,
            "scope": {"system": "evolvmem", "os": "Linux"},
            "skip_reason": "",
        }
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "input_tokens": 200,
            "output_tokens": 180,
        }

    res = extract_moment(
        conn,
        moment,
        model="deepseek-v4-flash",
        transport=rich_transport,
        owner_id="owner-35-rich",
        library="dev-lib",
        valid_record_indices=[1, 2, 3],
    )
    assert res.skipped is False
    assert res.payload["problem"] == prob
    assert res.payload["solution"] == sol
    assert res.payload["reusable_experience"] == reus
    assert res.payload["misbelief"] == mis
    verifier = HistoryReaderEvidenceVerifier(reader)
    verdict, reason, detail = evaluate_l0_fields(res.payload, evidence_verifier=verifier, require_evidence=True)
    assert verdict == "pass", f"Expected L0 pass, got {reason}: {detail}"
