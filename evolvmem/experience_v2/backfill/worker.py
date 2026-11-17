"""Stage 4 / Backfill Track: Nighttime automated historical backfill worker & CLI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Sequence

from evolvmem.experience_v2.backfill.cursor import (
    BackfillCursorStore,
    execute_scoped_backfill,
    validate_backfill_scope,
)
from evolvmem.experience_v2.backfill.queue import BackfillQueue, _norm_utc_iso
from evolvmem.experience_v2.backfill.runner import BackfillRunner, DEFAULT_BACKFILL_PIPELINE_VERSION
from evolvmem.experience_v2.llm_meter import BudgetConfig
from evolvmem.experience_v2.pg_experience_store import load_pg_experience_settings, open_experience_conn
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader, open_history_reader
from evolvmem.experience_v2.pipeline.live_worker import (
    _LazyEmbedder,
    _resolve_transports,
    _try_parse_utc,
    parse_utc_timestamp,
    resolve_owner_id,
)
from evolvmem.experience_v2.pipeline.scheduler import evaluate_circuit_breaker
from evolvmem.product_policy import ProductPolicy, load_policy

_MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])\Z')
BACKFILL_NIGHTLY_BUDGET = BudgetConfig(
    live_daily_tokens=2_000_000,
    backfill_daily_tokens=3_000_000,
    session_max_tokens=30_000,
)


def _scan_pre_cutover_inventory(
    history_reader: HistoryReader,
    *,
    before_dt: datetime,
    min_month: str = '2020-01',
    page_size: int = 2000,
) -> tuple[list[str], frozenset[str]]:
    """Collect valid pre-cutover YYYY-MM months newest-first and post-cutover source_refs to exclude."""
    offset = 0
    step = max(100, int(page_size))
    seen_refs: set[str] = set()
    post_cutover: set[str] = set()
    months: set[str] = set()
    while True:
        page = history_reader.list_sessions(limit=step, offset=offset, dedupe_logical_sessions=True)
        if not page:
            break
        for summ in page:
            if summ.source_ref in seen_refs:
                continue
            seen_refs.add(summ.source_ref)
            dt = _try_parse_utc(getattr(summ, 'created_at', ''))
            if dt is not None and dt >= before_dt:
                post_cutover.add(summ.source_ref)
                continue
            utc_iso = _norm_utc_iso(getattr(summ, 'created_at', ''))
            ym = utc_iso[:7]
            if _MONTH_RE.fullmatch(ym) and ym >= min_month:
                months.add(ym)
        if len(page) < step:
            break
        offset += len(page)
    return sorted(months, reverse=True), frozenset(post_cutover)


def run_backfill_worker_tick(
    conn,
    *,
    history_reader: HistoryReader,
    before: str,
    owner_id: str | None = None,
    policy: ProductPolicy | None = None,
    month: str | None = None,
    project: str | None = None,
    max_sessions: int = 50,
    now: datetime | None = None,
    enforce_night_window: bool = True,
    flash_model: str = 'deepseek-v4-flash',
    strong_model: str = 'deepseek-chat',
    judge_model: str = 'deepseek-chat',
    flash_transport: Callable[..., dict[str, Any]] | None = None,
    strong_transport: Callable[..., dict[str, Any]] | None = None,
    judge_transport: Callable[..., dict[str, Any]] | None = None,
    embedder: Any = None,
    data_dir: Path | None = None,
    ticket_secret: bytes | str = b'stage4-backfill-secret',
    budget: BudgetConfig | None = None,
    pipeline_version: str = DEFAULT_BACKFILL_PIPELINE_VERSION,
) -> dict[str, Any]:
    """Run one bounded nighttime backfill tick over strictly pre-cutover sessions."""
    active_policy = policy if policy is not None else load_policy()
    tick_now = now or datetime.now(timezone.utc)
    gate = BackfillQueue.check_run_gate(
        policy=active_policy, now=tick_now, shadow_mode=False, enforce_night_window=enforce_night_window,
    )
    if not gate['allowed']:
        return {'status': gate['status'], 'processed': 0, 'proposals': 0, 'tokens_spent': 0, 'llm_calls': 0}

    before_dt = parse_utc_timestamp(before)
    owner = resolve_owner_id(conn, owner_id)
    cb_row = conn.execute(
        "SELECT paused, pause_reason FROM pipeline_circuit_state WHERE owner_id = %s AND track = 'backfill'",
        (owner,),
    ).fetchone()
    if cb_row and bool(cb_row[0]):
        return {
            'status': 'circuit_open',
            'reason': str(cb_row[1] or 'circuit_paused'),
            'processed': 0,
            'proposals': 0,
            'tokens_spent': 0,
            'llm_calls': 0,
        }

    months_desc, post_cutover_refs = _scan_pre_cutover_inventory(history_reader, before_dt=before_dt)
    target_project: str | None = None
    target_month: str | None = None
    if project is not None or month is not None:
        sk, sv = validate_backfill_scope(project=project, month=month)
        if sk == 'project':
            target_project = sv
        else:
            target_month = sv
    else:
        cursor_store = BackfillCursorStore(owner_id=owner)
        for cand_month in months_desc:
            cur = cursor_store.get_cursor(conn, scope_kind='month', scope_value=cand_month, is_shadow=False)
            if not (cur and cur.get('completed')):
                target_month = cand_month
                break
        if target_month is None:
            return {'status': 'all_scopes_completed', 'processed': 0, 'proposals': 0, 'tokens_spent': 0, 'llm_calls': 0}

    f_tr, s_tr, j_tr = _resolve_transports(
        flash_model, strong_model, judge_model, flash_transport, strong_transport, judge_transport,
    )
    raw_mode = active_policy.experience_gate_mode
    gate_mode: str | dict[str, str] = raw_mode if isinstance(raw_mode, (str, dict)) else 'review'
    owned_embedder = _LazyEmbedder(data_dir=data_dir) if embedder is None else None
    max_call_before = int(
        conn.execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls WHERE track = 'backfill' AND is_shadow = false").fetchone()[0]
    )

    try:
        runner = BackfillRunner(
            history_reader=history_reader,
            policy=active_policy,
            owner_id=owner,
            flash_model=flash_model,
            strong_model=strong_model,
            judge_model=judge_model,
            flash_transport=f_tr,
            strong_transport=s_tr,
            judge_transport=j_tr,
            embedder=owned_embedder if owned_embedder is not None else embedder,
            pipeline_version=pipeline_version,
            ticket_secret=ticket_secret,
            budget=budget or BACKFILL_NIGHTLY_BUDGET,
            gate_mode=gate_mode,
        )
        res = execute_scoped_backfill(
            conn,
            runner=runner,
            project=target_project,
            month=target_month,
            max_sessions=max(1, int(max_sessions)),
            exclude_source_refs=post_cutover_refs,
            now=tick_now,
            shadow_mode=False,
            enforce_night_window=enforce_night_window,
        )
        usage = conn.execute(
            "SELECT COUNT(*)::int, COALESCE(SUM(input_tokens + output_tokens), 0)::int FROM llm_calls "
            "WHERE track = 'backfill' AND is_shadow = false AND id > %s",
            (max_call_before,),
        ).fetchone()
        res['llm_calls'] = int(usage[0] or 0) if usage else 0
        res['tokens_spent'] = int(usage[1] or 0) if usage else 0
        res['excluded_post_cutover'] = len(post_cutover_refs)
        if res['llm_calls'] > 0 or int(res.get('proposals') or 0) > 0:
            evaluate_circuit_breaker(conn, owner_id=owner, track='backfill')
        return res
    finally:
        if owned_embedder is not None:
            owned_embedder.close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for scheduled Stage 4 nighttime historical backfill ticks."""
    parser = argparse.ArgumentParser(description='EvolvMem nighttime historical backfill worker')
    parser.add_argument('--before', required=True, help='UTC cutover floor timestamp; only sessions < before run here')
    parser.add_argument('--max-sessions', type=int, default=50, help='Maximum historical sessions per tick')
    parser.add_argument('--data-dir', default='~/.claude/evolvmem', help='EvolvMem data directory')
    parser.add_argument('--month', default=None, help='Optional fixed YYYY-MM month scope')
    parser.add_argument('--project', default=None, help='Optional fixed project scope')
    args = parser.parse_args(list(argv) if argv is not None else None)

    policy = load_policy()
    gate = BackfillQueue.check_run_gate(
        policy=policy, now=datetime.now(timezone.utc), shadow_mode=False, enforce_night_window=True,
    )
    if not gate['allowed']:
        print(json.dumps({'status': gate['status'], 'processed': 0, 'tokens_spent': 0, 'llm_calls': 0}, ensure_ascii=False))
        return 0

    data_dir = Path(args.data_dir).expanduser()
    cand_env = (os.environ.get('EVOLVMEM_HISTORY_CANDIDATE_DB') or '').strip()
    reader = open_history_reader(data_dir, candidate_db=Path(cand_env).expanduser() if cand_env else None, policy=policy)
    settings = load_pg_experience_settings()
    with open_experience_conn(settings, role='coordinator') as conn:
        result = run_backfill_worker_tick(
            conn,
            history_reader=reader,
            before=args.before,
            policy=policy,
            month=args.month,
            project=args.project,
            max_sessions=args.max_sessions,
            data_dir=data_dir,
            ticket_secret=settings.ticket_secret,
        )
        conn.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
