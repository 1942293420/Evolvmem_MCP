"""Current recall must not treat a stale derived summary as current evidence."""
import hashlib
import json

from evolvmem.context_store import _now_iso
from evolvmem.context_temporal import window_contains


def source_ids(store, item_id):
    return tuple(sorted({int(s['source_ref']) for s in store.list_item_sources(item_id)
                         if s['source_kind'] == 'context_reference' and s['source_ref'].isdigit()}))


def content_signature(store, ids):
    if not ids:
        return 'content-v1:' + hashlib.sha256(b'[]').hexdigest()
    marks = ','.join('?' for _ in ids)
    rows = store._connection().execute(
        'SELECT i.id,i.project,i.content_type,l.content FROM context_items i '
        "JOIN context_layers l ON l.item_id=i.id AND l.layer='l1' "
        f'WHERE i.id IN ({marks}) ORDER BY i.id', tuple(ids)).fetchall()
    return 'content-v1:' + hashlib.sha256(json.dumps([tuple(r) for r in rows], ensure_ascii=False).encode()).hexdigest()


def current(store, item_id):
    conn = store._connection()
    summary = conn.execute('SELECT project,created_at FROM context_items WHERE id=?', (item_id,)).fetchone()
    ids = source_ids(store, item_id)
    if not ids:
        return True  # Hand-authored/legacy summaries without derived links are unchanged.
    marks = ','.join('?' for _ in ids)
    rows = conn.execute(f'SELECT * FROM context_items WHERE id IN ({marks})', ids).fetchall()
    if summary is None or len(rows) != len(ids):
        return False
    from evolvmem.unit_derivations import currently_backed
    from evolvmem.knowledge_provenance import unconfirmed_rule
    now = _now_iso()
    for row in rows:
        if (row['status'] != 'active' or row['project'] != summary['project']
                or (row['expires_at'] and row['expires_at'] <= now)
                or not window_contains(row['effective_from'], row['effective_until'], now)
                or not currently_backed(store, row['id'])):
            return False
        learning = conn.execute('SELECT category,payload FROM learning_memories WHERE item_id=?', (row['id'],)).fetchone()
        if learning and unconfirmed_rule(store, row['id'], learning['category'], learning['payload']):
            return False
    rollup = conn.execute('SELECT source_set_hash FROM context_project_rollups WHERE current_context_id=?', (item_id,)).fetchone()
    signature = (rollup['source_set_hash'] or '') if rollup else ''
    if signature.startswith('content-v1:'):
        return signature == content_signature(store, ids)
    # Older snapshots have only an ID hash: never manufacture content proof.
    return all(row['updated_at'] <= summary['created_at'] for row in rows)
