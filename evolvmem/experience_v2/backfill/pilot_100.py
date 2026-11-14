"""Task B.5: 100-session single-scope historical backfill shadow pilot & scrubbed report."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Callable, Sequence
import uuid

from evolvmem.experience_v2.backfill.cursor import execute_scoped_backfill, validate_backfill_scope
from evolvmem.experience_v2.backfill.runner import BackfillRunner, DEFAULT_BACKFILL_PIPELINE_VERSION
from evolvmem.experience_v2.llm_meter import BudgetConfig
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader
from evolvmem.experience_v2.pipeline.pilot import (
    _collect_stage_usage,
    _select_user_samples,
    scrub_pilot_text,
)
from evolvmem.experience_v2.pipeline.pilot_context import enrich_pilot_samples
from evolvmem.product_policy import ProductPolicy, load_policy


def run_backfill_pilot_batch(
    conn,
    *,
    history_reader: HistoryReader,
    owner_id: str,
    project: str | None = None,
    month: str | None = None,
    limit: int = 100,
    exclude_source_refs: set[str] | frozenset[str] = frozenset(),
    flash_model: str,
    strong_model: str,
    judge_model: str,
    flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    judge_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    embedder: Any = None,
    pipeline_version: str = DEFAULT_BACKFILL_PIPELINE_VERSION,
    budget: BudgetConfig | None = None,
    policy: ProductPolicy | None = None,
    run_id: str | None = None,
    sample_size: int = 100,
    extra_scrub_terms: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one scoped historical shadow backfill batch (default 100 sessions) and build scrubbed report."""
    scope_kind, scope_value = validate_backfill_scope(project=project, month=month)
    rid = run_id or f'bf-pilot-{uuid.uuid4().hex[:12]}'
    pol = policy if policy is not None else load_policy()
    active_budget = budget or BudgetConfig(backfill_daily_tokens=3_000_000)

    props_before = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    notes_before = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])
    max_call_row = conn.execute('SELECT COALESCE(MAX(id), 0) FROM llm_calls').fetchone()
    after_call_id = int(max_call_row[0]) if max_call_row else 0

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO shadow_pilot_runs(run_id, owner_id, track, pipeline_version)
            VALUES (%s, %s, 'backfill', %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (rid, owner_id, pipeline_version),
        )

    runner = BackfillRunner(
        history_reader=history_reader,
        policy=pol,
        owner_id=owner_id,
        flash_model=flash_model,
        strong_model=strong_model,
        judge_model=judge_model,
        flash_transport=flash_transport,
        strong_transport=strong_transport,
        judge_transport=judge_transport,
        embedder=embedder,
        pipeline_version=pipeline_version,
        budget=active_budget,
    )

    batch_res = execute_scoped_backfill(
        conn,
        runner=runner,
        project=project,
        month=month,
        max_sessions=max(1, int(limit)),
        exclude_source_refs=exclude_source_refs,
        now=datetime.now(timezone.utc),
        shadow_mode=True,
        shadow_run_id=rid,
        enforce_night_window=False,
    )

    rows = batch_res.get('results', [])
    sessions_total = len(rows)
    zero_moments = 0
    moments_detected = 0
    adapter_breakdown: dict[str, int] = {}
    processed_refs: list[str] = []
    for entry in rows:
        job = entry['job']
        res = entry['result']
        sref = str(job['source_ref'])
        processed_refs.append(sref)
        sess = history_reader.get_session(sref)
        adp = sess.summary.adapter if sess is not None else 'unknown'
        adapter_breakdown[adp] = adapter_breakdown.get(adp, 0) + 1
        if res.get('status') == 'skipped_zero_moments':
            zero_moments += 1
        moments_detected += int(res.get('moments') or 0)

    item_rows = conn.execute(
        'SELECT stage_outcome, gate_layer, gate_verdict FROM shadow_pilot_items WHERE run_id = %s',
        (rid,),
    ).fetchall()
    proposals_generated = 0
    admitted_new = 0
    merged_dup = 0
    skipped_extract = 0
    rejected_cnt = 0
    for outcome, layer, verdict in item_rows:
        if outcome == 'skipped_extract':
            skipped_extract += 1
            continue
        proposals_generated += 1
        if layer == 'L3' and verdict == 'pass':
            admitted_new += 1
        elif verdict == 'merge':
            merged_dup += 1
        else:
            rejected_cnt += 1
    admitted_cand = admitted_new + merged_dup

    by_stage, calls_total, tokens_total = _collect_stage_usage(conn, track='backfill', after_call_id=after_call_id)
    props_after = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    notes_after = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])

    extra_terms = [x for x in (os.environ.get('USER', ''), os.environ.get('USERNAME', ''), *extra_scrub_terms) if x]
    raw_samples = _select_user_samples(conn, run_id=rid, sample_size=max(20, int(sample_size)), extra_scrub_terms=extra_terms)
    enriched = enrich_pilot_samples(
        raw_samples,
        history_reader=history_reader,
        run_id=rid,
        gate_traces=runner.scheduler.shadow_gate_traces,
        scrub_fn=scrub_pilot_text,
        extra_scrub_terms=extra_terms,
    )
    admitted_items = [s for s in enriched if (s.get('gate_layer') == 'L3' and s.get('gate_verdict') == 'pass') or s.get('gate_verdict') == 'merge']
    intercepted_items = [s for s in enriched if s not in admitted_items]

    zero_ratio = round(zero_moments / sessions_total, 4) if sessions_total else 0.0
    tok_per_prop = round(tokens_total / proposals_generated, 2) if proposals_generated else 0.0
    tok_per_admit = round(tokens_total / admitted_cand, 2) if admitted_cand else None

    report = {
        'run_id': rid,
        'track': 'backfill',
        'scope_kind': scope_kind,
        'scope_value': scrub_pilot_text(scope_value, extra_terms=extra_terms),
        'pipeline_version': pipeline_version,
        'models': {'flash': flash_model, 'strong': strong_model, 'judge': judge_model},
        'sessions_total': sessions_total,
        'sessions_zero_moments': zero_moments,
        'zero_moment_session_ratio': zero_ratio,
        'moments_detected': moments_detected,
        'adapter_breakdown': adapter_breakdown,
        'proposals_generated': proposals_generated,
        'proposals_admitted_new': admitted_new,
        'proposals_merged_duplicate': merged_dup,
        'proposals_admitted_candidate': admitted_cand,
        'items_skipped_extract': skipped_extract,
        'items_rejected_gate': rejected_cnt,
        'llm_calls_total': calls_total,
        'tokens_total': tokens_total,
        'tokens_per_proposal': tok_per_prop,
        'tokens_per_admitted_candidate': tok_per_admit,
        'llm_usage_by_stage': by_stage,
        'cursor_checkpoint': batch_res.get('cursor'),
        'processed_source_refs': processed_refs,
        'live_tables_mutated': (props_after != props_before) or (notes_after != notes_before),
        'policy_guard': {
            'experience_backfill_enabled': bool(pol.experience_backfill_enabled),
            'ai_processing_enabled': bool(pol.ai_processing_enabled),
        },
        'admitted_or_merged_items': admitted_items,
        'intercepted_items': intercepted_items,
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }

    with conn.transaction():
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
                sessions_total, zero_moments, moments_detected, proposals_generated,
                admitted_cand, tokens_total, calls_total,
                json.dumps(report, ensure_ascii=False), rid,
            ),
        )
    return report
