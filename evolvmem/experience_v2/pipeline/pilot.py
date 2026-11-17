"""Task 3.9: 50-session shadow pilot runner, cost reporter, PII scrubber & R9 drill."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Callable, Literal, Sequence
import uuid

from evolvmem.experience_v2.llm_meter import BudgetConfig, LlmTransportError
from evolvmem.experience_v2.pipeline.pilot_context import enrich_pilot_samples
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistoryReader,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
)
from evolvmem.experience_v2.pipeline.scheduler import (
    DEFAULT_PIPELINE_VERSION,
    PipelineScheduler,
    evaluate_circuit_breaker,
)
from evolvmem.product_policy import ProductPolicy, load_policy

STAGE_ORDER: tuple[str, ...] = (
    'extract',
    'l2a_predict',
    'l2a_judge',
    'l2b_predict',
    'l2b_judge',
    'l3_reconcile',
)

_IGNORED_ENV_USERS = frozenset({'root', 'app', 'user', 'admin', 'administrator', 'ubuntu', 'runner', 'nobody', 'system'})
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'Bearer\s+[A-Za-z0-9._-]+', re.IGNORECASE), 'Bearer [REDACTED]'),
    (re.compile(r'sk-[A-Za-z0-9_-]{8,}'), '[REDACTED_KEY]'),
    (re.compile(r'(?:api[-_]?key|token|password|secret)\s*[:=]\s*["\']?[A-Za-z0-9._-]{6,}["\']?', re.IGNORECASE), '[REDACTED_CRED]'),
    (re.compile(r'/home/[A-Za-z0-9._-]+'), '/home/<owner>'),
    (re.compile(r'[A-Za-z]:(?:\\+|/+)Users(?:\\+|/+)[A-Za-z0-9._-]+', re.IGNORECASE), r'C:\\Users\\<owner>'),
    (re.compile(r'github\.com([:/])(?!<owner>)[A-Za-z0-9._-]+', re.IGNORECASE), r'github.com\1<owner>'),
    (re.compile(r'(?<![A-Za-z0-9])win-(?:[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4}){0,3}(?:-[0-9a-fA-F]{12})?(?:…|\.{3})?|[0-9a-fA-F]{6,}(?:…|\.{3}))(?![A-Za-z0-9-])', re.IGNORECASE), '<device-id>'),
    (re.compile(r'(?<![A-Za-z0-9])(?:DESKTOP|LAPTOP)-[A-Za-z0-9]{5,}(?:-[A-Za-z0-9]{2,})*(?:…|\.{3})?(?![A-Za-z0-9-])', re.IGNORECASE), '<host-id>'),
    (re.compile(r"(device[-_]?id\s*[:=]\s*[\"']?)(?!<device-id>)[A-Za-z0-9._-]{8,}([\"']?)", re.IGNORECASE), r'\1<device-id>\2'),
    (re.compile(r"https?://[A-Za-z0-9.-]*(?:xlwms|lingdongsz|lingxing)\.com(?::\d+)?(?:/[^\s\"'<>，。；;）)\]]*)?", re.IGNORECASE), '<internal-url>'),
    (re.compile(r'(?<![A-Za-z0-9.-])[A-Za-z0-9.-]*(?:xlwms|lingdongsz|lingxing)\.com(?![A-Za-z0-9-])', re.IGNORECASE), '<internal-host>'),
    (re.compile(r"https?://(?:omp|xinyue|xpmcp|xp)(?:\.[A-Za-z0-9.-]*(?:/[^\s\"'<>，。；;）)\]]*)?|…|\.{3})", re.IGNORECASE), '<internal-url>'),
    (re.compile(r"https?://(?!(?:open|www|docs)\.)[A-Za-z0-9-]{8,}\.feishu\.cn(?:/[^\s\"'<>，。；;）)\]]*)?", re.IGNORECASE), 'https://<tenant>.feishu.cn'),
    (re.compile(r'(?!(?:open|www|docs)\.)(?<![A-Za-z0-9.-])[A-Za-z0-9-]{8,}\.feishu\.cn(?![A-Za-z0-9-])', re.IGNORECASE), '<tenant>.feishu.cn'),
    (re.compile(r"https?://acnv6w0[A-Za-z0-9.-]*(?:/[^\s\"'<>，。；;）)\]]*)?", re.IGNORECASE), 'https://<tenant>.feishu.cn'),
    (re.compile(r'(?:账号|用户名|账户)\s*[:：]?\s*(?!\[REDACTED\])[A-Za-z0-9._@-]{2,}'), '账号 [REDACTED]'),
    (re.compile(r'尾号\s*\d{4}'), '尾号[REDACTED]'),
    (re.compile(r'(?<!\d)1[356789]\d{9}(?!\d)'), '[REDACTED_PHONE]'),
    (re.compile(r'(?!(?:git|nobody|noreply)@)[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'), '<email>'),
    (re.compile(r'(?<![0-9.])192\.168\.\d{1,3}\.\d{1,3}(?![0-9.])'), '<lan-host>'),
    (re.compile(r'(?<![0-9.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![0-9.])'), '<lan-host>'),
    (re.compile(r'(?<![0-9.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![0-9.])'), '<lan-host>'),
    (re.compile(r'(?<![0-9.])127\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![0-9.])'), '<local-host>'),
    (re.compile(r"(?<![0-9.])192\.(?:168(?:\.\d{1,3})?|\d{0,3})?(?=$|…|\s|[,，。.;；:：\"'()（）\[\]<]|[\u4e00-\u9fff])"), '<lan-host>'),
    (re.compile(r"(?<![0-9.])127\.(?:0(?:\.\d{1,3}){0,2})?(?=$|…|\s|[,，。.;；:：\"'()（）\[\]<]|[\u4e00-\u9fff])"), '<local-host>'),
    (re.compile(r"(https?://)(?:192|127)(?=$|…|[/\s\"'<>])", re.IGNORECASE), r'\1<lan-host>'),
    (re.compile(r"(https?://)(?:192|127|10|1)(?:…|\.{3})", re.IGNORECASE), r'\1<lan-host>'),
    (re.compile(r'(?<![A-Za-z0-9_])(?:ou|oc|on|om|cli)_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}(?:…|\.{3})?(?![A-Za-z0-9_])'), '[REDACTED_FEISHU_ID]'),
)


class _DrillRollback(Exception):
    """Internal sentinel rolling back savepoint after R9 circuit-breaker drill."""


def scrub_pilot_text(text: Any, *, extra_terms: Sequence[str] = ()) -> str:
    """Redact credentials, internal URLs, home dirs, private/truncated IPs and owner names."""
    out = str(text or '')
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    env_terms = [
        v for k in ('USER', 'USERNAME')
        if (v := str(os.environ.get(k, '')).strip()) and v.lower() not in _IGNORED_ENV_USERS
    ]
    for term in (*env_terms, *extra_terms):
        clean = str(term or '').strip()
        if len(clean) >= 2:
            out = re.sub(re.escape(clean), '<owner>', out, flags=re.IGNORECASE)
    return out


def simulate_circuit_breaker_drill(conn, *, owner_id: str) -> dict[str, Any]:
    """Simulate R9 3-day budget overrun inside a rolled-back savepoint and verify deferral."""
    dummy_sid = f'drill-{uuid.uuid4().hex[:8]}'
    dummy_sess = HistorySession(
        summary=HistorySessionSummary(
            session_id=dummy_sid,
            adapter='codex',
            source_format='codex-jsonl',
            project='drill',
            source_ref=dummy_sid,
            library='drill',
            external_session_id=dummy_sid,
            created_at=datetime.now(timezone.utc).isoformat(),
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=1, role='user', content='Always prefer uv over pip.'),
            HistoryRawRecord(event_index=2, role='tool', content='exit_code=1\nTimeoutError'),
            HistoryRawRecord(event_index=3, role='tool', content='exit_code=0\nok'),
        ),
    )

    def noop_transport(model: str, messages: list[dict[str, Any]], max_output_tokens: int) -> dict[str, Any]:
        return {'text': '{}', 'input_tokens': 1, 'output_tokens': 1}

    cb_res: dict[str, Any] = {}
    defer_res: dict[str, Any] = {}
    try:
        with conn.transaction():
            cb_res = evaluate_circuit_breaker(
                conn,
                owner_id=owner_id,
                track='live',
                daily_token_per_note=[
                    ('2026-11-11', 70_000, 1),
                    ('2026-11-12', 140_000, 2),
                    ('2026-11-13', 65_000, 1),
                ],
            )
            sched = PipelineScheduler(
                history_reader=InMemoryHistoryReader([dummy_sess]),
                policy=ProductPolicy(experience_pipeline_enabled=True),
                owner_id=owner_id,
                flash_model='deepseek-v4-flash',
                strong_model='deepseek-chat',
                judge_model='deepseek-reasoner',
                flash_transport=noop_transport,
            )
            now = datetime.now(timezone.utc)
            defer_res = sched.process_session_if_ready(
                conn, dummy_sid, is_closed=True, last_message_at=now, now=now, shadow_mode=True, shadow_run_id='drill',
            )
            raise _DrillRollback()
    except _DrillRollback:
        pass

    paused_ok = bool(cb_res.get('paused'))
    deferred_ok = defer_res.get('status') == 'deferred' and defer_res.get('reason') == 'circuit_paused'
    alerts = cb_res.get('alerts') or []
    return {
        'passed': bool(paused_ok and deferred_ok and alerts),
        'paused_triggered': paused_ok,
        'deferred_on_paused': deferred_ok,
        'defer_status': defer_res.get('status', ''),
        'defer_reason': defer_res.get('reason', ''),
        'alerts': alerts,
    }


def _collect_stage_usage(conn, *, track: str, after_call_id: int) -> tuple[dict[str, dict[str, Any]], int, int]:
    rows = conn.execute(
        """
        SELECT stage,
               COUNT(*)::int,
               COALESCE(SUM(input_tokens), 0)::int,
               COALESCE(SUM(output_tokens), 0)::int,
               COALESCE(SUM(cost_estimate), 0)::float
        FROM llm_calls
        WHERE is_shadow = true AND track = %s AND id > %s
        GROUP BY stage
        """,
        (track, after_call_id),
    ).fetchall()
    by_stage: dict[str, dict[str, Any]] = {
        st: {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'tokens': 0, 'cost_estimate': 0.0}
        for st in STAGE_ORDER
    }
    calls_total = 0
    tokens_total = 0
    for st, cnt, in_tok, out_tok, cost in rows:
        tok = int(in_tok) + int(out_tok)
        by_stage[str(st)] = {
            'calls': int(cnt),
            'input_tokens': int(in_tok),
            'output_tokens': int(out_tok),
            'tokens': tok,
            'cost_estimate': round(float(cost), 6),
        }
        calls_total += int(cnt)
        tokens_total += tok
    return by_stage, calls_total, tokens_total


def _select_user_samples(
    conn,
    *,
    run_id: str,
    sample_size: int,
    extra_scrub_terms: Sequence[str],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, session_id, moment_kind, stage_outcome, category, kind,
               problem, solution, reusable_experience, misbelief, evidence_json,
               gate_layer, gate_verdict, reason_code, tokens_used
        FROM shadow_pilot_items
        WHERE run_id = %s
        ORDER BY
          CASE
            WHEN gate_layer = 'L3' AND gate_verdict = 'pass' THEN 0
            WHEN gate_verdict = 'merge' THEN 1
            WHEN gate_layer IN ('L2b', 'L2a', 'L0') THEN 2
            ELSE 3
          END ASC,
          id ASC
        LIMIT %s
        """,
        (run_id, max(0, int(sample_size))),
    ).fetchall()
    selected_ids = [int(r[0]) for r in rows]
    if selected_ids:
        conn.execute(
            'UPDATE shadow_pilot_items SET sample_for_user_review = true WHERE id = ANY(%s)',
            (selected_ids,),
        )
    samples: list[dict[str, Any]] = []
    for rank, r in enumerate(rows, 1):
        ev_raw = r[10] if isinstance(r[10], list) else json.loads(r[10] or '[]')
        ev = [{k: scrub_pilot_text(v, extra_terms=extra_scrub_terms) if isinstance(v, str) else v for k, v in item.items()} if isinstance(item, dict) else item for item in ev_raw]
        samples.append({
            'sample_index': rank,
            'item_id': int(r[0]),
            '_raw_session_id': str(r[1]),
            '_raw_problem': str(r[6] or ''),
            'session_ref': scrub_pilot_text(r[1], extra_terms=extra_scrub_terms),
            'moment_kind': str(r[2]),
            'stage_outcome': str(r[3]),
            'category': str(r[4] or ''),
            'kind': str(r[5] or ''),
            'problem': scrub_pilot_text(r[6], extra_terms=extra_scrub_terms),
            'solution': scrub_pilot_text(r[7], extra_terms=extra_scrub_terms),
            'reusable_experience': scrub_pilot_text(r[8], extra_terms=extra_scrub_terms),
            'misbelief': scrub_pilot_text(r[9], extra_terms=extra_scrub_terms),
            'evidence': ev,
            'gate_layer': str(r[11] or ''),
            'gate_verdict': str(r[12] or ''),
            'reason_code': scrub_pilot_text(r[13], extra_terms=extra_scrub_terms),
            'tokens_used': int(r[14] or 0),
        })
    return samples


