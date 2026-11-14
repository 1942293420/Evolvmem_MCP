"""Stage 3 Task 3.9: Periodic live-session extraction worker CLI & cutover floor guard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from evolvmem.experience_v2.llm_meter import (
    BudgetConfig, BudgetDeferred, LlmEndpoint, LlmTransportError, default_http_transport, load_llm_endpoints_from_file,
)
from evolvmem.experience_v2.pg_experience_store import load_pg_experience_settings, open_experience_conn
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader, HistorySessionSummary, SqliteHistoryReader
from evolvmem.experience_v2.pipeline.scheduler import DEFAULT_PIPELINE_VERSION, PipelineScheduler, evaluate_circuit_breaker
from evolvmem.product_policy import ProductPolicy, load_policy

_SUMMARY_KEYS = ('completed', 'already_processed', 'skipped_zero_moments', 'active_wait', 'deferred', 'errors')


def parse_utc_timestamp(raw: str | None) -> datetime:
    """Parse an ISO-8601 or SQLite UTC timestamp into timezone-aware UTC."""
    text = str(raw or '').strip()
    if not text:
        raise ValueError('since_timestamp_required')
    norm = text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text
    dt = datetime.fromisoformat(norm)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _try_parse_utc(raw: str | None) -> datetime | None:
    try:
        return parse_utc_timestamp(raw)
    except ValueError:
        return None


def resolve_owner_id(conn, explicit_owner_id: str | None = None) -> str:
    """Resolve personal experience owner without hardcoded usernames."""
    for candidate in (explicit_owner_id, os.environ.get('EVOLVMEM_EXPERIENCE_OWNER_ID')):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    for sql in (
        'SELECT owner_id FROM experience_notes WHERE deleted_at IS NULL GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1',
        'SELECT owner_id FROM experience_notes GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1',
    ):
        row = conn.execute(sql).fetchone()
        if row and row[0]:
            return str(row[0])
    raise RuntimeError('experience_owner_id_unresolved')


class _LazyEmbedder:
    """Delays GGUF model load until L1 deduplication actually needs a vector."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir
        self._engine: Any = None

    def initialize(self) -> None:
        return None

    def encode_document(self, text: str) -> list[float]:
        if self._engine is None:
            from evolvmem.config import Config
            from evolvmem.embedding import EmbeddingEngine
            cfg = Config.from_file(data_dir=self.data_dir, apply_environment=False, ensure_dirs=False)
            eng = EmbeddingEngine(cfg)
            eng.initialize()
            self._engine = eng
        return list(self._engine.encode_document(text))

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close()
            self._engine = None


def _resolve_transports(
    flash_model: str, strong_model: str, judge_model: str,
    flash_transport: Any, strong_transport: Any, judge_transport: Any,
) -> tuple[Any, Any, Any]:
    if flash_transport and strong_transport and judge_transport:
        return flash_transport, strong_transport, judge_transport
    endpoints = load_llm_endpoints_from_file()
    if not endpoints and flash_transport is None:
        raise RuntimeError('llm_credentials_unavailable')
    pool = [ep for ep in endpoints.values() if ep is not None]

    def pick(model: str, fallbacks: tuple[str, ...]) -> LlmEndpoint | None:
        prefix = model.split('-', 1)[0].casefold()
        for slot in fallbacks:
            ep = endpoints.get(slot)
            if ep is not None and ep.model == model:
                return ep
        for ep in pool:
            if ep.model == model:
                return ep
        for slot in fallbacks:
            ep = endpoints.get(slot)
            if ep is not None and ep.provider.casefold() == prefix:
                return ep
        for ep in pool:
            if ep.provider.casefold() == prefix:
                return ep
        for slot in fallbacks:
            if slot in endpoints:
                return endpoints[slot]
        return pool[0] if pool else None

    f_ep = pick(flash_model, ('flash', 'primary', 'fallback'))
    f_tr = flash_transport or (default_http_transport(f_ep) if f_ep else None)
    if f_tr is None:
        raise RuntimeError('llm_flash_transport_unavailable')
    s_ep = pick(strong_model, ('strong', 'primary', 'fallback'))
    j_ep = pick(judge_model, ('strong', 'primary', 'fallback'))
    s_tr = strong_transport or (default_http_transport(s_ep) if s_ep else f_tr)
    j_tr = judge_transport or (default_http_transport(j_ep) if j_ep else s_tr)
    return f_tr, s_tr, j_tr


