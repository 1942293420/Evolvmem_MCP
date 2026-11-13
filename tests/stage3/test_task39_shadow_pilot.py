"""TDD tests for Stage 3 Task 3.9: 50-session shadow pilot & circuit breaker drill."""
from __future__ import annotations

import json
from pathlib import Path

from evolvmem.experience_v2.gate.value import L2_JUDGE_SYSTEM_PROMPT
from evolvmem.experience_v2.llm_meter import scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.extract import EXTRACT_SYSTEM_PROMPT
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
    scan_direct_sqlite_in_pipeline,
)
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer, scan_unauthorized_experience_notes_writes


def _make_routine_session(idx: int) -> HistorySession:
    sid = f"archive:{9000 + idx}"
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid,
            adapter="codex",
            source_format="codex-jsonl",
            project="demo-app",
            source_ref=sid,
            library="personal",
            external_session_id=f"ext-{sid}",
            created_at=f"2026-11-12T10:{idx:02d}:00Z",
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=1, role="user", content=f"List python files in module {idx}"),
            HistoryRawRecord(event_index=2, role="assistant", content="Checked files cleanly."),
        ),
    )


def _make_moment_session(idx: int) -> HistorySession:
    sid = f"archive:{9100 + idx}"
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid,
            adapter="codex",
            source_format="codex-jsonl",
            project="demo-app",
            source_ref=sid,
            library="personal",
            external_session_id=f"ext-{sid}",
            created_at=f"2026-11-12T11:{idx:02d}:00Z",
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=1, role="user", content=f"Fix gateway timeout issue #{idx}"),
            HistoryRawRecord(event_index=2, role="tool", content="exit_code=1\nTimerStalledError"),
            HistoryRawRecord(event_index=3, role="assistant", content="Move timer from browser tab to host runner."),
            HistoryRawRecord(event_index=4, role="tool", content="exit_code=0\nok"),
            HistoryRawRecord(event_index=5, role="user", content="Always prefer uv over pip when running smoke checks."),
        ),
    )


