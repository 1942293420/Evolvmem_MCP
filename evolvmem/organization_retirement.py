"""Retired automatic results remain history, never a new human review task."""
from evolvmem.context_store import _now_iso

RETIRED_REASON = '已被后续整理替代，保留为历史版本，无需审核'

def human_owned(conn, item_id):
    return bool(conn.execute(
        "SELECT 1 FROM context_project_resolutions WHERE item_id=? AND decision_source='human' "
        "UNION ALL SELECT 1 FROM organization_units WHERE item_id=? AND decision='manual' "
        "UNION ALL SELECT 1 FROM unit_derivations d JOIN organization_units u "
        "ON u.task_id=d.unit_task_id AND u.digest=d.unit_digest "
        "WHERE d.item_id=? AND u.decision='manual' "
        "UNION ALL SELECT 1 FROM context_layers WHERE item_id=? AND generator='knowledge-edit.v1' LIMIT 1",
        (item_id, item_id, item_id, item_id)).fetchone())


def mark_retired(service, item_id):
    """Caller holds the transaction and verified that no live source backs it."""
    conn = service.store._connection()
    if human_owned(conn, item_id):
        return False
    changed = conn.execute("UPDATE context_items SET status='superseded',updated_at=? "
                           "WHERE id=? AND status IN ('active','candidate')", (_now_iso(), item_id)).rowcount
    if not changed:
        return False
    if service.store.legacy_memory_table_exists():
        conn.execute("UPDATE memories SET status='superseded',updated_at=? WHERE status IN ('active','candidate') "
                     "AND id IN (SELECT legacy_memory_id FROM legacy_memory_migrations WHERE context_item_id=?)",
                     (_now_iso(), item_id))
    conn.execute("UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?",
                 (RETIRED_REASON, item_id))
    return True


def _eligible(service, item_id):
    from evolvmem.auto_organization import _has_live_output_owner, _independent_source_link, _own_archive_ids
    from evolvmem import conversation_tasks
    conn = service.store._connection()
    item = conn.execute('SELECT status FROM context_items WHERE id=?', (item_id,)).fetchone()
    if not item or item['status'] not in ('active', 'candidate') or human_owned(conn, item_id):
        return False
    if _has_live_output_owner(conn, item_id):
        return False
    keys = {r['source_key'] for r in conn.execute(
        "SELECT t.source_key FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
        "WHERE u.item_id=? AND t.status='superseded' UNION "
        "SELECT t.source_key FROM unit_derivations d JOIN organization_tasks t ON t.id=d.unit_task_id "
        "WHERE d.item_id=? AND t.status='superseded'", (item_id, item_id))}
    if not keys:
        return False
    own_keys, archives = set(keys), set()
    for key in keys:
        archives.update(_own_archive_ids(key))
        if conversation_tasks.is_conversation(key):
            try:
                members = conversation_tasks.members(service, key)
            except ValueError:
                return False
            own_keys.update(members)
            for member in members:
                archives.update(_own_archive_ids(member))
    return not _independent_source_link(conn, item_id, own_keys=own_keys, own_archives=archives)


def repair_retired_outputs(service, *, after_id=0, limit=100, apply=False):
    """Local bounded preview/apply for old candidate rows; never invokes a model.

    Cursor progress includes protected rows so they cannot starve later items.
    Apply rechecks provenance and human decisions inside the write transaction.
    """
    limit = min(200, max(1, int(limit)))
    after_id = max(0, int(after_id))
    conn = service.store._connection()
    rows = conn.execute("SELECT i.id FROM context_items i WHERE i.id>? AND i.status IN ('active','candidate') "
        "AND i.id IN (SELECT u.item_id FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
        "WHERE u.item_id IS NOT NULL AND t.status='superseded' UNION "
        "SELECT d.item_id FROM unit_derivations d JOIN organization_tasks t ON t.id=d.unit_task_id "
        "WHERE t.status='superseded') ORDER BY i.id LIMIT ?",
        (after_id, limit+1)).fetchall()
    ids = [r['id'] for r in rows[:limit]]
    eligible = [i for i in ids if _eligible(service, i)]
    updated = []
    if apply:
        with service.store.transaction():
            for item_id in eligible:
                if _eligible(service, item_id) and mark_retired(service, item_id):
                    updated.append(item_id)
        if updated:
            service.knowledge()._sync(updated)
    return {'examined': len(ids), 'after_id': ids[-1] if ids else after_id,
            'has_more': len(rows)>limit, 'eligible_ids': eligible, 'updated_ids': updated}
