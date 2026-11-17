"""Idempotent materialization of uploaded CAS archives into em_history browse tables."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from evolvmem.history_pg.config import PgHistoryConfig, PgHistoryPool
from evolvmem.transcript_formats import CODEX_FORMAT, adapter_name, parse_transcript, source_format, workspace_details

_SKIP_PREFIXES = ('# AGENTS.md instructions', '<environment_context>', '<permissions instructions>', '<collaboration_mode>')
_GENERIC_DIRS = frozenset({'', 'chatgpt', 'documents', 'desktop', 'asus', 'users', 'temp', 'tmp', 'system32'})
_NEW_CHAT_RE = re.compile(r'/Codex/\d{4}-\d{2}-\d{2}/new-chat(?:/|$)', re.I)


def _parse_iso_dt(value: str) -> datetime | None:
    text = (value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _is_preamble(role: str, content: str) -> bool:
    return role == 'user' and any(content.lstrip().startswith(p) for p in _SKIP_PREFIXES)


def _codex_timed_messages(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    prev: tuple[str, str, str] | None = None
    for row in rows:
        pl = row.get('payload')
        if not isinstance(pl, dict) or pl.get('channel') == 'analysis':
            continue
        role, content, rtype, kind = '', '', row.get('type'), pl.get('type')
        if rtype == 'response_item' and kind == 'message' and pl.get('role') in ('user', 'assistant'):
            role = str(pl['role'])
            parts = pl.get('content', [])
            if isinstance(parts, str):
                content = parts
            elif isinstance(parts, list):
                content = '\n'.join(
                    p['text'] for p in parts
                    if isinstance(p, dict) and isinstance(p.get('text'), str) and p.get('type') in ('input_text', 'output_text', 'text')
                )
        elif rtype == 'event_msg' and kind in ('user_message', 'agent_message'):
            role = 'user' if kind == 'user_message' else 'assistant'
            content = str(pl.get('message') or '')
        if not role or not content.strip():
            continue
        marker = (role, content)
        if prev is not None and prev[:2] == marker and prev[2] != rtype:
            prev = None
            continue
        prev = (*marker, str(rtype or ''))
        if not _is_preamble(role, content):
            msgs.append({'role': role, 'content': content, 'recorded_at': str(row.get('timestamp') or pl.get('timestamp') or '')})
    return msgs


def _extract_timed_messages(
    raw: bytes, session_id: str, fmt: str, cutoff_ts: str = '',
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    rows, dialogue = parse_transcript(raw, session_id, fmt)
    det = workspace_details(rows, fmt)
    if det.get('subagent') or det.get('native_subagent') or det.get('attribution_reason') == 'subagent_session':
        return [], {**det, 'skipped_subagent': True}
    cutoff_dt = _parse_iso_dt(cutoff_ts)
    if source_format(fmt) == CODEX_FORMAT:
        msgs = _codex_timed_messages(rows)
    else:
        msgs = [
            {'role': str(m['role']), 'content': str(m['content']), 'recorded_at': str(m.get('recorded_at') or m.get('timestamp') or '')}
            for m in dialogue
            if m.get('role') in ('user', 'assistant') and str(m.get('content') or '').strip()
            and not _is_preamble(str(m.get('role')), str(m.get('content')))
        ]
    if cutoff_dt is None:
        return msgs, det
    return [m for m in msgs if (dt := _parse_iso_dt(m['recorded_at'])) is None or dt > cutoff_dt], det


def _resolve_history_project(
    conn, *, device_id: str, upload_proj: str, declared_proj: str, cwd: str,
) -> str:
    candidate = upload_proj.strip() or declared_proj.strip()
    if not candidate and cwd.strip():
        ws_fp = hashlib.sha256(f'{device_id}::{cwd}'.encode('utf-8')).hexdigest()[:32]
        row = conn.execute(
            "SELECT project FROM em_runtime.context_project_workspace_bindings WHERE workspace_fingerprint = %s AND state = 'active' "
            'ORDER BY is_default DESC, updated_at DESC LIMIT 1',
            (ws_fp,),
        ).fetchone()
        if row and str(row[0] or '').strip():
            candidate = str(row[0]).strip()
    if not candidate:
        norm = cwd.replace('\\', '/').rstrip('/')
        tail = 'codex' if _NEW_CHAT_RE.search(norm + '/') else (Path(norm).name.strip() if norm else '')
        candidate = 'codex' if tail.lower() in _GENERIC_DIRS else tail
    row = conn.execute('SELECT project FROM em_runtime.context_project_aliases WHERE lower(alias) = lower(%s) LIMIT 1', (candidate,)).fetchone()
    if row and str(row[0] or '').strip():
        candidate = str(row[0]).strip()
    else:
        row = conn.execute('SELECT project FROM em_history.projects WHERE lower(project) = lower(%s) LIMIT 1', (candidate,)).fetchone()
        if row and str(row[0] or '').strip():
            candidate = str(row[0]).strip()
    cleaned = candidate.replace('..', '-').replace('/', '-').replace('\\', '-').replace('\x00', '').strip()
    return cleaned[:128].strip() or 'codex'


def _refresh_project_stats(conn, proj: str, now_iso: str) -> None:
    agg = conn.execute(
        'SELECT COUNT(*)::int, COALESCE(SUM(b.message_count), 0)::int, COALESCE(SUM(b.turn_count), 0)::int, '
        "COALESCE(MIN(NULLIF(b.observed_first_at, '')), ''), COALESCE(MAX(NULLIF(b.observed_last_at, '')), '') "
        'FROM em_history.block_projects bp JOIN em_history.blocks b ON b.block_id = bp.block_id WHERE bp.project = %s',
        (proj,),
    ).fetchone()
    bcnt, mcnt, tcnt, first_at, last_at = (
        int(agg[0] or 0), int(agg[1] or 0), int(agg[2] or 0), str(agg[3] or ''), str(agg[4] or ''),
    ) if agg else (0, 0, 0, '', '')
    conn.execute(
        "INSERT INTO em_history.projects (project, display_name, status, revision, created_at, updated_at, block_count, message_count, turn_count, first_at, last_at) "
        "VALUES (%s, %s, 'active', 1, %s, %s, %s, %s, %s, %s, %s) "
        'ON CONFLICT (project) DO UPDATE SET '
        "status = CASE WHEN em_history.projects.status = 'archived' THEN 'active' ELSE em_history.projects.status END, "
        'revision = em_history.projects.revision + 1, updated_at = EXCLUDED.updated_at, '
        'block_count = EXCLUDED.block_count, message_count = EXCLUDED.message_count, turn_count = EXCLUDED.turn_count, '
        "first_at = CASE WHEN em_history.projects.first_at = '' OR (EXCLUDED.first_at != '' AND EXCLUDED.first_at < em_history.projects.first_at) THEN EXCLUDED.first_at ELSE em_history.projects.first_at END, "
        "last_at = CASE WHEN EXCLUDED.last_at > em_history.projects.last_at THEN EXCLUDED.last_at ELSE em_history.projects.last_at END",
        (proj, proj, now_iso, now_iso, bcnt, mcnt, tcnt, first_at, last_at),
    )


def sync_session_archive_conn(
    conn, *, device_id: str, session_id: str, sha256: str, archive_id: int, payload_path: str,
    source_format_str: str, project: str = '', declared_project: str = '', attribution_reason: str = '', archive_created_at: str = '',
) -> dict[str, Any]:
    """Upsert one CAS session archive into em_history browse tables idempotently."""
    if attribution_reason == 'subagent_session':
        return {'synced': False, 'reason': 'subagent_session'}
    cas_file = Path(payload_path)
    if not cas_file.is_file():
        return {'synced': False, 'reason': 'missing_cas_payload'}
    raw = cas_file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        return {'synced': False, 'reason': 'cas_sha256_mismatch'}

    cut_row = conn.execute(
        "SELECT COALESCE(MAX(observed_last_at), '') FROM em_history.blocks WHERE native_session_id = %s AND block_id NOT LIKE 'live-%%'",
        (session_id,),
    ).fetchone()
    msgs, det = _extract_timed_messages(raw, session_id, source_format_str or CODEX_FORMAT, str(cut_row[0] or '') if cut_row else '')
    if det.get('skipped_subagent'):
        return {'synced': False, 'reason': 'subagent_session'}
    if not msgs:
        conn.execute(
            'INSERT INTO em_history.candidate_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (f'live_sync_covered:{session_id}', sha256),
        )
        return {'synced': False, 'reason': 'no_visible_messages'}

    proj = _resolve_history_project(conn, device_id=device_id, upload_proj=project, declared_proj=declared_project, cwd=str(det.get('cwd') or ''))
    block_id = f'live-{session_id}'
    user_msgs = [m for m in msgs if m['role'] == 'user']
    title = ' '.join((user_msgs[0]['content'] if user_msgs else msgs[0]['content']).split())[:120] or f'Session {session_id[:8]}'
    ts_list = [m['recorded_at'] for m in msgs if m['recorded_at']]
    now_iso = datetime.now(timezone.utc).isoformat()
    fallback_ts = archive_created_at or now_iso
    first_at, last_at = (min(ts_list), max(ts_list)) if ts_list else (fallback_ts, fallback_ts)
    msg_cnt, turn_cnt, adap = len(msgs), max(1, len(user_msgs)), adapter_name(source_format_str or CODEX_FORMAT)

    conn.execute(
        'INSERT INTO em_history.blocks (block_id, adapter, kind, family_key, native_session_id, native_device_id, native_id_evidence, '
        'title, message_count, turn_count, observed_first_at, observed_last_at, ended_at, coverage_state, source_archive_ids, projects, block_sha256, device_id) '
        "VALUES (%s, %s, 'live_session', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', %s, %s, %s, %s) "
        'ON CONFLICT (block_id) DO UPDATE SET adapter = EXCLUDED.adapter, title = EXCLUDED.title, message_count = EXCLUDED.message_count, '
        'turn_count = EXCLUDED.turn_count, observed_first_at = EXCLUDED.observed_first_at, observed_last_at = EXCLUDED.observed_last_at, '
        'ended_at = EXCLUDED.ended_at, source_archive_ids = EXCLUDED.source_archive_ids, projects = EXCLUDED.projects, '
        'block_sha256 = EXCLUDED.block_sha256, device_id = EXCLUDED.device_id',
        (block_id, adap, session_id, session_id, device_id, title[:80], title, msg_cnt, turn_cnt, first_at, last_at, last_at,
         json.dumps([int(archive_id)]), json.dumps([proj], ensure_ascii=False), sha256, device_id),
    )
    old_projs = [str(r[0]) for r in conn.execute('SELECT project FROM em_history.block_projects WHERE block_id = %s AND project != %s', (block_id, proj)).fetchall()]
    if old_projs:
        conn.execute('DELETE FROM em_history.block_projects WHERE block_id = %s AND project != %s', (block_id, proj))
    conn.execute("INSERT INTO em_history.block_projects (block_id, project, evidence) VALUES (%s, %s, 'live_sync') ON CONFLICT (block_id, project) DO NOTHING", (block_id, proj))
    conn.execute('DELETE FROM em_history.messages WHERE block_id = %s AND ordinal > %s', (block_id, msg_cnt))
    msg_params = [(block_id, i, m['role'], m['content'], m['recorded_at'] or fallback_ts, int(archive_id), i - 1) for i, m in enumerate(msgs, 1)]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO em_history.messages (block_id, ordinal, role, content, recorded_at, archive_id, message_index, attachments) VALUES (%s, %s, %s, %s, %s, %s, %s, '[]') "
            'ON CONFLICT (block_id, ordinal) DO UPDATE SET role = EXCLUDED.role, content = EXCLUDED.content, recorded_at = EXCLUDED.recorded_at, '
            'archive_id = EXCLUDED.archive_id, message_index = EXCLUDED.message_index '
            'WHERE em_history.messages.content IS DISTINCT FROM EXCLUDED.content OR em_history.messages.recorded_at IS DISTINCT FROM EXCLUDED.recorded_at',
            msg_params,
        )
    conn.execute(
        'INSERT INTO em_history.sources (archive_id, family_id, block_id, adapter, kind, external_session_id, native_session_id, '
        'native_device_id, selection_status, state, status, logical_bytes, manifest_bytes, manifest_sha256_verified, content_sha256_verified, native_hash_verified, messages) '
        "VALUES (%s, %s, %s, %s, 'live_session', %s, %s, %s, 'selected', 'available', 'ok', %s, %s, 1, 1, 1, %s) "
        'ON CONFLICT (archive_id) DO UPDATE SET block_id = EXCLUDED.block_id, messages = EXCLUDED.messages, logical_bytes = EXCLUDED.logical_bytes',
        (int(archive_id), session_id, block_id, adap, session_id, session_id, device_id, len(raw), len(raw), msg_cnt),
    )
    for target_proj in [*old_projs, proj]:
        _refresh_project_stats(conn, target_proj, now_iso)
    return {'synced': True, 'block_id': block_id, 'project': proj, 'messages': msg_cnt}


def sync_single_head(pool: PgHistoryPool, dbname: str, *, device_id: str, session_id: str, sha256: str) -> dict[str, Any]:
    """Sync one promoted upload into em_history inside its own writer transaction."""
    with pool.connection(dbname, role_kind='writer', statement_timeout_ms=20000) as conn:
        row = conn.execute(
            'SELECT u.archive_id, a.payload_path, u.source_format, u.project, u.declared_project, u.attribution_reason, a.created_at '
            'FROM em_runtime.lan_session_uploads u JOIN em_runtime.session_archives a ON a.id = u.archive_id '
            "WHERE u.device_id = %s AND u.session_id = %s AND u.sha256 = %s AND a.state = 'available'",
            (device_id, session_id, sha256),
        ).fetchone()
        if not row:
            return {'synced': False, 'reason': 'upload_or_archive_not_found'}
        with conn.transaction():
            return sync_session_archive_conn(
                conn, device_id=device_id, session_id=session_id, sha256=sha256,
                archive_id=int(row[0]), payload_path=str(row[1]), source_format_str=str(row[2] or CODEX_FORMAT),
                project=str(row[3] or ''), declared_project=str(row[4] or ''),
                attribution_reason=str(row[5] or ''), archive_created_at=str(row[6] or ''),
            )


def sync_pending_live_history(
    cfg: PgHistoryConfig, user_id: str = 'jiangli', *, limit: int = 300, pool: PgHistoryPool | None = None,
) -> dict[str, Any]:
    """Backfill/reconcile pending uploaded session heads since 2026-11-07 into em_history."""
    pg_pool = pool or PgHistoryPool(cfg)
    dbname = cfg.resolve_user_history_db(user_id)
    synced, skipped, errors = 0, 0, []
    with pg_pool.connection(dbname, role_kind='writer', statement_timeout_ms=30000) as conn:
        rows = conn.execute(
            'SELECT h.device_id, h.session_id, h.sha256, u.archive_id, a.payload_path, '
            '       u.source_format, u.project, u.declared_project, u.attribution_reason, a.created_at '
            'FROM em_runtime.lan_session_heads h '
            'JOIN em_runtime.lan_session_uploads u ON u.device_id = h.device_id AND u.session_id = h.session_id AND u.sha256 = h.sha256 '
            'JOIN em_runtime.session_archives a ON a.id = u.archive_id '
            "LEFT JOIN em_history.blocks b ON b.block_id = ('live-' || h.session_id) "
            "LEFT JOIN em_history.candidate_meta cm ON cm.key = ('live_sync_covered:' || h.session_id) "
            "WHERE a.state = 'available' AND COALESCE(u.attribution_reason, '') != 'subagent_session' "
            "  AND (u.received_at >= EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-11-07T00:00:00Z') "
            '       OR NOT EXISTS (SELECT 1 FROM em_history.blocks b0 WHERE b0.native_session_id = h.session_id)) '
            "  AND COALESCE(b.block_sha256, '') != h.sha256 AND COALESCE(cm.value, '') != h.sha256 "
            'ORDER BY u.received_at DESC LIMIT %s',
            (int(limit),),
        ).fetchall()
        for r in rows:
            try:
                with conn.transaction():
                    res = sync_session_archive_conn(
                        conn, device_id=str(r[0]), session_id=str(r[1]), sha256=str(r[2]),
                        archive_id=int(r[3]), payload_path=str(r[4]), source_format_str=str(r[5] or CODEX_FORMAT),
                        project=str(r[6] or ''), declared_project=str(r[7] or ''),
                        attribution_reason=str(r[8] or ''), archive_created_at=str(r[9] or ''),
                    )
                synced += 1 if res.get('synced') else 0
                skipped += 0 if res.get('synced') else 1
            except Exception as exc:
                errors.append(f'{r[1]}:{type(exc).__name__}')
    return {'synced': synced, 'skipped': skipped, 'errors': errors[:20]}
