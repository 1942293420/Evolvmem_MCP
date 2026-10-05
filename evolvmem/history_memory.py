"""Clean dialogue stored in SQLite; encrypted transport evidence is separate."""
from __future__ import annotations

import hashlib
import json

from evolvmem.conversation import from_payload, render
from evolvmem.context_store import _now_iso
from evolvmem.extraction_policy import redact_messages
from evolvmem.project_ownership import load_ownership, UNREVIEWED_FACT


def save_clean(store, archive_id, payload, *, policy=None):
    """Called in the archive transaction. Unknown technical payloads stay technical."""
    try:
        messages, _ = redact_messages(from_payload(payload, policy=policy))
    except (ValueError, KeyError, TypeError):
        return False
    body = render(messages)
    encoded = json.dumps(messages, ensure_ascii=False)
    store._connection().execute(
        'INSERT INTO conversation_history VALUES(?,?,?,?,?) ON CONFLICT(archive_id) DO UPDATE SET '
        'messages=excluded.messages,body=excluded.body,content_hash=excluded.content_hash,cleaned_at=excluded.cleaned_at',
        (archive_id, encoded, body, hashlib.sha256(encoded.encode()).hexdigest(), _now_iso()))
    return True


def read(service, project, archive_id):
    result = _read_original(service, project, archive_id)
    from evolvmem.knowledge_cleaning import review
    cleaned = review(service, f'archive:{archive_id}')
    if cleaned and cleaned['state'] == 'assigned':
        result['cleaning'] = {'text': cleaned['cleaned_text'], 'category': cleaned['category'],
                              'updated_at': cleaned['updated_at']}
    return result


def _read_original(service, project, archive_id):
    archive = service.store.get_session_archive(archive_id)
    if not archive or archive['project'] != project or not project:
        raise ValueError('conversation_not_in_project')
    row = service.store._connection().execute('SELECT * FROM conversation_history WHERE archive_id=?', (archive_id,)).fetchone()
    if row:
        return {'archive_id': archive_id, 'project': project, 'available': True,
                'messages': json.loads(row['messages']), 'text': row['body'],
                'revision': row['content_hash'], 'storage': 'database'}
    # Backward compatible read does not silently mutate old archives.
    from evolvmem.session_archive import SessionArchiver
    payload = SessionArchiver(service.config, service.store).read_payload(archive_id)
    if payload is not None:
        try:
            messages, _ = redact_messages(from_payload(payload, policy=service.knowledge().rules.read()))
            return {'archive_id': archive_id, 'project': project, 'available': True,
                    'messages': messages, 'text': render(messages), 'storage': 'legacy_clean_view'}
        except (ValueError, KeyError, TypeError):
            pass
    return {'archive_id': archive_id, 'available': False, 'messages': [],
            'text': '来源已过期或暂不可读取，尚无可恢复的清洗后正文。', 'storage': 'unavailable'}


def migrate(service, options):
    """Bounded, local, idempotent conversion. No provider calls or source deletions."""
    project = str(options.get('project') or '')
    limit = min(100, max(1, int(options.get('limit', 50))))
    after = max(0, int(options.get('after_id', 0)))
    conn = service.store._connection()
    where, args = ['h.archive_id IS NULL', 'a.id>?', "a.project!=''"], [after]
    if project:
        where.append('a.project=?'); args.append(project)
    rows = conn.execute('SELECT a.id FROM session_archives a LEFT JOIN conversation_history h ON h.archive_id=a.id WHERE '
                        + ' AND '.join(where) + ' ORDER BY a.id', args).fetchall()
    # The reader exposes one current snapshot per logical conversation. Migrate
    # that same set, never all transport versions or unassigned generic logs.
    projects = [project] if project else [r[0] for r in conn.execute("SELECT DISTINCT project FROM session_archives WHERE project!=''")]
    visible = {row['id'] for name in projects for row in sessions(service, name)}
    rows = [row for row in rows if row['id'] in visible][:limit]
    from evolvmem.session_archive import SessionArchiver
    archiver = SessionArchiver(service.config, service.store)
    result = {'migrated': 0, 'unavailable': [], 'after_id': after, 'processed': len(rows)}
    for row in rows:
        payload = archiver.read_payload(row['id'])
        result['after_id'] = row['id']
        if payload is None:
            result['unavailable'].append(row['id'])
            continue
        with service.store.transaction():
            saved = save_clean(service.store, row['id'], payload, policy=service.knowledge().rules.read())
        if saved:
            result['migrated'] += 1
        else:
            result['unavailable'].append(row['id'])
    result['has_more'] = len(rows) == limit
    return result


def sessions(service, project):
    conn = service.store._connection()
    rows = conn.execute('SELECT a.*,h.content_hash,h.cleaned_at FROM session_archives a '
                        'LEFT JOIN conversation_history h ON h.archive_id=a.id WHERE a.project=? '
                        'ORDER BY a.created_at DESC,a.id DESC', (project,)).fetchall()
    jobs = {}
    ignored = set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='lan_session_uploads'").fetchone():
        jobs = {r['archive_id']: r['extraction_status'] for r in conn.execute('SELECT archive_id,extraction_status FROM lan_session_uploads WHERE archive_id IS NOT NULL')}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='lan_session_heads'").fetchone():
            ignored = {r[0] for r in conn.execute('SELECT u.archive_id FROM lan_session_uploads u '
                'JOIN lan_session_heads h ON h.device_id=u.device_id AND h.session_id=u.session_id '
                'WHERE h.sha256!=u.sha256 AND u.archive_id IS NOT NULL')}
    result, seen = [], set()
    for row in rows:
        if row['id'] in ignored:
            continue
        key = (row['adapter'], row['external_session_id'].split(':')[0] if row['adapter'] == 'codex' else row['external_session_id'])
        if key in seen:
            continue
        seen.add(key)
        linked = conn.execute("SELECT DISTINCT i.id,l.content FROM context_sources s JOIN context_items i ON i.id=s.item_id "
            "JOIN context_layers l ON l.item_id=i.id AND l.layer='l1' WHERE s.archive_id=? AND i.project=? "
            "AND i.status='active' AND i.content_type='session_summary'", (row['id'], project)).fetchall()
        facts = load_ownership(service.store, [r['id'] for r in linked])
        linked = [r for r in linked if not facts.get(r['id'], UNREVIEWED_FACT).excluded]
        summary = '\n'.join(r['content'] for r in linked)
        stage = '已入库' if summary else {
            'pending': '等待清洗提炼', 'processing': '清洗提炼中', 'unassigned': '归属待确认',
            'failed': '提炼失败 · 待重试', 'not_requested': '已同步 · 尚未提炼',
            'extracted': '已提炼 · 尚无入库摘要', 'superseded': '历史快照',
        }.get(jobs.get(row['id']), '已同步 · 尚无入库摘要')
        from evolvmem.knowledge_cleaning import review
        cleaned = review(service, f"archive:{row['id']}")
        if not summary and cleaned and cleaned['state'] == 'assigned':
            stage = {'pending': '清洗已确认 · 待提炼', 'processing': '清洗已确认 · 提炼中'}.get(jobs.get(row['id']), stage)
        result.append({'id': row['id'], 'adapter': row['adapter'], 'created_at': row['created_at'],
                       'state': row['state'], 'summary': summary, 'stage': stage,
                       'source_ids': [r['id'] for r in linked],
                       'storage': 'database' if row['content_hash'] else 'pending_migration'})
    return result
