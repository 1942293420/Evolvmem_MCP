"""TDD tests for Stage 3 Task 3.9: live pipeline periodic worker CLI & cutover floor."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from evolvmem.experience_v2.gate.value import L2_JUDGE_SYSTEM_PROMPT
from evolvmem.experience_v2.llm_meter import scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.extract import EXTRACT_SYSTEM_PROMPT
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord, HistorySession, HistorySessionSummary, InMemoryHistoryReader, scan_direct_sqlite_in_pipeline,
)
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer, scan_unauthorized_experience_notes_writes
from evolvmem.product_policy import ProductPolicy


def _make_live_session(sid: str, *, created_at: str) -> HistorySession:
    recs = (
        HistoryRawRecord(event_index=1, role='user', content=f'Check gateway timeout on {sid}'),
        HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nGatewayTimeoutError: 504 on /internal-sync'),
        HistoryRawRecord(event_index=3, role='assistant', content='Move internal-sync loop to async worker queue.'),
        HistoryRawRecord(event_index=4, role='tool', content='exit_code=0\nok'),
        HistoryRawRecord(event_index=5, role='user', content='Always offload internal-sync loops to async worker queue.'),
    )
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid, adapter='codex', source_format='codex-jsonl', project='gateway-ops',
            source_ref=sid, library='personal', external_session_id=f'ext-{sid}', created_at=created_at,
        ),
        records=recs,
    )


def _live_fake_transports():
    calls = {'flash': 0, 'strong': 0, 'judge': 0}

    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['flash'] += 1
        sys_p = messages[0]['content']
        if sys_p == EXTRACT_SYSTEM_PROMPT or '经验提炼器' in sys_p:
            return {'text': json.dumps({
                'skip_reason': '', 'category': '业务经验', 'kind': 'pitfall',
                'problem': 'internal-sync 接口在前台同步轮询导致 504 超时',
                'solution': '将 internal-sync 轮询迁入后台 worker 队列执行并异步回写状态',
                'reusable_experience': '内部同步长任务不得占用前台请求线程，必须下沉后台 worker 队列',
                'misbelief': '误以为提高 Nginx read timeout 可解决前台长轮询阻塞',
                'scope': {'service': 'internal-sync'},
            }, ensure_ascii=False), 'input_tokens': 140, 'output_tokens': 90}
        if sys_p == L2_JUDGE_SYSTEM_PROMPT or '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.9, 'detail': 'private'}), 'input_tokens': 60, 'output_tokens': 25}
        return {'text': 'increase nginx proxy_read_timeout', 'input_tokens': 50, 'output_tokens': 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['strong'] += 1
        sys_p = messages[0]['content']
        if sys_p == L2_JUDGE_SYSTEM_PROMPT or '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.94, 'detail': 'internal'}), 'input_tokens': 60, 'output_tokens': 25}
        return {'text': 'check gateway upstream timeout', 'input_tokens': 50, 'output_tokens': 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['judge'] += 1
        return {'text': json.dumps({'relation': 'novel', 'confidence': 0.91, 'reason': '私有内部服务经验'}), 'input_tokens': 80, 'output_tokens': 30}

    return calls, flash, strong, judge


class _ExplodingEmbedder:
    def initialize(self) -> None:
        raise AssertionError('Embedder must not initialize when disabled')

    def encode_document(self, text: str) -> list[float]:
        raise AssertionError('Embedder must not encode when disabled')


class _DummyEmbedder:
    def initialize(self) -> None:
        return None

    def encode_document(self, text: str) -> list[float]:
        vec = [0.0] * 768
        vec[0], vec[1] = 0.8, 0.6
        return vec


def test_live_worker_disabled_cli_and_static_redlines(pg_s3_migrated, tmp_path: Path, monkeypatch, capsys) -> None:
    from evolvmem.experience_v2.pipeline.live_worker import main, run_live_pipeline_tick

    conn, *_ = pg_s3_migrated

    class _NoCallReader(InMemoryHistoryReader):
        def list_sessions(self, **kwargs):
            raise AssertionError('list_sessions must not run when disabled')

    def _boom(*args, **kwargs):
        raise AssertionError('LLM transport must not run when disabled')

    for pol in (
        ProductPolicy(experience_pipeline_enabled=False, valid=True),
        ProductPolicy(experience_pipeline_enabled=True, valid=False),
    ):
        assert run_live_pipeline_tick(
            conn, history_reader=_NoCallReader([]), since='2026-11-14T10:00:00Z',
            owner_id='owner-lw', policy=pol, flash_transport=_boom, embedder=_ExplodingEmbedder(),
        ) == {'status': 'disabled', 'llm_calls': 0}

    pol_path = tmp_path / 'pol.json'
    pol_path.write_text(json.dumps({'experience_pipeline_enabled': False}), encoding='utf-8')
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(pol_path))
    assert main(['--since', '2026-11-14T10:00:00Z']) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {'status': 'disabled', 'llm_calls': 0}

    root = Path(__file__).resolve().parents[2]
    assert scan_direct_sqlite_in_pipeline(root / 'evolvmem' / 'experience_v2' / 'pipeline') == []
    assert scan_unmetered_llm_calls(root / 'evolvmem' / 'experience_v2') == []
    assert scan_unauthorized_experience_notes_writes(root) == []


def test_live_worker_cutover_floor_db_owner_resolution_and_idempotency(pg_s3_migrated, monkeypatch) -> None:
    from evolvmem.experience_v2.pipeline.live_worker import run_live_pipeline_tick

    conn, *_ = pg_s3_migrated
    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_OWNER_ID', raising=False)
    secret = b'lw-test-secret'
    tkt = TicketIssuer(secret).issue(proposal_uid='p-lw-1', decision_id='d-lw-1', lane='user_fast')
    ExperienceWriter(secret).apply(conn, tkt, Op(
        action='create', owner_id='db-owner', actor_id='db-owner', request_id='lw-seed-0001',
        category='系统环境经验', problem='P', solution='S', reusable_experience='R',
    ))

    reader = InMemoryHistoryReader([
        _make_live_session('archive:8101', created_at='2026-11-14 09:59:59'),
        _make_live_session('archive:8102', created_at='2026-11-14T09:50:00Z'),
        _make_live_session('archive:8103', created_at='2026-11-14 10:05:00'),
    ])
    calls, flash, strong, judge = _live_fake_transports()

    with pytest.raises(ValueError):
        run_live_pipeline_tick(
            conn, history_reader=reader, since='',
            policy=ProductPolicy(experience_pipeline_enabled=True, experience_gate_mode='review'),
            flash_transport=flash, strong_transport=strong, judge_transport=judge, embedder=_DummyEmbedder(),
        )

    res1 = run_live_pipeline_tick(
        conn, history_reader=reader, since='2026-11-14T10:00:00Z',
        policy=ProductPolicy(experience_pipeline_enabled=True, experience_gate_mode='review'),
        now=datetime(2026, 11, 14, 10, 10, 0, tzinfo=timezone.utc),
        flash_transport=flash, strong_transport=strong, judge_transport=judge,
        embedder=_DummyEmbedder(), ticket_secret=secret,
    )
    assert res1['status'] == 'completed'
    assert res1['skipped_pre_cutover'] == 2
    assert res1['processed'] == 1
    assert res1['proposals'] == 1
    assert res1['llm_calls'] > 0

    assert conn.execute(
        'SELECT session_id, source_track, source_kind, state FROM experience_proposals WHERE owner_id = %s', ('db-owner',),
    ).fetchall() == [('archive:8103', 'live', 'pipeline', 'review')]
    assert conn.execute("SELECT COUNT(*) FROM experience_notes WHERE owner_id = %s", ('db-owner',)).fetchone()[0] == 1
    assert conn.execute(
        'SELECT session_id, track, status FROM pipeline_segment_runs WHERE owner_id = %s', ('db-owner',),
    ).fetchall() == [('archive:8103', 'live', 'completed')]

    calls_after = dict(calls)
    res2 = run_live_pipeline_tick(
        conn, history_reader=reader, since='2026-11-14 10:00:00',
        policy=ProductPolicy(experience_pipeline_enabled=True, experience_gate_mode='review'),
        now=datetime(2026, 11, 14, 10, 15, 0, tzinfo=timezone.utc),
        flash_transport=flash, strong_transport=strong, judge_transport=judge,
        embedder=_DummyEmbedder(), ticket_secret=secret,
    )
    assert res2['already_processed'] == 1
    assert res2['llm_calls'] == 0
    assert calls == calls_after
