"""TDD tests for Backfill Track Task B.5: 100-session shadow backfill pilot batch & report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from evolvmem.experience_v2.backfill.pilot_100 import run_backfill_pilot_batch
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


def test_run_backfill_pilot_batch_100_sessions_isolated_and_scrubbed(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    base_dt = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
    sessions = []
    # 120 sessions in month 2026-10 (every 10th has a key moment, others are quiet)
    for i in range(120):
        sid = f'hist-{i:03d}'
        ts = (base_dt + timedelta(hours=i)).isoformat().replace('+00:00', 'Z')
        if i % 15 == 0:
            recs = (
                HistoryRawRecord(event_index=1, role='user', content='Remember billing portal requires tenant_id header; secret=sk-testsecret999'),
                HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nMissingTenantHeader at C:\\Users\\bob\\app'),
                HistoryRawRecord(event_index=3, role='assistant', content='Added header from /home/bob/.env'),
                HistoryRawRecord(event_index=4, role='tool', content='exit_code=0\nok'),
            )
        else:
            recs = (HistoryRawRecord(event_index=1, role='user', content='hello check status'),)
        sessions.append(HistorySession(
            summary=HistorySessionSummary(
                session_id=sid, adapter='codex', source_format='codex-jsonl',
                project='alpha', source_ref=sid, library='dev',
                external_session_id=sid, created_at=ts,
            ),
            records=recs,
        ))
    # Plus 5 sessions from Stage 3 pilot to exclude
    excluded = {f'hist-{119 - k:03d}' for k in range(5)}
    reader = InMemoryHistoryReader(sessions)

    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]['content']
        if '经验提炼器' in sys_p:
            return {
                'text': json.dumps({
                    'category': '业务经验',
                    'problem': '计费门户接口缺少 tenant_id 请求头报错 /home/bob/x',
                    'solution': '调用接口前必须显式设置 tenant_id 头部，切勿透传 sk-testsecret999',
                    'reusable_experience': '调用内部计费门户前先注入 tenant_id 标头',
                    'kind': 'pitfall',
                    'misbelief': '误以为网关自动填充租户标识',
                    'scope': {'system': 'billing'},
                    'skip_reason': '',
                }, ensure_ascii=False),
                'input_tokens': 110,
                'output_tokens': 70,
            }
        if '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.9, 'detail': 'miss'}), 'input_tokens': 40, 'output_tokens': 20}
        return {'text': 'retry request', 'input_tokens': 40, 'output_tokens': 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]['content']
        if '一致性' in sys_p or '评审员' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.92, 'detail': 'miss'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': 'check auth', 'input_tokens': 45, 'output_tokens': 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {'text': json.dumps({'relation': 'novel', 'confidence': 0.9, 'reason': 'new'}), 'input_tokens': 40, 'output_tokens': 20}

    report = run_backfill_pilot_batch(
        conn,
        history_reader=reader,
        owner_id='owner-b5',
        month='2026-10',
        limit=100,
        exclude_source_refs=excluded,
        flash_model='deepseek-v4-flash',
        strong_model='deepseek-chat',
        judge_model='deepseek-reasoner',
        flash_transport=flash,
        strong_transport=strong,
        judge_transport=judge,
        embedder=_FakeEmbedder(),
        policy=ProductPolicy(experience_backfill_enabled=False, ai_processing_enabled=False),
        extra_scrub_terms=('bob',),
    )

    assert report['track'] == 'backfill'
    assert report['scope_kind'] == 'month' and report['scope_value'] == '2026-10'
    assert report['sessions_total'] == 100
    assert report['sessions_zero_moments'] > 80
    assert report['live_tables_mutated'] is False
    assert report['policy_guard'] == {'experience_backfill_enabled': False, 'ai_processing_enabled': False}
    assert report['cursor_checkpoint']['processed_count'] == 100
    assert all(ref not in excluded for ref in report['processed_source_refs'])
    blob = json.dumps(report, ensure_ascii=False)
    assert 'sk-testsecret' not in blob
    assert '/home/bob' not in blob
    assert 'Users\\bob' not in blob


def test_parse_json_lines_skips_truncated_jsonl_line():
    from evolvmem.experience_v2.pipeline.events import parse_session_events

    raw = (
        json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'hello'}]}}).encode('utf-8')
        + b'\n{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"text":"unterminated'
    )
    sess = HistorySession(
        summary=HistorySessionSummary(
            session_id='corrupt-1',
            adapter='codex',
            source_format='codex-jsonl',
            project='alpha',
            source_ref='corrupt-1',
            library='dev',
            external_session_id='corrupt-1',
            created_at='2026-10-10T01:00:00Z',
            has_raw_bytes=True,
        ),
        records=(),
        raw_bytes=raw,
    )
    parsed = parse_session_events(sess)
    assert len(parsed.events) == 1
    assert parsed.events[0]['role'] == 'user'


def test_backfill_runner_isolates_corrupt_session_exception(pg_s4_migrated):
    from evolvmem.experience_v2.backfill.runner import BackfillRunner

    conn, _schema, _dsn = pg_s4_migrated
    s_good = HistorySession(
        summary=HistorySessionSummary(
            session_id='good-1',
            adapter='codex',
            source_format='codex-jsonl',
            project='alpha',
            source_ref='good-1',
            library='dev',
            external_session_id='good-1',
            created_at='2026-10-10T02:00:00Z',
        ),
        records=(HistoryRawRecord(event_index=1, role='user', content='status check'),),
    )
    s_bad = HistorySession(
        summary=HistorySessionSummary(
            session_id='bad-1',
            adapter='codex',
            source_format='codex-jsonl',
            project='alpha',
            source_ref='bad-1',
            library='dev',
            external_session_id='bad-1',
            created_at='2026-10-10T03:00:00Z',
        ),
        records=(HistoryRawRecord(event_index=1, role='user', content='broken'),),
    )
    reader = InMemoryHistoryReader([s_good, s_bad])
    runner = BackfillRunner(
        history_reader=reader,
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-b5-err',
        flash_model='f',
        strong_model='s',
        judge_model='j',
        flash_transport=lambda m, ms, mt: {'text': 'ok', 'input_tokens': 1, 'output_tokens': 1},
    )
    runner.queue.enqueue_sessions(
        conn,
        scope_kind='month',
        scope_value='2026-10',
        sessions=[s_bad.summary, s_good.summary],
        is_shadow=True,
    )

    orig_process = runner.scheduler.process_session_if_ready

    def flaky(c, sref, **kwargs):
        if sref == 'bad-1':
            raise ValueError('simulated malformed session')
        return orig_process(c, sref, **kwargs)

    runner.scheduler.process_session_if_ready = flaky  # type: ignore[method-assign]
    out = runner.run_queued_batch(
        conn,
        scope_kind='month',
        scope_value='2026-10',
        max_jobs=10,
        now=datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc),
        shadow_mode=True,
        shadow_run_id='err-isolation-1',
        enforce_night_window=False,
    )
    assert out['processed'] == 2
    assert out['completed'] == 1
    statuses = dict(
        conn.execute("SELECT source_ref, status FROM backfill_jobs WHERE scope_value='2026-10'").fetchall()
    )
    assert statuses == {'bad-1': 'failed', 'good-1': 'skipped_zero_moments'}
