"""TDD tests for Stage 3 Task 3.8: Pipeline scheduler, R6/R7/R9 & shadow isolation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from evolvmem.experience_v2.llm_meter import BudgetConfig, scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
    scan_direct_sqlite_in_pipeline,
)
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes
from evolvmem.product_policy import ProductPolicy


def _session_with_failure_fix(sid: str, *, extra_fail: bool = False) -> HistorySession:
    recs = [
        HistoryRawRecord(event_index=1, role="user", content="Always prefer uv over pip in this repo."),
        HistoryRawRecord(event_index=2, role="tool", content="exit_code=1\nTimerStalledError"),
        HistoryRawRecord(event_index=3, role="assistant", content="Use host runner."),
        HistoryRawRecord(event_index=4, role="tool", content="exit_code=0\nok"),
    ]
    if extra_fail:
        recs.extend([
            HistoryRawRecord(event_index=5, role="user", content="No, don't use global python; actually use uv run."),
            HistoryRawRecord(event_index=6, role="assistant", content="Switched to uv run."),
        ])
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid,
            adapter="codex",
            source_format="codex-jsonl",
            project="demo-app",
            source_ref=sid,
            library="dev",
            external_session_id=sid,
            created_at="2026-11-13T10:00:00Z",
            has_raw_bytes=False,
        ),
        records=tuple(recs),
        raw_bytes=None,
    )


def _fake_transports():
    calls = {"flash": 0, "strong": 0, "judge": 0}

    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls["flash"] += 1
        sys_p = messages[0]["content"]
        if "经验提炼器" in sys_p:
            return {
                "text": json.dumps({
                    "category": "系统环境经验",
                    "problem": "内网长等待放页面定时器会挂起",
                    "solution": "把等待计时移到宿主执行端并用 uv run 启动",
                    "reusable_experience": "宿主端负责等待计时且固定使用 uv",
                    "kind": "pitfall",
                    "misbelief": "误以为页面 setTimeout 始终稳定触发",
                    "scope": {"os": "Windows"},
                    "skip_reason": "",
                }, ensure_ascii=False),
                "input_tokens": 120,
                "output_tokens": 80,
            }
        if "一致性" in sys_p:
            return {"text": json.dumps({"consistent": False, "confidence": 0.9, "detail": "miss"}), "input_tokens": 60, "output_tokens": 20}
        return {"text": "use setTimeout", "input_tokens": 50, "output_tokens": 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls["strong"] += 1
        sys_p = messages[0]["content"]
        if "一致性" in sys_p:
            return {"text": json.dumps({"consistent": False, "confidence": 0.92, "detail": "private"}), "input_tokens": 60, "output_tokens": 20}
        return {"text": "check async loop", "input_tokens": 50, "output_tokens": 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls["judge"] += 1
        return {"text": json.dumps({"relation": "refines", "confidence": 0.85, "reason": "补充细节"}), "input_tokens": 80, "output_tokens": 30}

    return calls, flash, strong, judge


def test_trigger_r6_idempotency_append_and_version_bump(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler, should_trigger_session

    conn, *_ = pg_s3_migrated
    t0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert should_trigger_session(is_closed=False, last_message_at=t0, now=t0 + timedelta(minutes=15)) == (False, "active_wait")
    assert should_trigger_session(is_closed=False, last_message_at=t0, now=t0 + timedelta(minutes=30)) == (True, "idle_30m")
    assert should_trigger_session(is_closed=True, last_message_at=t0, now=t0 + timedelta(minutes=1)) == (True, "session_closed")

    # Disabled policy returns immediately with 0 calls (ai_processing_enabled decoupled)
    calls, flash, strong, judge = _fake_transports()
    sess1 = _session_with_failure_fix("archive:3801")
    reader = InMemoryHistoryReader([sess1])
    disabled_sched = PipelineScheduler(
        history_reader=reader,
        policy=ProductPolicy(ai_processing_enabled=True, experience_pipeline_enabled=False),
        owner_id="owner-38",
        flash_model="deepseek-v4-flash",
        strong_model="mimo-v2.6-flash",
        judge_model="mimo-v2.6-flash",
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
    )
    res_off = disabled_sched.process_session_if_ready(conn, "archive:3801", is_closed=True, last_message_at=t0, now=t0)
    assert res_off["status"] == "disabled"
    assert calls["flash"] == 0

    sched = PipelineScheduler(
        history_reader=reader,
        policy=ProductPolicy(ai_processing_enabled=False, experience_pipeline_enabled=True),
        owner_id="owner-38",
        flash_model="deepseek-v4-flash",
        strong_model="mimo-v2.6-flash",
        judge_model="mimo-v2.6-flash",
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
    )

    # Not ready -> active_wait, 0 calls
    r_wait = sched.process_session_if_ready(conn, "archive:3801", is_closed=False, last_message_at=t0, now=t0 + timedelta(minutes=10))
    assert r_wait["status"] == "active_wait"
    assert calls["flash"] == 0

    # Ready -> completes
    r1 = sched.process_session_if_ready(conn, "archive:3801", is_closed=False, last_message_at=t0, now=t0 + timedelta(minutes=31))
    assert r1["status"] == "completed"
    first_flash = calls["flash"]
    assert first_flash > 0

    # R6 same version & same content -> already_processed, 0 new calls
    r2 = sched.process_session_if_ready(conn, "archive:3801", is_closed=True, last_message_at=t0, now=t0 + timedelta(minutes=35))
    assert r2["status"] == "already_processed"
    assert r2["replayed"] is True
    assert calls["flash"] == first_flash

    # Append extra events -> only processes delta
    sess1_ext = _session_with_failure_fix("archive:3801", extra_fail=True)
    sched.history_reader = InMemoryHistoryReader([sess1_ext])
    r3 = sched.process_session_if_ready(conn, "archive:3801", is_closed=True, last_message_at=t0, now=t0 + timedelta(minutes=40))
    assert r3["status"] == "completed"
    assert calls["flash"] > first_flash

    # Bump pipeline_version -> reprocesses cleanly
    sched_v2 = PipelineScheduler(
        history_reader=InMemoryHistoryReader([sess1_ext]),
        policy=ProductPolicy(experience_pipeline_enabled=True),
        owner_id="owner-38",
        pipeline_version="stage3-v2",
        flash_model="deepseek-v4-flash",
        strong_model="mimo-v2.6-flash",
        judge_model="mimo-v2.6-flash",
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
    )
    r4 = sched_v2.process_session_if_ready(conn, "archive:3801", is_closed=True, last_message_at=t0, now=t0 + timedelta(minutes=45))
    assert r4["status"] == "completed"


def test_r7_budget_deferral_r9_circuit_breaker_and_shadow_isolation(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler, evaluate_circuit_breaker

    conn, *_ = pg_s3_migrated
    t0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    calls, flash, strong, judge = _fake_transports()
    sess = _session_with_failure_fix("archive:3802")
    reader = InMemoryHistoryReader([sess])

    # R7: tight daily budget -> defers without dropping; drain succeeds after raising budget
    tight_sched = PipelineScheduler(
        history_reader=reader,
        policy=ProductPolicy(experience_pipeline_enabled=True),
        owner_id="owner-38",
        flash_model="deepseek-v4-flash",
        strong_model="mimo-v2.6-flash",
        judge_model="mimo-v2.6-flash",
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
        budget=BudgetConfig(live_daily_tokens=50, session_max_tokens=30_000),
    )
    d_res = tight_sched.process_session_if_ready(conn, "archive:3802", is_closed=True, last_message_at=t0, now=t0)
    assert d_res["status"] == "deferred"
    row = conn.execute("SELECT status, error_code FROM pipeline_segment_runs WHERE session_id = 'archive:3802'").fetchone()
    assert row == ("deferred", "daily")

    tight_sched.budget = BudgetConfig(live_daily_tokens=2_000_000, session_max_tokens=30_000)
    drained = tight_sched.drain_deferred_segments(conn)
    assert len(drained) == 1
    assert drained[0]["status"] == "completed"

    # Shadow mode writes ONLY to shadow tables, 0 experience_proposals or experience_notes
    prop_before = conn.execute("SELECT COUNT(*) FROM experience_proposals").fetchone()[0]
    note_before = conn.execute("SELECT COUNT(*) FROM experience_notes").fetchone()[0]
    s_res = tight_sched.process_session_if_ready(
        conn, "archive:3802", is_closed=True, last_message_at=t0, now=t0, shadow_mode=True, shadow_run_id="run-shadow-38"
    )
    assert s_res["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM experience_proposals").fetchone()[0] == prop_before
    assert conn.execute("SELECT COUNT(*) FROM experience_notes").fetchone()[0] == note_before
    assert conn.execute("SELECT COUNT(*) FROM shadow_pilot_items WHERE run_id = 'run-shadow-38'").fetchone()[0] >= 1

    # R9 (a): 3 consecutive days > 60,000 tokens/admitted-note pauses pipeline & alerts user
    cb1 = evaluate_circuit_breaker(
        conn,
        owner_id="owner-38",
        track="live",
        daily_token_per_note=[("2026-11-11", 70_000, 1), ("2026-11-12", 140_000, 2), ("2026-11-13", 65_000, 1)],
    )
    assert cb1["paused"] is True
    assert len(cb1["alerts"]) >= 1

    # While paused, scheduler defers new session with circuit_paused
    sess3 = _session_with_failure_fix("archive:3803")
    tight_sched.history_reader = InMemoryHistoryReader([sess3])
    p_res = tight_sched.process_session_if_ready(conn, "archive:3803", is_closed=True, last_message_at=t0, now=t0)
    assert p_res["status"] == "deferred"
    assert p_res["reason"] == "circuit_paused"

    # R9 (b): review pass rate < 30% also triggers pause for owner-38-rate
    prop_id = "prop-cb-rate"
    conn.execute(
        """
        INSERT INTO experience_proposals(
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            scope, payload_json, evidence_json, source_kind, source_track, agent_client, model, actor_id, request_id
        ) VALUES (%s, 'owner-38-rate', 'add', '系统环境经验', 'pitfall', 'p', 's', 'r', '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'pipeline', 'live', 'codex', 'm', 'a', 'req-cb-rate-01')
        """,
        (prop_id,),
    )
    for idx in range(5):
        verdict = "accept" if idx == 0 else "reject"
        conn.execute(
            """
            INSERT INTO review_decisions(
                review_id, owner_id, reviewer_id, proposal_uid, category, source_track, user_verdict
            ) VALUES (%s, 'owner-38-rate', 'rev-1', %s, '系统环境经验', 'live', %s)
            """,
            (f"rev-38-{idx}", prop_id, verdict),
        )
    cb2 = evaluate_circuit_breaker(conn, owner_id="owner-38-rate", track="live", min_review_samples=5)
    assert cb2["paused"] is True
    assert cb2["pass_rate"] == 0.2

    assert scan_unauthorized_experience_notes_writes(Path(".")) == []
    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
