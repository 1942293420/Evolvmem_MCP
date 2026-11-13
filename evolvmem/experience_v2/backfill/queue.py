"""Task B.1: Backfill night-window gate, policy check & backfill_jobs PostgreSQL queue."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal, Sequence
from zoneinfo import ZoneInfo

from evolvmem.experience_v2.pipeline.history_reader import HistorySessionSummary
from evolvmem.product_policy import ProductPolicy

NIGHT_WINDOW_START_HOUR = 1
NIGHT_WINDOW_END_HOUR = 7
DEFAULT_BACKFILL_TZ = 'Asia/Shanghai'


def is_within_night_window(
    now: datetime,
    *,
    tz_name: str = DEFAULT_BACKFILL_TZ,
    start_hour: int = NIGHT_WINDOW_START_HOUR,
    end_hour: int = NIGHT_WINDOW_END_HOUR,
) -> bool:
    """Return True iff local hour in tz_name is inside [01:00, 07:00)."""
    dt = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    return int(start_hour) <= local_dt.hour < int(end_hour)


def _make_job_id(owner_id: str, scope_kind: str, scope_value: str, source_ref: str, is_shadow: bool) -> str:
    raw = f'{owner_id}|{scope_kind}|{scope_value}|{source_ref}|{"shadow" if is_shadow else "live"}'
    return 'bfjob-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


class BackfillQueue:
    """PostgreSQL queue manager for isolated historical backfill jobs."""

    def __init__(self, *, owner_id: str) -> None:
        self.owner_id = owner_id

    @staticmethod
    def check_run_gate(
        *,
        policy: ProductPolicy,
        now: datetime,
        shadow_mode: bool = False,
        enforce_night_window: bool = True,
        tz_name: str = DEFAULT_BACKFILL_TZ,
    ) -> dict[str, Any]:
        """Enforce independent backfill switch and 01:00-07:00 night window for live runs."""
        if not shadow_mode and not policy.experience_backfill_enabled:
            return {'allowed': False, 'status': 'disabled', 'llm_calls': 0}
        if not shadow_mode and enforce_night_window and not is_within_night_window(now, tz_name=tz_name):
            return {'allowed': False, 'status': 'outside_night_window', 'llm_calls': 0}
        return {'allowed': True, 'status': 'ready', 'llm_calls': 0}

    def enqueue_sessions(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        sessions: Sequence[HistorySessionSummary],
        is_shadow: bool = False,
    ) -> int:
        """Idempotently insert scoped sessions into backfill_jobs newest-first."""
        inserted = 0
        with conn.transaction():
            for s in sessions:
                jid = _make_job_id(self.owner_id, scope_kind, scope_value, s.source_ref, is_shadow)
                res = conn.execute(
                    """
                    INSERT INTO backfill_jobs(
                        job_id, owner_id, scope_kind, scope_value,
                        source_ref, session_id, session_created_at, status, is_shadow
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                    ON CONFLICT (owner_id, scope_kind, scope_value, source_ref, is_shadow) DO NOTHING
                    RETURNING job_id
                    """,
                    (
                        jid, self.owner_id, scope_kind, scope_value,
                        s.source_ref, s.session_id, s.created_at or '', bool(is_shadow),
                    ),
                ).fetchone()
                if res is not None:
                    inserted += 1
        return inserted

    def claim_next_jobs(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        limit: int = 50,
        is_shadow: bool = False,
    ) -> list[dict[str, Any]]:
        """Atomically claim queued backfill jobs ordered newest-to-oldest."""
        safe_limit = max(1, min(int(limit), 500))
        with conn.transaction():
            rows = conn.execute(
                """
                SELECT job_id, source_ref, session_id, session_created_at
                FROM backfill_jobs
                WHERE owner_id = %s
                  AND scope_kind = %s
                  AND scope_value = %s
                  AND is_shadow = %s
                  AND status = 'queued'
                ORDER BY session_created_at DESC, source_ref DESC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (self.owner_id, scope_kind, scope_value, bool(is_shadow), safe_limit),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for jid, sref, sid, created_at in rows:
                conn.execute(
                    "UPDATE backfill_jobs SET status = 'running', updated_at = now() WHERE job_id = %s",
                    (jid,),
                )
                claimed.append({
                    'job_id': jid,
                    'source_ref': sref,
                    'session_id': sid,
                    'session_created_at': created_at,
                    'status': 'running',
                })
        return claimed

    def mark_job_result(
        self,
        conn,
        *,
        job_id: str,
        status: Literal['completed', 'deferred', 'skipped_zero_moments', 'failed'],
        moments_count: int = 0,
        proposals_count: int = 0,
        tokens_spent: int = 0,
        error_code: str = '',
    ) -> None:
        with conn.transaction():
            conn.execute(
                """
                UPDATE backfill_jobs
                SET status = %s,
                    moments_count = %s,
                    proposals_count = %s,
                    tokens_spent = %s,
                    error_code = %s,
                    updated_at = now()
                WHERE owner_id = %s AND job_id = %s
                """,
                (status, int(moments_count), int(proposals_count), int(tokens_spent), error_code[:120], self.owner_id, job_id),
            )

    def requeue_deferred_jobs(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        is_shadow: bool = False,
    ) -> int:
        with conn.transaction():
            cur = conn.execute(
                """
                UPDATE backfill_jobs
                SET status = 'queued', updated_at = now()
                WHERE owner_id = %s AND scope_kind = %s AND scope_value = %s
                  AND is_shadow = %s AND status = 'deferred'
                """,
                (self.owner_id, scope_kind, scope_value, bool(is_shadow)),
            )
            return int(cur.rowcount or 0)
