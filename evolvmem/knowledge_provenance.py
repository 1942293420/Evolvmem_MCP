"""Restore lost source pointers only from exact, existing legacy mappings."""


def restore_legacy_sources(store, *, apply=True):
    conn = store._connection()
    rows = conn.execute(
        'SELECT DISTINCT i.id,m.source_session FROM context_items i '
        'JOIN legacy_memory_migrations a ON a.context_item_id=i.id '
        'JOIN memories m ON m.id=a.legacy_memory_id '
        "JOIN context_layers l ON l.item_id=i.id AND l.layer='l2' "
        "WHERE i.status='active' AND l.content=replace(m.value,char(13)||char(10),char(10)) "
        "AND trim(COALESCE(m.source_session,''))!='' "
        'AND NOT EXISTS(SELECT 1 FROM context_sources s WHERE s.item_id=i.id) '
        'AND NOT EXISTS(SELECT 1 FROM unit_derivations d WHERE d.item_id=i.id) '
        'ORDER BY i.id').fetchall()
    ids = []
    if apply:
        with store.transaction():
            for row in rows:
                store.record_migration_source(row['id'], source_ref=row['source_session'],
                    extraction_version='legacy-source-repair.v1')
                ids.append(row['id'])
    return {'eligible_ids': [r['id'] for r in rows], 'restored_ids': ids}