def _pilot_fake_transports():
    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]["content"]
        if sys_p == EXTRACT_SYSTEM_PROMPT:
            if "Always prefer uv" in messages[1]["content"] and "preference_expression" in messages[1]["content"]:
                return {
                    "text": json.dumps({"skip_reason": "common_knowledge: repeated generic preference"}, ensure_ascii=False),
                    "input_tokens": 90,
                    "output_tokens": 20,
                }
            return {
                "text": json.dumps({
                    "category": "系统环境经验",
                    "problem": "内网采集页切到后台后计时器暂停触发",
                    "solution": "将等待计时放在宿主端执行并用轮询确认完成",
                    "reusable_experience": "后台页面定时器不可靠应由宿主端计时",
                    "kind": "pitfall",
                    "misbelief": "误以为页面内 setTimeout 始终按时回调",
                    "scope": {"system": "lan-browser", "os": "Windows"},
                    "skip_reason": "",
                }, ensure_ascii=False),
                "input_tokens": 110,
                "output_tokens": 70,
            }
        if sys_p == L2_JUDGE_SYSTEM_PROMPT:
            return {"text": json.dumps({"consistent": False, "confidence": 0.9, "detail": "flash missed host timer"}), "input_tokens": 50, "output_tokens": 20}
        return {"text": "Check network timeout settings", "input_tokens": 40, "output_tokens": 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]["content"]
        if sys_p == L2_JUDGE_SYSTEM_PROMPT:
            return {"text": json.dumps({"consistent": False, "confidence": 0.94, "detail": "private browser behavior"}), "input_tokens": 55, "output_tokens": 20}
        return {"text": "Retry request with exponential backoff", "input_tokens": 45, "output_tokens": 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {"text": json.dumps({"relation": "refines", "confidence": 0.88, "reason": "细化宿主端计时触发条件"}), "input_tokens": 70, "output_tokens": 25}

    return flash, strong, judge


def test_shadow_pilot_50_sessions_writes_only_shadow_tables_and_produces_report(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.pilot import run_shadow_pilot

    conn, *_ = pg_s3_migrated
    owner_id = "owner-s3-pilot"
    secret = b"pilot-unit-secret"
    issuer = TicketIssuer(secret, gate_id="pilot-seed")
    writer = ExperienceWriter(secret)
    for idx in range(2):
        ticket = issuer.issue(proposal_uid=f"seed-prop-{idx}", decision_id=f"seed-dec-{idx}", lane="user_fast")
        writer.apply(
            conn,
            ticket,
            Op(
                action="create",
                owner_id=owner_id,
                actor_id=owner_id,
                request_id=f"seed-manual-{idx:02d}",
                category="系统环境经验",
                problem=f"旧环境经验问题 {idx}",
                solution=f"旧环境经验解法 {idx}",
                reusable_experience=f"旧经验要点 {idx}",
                origin="manual",
            ),
        )

    sessions = [_make_routine_session(i) for i in range(34)] + [_make_moment_session(j) for j in range(16)]
    reader = InMemoryHistoryReader(sessions)
    flash, strong, judge = _pilot_fake_transports()

    report = run_shadow_pilot(
        conn,
        history_reader=reader,
        owner_id=owner_id,
        flash_model="deepseek-v4-flash",
        strong_model="deepseek-chat",
        judge_model="deepseek-reasoner",
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
        limit=50,
        sample_size=20,
    )

    # Zero mutations to live proposal/note tables
    assert conn.execute("SELECT COUNT(*) FROM experience_proposals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM experience_notes").fetchone()[0] == 2

    # Shadow persistence verified
    run_row = conn.execute("SELECT sessions_total, sessions_zero_moments FROM shadow_pilot_runs WHERE run_id = %s", (report["run_id"],)).fetchone()
    assert run_row == (50, 34)
    sampled_cnt = conn.execute(
        "SELECT COUNT(*) FROM shadow_pilot_items WHERE run_id = %s AND sample_for_user_review = true",
        (report["run_id"],),
    ).fetchone()[0]
    assert sampled_cnt == 20
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE is_shadow = false").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_segment_runs WHERE is_shadow = false").fetchone()[0] == 0

    # Metrics contract
    assert report["sessions_total"] == 50
    assert report["sessions_zero_moments"] == 34
    assert report["zero_moment_session_ratio"] >= 0.60
    assert report["proposals_generated"] >= 16
    assert report["proposals_admitted_candidate"] >= 16
    assert 0 < report["tokens_per_admitted_candidate"] <= 30_000
    assert len(report["samples_for_user_review"]) == 20
    for st in ("extract", "l2a_predict", "l2a_judge", "l2b_predict", "l2b_judge", "l3_reconcile"):
        assert report["llm_usage_by_stage"][st]["calls"] > 0
        assert report["llm_usage_by_stage"][st]["tokens"] > 0
    assert report["user_review"]["status"] == "pending_user_approval"
    assert report["user_review"]["experience_pipeline_enabled"] is False
    assert report["circuit_breaker_drill"]["passed"] is True


def test_shadow_pilot_circuit_breaker_drill_and_scrubber(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.pilot import scrub_pilot_text, simulate_circuit_breaker_drill

    conn, *_ = pg_s3_migrated
    drill = simulate_circuit_breaker_drill(conn, owner_id="owner-drill")
    assert drill["passed"] is True
    assert drill["paused_triggered"] is True
    assert drill["deferred_on_paused"] is True
    assert len(drill["alerts"]) >= 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_circuit_state WHERE owner_id = 'owner-drill'").fetchone()[0] == 0

    raw = "Auth Bearer secret_tok_123 key sk-abcdef1234567890 path /home/alice/work owner alice"
    cleaned = scrub_pilot_text(raw, extra_terms=["alice"])
    assert "secret_tok_123" not in cleaned
    assert "sk-abcdef1234567890" not in cleaned
    assert "/home/alice" not in cleaned
    assert "alice" not in cleaned.lower()

    from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler
    from evolvmem.product_policy import ProductPolicy

    empty_sess = HistorySession(
        summary=HistorySessionSummary(
            session_id="archive:99999",
            adapter="codex",
            source_format="codex-jsonl",
            project="demo",
            source_ref="archive:99999",
            library="personal",
            external_session_id="ext-empty",
            created_at="2026-11-12T10:00:00Z",
            has_raw_bytes=False,
        ),
        records=(),
        raw_bytes=None,
    )
    sched = PipelineScheduler(
        history_reader=InMemoryHistoryReader([empty_sess]),
        policy=ProductPolicy(experience_pipeline_enabled=False),
        owner_id="owner-empty",
        flash_model="deepseek-v4-flash",
        strong_model="deepseek-chat",
        judge_model="deepseek-chat",
        flash_transport=lambda m, msgs, max_tok: {"text": "{}", "input_tokens": 1, "output_tokens": 1},
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    r_first = sched.process_session_if_ready(conn, "archive:99999", is_closed=True, last_message_at=now, now=now, shadow_mode=True)
    r_replay = sched.process_session_if_ready(conn, "archive:99999", is_closed=True, last_message_at=now, now=now, shadow_mode=True)
    assert r_first == {"status": "skipped_zero_moments", "moments": 0, "llm_calls": 0}
    assert r_replay == {"status": "already_processed", "replayed": True, "llm_calls": 0}

    assert scan_unauthorized_experience_notes_writes(Path(".")) == []
    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
