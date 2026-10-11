"""Repair only proven legacy provenance gaps; preserve sources and confirmation."""
import json


def unconfirmed_rule(store, item_id, category, payload):
    """Known old inference is not an instruction; unknown legacy rows stay intact."""
    if category not in ('habit', 'project_convention', 'task_requirement') or not payload:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    evidence = data.get('evidence', [])
    if (data.get('basis') != 'inferred' or not evidence
            or not isinstance(evidence, list)
            or any(not isinstance(e, dict) or e.get('role') not in ('assistant', 'tool') for e in evidence)):
        return False
    conn = store._connection()
    protected = conn.execute("SELECT 1 FROM context_project_resolutions WHERE item_id=? "
                             "AND decision_source='human' AND review_state='accepted'", (item_id,)).fetchone()
    manual = conn.execute("SELECT 1 FROM context_sources WHERE item_id=? AND source_kind='manual'", (item_id,)).fetchone()
    return not (protected or manual)


def repair_unconfirmed_rules(service, *, after_id=0, limit=100, apply=False):
    """Explicit bounded preview/apply; retain history, sources and human decisions."""
    from evolvmem.context_store import _now_iso
    from evolvmem.auto_organization import _mirror_legacy_candidate
    limit = min(200, max(1, int(limit)))
    conn = service.store._connection()
    rows = conn.execute('SELECT i.id,l.category,l.payload FROM context_items i '
                        "JOIN learning_memories l ON l.item_id=i.id WHERE i.status='active' AND i.id>? "
                        'ORDER BY i.id LIMIT ?', (max(0, int(after_id)), limit+1)).fetchall()
    ids = [r['id'] for r in rows[:limit] if unconfirmed_rule(service.store, r['id'], r['category'], r['payload'])]
    updated = []
    if apply:
        with service.store.transaction():
            for ident in ids:
                row = conn.execute("SELECT l.category,l.payload FROM learning_memories l JOIN context_items i "
                                   "ON i.id=l.item_id WHERE i.id=? AND i.status='active'", (ident,)).fetchone()
                if row is None or not unconfirmed_rule(service.store, ident, row['category'], row['payload']):
                    continue
                conn.execute("UPDATE context_items SET status='candidate',updated_at=? WHERE id=?", (_now_iso(), ident))
                service.knowledge()._stamp(ident, reason='旧自动约定只有助手或工具转述，缺少用户原话，保留来源待核对')
                updated.append(ident)
            _mirror_legacy_candidate(service, updated)
        if updated:
            service.knowledge()._sync(updated)
    return dict(examined=min(len(rows), limit), after_id=rows[min(len(rows), limit)-1]['id'] if rows else after_id,
                has_more=len(rows)>limit, eligible_ids=ids, updated_ids=updated)


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
