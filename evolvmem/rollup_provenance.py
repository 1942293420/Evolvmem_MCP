"""Current recall must not treat a stale derived summary as current evidence."""
from evolvmem.context_store import _now_iso


def source_ids(store, item_id):
    return tuple(sorted({int(s['source_ref']) for s in store.list_item_sources(item_id)
                         if s['source_kind'] == 'context_reference' and s['source_ref'].isdigit()}))


def content_signature(store, ids):
    from evolvmem.source_qualification import rollup_content_signature
    if not ids:
        return rollup_content_signature(())
    marks = ','.join('?' for _ in ids)
    rows = store._connection().execute(
        'SELECT i.id,i.project,i.content_type,l.content FROM context_items i '
        "JOIN context_layers l ON l.item_id=i.id AND l.layer='l1' "
        f'WHERE i.id IN ({marks}) ORDER BY i.id', tuple(ids)).fetchall()
    return rollup_content_signature(rows)


def current(store, item_id):
    from evolvmem.source_qualification import summary_current
    conn = store._connection()
    summary = conn.execute('SELECT project,created_at FROM context_items WHERE id=?', (item_id,)).fetchone()
    ids = source_ids(store, item_id)
    if not ids:
        return True
    def rows():
        marks = ','.join('?' for _ in ids)
        return conn.execute(f'SELECT * FROM context_items WHERE id IN ({marks})', ids).fetchall()
    def backed(ident):
        from evolvmem.unit_derivations import currently_backed
        return currently_backed(store, ident)
    def unconfirmed(ident):
        from evolvmem.knowledge_provenance import unconfirmed_rule
        learning = conn.execute('SELECT category,payload FROM learning_memories WHERE item_id=?', (ident,)).fetchone()
        return bool(learning and unconfirmed_rule(store, ident, learning['category'], learning['payload']))
    def signature():
        rollup = conn.execute('SELECT source_set_hash FROM context_project_rollups WHERE current_context_id=?', (item_id,)).fetchone()
        return (rollup['source_set_hash'] or '') if rollup else ''
    return summary_current(summary, ids, rows, now=_now_iso(), backed=backed,
        unconfirmed=unconfirmed, signature=signature, content_signature=lambda:content_signature(store, ids))
