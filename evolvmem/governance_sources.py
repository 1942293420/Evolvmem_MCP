"""Owner-authorized content reads, independent of client project attribution.

An enrollment never confirms ownership. It binds the original archive identity;
cleaning/body and task versions are evaluated afresh by the existing pipeline.
Public unassigned classification retains its original guard.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

TABLE = 'governance_source_enrollments'
AUDIT = 'governance_source_enrollment_audit'
SCHEMA = (
    f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        archive_id INTEGER PRIMARY KEY REFERENCES session_archives(id),
        identity_sha256 TEXT NOT NULL, origin TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)''',
    f'''CREATE TABLE IF NOT EXISTS {AUDIT}(
        plan_sha256 TEXT NOT NULL, archive_id INTEGER NOT NULL,
        state TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(plan_sha256,archive_id))''',
)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def _service(conn):
    return SimpleNamespace(store=SimpleNamespace(_connection=lambda: conn))


def identity(archive):
    return _hash({key: archive[key] for key in
                  ('id', 'project', 'adapter', 'external_session_id', 'payload_sha256', 'payload_path', 'created_at')})


def _grants(conn):
    return {r['archive_id']: r for r in _rows(conn, f'SELECT * FROM {TABLE}')} if _exists(conn, TABLE) else {}


def current_archives(service, *, excluded=None):
    from evolvmem.history_organization import _current_archives
    return _current_archives(service, excluded=excluded)


def valid_ids(service, *, current=None):
    """Metadata-only; compatible with ReadStore and databases without this schema."""
    conn = service.store._connection()
    grants = _grants(conn)
    if not grants:
        return set()
    current = current if current is not None else current_archives(service)
    current_ids = {r['id'] for r in current}
    deleted = {r[0] for r in conn.execute("SELECT source_key FROM knowledge_cleaning_reviews WHERE state='deleted'")}
    return {r['id'] for r in conn.execute('SELECT * FROM session_archives')
            if r['id'] in grants and r['id'] in current_ids and r['state'] == 'available'
            and f"archive:{r['id']}" not in deleted
            and identity(r) == grants[r['id']]['identity_sha256']}


def visible_ids(service, *, excluded=None):
    current = current_archives(service, excluded=excluded)
    grants = _grants(service.store._connection())
    # A changed enrolled origin must not regain access by clearing its project
    # field and falling through to the ordinary unassigned source path.
    return {r['id'] for r in current if not r['project'] and r['id'] not in grants} | valid_ids(service, current=current)


def allows(service, archive, *, visible=None):
    conn = service.store._connection()
    if not _exists(conn, TABLE) or archive['state'] != 'available':
        return False
    row = conn.execute(f'SELECT identity_sha256 FROM {TABLE} WHERE archive_id=?', (archive['id'],)).fetchone()
    if not row or row[0] != identity(archive):
        return False
    if visible is None:
        visible = visible_ids(service)
    return archive['id'] in visible


def pending_refs(service, *, include_unenrolled=False, after_id=0):
    from evolvmem import history_organization as history
    handled = history._handled_sources(service)
    deleted = history._deleted_sources(service)
    current = current_archives(service)
    allowed = valid_ids(service, current=current)
    available = {r[0] for r in service.store._connection().execute("SELECT id FROM session_archives WHERE state='available'")}
    return [{'key': f"archive:{r['id']}", 'created_at': r['created_at'],
             'adapter': r['adapter'], 'external': r['external_session_id']}
            for r in current if r['project'] and r['id'] > after_id and r['id'] in available
            and (include_unenrolled or r['id'] in allowed)
            and f"archive:{r['id']}" not in handled | deleted]


def _fingerprint_rows(conn, table, where, args, *, text_fields=(), order='rowid'):
    """Hash long derived text using bounded SQLite slices, never whole bodies."""
    columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
    selected = [name for name in columns if name not in text_fields]
    result = _rows(conn, f"SELECT rowid AS _rid,{','.join(selected)} FROM {table} WHERE {where} ORDER BY {order}", args)
    for row in result:
        rid = row.pop('_rid')
        for field in text_fields:
            length = conn.execute(f'SELECT length({field}) FROM {table} WHERE rowid=?', (rid,)).fetchone()[0]
            digest = hashlib.sha256()
            if length is not None:
                for offset in range(1, length + 1, 65536):
                    chunk = conn.execute(f'SELECT substr({field},?,65536) FROM {table} WHERE rowid=?', (offset, rid)).fetchone()[0]
                    digest.update(chunk.encode())
            row[field] = {'length': length, 'sha256': digest.hexdigest()}
    return result


def _stamp(conn, archive):
    aid = archive['id']; key = f'archive:{aid}'
    clean = _fingerprint_rows(conn, 'conversation_history', 'archive_id=?', (aid,), text_fields=('body', 'messages'))
    review = _fingerprint_rows(conn, 'knowledge_cleaning_reviews', 'source_key=?', (key,), text_fields=('source_text', 'cleaned_text'))
    links = _rows(conn, 'SELECT * FROM context_sources WHERE archive_id=? ORDER BY id', (aid,))
    linked = []
    for item_id in sorted({r['item_id'] for r in links}):
        item = _rows(conn, 'SELECT * FROM context_items WHERE id=?', (item_id,))
        if item:
            item[0].pop('access_count', None); item[0].pop('last_accessed', None)
        linked.append([item, _rows(conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (item_id,)),
                       _fingerprint_rows(conn, 'context_layers', 'item_id=?', (item_id,), text_fields=('content',), order='layer')])
    grant = _grants(conn).get(aid)
    return _hash([dict(archive), clean, review, links, linked, grant])


def _database(conn):
    raw = conn.execute('PRAGMA database_list').fetchone()['file']
    path = Path(raw).resolve(strict=True)
    stat = path.stat()
    return {'path': str(path), 'device': stat.st_dev, 'inode': stat.st_ino}


def plan_hash(plan):
    return _hash({k: v for k, v in plan.items() if k != 'plan_sha256'})


def build_plan(conn, *, keys=None, origin='owner_migration'):
    if origin not in {'owner_migration', 'auto_new'}:
        raise ValueError('invalid_enrollment_origin')
    service = _service(conn)
    current = current_archives(service)
    valid = valid_ids(service, current=current)
    grants = _grants(conn)
    deleted = {r[0] for r in conn.execute("SELECT source_key FROM knowledge_cleaning_reviews WHERE state='deleted'")}
    selected = set(keys) if keys is not None else None
    entries = []
    for row in current:
        aid = row['id']; key = f'archive:{aid}'
        if selected is not None and key not in selected:
            continue
        archive = conn.execute('SELECT * FROM session_archives WHERE id=?', (aid,)).fetchone()
        if ((not archive['project'] and aid not in grants) or archive['state'] != 'available'
                or aid in valid or key in deleted):
            continue
        entries.append({'source_key': key, 'archive_id': aid, 'identity_sha256': identity(archive),
                        'expected_sha256': _stamp(conn, archive)})
    entries.sort(key=lambda r: r['archive_id'])
    result = {'version': 1, 'kind': 'owner_source_enrollment', 'database': _database(conn),
              'origin': origin, 'entries': entries, 'summary': {'selected': len(entries)}}
    result['plan_sha256'] = plan_hash(result)
    return result


def _validate(conn, plan):
    if (plan.get('version') != 1 or plan.get('kind') != 'owner_source_enrollment'
            or plan.get('plan_sha256') != plan_hash(plan)
            or plan.get('origin') not in {'owner_migration', 'auto_new'}
            or len({e['archive_id'] for e in plan['entries']}) != len(plan['entries'])):
        raise ValueError('invalid_enrollment_plan')
    if plan.get('database') != _database(conn):
        raise ValueError('enrollment_database_revision_conflict')


def apply_plan(service, plan, *, limit=20):
    conn = service.store._connection(); _validate(conn, plan)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_enrollment_limit')
    from evolvmem.context_store import _now_iso
    with service.store.transaction():
        for statement in SCHEMA:
            conn.execute(statement)
        audit = {r['archive_id']: r for r in _rows(conn, f'SELECT * FROM {AUDIT} WHERE plan_sha256=?', (plan['plan_sha256'],))}
        if any(r['state'] == 'rolled_back' for r in audit.values()):
            raise ValueError('enrollment_plan_rolled_back')
        current_grants = _grants(conn); live_ids = valid_ids(service)
        if any(current_grants.get(aid) != json.loads(row['after_json']) or aid not in live_ids
               for aid, row in audit.items()):
            raise ValueError('enrollment_revision_conflict')
        selected = [e for e in plan['entries'] if e['archive_id'] not in audit][:limit]
        fresh = build_plan(conn, keys=[e['source_key'] for e in selected], origin=plan['origin'])
        if fresh['entries'] != selected:
            raise ValueError('enrollment_revision_conflict')
        old = _grants(conn)
        for entry in selected:
            aid = entry['archive_id']; before = old.get(aid); now = _now_iso()
            after = {'archive_id': aid, 'identity_sha256': entry['identity_sha256'], 'origin': plan['origin'],
                     'plan_sha256': plan['plan_sha256'], 'revision': (before['revision'] if before else 0) + 1,
                     'created_at': now}
            conn.execute(f'INSERT OR REPLACE INTO {TABLE} VALUES(?,?,?,?,?,?)', tuple(after.values()))
            conn.execute(f'INSERT INTO {AUDIT} VALUES(?,?,?,?,?,?,?)',
                         (plan['plan_sha256'], aid, 'applied', json.dumps(before), json.dumps(after), now, now))
    return {'plan_sha256': plan['plan_sha256'], 'applied': len(selected), 'already_applied': len(audit),
            'remaining': len(plan['entries']) - len(audit) - len(selected), 'ownership_confirmed': False}


def rollback_plan(service, plan):
    conn = service.store._connection(); _validate(conn, plan)
    from evolvmem.context_store import _now_iso
    with service.store.transaction():
        audit = _rows(conn, f'SELECT * FROM {AUDIT} WHERE plan_sha256=?', (plan['plan_sha256'],)) if _exists(conn, AUDIT) else []
        active = [r for r in audit if r['state'] == 'applied']; grants = _grants(conn)
        for row in active:
            if grants.get(row['archive_id']) != json.loads(row['after_json']):
                raise ValueError('enrollment_revision_conflict')
        for row in active:
            before = json.loads(row['before_json'])
            if before:
                conn.execute(f'INSERT OR REPLACE INTO {TABLE} VALUES(?,?,?,?,?,?)',
                             tuple(before[k] for k in ('archive_id', 'identity_sha256', 'origin', 'plan_sha256', 'revision', 'created_at')))
            else:
                conn.execute(f'DELETE FROM {TABLE} WHERE archive_id=?', (row['archive_id'],))
            conn.execute(f"UPDATE {AUDIT} SET state='rolled_back',updated_at=? WHERE plan_sha256=? AND archive_id=?",
                         (_now_iso(), plan['plan_sha256'], row['archive_id']))
    return {'plan_sha256': plan['plan_sha256'], 'rolled_back': len(active), 'already_rolled_back': len(audit)-len(active)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if args.operation != 'prepare' and not args.plan:
        parser.error('apply/rollback requires --plan')
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('enrollment_receipt_exists')
    conn = sqlite3.connect(args.db.resolve(strict=True).as_uri() + ('?mode=ro' if args.operation == 'prepare' else '?mode=rw'), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.operation == 'prepare':
            conn.execute('PRAGMA query_only=ON'); conn.execute('BEGIN')
            result = build_plan(conn); conn.rollback()
        else:
            from evolvmem.config import Config
            from evolvmem.context_store import ContextStore
            store = ContextStore(Config(data_dir=args.db.parent, apply_environment=False)); store._conn = conn
            service = SimpleNamespace(store=store)
            plan = json.loads(args.plan.read_text())
            result = apply_plan(service, plan, limit=args.limit) if args.operation == 'apply' else rollback_plan(service, plan)
        with args.output.open('x') as stream:
            os.chmod(args.output, 0o600); json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'operation': args.operation, 'output': str(args.output),
                          'plan_sha256': result['plan_sha256'], **result.get('summary', {})}))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
