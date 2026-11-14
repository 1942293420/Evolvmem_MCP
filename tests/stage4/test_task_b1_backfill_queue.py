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


def test_backfill_invalid_policy_fail_closed(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    q = BackfillQueue(owner_id='owner-invalid')
    sh_night = datetime(2026, 11, 14, 2, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    bad_pol = ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=True, valid=False)
    assert q.check_run_gate(policy=bad_pol, now=sh_night) == {'allowed': False, 'status': 'disabled', 'llm_calls': 0}

    sess = HistorySession(
        summary=HistorySessionSummary(
            session_id='inv-1', adapter='codex', source_format='codex-jsonl',
            project='demo', source_ref='inv-1', library='dev',
            external_session_id='inv-1', created_at='2026-10-15T10:00:00Z',
        ),
        records=(HistoryRawRecord(event_index=1, role='user', content='hi'),),
    )
    sched = PipelineScheduler(
        history_reader=InMemoryHistoryReader([sess]), policy=bad_pol, owner_id='owner-invalid',
        flash_model='m-flash', strong_model='m-strong', judge_model='m-judge',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
        track='backfill',
    )
    assert sched.process_session_if_ready(
        conn, 'inv-1', is_closed=True, last_message_at=sh_night, now=sh_night,
    ) == {'status': 'disabled', 'llm_calls': 0}


def test_runner_night_window_recheck_deferred_break_savepoint_and_dict_gate_mode(pg_s4_migrated):
    import json
    from evolvmem.experience_v2.backfill.runner import BackfillRunner

    conn, _schema, _dsn = pg_s4_migrated
    tz = ZoneInfo('Asia/Shanghai')

    def _s(sid: str, ts: str, content: str = 'Hello only') -> HistorySession:
        return HistorySession(
            summary=HistorySessionSummary(
                session_id=sid, adapter='codex', source_format='codex-jsonl',
                project='alpha', source_ref=sid, library='dev',
                external_session_id=sid, created_at=ts,
            ),
            records=(HistoryRawRecord(event_index=1, role='user', content=content),),
        )

    sessions = [
        _s('nw-3', '2026-10-03T10:00:00Z'),
        _s('nw-2', '2026-10-02T10:00:00Z'),
        _s('nw-1', '2026-10-01T10:00:00Z'),
    ]
    q = BackfillQueue(owner_id='owner-nw')
    q.enqueue_sessions(conn, scope_kind='project', scope_value='alpha', sessions=[x.summary for x in sessions])

    # 1. Night window crossed after first job: ticks 06:59 then 07:01 -> stops before nw-2 and requeues nw-2, nw-1
    ticks = iter([
        datetime(2026, 11, 14, 6, 59, 0, tzinfo=tz),
        datetime(2026, 11, 14, 7, 1, 0, tzinfo=tz),
    ])
    runner = BackfillRunner(
        history_reader=InMemoryHistoryReader(sessions),
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-nw', flash_model='f', strong_model='s', judge_model='j',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
    )
    out = runner.run_queued_batch(
        conn, scope_kind='project', scope_value='alpha', max_jobs=3,
        now=datetime(2026, 11, 14, 6, 59, 0, tzinfo=tz), shadow_mode=False,
        clock_fn=lambda: next(ticks, datetime(2026, 11, 14, 7, 2, 0, tzinfo=tz)),
    )
    assert [r['job']['source_ref'] for r in out['results']] == ['nw-3']
    statuses = dict(conn.execute("SELECT source_ref, status FROM backfill_jobs WHERE owner_id = 'owner-nw'").fetchall())
    assert statuses == {'nw-3': 'skipped_zero_moments', 'nw-2': 'queued', 'nw-1': 'queued'}

    # 2. Deferred break + SQL error savepoint isolation
    ran: list[str] = []
    class _ErrThenOkRunner(BackfillRunner):
        pass

    runner_err = _ErrThenOkRunner(
        history_reader=InMemoryHistoryReader(sessions),
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-nw', flash_model='f', strong_model='s', judge_model='j',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
    )
    orig = runner_err.scheduler.process_session_if_ready
    def _flaky(c, source_ref, **kw):
        ran.append(source_ref)
        if source_ref == 'nw-2':
            c.execute('SELECT * FROM nonexistent_table_for_savepoint_test')
        return orig(c, source_ref, **kw)
    runner_err.scheduler.process_session_if_ready = _flaky
    out_err = runner_err.run_queued_batch(
        conn, scope_kind='project', scope_value='alpha', max_jobs=2,
        now=datetime(2026, 11, 14, 2, 0, tzinfo=tz), shadow_mode=False,
    )
    assert ran == ['nw-2', 'nw-1']
    statuses2 = dict(conn.execute("SELECT source_ref, status FROM backfill_jobs WHERE owner_id = 'owner-nw'").fetchall())
    assert statuses2['nw-2'] == 'failed' and statuses2['nw-1'] == 'skipped_zero_moments'

    # 3. Break immediately on 'deferred' and requeue remaining running jobs
    q.enqueue_sessions(
        conn, scope_kind='project', scope_value='defp',
        sessions=[_s('df-2', '2026-10-02T10:00:00Z').summary, _s('df-1', '2026-10-01T10:00:00Z').summary],
    )
    ran_def: list[str] = []
    def _defer_first(c, source_ref, **kw):
        ran_def.append(source_ref)
        return {'status': 'deferred', 'reason': 'track_daily_limit', 'detail': 'cap'}
    runner_err.scheduler.process_session_if_ready = _defer_first
    out_def = runner_err.run_queued_batch(
        conn, scope_kind='project', scope_value='defp', max_jobs=2,
        now=datetime(2026, 11, 14, 2, 0, tzinfo=tz), shadow_mode=False,
    )
    assert ran_def == ['df-2']
    st_def = dict(conn.execute("SELECT source_ref, status FROM backfill_jobs WHERE scope_value = 'defp'").fetchall())
    assert st_def == {'df-2': 'deferred', 'df-1': 'queued'}

    # 4. Per-category dict gate_mode preserved through BackfillRunner -> PipelineScheduler
    class _Emb:
        def encode_document(self, text: str) -> list[float]:
            return [1.0] + [0.0] * 767

    def _make_cat_sess(sid: str) -> HistorySession:
        return HistorySession(
            summary=HistorySessionSummary(
                session_id=sid, adapter='codex', source_format='codex-jsonl',
                project='catp', source_ref=sid, library='dev',
                external_session_id=sid, created_at='2026-10-05T10:00:00Z',
            ),
            records=(
                HistoryRawRecord(event_index=1, role='user', content='Remember internal billing rule for service.'),
                HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nBillingTenantError'),
                HistoryRawRecord(event_index=3, role='assistant', content='Fixed with billing header.'),
                HistoryRawRecord(event_index=4, role='tool', content='exit_code=0\nok'),
            ),
        )

    cat_sess = _make_cat_sess('cat-auto-1')
    def _flash_cat(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]['content']
        if '经验提炼器' in sys_p:
            return {
                'text': json.dumps({
                    'skip_reason': '', 'category': '业务经验', 'kind': 'pitfall',
                    'problem': '内部 billing-v2 接口未传 tenant 报 TenantRequired',
                    'solution': '调用 billing-v2 前统一注入 tenant_header 并校验响应码',
                    'reusable_experience': '内部 billing-v2 接口必须透传 tenant_header',
                    'misbelief': '误以为网关自动注入', 'scope': {'service': 'billing-v2'},
                }, ensure_ascii=False),
                'input_tokens': 120, 'output_tokens': 80,
            }
        if '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.92, 'detail': 'private'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': 'check gateway', 'input_tokens': 40, 'output_tokens': 20}

    def _strong_cat(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if '一致性' in messages[0]['content'] or '评审员' in messages[0]['content']:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.94, 'detail': 'miss'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': 'check gateway', 'input_tokens': 40, 'output_tokens': 20}

    def _judge_cat(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {'text': json.dumps({'relation': 'novel', 'confidence': 0.92, 'reason': 'novel'}), 'input_tokens': 40, 'output_tokens': 20}

    q_cat = BackfillQueue(owner_id='owner-cat')
    q_cat.enqueue_sessions(conn, scope_kind='project', scope_value='catp', sessions=[cat_sess.summary])
    cat_runner = BackfillRunner(
        history_reader=InMemoryHistoryReader([cat_sess]),
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-cat', flash_model='f', strong_model='s', judge_model='j',
        flash_transport=_flash_cat, strong_transport=_strong_cat, judge_transport=_judge_cat,
        embedder=_Emb(), gate_mode={'业务经验': 'auto', '前端经验': 'review'},
    )
    cat_runner.run_queued_batch(
        conn, scope_kind='project', scope_value='catp', max_jobs=1,
        now=datetime(2026, 11, 14, 2, 0, tzinfo=tz), shadow_mode=False,
    )
    st_row = conn.execute("SELECT state FROM experience_proposals WHERE owner_id = 'owner-cat'").fetchone()
    assert st_row and st_row[0] == 'accepted'
