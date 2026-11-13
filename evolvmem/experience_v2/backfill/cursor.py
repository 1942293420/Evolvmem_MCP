"""Task B.3: Single-scope validation (project XOR month), newest-to-oldest planning & cursor resume."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable, Literal, Sequence

from evolvmem.experience_v2.backfill.runner import BackfillRunner
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader, HistorySessionSummary

_MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])\Z')


class BackfillScopeError(ValueError):
    """Raised when backfill scope violates the single-project or single-month rule."""


def validate_backfill_scope(*, project: str | None = None, month: str | None = None) -> tuple[Literal['project', 'month'], str]:
    """Enforce strictly one scope: either one project OR one YYYY-MM month."""
    has_proj = project is not None
    has_month = month is not None
    if has_proj == has_month:
        raise BackfillScopeError('must_specify_single_project_or_month')
    if has_proj:
        clean = str(project or '').strip()
        if not clean:
            raise BackfillScopeError('empty_project_scope')
        return 'project', clean
    clean_m = str(month or '').strip()
    if not _MONTH_RE.fullmatch(clean_m):
        raise BackfillScopeError('invalid_month_scope_expected_YYYY_MM')
    return 'month', clean_m


def _matches_scope(summary: HistorySessionSummary, scope_kind: str, scope_value: str) -> bool:
    if scope_kind == 'project':
        return (summary.project or '').strip() == scope_value
    return (summary.created_at or '').strip()[:7] == scope_value


def plan_scoped_sessions(
    history_reader: HistoryReader,
    *,
    scope_kind: Literal['project', 'month'],
    scope_value: str,
    exclude_source_refs: set[str] | frozenset[str] = frozenset(),
    fetch_limit: int = 20_000,
) -> list[HistorySessionSummary]:
    """List matching sessions ordered strictly newest-to-oldest (created_at DESC, source_ref DESC)."""
    proj_filter = scope_value if scope_kind == 'project' else None
    raw = history_reader.list_sessions(project=proj_filter, limit=fetch_limit, offset=0)
    seen: set[str] = set()
    filtered: list[HistorySessionSummary] = []
    for item in raw:
        if item.source_ref in exclude_source_refs or item.source_ref in seen:
            continue
        if _matches_scope(item, scope_kind, scope_value):
            seen.add(item.source_ref)
            filtered.append(item)
    filtered.sort(key=lambda s: (s.created_at or '', s.source_ref), reverse=True)
    return filtered


class BackfillCursorStore:
    """PostgreSQL checkpoint store enabling crash-safe newest-to-oldest resume."""

    def __init__(self, *, owner_id: str) -> None:
        self.owner_id = owner_id

    def get_cursor(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        is_shadow: bool = False,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT last_created_at, last_source_ref, processed_count, completed
            FROM backfill_cursors
            WHERE owner_id = %s AND scope_kind = %s AND scope_value = %s AND is_shadow = %s
            """,
            (self.owner_id, scope_kind, scope_value, bool(is_shadow)),
        ).fetchone()
        if not row:
            return None
        return {
            'last_created_at': row[0],
            'last_source_ref': row[1],
            'processed_count': int(row[2]),
            'completed': bool(row[3]),
        }

    @staticmethod
    def slice_after_cursor(
        sessions: Sequence[HistorySessionSummary],
        cursor: dict[str, Any] | None,
    ) -> list[HistorySessionSummary]:
        """Keep only sessions strictly older than the last checkpointed (created_at, source_ref)."""
        if not cursor:
            return list(sessions)
        if cursor.get('completed'):
            return []
        boundary = (str(cursor.get('last_created_at') or ''), str(cursor.get('last_source_ref') or ''))
        if not boundary[1]:
            return list(sessions)
        return [s for s in sessions if (s.created_at or '', s.source_ref) < boundary]

    def advance_cursor(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        last_created_at: str,
        last_source_ref: str,
        delta_processed: int,
        completed: bool,
        is_shadow: bool = False,
    ) -> dict[str, Any]:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO backfill_cursors(
                    owner_id, scope_kind, scope_value, is_shadow,
                    last_created_at, last_source_ref, processed_count, completed, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (owner_id, scope_kind, scope_value, is_shadow) DO UPDATE SET
                    last_created_at = EXCLUDED.last_created_at,
                    last_source_ref = EXCLUDED.last_source_ref,
                    processed_count = backfill_cursors.processed_count + EXCLUDED.processed_count,
                    completed = EXCLUDED.completed,
                    updated_at = now()
                RETURNING last_created_at, last_source_ref, processed_count, completed
                """,
                (
                    self.owner_id, scope_kind, scope_value, bool(is_shadow),
                    last_created_at, last_source_ref, max(0, int(delta_processed)), bool(completed),
                ),
            ).fetchone()
        return {
            'last_created_at': row[0],
            'last_source_ref': row[1],
            'processed_count': int(row[2]),
            'completed': bool(row[3]),
        }


def execute_scoped_backfill(
    conn,
    *,
    runner: BackfillRunner,
    project: str | None = None,
    month: str | None = None,
    max_sessions: int = 100,
    exclude_source_refs: set[str] | frozenset[str] = frozenset(),
    now: datetime | None = None,
    shadow_mode: bool = False,
    shadow_run_id: str | None = None,
    enforce_night_window: bool = True,
    on_job_done: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one single-scope newest-to-oldest backfill batch with DB cursor checkpointing."""
    scope_kind, scope_value = validate_backfill_scope(project=project, month=month)
    ts = now or datetime.now(timezone.utc)
    gate = runner.queue.check_run_gate(
        policy=runner.policy, now=ts, shadow_mode=shadow_mode, enforce_night_window=enforce_night_window,
    )
    if not gate['allowed']:
        return {'status': gate['status'], 'scope_kind': scope_kind, 'scope_value': scope_value, 'processed': 0, 'ordered_source_refs': []}

    store = BackfillCursorStore(owner_id=runner.owner_id)
    cursor = store.get_cursor(conn, scope_kind=scope_kind, scope_value=scope_value, is_shadow=shadow_mode)
    if cursor and cursor.get('completed'):
        return {
            'status': 'cursor_completed',
            'scope_kind': scope_kind,
            'scope_value': scope_value,
            'processed': 0,
            'ordered_source_refs': [],
            'cursor': cursor,
        }

    planned = plan_scoped_sessions(
        runner.scheduler.history_reader,
        scope_kind=scope_kind,
        scope_value=scope_value,
        exclude_source_refs=exclude_source_refs,
    )
    remaining = store.slice_after_cursor(planned, cursor)
    if not remaining:
        done_cur = store.advance_cursor(
            conn,
            scope_kind=scope_kind,
            scope_value=scope_value,
            last_created_at=(cursor or {}).get('last_created_at', ''),
            last_source_ref=(cursor or {}).get('last_source_ref', ''),
            delta_processed=0,
            completed=True,
            is_shadow=shadow_mode,
        )
        return {
            'status': 'cursor_completed',
            'scope_kind': scope_kind,
            'scope_value': scope_value,
            'processed': 0,
            'ordered_source_refs': [],
            'cursor': done_cur,
        }

    batch = remaining[: max(1, int(max_sessions))]
    runner.queue.enqueue_sessions(conn, scope_kind=scope_kind, scope_value=scope_value, sessions=batch, is_shadow=shadow_mode)
    run_res = runner.run_queued_batch(
        conn,
        scope_kind=scope_kind,
        scope_value=scope_value,
        now=ts,
        max_jobs=len(batch),
        shadow_mode=shadow_mode,
        shadow_run_id=shadow_run_id,
        enforce_night_window=enforce_night_window,
        on_job_done=on_job_done,
    )

    ordered_refs: list[str] = []
    progressed: list[tuple[str, str]] = []
    hit_defer = False
    for entry in run_res.get('results', []):
        job = entry['job']
        res = entry['result']
        ordered_refs.append(job['source_ref'])
        if res.get('status') == 'deferred':
            hit_defer = True
            break
        progressed.append((job['session_created_at'], job['source_ref']))

    new_cursor = cursor
    if progressed:
        last_ts, last_ref = progressed[-1]
        is_done = (not hit_defer) and (len(remaining) <= len(batch)) and (len(progressed) == len(batch))
        new_cursor = store.advance_cursor(
            conn,
            scope_kind=scope_kind,
            scope_value=scope_value,
            last_created_at=last_ts,
            last_source_ref=last_ref,
            delta_processed=len(progressed),
            completed=is_done,
            is_shadow=shadow_mode,
        )

    return {
        'status': run_res['status'],
        'scope_kind': scope_kind,
        'scope_value': scope_value,
        'processed': int(run_res.get('processed') or 0),
        'proposals': int(run_res.get('proposals') or 0),
        'deferred': int(run_res.get('deferred') or 0),
        'tokens_spent': int(run_res.get('tokens_spent') or 0),
        'ordered_source_refs': ordered_refs,
        'cursor': new_cursor,
        'results': run_res.get('results', []),
    }
