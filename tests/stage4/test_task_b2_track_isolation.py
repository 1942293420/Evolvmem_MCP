"""TDD tests for Backfill Track Task B.2: strict 5-dimension live vs backfill track isolation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from evolvmem.experience_v2.backfill.runner import BackfillRunner
from evolvmem.experience_v2.gate.review_inbox import ReviewInboxService
from evolvmem.experience_v2.llm_meter import BudgetConfig
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
)
from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler, evaluate_circuit_breaker
from evolvmem.product_policy import ProductPolicy


class _FakeEmbedder:
    def encode_document(self, text: str) -> list[float]:
        v = [0.0] * 768
        v[abs(hash(text)) % 16] = 1.0
        return v


def _make_session(sid: str) -> HistorySession:
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid, adapter='codex', source_format='codex-jsonl',
            project='iso-app', source_ref=sid, library='dev',
            external_session_id=sid, created_at='2026-10-12T08:00:00Z',
        ),
        records=(
            HistoryRawRecord(event_index=1, role='user', content='Remember internal rule: billing SKU lock requires tenant flag.'),
            HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nTenantLockMissing'),
            HistoryRawRecord(event_index=3, role='assistant', content='Fixed with tenant lock header.'),
            HistoryRawRecord(event_index=4, role='tool', content='exit_code=0\nok'),
        ),
    )


def _transports(problem_prefix: str):
    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]['content']
        if '经验提炼器' in sys_p:
            return {
                'text': json.dumps({
                    'category': '业务经验',
                    'problem': f'{problem_prefix}: 私有计费锁缺少租户标识报错',
                    'solution': '调用内部计费网关前必须先核验并附加 tenant_lock 头部字段',
                    'reusable_experience': '私有计费写操作必须校验并透传租户锁标头',
                    'kind': 'pitfall',
                    'misbelief': '误以为默认请求头会自动带租户锁',
                    'scope': {'system': 'billing'},
                    'skip_reason': '',
                }, ensure_ascii=False),
                'input_tokens': 100,
                'output_tokens': 60,
            }
        if '一致性' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.9, 'detail': 'private rule'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': 'check config', 'input_tokens': 40, 'output_tokens': 20}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]['content']
        if '一致性' in sys_p or '评审员' in sys_p:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.92, 'detail': 'miss'}), 'input_tokens': 55, 'output_tokens': 20}
        return {'text': 'retry billing request', 'input_tokens': 45, 'output_tokens': 20}

    def judge(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {'text': json.dumps({'relation': 'novel', 'confidence': 0.9, 'reason': 'new rule'}), 'input_tokens': 40, 'output_tokens': 20}

    return flash, strong, judge


def test_five_dimension_isolation_between_live_and_backfill(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    sess = _make_session('shared-sid-1')
    reader = InMemoryHistoryReader([sess])
    secret = b'stage4-b2-isolation-secret'
    policy = ProductPolicy(experience_pipeline_enabled=True, experience_backfill_enabled=True)
    sh_night = datetime(2026, 11, 14, 2, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    now_utc = datetime.now(timezone.utc)

    f_live, s_live, j_live = _transports('LIVE')
    live_sched = PipelineScheduler(
        history_reader=reader, policy=policy, owner_id='owner-iso',
        flash_model='deepseek-v4-flash', strong_model='deepseek-chat', judge_model='deepseek-reasoner',
        flash_transport=f_live, strong_transport=s_live, judge_transport=j_live,
        embedder=_FakeEmbedder(), ticket_secret=secret, track='live', pipeline_version='stage3-v2',
    )
    res_live = live_sched.process_session_if_ready(
        conn, 'shared-sid-1', is_closed=True, last_message_at=now_utc, now=now_utc,
    )
    assert res_live['status'] == 'completed' and res_live['proposals'] == 1

    f_bf, s_bf, j_bf = _transports('BACKFILL')
    bf_runner = BackfillRunner(
        history_reader=reader, policy=policy, owner_id='owner-iso',
        flash_model='deepseek-v4-flash', strong_model='deepseek-chat', judge_model='deepseek-reasoner',
        flash_transport=f_bf, strong_transport=s_bf, judge_transport=j_bf,
        embedder=_FakeEmbedder(), ticket_secret=secret, pipeline_version='stage3-v2',
    )
    bf_runner.queue.enqueue_sessions(conn, scope_kind='month', scope_value='2026-10', sessions=[sess.summary], is_shadow=False)
    res_bf = bf_runner.run_queued_batch(conn, scope_kind='month', scope_value='2026-10', now=sh_night, max_jobs=5)
    assert res_bf['status'] == 'completed' and res_bf['processed'] == 1 and res_bf['proposals'] == 1

    # 1. Database isolation: same session_id tracked independently across tracks
    seg_tracks = {
        r[0] for r in conn.execute(
            "SELECT track FROM pipeline_segment_runs WHERE owner_id = 'owner-iso' AND session_id = 'shared-sid-1'"
        ).fetchall()
    }
    assert seg_tracks == {'live', 'backfill'}

    # 2. Queue isolation: backfill_jobs only tracks backfill jobs
    assert conn.execute("SELECT status FROM backfill_jobs WHERE owner_id = 'owner-iso'").fetchone()[0] == 'completed'

    # 4. Review Inbox tab isolation ('新会话' vs '旧聊天')
    inbox = ReviewInboxService(ticket_secret=secret, shadow_mode=True)
    live_tab = inbox.list_inbox(conn, owner_id='owner-iso', source_track='live')
    bf_tab = inbox.list_inbox(conn, owner_id='owner-iso', source_track='backfill')
    assert len(live_tab['items']) == 1 and live_tab['items'][0]['problem'].startswith('LIVE:')
    assert len(bf_tab['items']) == 1 and bf_tab['items'][0]['problem'].startswith('BACKFILL:')
    assert live_tab['counts'] == {'live': 1, 'backfill': 1}

    # 3. Budget isolation: exhausting backfill_daily_tokens defers backfill without blocking live
    sess_b2 = _make_session('shared-sid-2')
    sess_l2 = _make_session('live-sid-2')
    reader2 = InMemoryHistoryReader([sess_b2, sess_l2])
    tiny_bf_budget = BudgetConfig(live_daily_tokens=2_000_000, backfill_daily_tokens=1, session_max_tokens=30_000)
    bf_runner2 = BackfillRunner(
        history_reader=reader2, policy=policy, owner_id='owner-iso',
        flash_model='deepseek-v4-flash', strong_model='deepseek-chat', judge_model='deepseek-reasoner',
        flash_transport=f_bf, strong_transport=s_bf, judge_transport=j_bf,
        embedder=_FakeEmbedder(), ticket_secret=secret, budget=tiny_bf_budget, pipeline_version='stage3-v2',
    )
    bf_runner2.queue.enqueue_sessions(conn, scope_kind='month', scope_value='2026-10', sessions=[sess_b2.summary], is_shadow=False)
    defer_bf = bf_runner2.run_queued_batch(conn, scope_kind='month', scope_value='2026-10', now=sh_night, max_jobs=5)
    assert defer_bf['deferred'] == 1
    assert conn.execute("SELECT status FROM backfill_jobs WHERE session_id = 'shared-sid-2'").fetchone()[0] == 'deferred'

    live_sched2 = PipelineScheduler(
        history_reader=reader2, policy=policy, owner_id='owner-iso',
        flash_model='deepseek-v4-flash', strong_model='deepseek-chat', judge_model='deepseek-reasoner',
        flash_transport=f_live, strong_transport=s_live, judge_transport=j_live,
        embedder=_FakeEmbedder(), ticket_secret=secret, budget=tiny_bf_budget, track='live', pipeline_version='stage3-v2',
    )
    live_ok = live_sched2.process_session_if_ready(
        conn, 'live-sid-2', is_closed=True, last_message_at=now_utc, now=now_utc,
    )
    assert live_ok['status'] == 'completed'

    # 5. Metrics & circuit-breaker isolation: pausing backfill keeps live active
    evaluate_circuit_breaker(
        conn, owner_id='owner-iso', track='backfill',
        daily_token_per_note=[('2026-11-11', 70_000, 1), ('2026-11-12', 70_000, 1), ('2026-11-13', 70_000, 1)],
    )
    bf_paused = conn.execute("SELECT paused FROM pipeline_circuit_state WHERE owner_id='owner-iso' AND track='backfill'").fetchone()[0]
    live_state = evaluate_circuit_breaker(conn, owner_id='owner-iso', track='live')
    assert bf_paused is True and live_state['paused'] is False
    toks_by_track = dict(conn.execute("SELECT track, SUM(input_tokens + output_tokens) FROM llm_calls GROUP BY track").fetchall())
    assert set(toks_by_track.keys()) == {'live', 'backfill'}
