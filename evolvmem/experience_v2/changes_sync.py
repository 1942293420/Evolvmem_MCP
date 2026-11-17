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
    deadline: float | None = None,
    embedding_client=None,
    authorized_owner_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Adapt shared retrieval to MCP previews; only this adapter logs exposures.

    authorized_owner_ids must come from the authenticated host, never tool args.
    Without it, a request can only search its own owner_id.
    """
    import time
    from evolvmem.experience_v2.hybrid_search import hybrid_search, _validate, _budget

    if not owner_id or mode not in ('full', 'hook'):
        raise ValueError('invalid_recall_arguments')
    if type(max_chars) is not int or not 1 <= max_chars <= 16000:
        raise ValueError('invalid_recall_budget')
    owners = [owner_id] if authorized_owner_ids is None else authorized_owner_ids
    observed = {} if constraints is None else constraints
    _validate(owners, query, None, None, observed, 'active', 3, 0, deadline, .72, project)
    started = time.monotonic()
    until = min(started + 1.8, deadline) if deadline is not None else started + 1.8
    if until <= started:
        raise TimeoutError('experience_recall_deadline_exceeded')

    vector, contract, embedding_error = None, None, ''
    try:
        if embedding_client is None:
            from evolvmem.experience_v2.embedding_client import EmbeddingClient
            embedding_client = EmbeddingClient.from_environment()
        if embedding_client is None:
            embedding_error = 'embedding_unconfigured'
        else:
            contract = embedding_client.retrieval_contract_id
            vector = embedding_client.encode_query(query.strip(), deadline=min(until - 0.2, time.monotonic() + .65))
    except Exception as exc:
        # Keep a stable reason without relaying service responses or secrets.
        code = getattr(exc, 'code', '') or str(exc)
        embedding_error = code if re.fullmatch(r'[a-z][a-z0-9_]{2,63}', code) else 'embedding_unavailable'
        vector, contract = None, None
    encoded = time.monotonic()
    result = hybrid_search(conn, owner_ids=owners, query=query,
        query_vector=vector, contract_id=contract, project=project, constraints=observed,
        limit=3, deadline=until)
    selected, used_chars = [], 0
    for note in result['items']:
        scope = note['scope'] if isinstance(note['scope'], dict) else {}
        evidence = note['evidence'] if isinstance(note['evidence'], list) else []
        scope_project = str(scope.get('project') or scope.get('repo') or '')
        verification = [e for e in evidence if isinstance(e, dict)]
        preview = {
            'id': note['id'], 'owner_id': note['owner_id'], 'project': scope_project,
            'category': note['category'], 'problem': note['problem'][:240],
            'solution': note['solution'][:360],
            'reusable_experience': note['reusable_experience'][:240],
            'conditions': scope, 'steps': [note['solution'][:240]],
            'applicability': [note['reusable_experience'][:120]], 'exclusions': [],
            'transferable': not bool(scope_project), 'parent_experience_id': None,
            'validation_level': ('user_authored' if note['origin'] == 'manual'
                else 'evidence_available' if verification else 'active'),
            'success_count': sum(e.get('outcome') in ('success', 'confirmed') for e in verification),
            'failure_count': sum(e.get('outcome') in ('failure', 'contradicted') for e in verification),
            'occurrence_count': int(note['occurrence_count']), 'verification': verification[:2],
            'score': round(note['score'], 4), 'keyword_score': note['keyword_score'],
            'cosine': note['cosine'], 'match_types': note['match_types'],
            'match_reason': 'PG关键词与语义匹配' if len(note['match_types']) == 2
                else 'PG语义匹配' if 'vector' in note['match_types'] else 'PG关键词匹配',
            'adaptation_required': bool(scope_project.strip().lower() != project.strip().lower() or any(
                k not in observed for k in scope if k not in ('project', 'repo'))),
            'instruction': '结合当前项目与约束核对前置条件后再执行。',
        }
        cost = len(json.dumps(preview, ensure_ascii=False, separators=(',', ':')))
        if used_chars + cost > max_chars:
            continue
        selected.append(preview)
        used_chars += cost
        _budget(conn, until)
        conn.execute(
            """INSERT INTO retrieval_impressions
                (owner_id,item_uid,session_id,task_id,channel,rank,agent_client,model)
               VALUES (%s,%s,%s,NULL,%s,%s,%s,%s)""",
            (note['owner_id'], note['id'], session_id, channel, len(selected),
             agent_client or '', model or ''),
        )
    _budget(conn, until)
    conn.commit()
    diagnostics = dict(result['diagnostics'])
    if embedding_error:
        diagnostics['embedding_error'] = embedding_error
    diagnostics.update(embedding_ms=round((encoded-started)*1000, 2),
                       total_ms=round((time.monotonic()-started)*1000, 2))
    return {'results': selected, 'used_chars': used_chars,
            'source': 'pg_experience_v2', 'diagnostics': diagnostics}
