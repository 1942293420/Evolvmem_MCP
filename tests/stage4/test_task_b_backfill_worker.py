"""TDD tests for Backfill Nighttime Worker CLI & pre-cutover single-month scheduling."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from evolvmem.experience_v2.backfill.cursor import BackfillCursorStore
from evolvmem.experience_v2.backfill.worker import run_backfill_worker_tick
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
)
from evolvmem.product_policy import ProductPolicy


class _FakeEmbedder:
    def encode_document(self, text: str) -> list[float]:
        v = [0.0] * 768
        v[abs(hash(text)) % 16] = 1.0
        return v


def _sess(sid: str, created_at: str) -> HistorySession:
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid,
            adapter='codex',
            source_format='codex-jsonl',
            project='demo',
            source_ref=sid,
            library='personal',
            external_session_id=sid,
            created_at=created_at,
        ),
        records=(
            HistoryRawRecord(event_index=1, role='user', content='Remember internal rule: billing SKU lock requires tenant flag.'),
            HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nTenantLockMissing'),
            HistoryRawRecord(event_index=3, role='assistant', content='Fixed with tenant lock header.'),
            HistoryRawRecord(event_index=4, role='tool', content='exit_code=0\nok'),
        ),
    )


def _mock_transports():
    calls = {'count': 0}

    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['count'] += 1
        sys_p = messages[0]['content']
        if '经验提炼器' in sys_p:
            return {
                'text': json.dumps({
                    'category': '业务经验',
                    'problem': '私有计费锁缺少租户标识报错',
                    'solution': '调用内部计费网关前先校验并透传 tenant_lock 请求头',
                    'reusable_experience': '私有计费写操作必须校验租户锁标头',
                    'kind': 'pitfall',
                    'misbelief': '误以为网关默认注入',
                    'scope': {'system': 'billing'},
                    'skip_reason': '',
                }, ensure_ascii=False),
                'input_tokens': 100,
                'output_tokens': 60,
            }
        if '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.9, 'detail': 'private'}), 'input_tokens': 40, 'output_tokens': 20}
        return {'text': 'generic answer', 'input_tokens': 40, 'output_tokens': 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['count'] += 1
        sys_p = messages[0]['content']
        if '一致性' in sys_p or '评审员' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.92, 'detail': 'miss'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': 'generic answer', 'input_tokens': 40, 'output_tokens': 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['count'] += 1
        return {'text': json.dumps({'relation': 'novel', 'confidence': 0.9, 'reason': 'new'}), 'input_tokens': 40, 'output_tokens': 20}

    return calls, flash, strong, judge


def test_worker_respects_policy_night_window_and_pre_cutover_month_cursor(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    reader = InMemoryHistoryReader([
        _sess('s-post-cutover', '2026-11-14T15:00:00Z'),
        _sess('s-nov-pre', '2026-11-12T08:00:00Z'),
        _sess('s-oct', '2026-10-20T08:00:00Z'),
        _sess('s-undated', '0001-01-01T00:00:00Z'),
    ])
    calls, f_tr, s_tr, j_tr = _mock_transports()
    day_now = datetime(2026, 11, 14, 14, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
    night_now = datetime(2026, 11, 15, 2, 15, tzinfo=ZoneInfo('Asia/Shanghai'))

    # 1. Disabled switch -> 0 LLM calls
    r_dis = run_backfill_worker_tick(
        conn, history_reader=reader, before='2026-11-14T12:39:04Z', owner_id='owner-bf',
        policy=ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=False),
        now=night_now, flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr, embedder=_FakeEmbedder(),
    )
    assert r_dis['status'] == 'disabled' and calls['count'] == 0

    # 2. Enabled switch but daytime -> outside_night_window, 0 LLM calls
    pol_on = ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=True)
    r_day = run_backfill_worker_tick(
        conn, history_reader=reader, before='2026-11-14T12:39:04Z', owner_id='owner-bf',
        policy=pol_on, now=day_now,
        flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr, embedder=_FakeEmbedder(),
    )
    assert r_day['status'] == 'outside_night_window' and calls['count'] == 0

    # 3. Night window -> picks newest valid pre-cutover month (2026-11), excludes s-post-cutover!
    r_n1 = run_backfill_worker_tick(
        conn, history_reader=reader, before='2026-11-14T12:39:04Z', owner_id='owner-bf',
        policy=pol_on, now=night_now, max_sessions=10,
        flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr, embedder=_FakeEmbedder(),
    )
    assert r_n1['scope_kind'] == 'month' and r_n1['scope_value'] == '2026-11'
    assert r_n1['ordered_source_refs'] == ['s-nov-pre']
    assert r_n1['llm_calls'] > 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls WHERE track = 'live'").fetchone()[0] == 0

    assert r_n1['cursor']['completed'] is True
    r_n2 = run_backfill_worker_tick(
        conn, history_reader=reader, before='2026-11-14T12:39:04Z', owner_id='owner-bf',
        policy=pol_on, now=night_now, max_sessions=10,
        flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr, embedder=_FakeEmbedder(),
    )
    assert r_n2['scope_value'] == '2026-10'
    assert r_n2['ordered_source_refs'] == ['s-oct']

    # Paused backfill circuit breaker halts cleanly before LLM call
    conn.execute(
        "INSERT INTO pipeline_circuit_state(owner_id, track, paused, pause_reason) VALUES ('owner-bf', 'backfill', true, 'test_pause') "
        "ON CONFLICT (owner_id, track) DO UPDATE SET paused = true"
    )
    calls_before = calls['count']
    r_cb = run_backfill_worker_tick(
        conn, history_reader=reader, before='2026-11-14T12:39:04Z', owner_id='owner-bf',
        policy=pol_on, now=night_now, month='2026-09',
        flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr, embedder=_FakeEmbedder(),
    )
    assert r_cb['status'] == 'circuit_open' and calls['count'] == calls_before


def test_worker_cli_main_exits_cleanly_when_disabled_or_daytime(tmp_path, monkeypatch, capsys):
    from evolvmem.experience_v2.backfill.worker import main

    pfile = tmp_path / 'policy.json'
    pfile.write_text(
        json.dumps({
            'console_enabled': True,
            'legacy_web_enabled': False,
            'ai_processing_enabled': False,
            'experience_v2_enabled': True,
            'experience_pipeline_enabled': True,
            'experience_backfill_enabled': False,
        }),
        encoding='utf-8',
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(pfile))
    rc = main(['--before', '2026-11-14T12:39:04Z', '--max-sessions', '10'])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out['status'] == 'disabled' and out['llm_calls'] == 0