def _empty_summary(status: str, since_iso: str, scanned: int, skipped_pre: int, processed: int) -> dict[str, Any]:
    return {
        'status': status, 'since': since_iso, 'scanned': scanned, 'skipped_pre_cutover': skipped_pre,
        'processed': processed, 'completed': 0, 'already_processed': 0, 'skipped_zero_moments': 0,
        'active_wait': 0, 'deferred': 0, 'errors': 0, 'proposals': 0, 'tokens_spent': 0, 'llm_calls': 0,
    }


def _scan_since_cutover(
    history_reader: HistoryReader, *, since_dt: datetime, page_size: int = 100,
) -> tuple[list[tuple[HistorySessionSummary, datetime]], int, int]:
    summaries: list[HistorySessionSummary] = []
    seen_refs: set[str] = set()
    offset = 0
    step = max(50, int(page_size))
    while True:
        page = history_reader.list_sessions(limit=step, offset=offset, dedupe_logical_sessions=True)
        if not page:
            break
        reached_floor = False
        for s in page:
            if s.source_ref not in seen_refs:
                seen_refs.add(s.source_ref)
                summaries.append(s)
            dt = _try_parse_utc(getattr(s, 'created_at', ''))
            if dt is not None and dt < since_dt:
                reached_floor = True
        if len(page) < step or reached_floor:
            break
        offset += len(page)
    eligible = [(s, dt) for s in summaries if (dt := _try_parse_utc(getattr(s, 'created_at', ''))) and dt >= since_dt]
    return eligible, len(summaries), len(summaries) - len(eligible)


def _session_readiness(history_reader: HistoryReader, summ: HistorySessionSummary, created_dt: datetime) -> tuple[bool, datetime]:
    if str(summ.source_ref).startswith('archive:'):
        return True, created_dt
    sess = history_reader.get_session(summ.source_ref)
    if sess is None or not sess.records:
        return False, created_dt
    last_dt = created_dt
    for rec in sess.records:
        raw = rec.raw_event if isinstance(rec.raw_event, dict) else {}
        for key in ('recorded_at', 'timestamp', 'created_at', 'ts'):
            cand = _try_parse_utc(raw.get(key))
            if cand is not None and cand > last_dt:
                last_dt = cand
                break
    return False, last_dt