def run_shadow_pilot(
    conn,
    *,
    history_reader: HistoryReader,
    owner_id: str,
    flash_model: str,
    strong_model: str,
    judge_model: str,
    flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    judge_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    limit: int = 50,
    sample_size: int = 20,
    run_id: str | None = None,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    track: Literal['live', 'backfill'] = 'live',
    budget: BudgetConfig | None = None,
    embedder: Any = None,
    extra_scrub_terms: Sequence[str] = (),
) -> dict[str, Any]:
    """Execute 50-session shadow pilot strictly into shadow tables and build pilot-report."""
    rid = run_id or f'pilot-{uuid.uuid4().hex[:12]}'
    props_before = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    notes_before = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])
    max_call_before = int(conn.execute('SELECT COALESCE(MAX(id), 0) FROM llm_calls').fetchone()[0])

    conn.execute(
        """
        INSERT INTO shadow_pilot_runs(run_id, owner_id, track, pipeline_version)
        VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING
        """,
        (rid, owner_id, track, pipeline_version),
    )

    sessions = history_reader.list_sessions(limit=limit)
    scheduler = PipelineScheduler(
        history_reader=history_reader,
        policy=ProductPolicy(experience_pipeline_enabled=False),
        owner_id=owner_id,
        flash_model=flash_model,
        strong_model=strong_model,
        judge_model=judge_model,
        flash_transport=flash_transport,
        strong_transport=strong_transport,
        judge_transport=judge_transport,
        embedder=embedder,
        pipeline_version=pipeline_version,
        track=track,
        budget=budget,
    )

    zero_moments = 0
    moments_detected = 0
    transport_errors: list[dict[str, str]] = []
    adapter_counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    for summ in sessions:
        adapter_counts[summ.adapter] = adapter_counts.get(summ.adapter, 0) + 1
        try:
            res = scheduler.process_session_if_ready(
                conn,
                summ.source_ref,
                is_closed=True,
                last_message_at=now,
                now=now,
                shadow_mode=True,
                shadow_run_id=rid,
            )
            if res.get('status') == 'skipped_zero_moments':
                zero_moments += 1
            else:
                moments_detected += int(res.get('moments') or 0)
        except LlmTransportError as exc:
            transport_errors.append({'session_ref': summ.source_ref, 'error': str(exc)})

    item_stats = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE stage_outcome != 'skipped_extract')::int,
               COUNT(*) FILTER (WHERE gate_layer = 'L3' AND gate_verdict = 'pass')::int,
               COUNT(*) FILTER (WHERE gate_verdict = 'merge')::int,
               COUNT(*) FILTER (WHERE gate_verdict = 'reject')::int
        FROM shadow_pilot_items
        WHERE run_id = %s
        """,
        (rid,),
    ).fetchone()
    proposals_generated = int(item_stats[0] or 0) if item_stats else 0
    proposals_admitted_new = int(item_stats[1] or 0) if item_stats else 0
    proposals_merged_dup = int(item_stats[2] or 0) if item_stats else 0
    proposals_admitted = proposals_admitted_new + proposals_merged_dup
    items_rejected = int(item_stats[3] or 0) if item_stats else 0

    by_stage, calls_total, tokens_total = _collect_stage_usage(conn, track=track, after_call_id=max_call_before)
    samples = _select_user_samples(
        conn, run_id=rid, sample_size=sample_size, extra_scrub_terms=extra_scrub_terms,
    )
    samples = enrich_pilot_samples(
        samples,
        history_reader=history_reader,
        run_id=rid,
        gate_traces=scheduler.shadow_gate_traces,
        scrub_fn=scrub_pilot_text,
        extra_scrub_terms=extra_scrub_terms,
    )
    drill = simulate_circuit_breaker_drill(conn, owner_id=f'{owner_id}-drill')

    total_sess = len(sessions)
    zero_ratio = round(zero_moments / max(1, total_sess), 4)
    tok_per_prop = round(tokens_total / proposals_generated, 2) if proposals_generated else 0.0
    tok_per_new = round(tokens_total / proposals_admitted_new, 2) if proposals_admitted_new else 0.0
    tok_per_admit = round(tokens_total / proposals_admitted, 2) if proposals_admitted else 0.0
    policy_snapshot = load_policy()

    props_after = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    notes_after = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])

    report: dict[str, Any] = {
        'run_id': rid,
        'track': track,
        'pipeline_version': pipeline_version,
        'models': {'flash': flash_model, 'strong': strong_model, 'judge': judge_model},
        'sessions_total': total_sess,
        'sessions_zero_moments': zero_moments,
        'zero_moment_session_ratio': zero_ratio,
        'moments_detected': moments_detected,
        'adapter_breakdown': adapter_counts,
        'proposals_generated': proposals_generated,
        'proposals_admitted_new': proposals_admitted_new,
        'proposals_merged_duplicate': proposals_merged_dup,
        'proposals_admitted_candidate': proposals_admitted,
        'items_rejected_or_skipped': items_rejected,
        'llm_calls_total': calls_total,
        'tokens_total': tokens_total,
        'tokens_per_proposal': tok_per_prop,
        'tokens_per_new_candidate': tok_per_new,
        'tokens_per_admitted_candidate': tok_per_admit,
        'llm_usage_by_stage': by_stage,
        'transport_errors': transport_errors,
        'circuit_breaker_drill': drill,
        'live_tables_mutated': (props_after != props_before) or (notes_after != notes_before),
        'samples_for_user_review': samples,
        'user_review': {
            'status': 'pending_user_approval',
            'sample_count': len(samples),
            'reviewed_count': 0,
            'approved_count': 0,
            'user_pass_rate': None,
            'experience_pipeline_enabled': bool(policy_snapshot.experience_pipeline_enabled),
            'ai_processing_enabled': bool(policy_snapshot.ai_processing_enabled),
        },
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }

    conn.execute(
        """
        UPDATE shadow_pilot_runs
        SET sessions_total = %s,
            sessions_zero_moments = %s,
            moments_detected = %s,
            proposals_generated = %s,
            proposals_admitted_candidate = %s,
            tokens_total = %s,
            llm_calls_total = %s,
            report_json = %s::jsonb
        WHERE run_id = %s
        """,
        (
            total_sess, zero_moments, moments_detected, proposals_generated,
            proposals_admitted, tokens_total, calls_total,
            json.dumps(report, ensure_ascii=False), rid,
        ),
    )
    return report
