"""TDD tests for Backfill Track Task B.1: 003 schema, switch, night window & backfill_jobs queue."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from evolvmem.experience_v2.backfill.queue import (
    BackfillQueue,
    is_within_night_window,
)
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
)
from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler, evaluate_circuit_breaker
from evolvmem.product_policy import ProductPolicy


def test_migration_003_creates_backfill_tables(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    for tbl in ('backfill_jobs', 'backfill_cursors', 'track_calibration_snapshots'):
        row = conn.execute("SELECT to_regclass(%s)", (tbl,)).fetchone()
        assert row and row[0] == tbl, f'Missing table {tbl}'


def test_night_window_hours_asia_shanghai():
    tz = ZoneInfo('Asia/Shanghai')
    assert is_within_night_window(datetime(2026, 11, 14, 1, 0, tzinfo=tz)) is True
    assert is_within_night_window(datetime(2026, 11, 14, 6, 59, tzinfo=tz)) is True
    assert is_within_night_window(datetime(2026, 11, 14, 7, 0, tzinfo=tz)) is False
    assert is_within_night_window(datetime(2026, 11, 14, 14, 30, tzinfo=tz)) is False
    assert is_within_night_window(datetime(2026, 11, 14, 0, 59, tzinfo=tz)) is False


def test_backfill_respects_own_switch_and_night_window(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    q = BackfillQueue(owner_id='owner-1')
    sessions = [
        HistorySessionSummary(
            session_id='s1', adapter='codex', source_format='codex-jsonl',
            project='demo', source_ref='s1', library='dev',
            external_session_id='s1', created_at='2026-10-15T10:00:00Z',
        ),
    ]
    inserted = q.enqueue_sessions(conn, scope_kind='month', scope_value='2026-10', sessions=sessions)
    assert inserted == 1

    # Even if experience_pipeline_enabled=True, experience_backfill_enabled=False MUST block live backfill.
    sh_night = datetime(2026, 11, 14, 2, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    gate_off = q.check_run_gate(
        policy=ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=False),
        now=sh_night,
    )
    assert gate_off == {'allowed': False, 'status': 'disabled', 'llm_calls': 0}

    # Enabled, but daytime -> outside_night_window
    sh_day = datetime(2026, 11, 14, 14, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    gate_day = q.check_run_gate(
        policy=ProductPolicy(experience_pipeline_enabled=False, experience_backfill_enabled=True),
        now=sh_day,
    )
    assert gate_day == {'allowed': False, 'status': 'outside_night_window', 'llm_calls': 0}

    # Enabled + inside 01:00..07:00 -> allowed
    gate_ok = q.check_run_gate(
        policy=ProductPolicy(experience_pipeline_enabled=False, experience_backfill_enabled=True),
        now=sh_night,
    )
    assert gate_ok['allowed'] is True

    claimed = q.claim_next_jobs(conn, scope_kind='month', scope_value='2026-10', limit=5)
    assert [j['source_ref'] for j in claimed] == ['s1']
    assert claimed[0]['status'] == 'running'
    q.mark_job_result(conn, job_id=claimed[0]['job_id'], status='deferred', error_code='track_daily_limit')
    requeued = q.requeue_deferred_jobs(conn, scope_kind='month', scope_value='2026-10')
    assert requeued == 1

    # PipelineScheduler with track='backfill' checks experience_backfill_enabled, NOT experience_pipeline_enabled.
    sess = HistorySession(
        summary=sessions[0],
        records=(HistoryRawRecord(event_index=1, role='user', content='hi'),),
    )
    sched = PipelineScheduler(
        history_reader=InMemoryHistoryReader([sess]),
        policy=ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=False),
        owner_id='owner-1',
        flash_model='m-flash', strong_model='m-strong', judge_model='m-judge',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
        track='backfill',
    )
    res = sched.process_session_if_ready(
        conn, 's1', is_closed=True,
        last_message_at=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
    )
    assert res == {'status': 'disabled', 'llm_calls': 0}


def test_circuit_breaker_separates_note_denominators(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    res_bf = evaluate_circuit_breaker(conn, owner_id='owner-1', track='backfill')
    res_live = evaluate_circuit_breaker(conn, owner_id='owner-1', track='live')
    assert res_bf['paused'] is False and res_live['paused'] is False