def run_live_pipeline_tick(
    conn, *, history_reader: HistoryReader, since: str, owner_id: str | None = None,
    policy: ProductPolicy | None = None, limit: int = 30, now: datetime | None = None,
    flash_model: str = 'deepseek-v4-flash', strong_model: str = 'deepseek-chat', judge_model: str = 'deepseek-chat',
    flash_transport: Callable[..., dict[str, Any]] | None = None,
    strong_transport: Callable[..., dict[str, Any]] | None = None,
    judge_transport: Callable[..., dict[str, Any]] | None = None,
    embedder: Any = None, data_dir: Path | None = None, ticket_secret: bytes | str = b'stage3-pipeline-secret',
    budget: BudgetConfig | None = None, pipeline_version: str = DEFAULT_PIPELINE_VERSION, drain_deferred: bool = True,
) -> dict[str, Any]:
    """Run one bounded live-track extraction tick for sessions at or after cutover floor."""
    since_dt = parse_utc_timestamp(since)
    active_policy = policy if policy is not None else load_policy()
    if not active_policy.valid or not active_policy.experience_pipeline_enabled:
        return {'status': 'disabled', 'llm_calls': 0}

    owner = resolve_owner_id(conn, owner_id)
    max_per_tick = max(1, int(limit))
    eligible, scanned_cnt, skipped_pre = _scan_since_cutover(history_reader, since_dt=since_dt)
    deferred_row = conn.execute(
        "SELECT 1 FROM pipeline_segment_runs WHERE owner_id = %s AND track = 'live' "
        "AND pipeline_version = %s AND is_shadow = false AND status = 'deferred' LIMIT 1",
        (owner, pipeline_version),
    ).fetchone() if drain_deferred else None
    if not eligible and deferred_row is None:
        return _empty_summary('idle', since_dt.isoformat(), scanned_cnt, skipped_pre, 0)

    f_tr, s_tr, j_tr = _resolve_transports(
        flash_model, strong_model, judge_model, flash_transport, strong_transport, judge_transport,
    )
    raw_mode = active_policy.experience_gate_mode
    gate_mode: str | dict[str, str] = raw_mode if isinstance(raw_mode, (str, dict)) else 'review'
    owned_embedder = _LazyEmbedder(data_dir=data_dir) if embedder is None else None
    scheduler = PipelineScheduler(
        history_reader=history_reader, policy=active_policy, owner_id=owner,
        flash_model=flash_model, strong_model=strong_model, judge_model=judge_model,
        flash_transport=f_tr, strong_transport=s_tr, judge_transport=j_tr,
        embedder=owned_embedder if owned_embedder is not None else embedder,
        pipeline_version=pipeline_version, track='live', ticket_secret=ticket_secret, budget=budget, gate_mode=gate_mode,
    )
    summary = _empty_summary('completed', since_dt.isoformat(), scanned_cnt, skipped_pre, 0)
    tick_now = now or datetime.now(timezone.utc)
    max_call_before = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls WHERE track = 'live' AND is_shadow = false").fetchone()[0])

    def _record(res: dict[str, Any]) -> None:
        st = str(res.get('status') or '')
        if st:
            summary[st if st in _SUMMARY_KEYS else 'errors'] += 1
        summary['proposals'] += int(res.get('proposals') or 0)

    try:
        if deferred_row is not None:
            try:
                with conn.transaction():
                    for res in scheduler.drain_deferred_segments(conn):
                        _record(res)
            except (BudgetDeferred, LlmTransportError, Exception):
                summary['errors'] += 1
        active_ran = 0
        for summ, created_dt in reversed(eligible):
            if active_ran >= max_per_tick:
                break
            is_closed, last_msg_dt = _session_readiness(history_reader, summ, created_dt)
            try:
                with conn.transaction():
                    res = scheduler.process_session_if_ready(
                        conn, summ.source_ref, is_closed=is_closed, last_message_at=last_msg_dt, now=tick_now, shadow_mode=False,
                    )
            except BudgetDeferred:
                res = {'status': 'deferred'}
            except (LlmTransportError, Exception):
                res = {'status': 'errors'}
            summary['processed'] += 1
            _record(res)
            if res.get('status') != 'already_processed':
                active_ran += 1
        usage = conn.execute(
            "SELECT COUNT(*)::int, COALESCE(SUM(input_tokens + output_tokens), 0)::int FROM llm_calls "
            "WHERE track = 'live' AND is_shadow = false AND id > %s", (max_call_before,),
        ).fetchone()
        summary['llm_calls'] = int(usage[0] or 0) if usage else 0
        summary['tokens_spent'] = int(usage[1] or 0) if usage else 0
        if summary['llm_calls'] > 0 or summary['proposals'] > 0:
            evaluate_circuit_breaker(conn, owner_id=owner, track='live')
    finally:
        if owned_embedder is not None:
            owned_embedder.close()
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for scheduled Stage 3 live pipeline ticks."""
    parser = argparse.ArgumentParser(description='Stage 3 live pipeline periodic tick worker')
    parser.add_argument('--since', required=True, help='UTC cutover floor timestamp (ISO-8601 or YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--limit', type=int, default=30, help='Maximum active sessions to process per tick')
    parser.add_argument('--data-dir', default='~/.claude/evolvmem', help='EvolvMem data directory')
    args = parser.parse_args(list(argv) if argv is not None else None)

    since_dt = parse_utc_timestamp(args.since)
    policy = load_policy()
    if not policy.valid or not policy.experience_pipeline_enabled:
        print(json.dumps({'status': 'disabled', 'llm_calls': 0}, ensure_ascii=False))
        return 0

    data_dir = Path(args.data_dir).expanduser()
    cand_env = (os.environ.get('EVOLVMEM_HISTORY_CANDIDATE_DB') or '').strip()
    candidate_db = Path(cand_env).expanduser() if cand_env else (data_dir / 'candidate.db')
    reader = SqliteHistoryReader(
        memory_db=data_dir / 'memory.db', candidate_db=candidate_db if candidate_db.is_file() else None,
        data_dir=data_dir, library='personal',
    )
    settings = load_pg_experience_settings()
    with open_experience_conn(settings, role='coordinator') as conn:
        result = run_live_pipeline_tick(
            conn, history_reader=reader, since=since_dt.isoformat(), policy=policy,
            limit=args.limit, data_dir=data_dir, ticket_secret=settings.ticket_secret,
        )
        conn.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
