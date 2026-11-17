"""Stage 2 Task 2.10: Incremental experience change feed, PG LISTEN/NOTIFY & PG recall."""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

import psycopg

_CHANNEL_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_TERM_RE = re.compile(r'[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{1,2}')


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat(timespec='seconds')
    return str(val)


def _extract_terms(text: str) -> set[str]:
    lowered = (text or '').lower()
    return {m.group(0) for m in _TERM_RE.finditer(lowered)}


class ExperienceChangeListener:
    """Dedicated autocommit psycopg listener subscribing to PG LISTEN experience_changes."""

    def __init__(self, dsn: str, *, channel: str = 'experience_changes'):
        if not _CHANNEL_RE.fullmatch(channel):
            raise ValueError('invalid_notify_channel')
        self._dsn = dsn
        self._channel = channel
        self._conn = None

    def __enter__(self) -> ExperienceChangeListener:
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        self._conn.execute(f'LISTEN {self._channel}')
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def poll(self, *, timeout: float = 1.0, max_events: int = 32) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError('listener_not_open')
        events: list[dict[str, Any]] = []
        for notify in self._conn.notifies(timeout=max(0.05, float(timeout)), stop_after=max(1, int(max_events))):
            try:
                payload = json.loads(notify.payload)
            except Exception:
                payload = {'raw': notify.payload}
            if isinstance(payload, dict):
                payload.setdefault('channel', notify.channel)
                events.append(payload)
            break_check = len(events) >= max_events
            if break_check:
                break
        return events


def changes_since(
    conn,
    *,
    owner_id: str,
    cursor: int = 0,
    limit: int = 50,
    bootstrap_only: bool = False,
) -> dict[str, Any]:
    if not owner_id:
        raise ValueError('owner_required')
    safe_cursor = max(0, int(cursor))
    safe_limit = max(1, min(int(limit), 200))

    max_row = conn.execute(
        'SELECT COALESCE(MAX(seq), 0) FROM experience_events WHERE owner_id = %s',
        (owner_id,),
    ).fetchone()
    latest_seq = int(max_row[0] if max_row else 0)

    if bootstrap_only:
        return {
            'ok': True,
            'cursor': safe_cursor,
            'next_cursor': latest_seq,
            'has_more': False,
            'changes': [],
        }

    rows = conn.execute(
        """
        SELECT e.seq, e.event_id, e.action, e.note_id, e.agent_client, e.model,
               e.after_json, e.created_at,
               n.status, n.deleted_at, n.category, n.problem, n.solution, n.reusable_experience, n.revision
        FROM experience_events AS e
        LEFT JOIN experience_notes AS n
          ON n.owner_id = e.owner_id AND n.id = e.note_id
        WHERE e.owner_id = %s AND e.seq > %s
        ORDER BY e.seq ASC
        LIMIT %s
        """,
        (owner_id, safe_cursor, safe_limit + 1),
    ).fetchall()

    has_more = len(rows) > safe_limit
    page = rows[:safe_limit]
    next_cursor = int(page[-1][0]) if page else safe_cursor
    changes: list[dict[str, Any]] = []
    for row in page:
        seq, event_id, action, note_id, agent_client, model, after_json, created_at = row[:8]
        n_status, n_deleted_at, n_cat, n_prob, n_sol, n_reusable, n_rev = row[8:]
        tombstone = bool(n_deleted_at is not None or n_status == 'retired' or action in ('delete', 'retire'))
        changes.append({
            'seq': int(seq),
            'event_id': event_id,
            'action': action,
            'note_id': note_id,
            'agent_client': agent_client or '',
            'model': model or '',
            'created_at': _iso(created_at),
            'tombstone': tombstone,
            'note': (
                None if tombstone else {
                    'id': note_id,
                    'category': n_cat or (after_json or {}).get('category', ''),
                    'problem': n_prob or (after_json or {}).get('problem', ''),
                    'solution': n_sol or (after_json or {}).get('solution', ''),
                    'reusable_experience': n_reusable or (after_json or {}).get('reusable_experience', ''),
                    'revision': int(n_rev or (after_json or {}).get('revision', 1)),
                }
            ),
        })

    return {
        'ok': True,
        'cursor': safe_cursor,
        'next_cursor': next_cursor,
        'has_more': has_more,
        'changes': changes,
    }


def recall_pg_experiences(
    conn,
    *,
    owner_id: str,
    query: str | None = None,
    project: str = '',
    constraints: dict[str, Any] | None = None,
    mode: str = 'full',
    max_chars: int = 2400,
    channel: str = 'mcp',
    session_id: str | None = None,
    agent_client: str = '',
    model: str = '',
) -> dict[str, Any]:
    """Read active experience_notes from PG, rank, format compatible with legacy recall, and log impressions."""
    if not owner_id:
        raise ValueError('owner_required')
    rows = conn.execute(
        """
        SELECT id, category, problem, solution, reusable_experience, scope,
               evidence, occurrence_count, importance, revision, pinned
        FROM experience_notes
        WHERE owner_id = %s AND deleted_at IS NULL AND status = 'active'
        ORDER BY pinned DESC, importance DESC, updated_at DESC, id ASC
        LIMIT 200
        """,
        (owner_id,),
    ).fetchall()

    q_terms = _extract_terms(query or '')
    c_dict = constraints if isinstance(constraints, dict) else {}
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        note_id, category, problem, solution, reusable, scope, evidence, occ, imp, _rev, pinned = row
        scope_dict = scope if isinstance(scope, dict) else {}
        if c_dict and any(k in c_dict and k in scope_dict and scope_dict[k] != c_dict[k] for k in c_dict):
            continue
        blob = f'{category} {problem} {solution} {reusable}'
        overlap = (len(q_terms & _extract_terms(blob)) / max(1, len(q_terms))) if q_terms else 0.3
        if q_terms and overlap == 0.0:
            continue
        score = round(min(1.0, overlap * 0.7 + (int(imp) / 10.0) * 0.2 + (0.1 if pinned else 0.0)), 4)
        ev_list = evidence if isinstance(evidence, list) else []
        preview = {
            'id': note_id,
            'project': project or str(scope_dict.get('project') or scope_dict.get('repo') or ''),
            'category': category,
            'problem': problem[:240],
            'solution': solution[:360],
            'reusable_experience': reusable[:240],
            'conditions': scope_dict,
            'steps': [solution[:240]],
            'applicability': [reusable[:120]],
            'exclusions': [],
            'transferable': True,
            'parent_experience_id': None,
            'validation_level': 'verified',
            'success_count': int(occ),
            'failure_count': 0,
            'verification': ev_list[:2],
            'score': score,
            'match_reason': 'PG经验库匹配' if mode == 'hook' else 'PG经验检索',
            'adaptation_required': False,
            'instruction': '结合当前项目与约束核对前置条件后再执行。',
        }
        scored.append((score, preview))

    scored.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    selected: list[dict[str, Any]] = []
    used_chars = 0
    for rank_idx, (_score, preview) in enumerate(scored[:3], start=1):
        cost = len(json.dumps(preview, ensure_ascii=False, separators=(',', ':')))
        if selected and used_chars + cost > max_chars:
            continue
        selected.append(preview)
        used_chars += cost
        conn.execute(
            """
            INSERT INTO retrieval_impressions (
                owner_id, item_uid, session_id, task_id, channel, rank, agent_client, model
            ) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s)
            """,
            (owner_id, preview['id'], session_id, channel, rank_idx, agent_client or '', model or ''),
        )

    conn.commit()
    return {'results': selected, 'used_chars': used_chars, 'source': 'pg_experience_v2'}