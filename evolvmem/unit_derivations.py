"""Which knowledge items one organization unit produced, and how they are shared.

A unit's derived rows are tracked explicitly so a correction can move or
supersede exactly the knowledge that came from that unit. Knowledge shared with
another source keeps its links: it is never blindly moved or deleted.
"""
from __future__ import annotations

from evolvmem.context_store import _now_iso


def record_derivation(service, task_id, digest, item_id, *, kind='', project=''):
    if not item_id:
        return
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT OR IGNORE INTO unit_derivations(unit_task_id,unit_digest,item_id,kind,project,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (int(task_id), str(digest), int(item_id), str(kind)[:40], str(project or ''), _now_iso()))


def derived_ids(service, task_id, digest):
    rows = service.store._connection().execute(
        'SELECT item_id,kind,project FROM unit_derivations WHERE unit_task_id=? AND unit_digest=?',
        (int(task_id), str(digest))).fetchall()
    return [dict(row) for row in rows]


def derived_for_digest(service, digest):
    """Every derivation of the same exact unit across task revisions."""
    rows = service.store._connection().execute(
        'SELECT unit_task_id,item_id,kind,project FROM unit_derivations WHERE unit_digest=?',
        (str(digest),)).fetchall()
    return [dict(row) for row in rows]


def currently_backed(store, item_id):
    """A withdrawn unit cannot keep supplying knowledge to current recall.

    An independent source or another current unit can still back a shared item.
    The source rows remain intact for provenance and restoration.
    """
    conn = store._connection()
    rows = conn.execute(
        'SELECT t.source_key,t.status,u.decision,u.disposition,u.project,i.project AS item_project '
        'FROM unit_derivations d JOIN organization_tasks t ON t.id=d.unit_task_id '
        'JOIN context_items i ON i.id=d.item_id LEFT JOIN organization_units u '
        'ON u.task_id=d.unit_task_id AND u.digest=d.unit_digest WHERE d.item_id=?', (item_id,)).fetchall()
    if not rows:
        return True
    if any(r['status'] != 'superseded' and r['decision'] in ('auto', 'manual')
           and r['disposition'] == 'keep' and r['project'] == r['item_project'] for r in rows):
        return True
    keys = {r['source_key'] for r in rows}
    archives = {int(k.split(':')[1]) for k in keys if k.startswith('archive:')}
    for source in conn.execute('SELECT * FROM context_sources WHERE item_id=?', (item_id,)):
        if source['source_kind'] == 'session' and source['archive_id'] in archives:
            continue
        if source['source_kind'] in ('organization', 'extraction') and any(
                source['source_ref'] == key or source['source_ref'].startswith(key + '#') for key in keys):
            continue
        if source['archive_id'] in archives:
            continue
        return True
    return False


def current_item_ids(store, item_ids):
    """Keep only items a current source still backs, for shared read paths.

    A re-segmentation, set-aside or project move can leave an item's derivation
    rows behind while no current unit references them any more. History listing
    and the project document must drop those items. An item with no
    organization derivation at all is ordinary history and is kept; anything
    else is decided by the single canonical ``currently_backed`` rule, so the
    derivation's stale ``project`` column can never mark a moved unit valid.
    """
    ids = [int(i) for i in item_ids if i is not None]
    if not ids:
        return []
    conn = store._connection()
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='unit_derivations'").fetchone():
        return ids
    placeholders = ','.join('?' * len(ids))
    has_derivation = {row[0] for row in conn.execute(
        'SELECT DISTINCT item_id FROM unit_derivations WHERE item_id IN (%s)' % placeholders, ids)}
    return [item_id for item_id in ids
            if item_id not in has_derivation or currently_backed(store, item_id)]


def qa_move_snapshot(service, item_id):
    """Snapshot a currently-valid Q&A before a pure project move.

    Returns the stored question/answer/origin only when the row is active, its
    answer still matches the item body, the item is still usable, it is not an
    experience, and the stored fingerprint matches the pre-move row. An
    already-stale row (for example a changed trigger or category) therefore
    returns None and is never reactivated. Shared items never reach the move
    branch, and a candidate, unverified or rejected row returns None too.
    """
    from evolvmem import qa_memory
    conn = service.store._connection()
    saved = conn.execute('SELECT question,answer,status,origin,source_fingerprint '
                         'FROM knowledge_qa WHERE item_id=?', (int(item_id),)).fetchone()
    if not saved or saved['status'] != 'active' or not saved['question']:
        return None
    detail = service.knowledge().detail(item_id)
    if detail['content_type'] == 'experience' or saved['answer'] != detail['body']:
        return None
    if not service.learning().usable(detail):
        return None
    if saved['source_fingerprint'] != qa_memory.fingerprint(detail):
        return None
    return {'question': saved['question'], 'answer': saved['answer'],
            'origin': saved['origin'] or 'extraction'}


def rerecord_moved_qa(service, item_id, snapshot) -> bool:
    """Re-record a snapshot through the normal Q&A path after the move.

    ``qa_memory.record`` recomputes the fingerprint and re-checks conflicts
    under the new project, so a conflicting answer becomes a candidate instead
    of leaving two active answers. The stored origin is preserved and
    ``approved`` restates the approval that already made this row active, so
    no candidate is promoted; a conflict may demote the moved Q&A.
    """
    from evolvmem import qa_memory
    if not snapshot:
        return False
    with service.store.transaction():
        qa_memory.record(service, item_id,
                         {'question': snapshot['question'], 'answer': snapshot['answer']},
                         origin=snapshot['origin'], approved=True)
    return True


def is_shared(service, item_id, *, archive_id=None, unit_ref='', session_ref='') -> bool:
    """True when a source link other than this unit's own provenance backs the item.

    The unit's own extraction adds an ``extraction`` row and an archive
    ``session`` row; those are the same source and must not be mistaken for
    independent corroboration.
    """
    rows = service.store._connection().execute(
        'SELECT source_kind,archive_id,source_ref FROM context_sources WHERE item_id=?',
        (int(item_id),)).fetchall()
    for row in rows:
        if row['source_kind'] == 'organization' and unit_ref and str(row['source_ref']).startswith(unit_ref):
            continue
        if row['source_kind'] == 'extraction' and session_ref and row['source_ref'] == session_ref:
            continue
        if row['source_kind'] == 'session' and archive_id is not None and row['archive_id'] == archive_id:
            continue
        return True
    return False


def retarget(service, item_id, project, *, archive_id=None, unit_ref='', session_ref='') -> bool:
    """Move one purely unit-derived item to the corrected project.

    Returns False when the item carries a source link from somewhere else, so the
    caller re-extracts instead of silently moving shared knowledge.
    """
    knowledge = service.knowledge()
    row = knowledge.detail(item_id)
    if row['project'] == project:
        return True
    if is_shared(service, item_id, archive_id=archive_id, unit_ref=unit_ref, session_ref=session_ref):
        return False
    knowledge.assign(item_id, {'project': project, 'expected_revision': row['revision']})
    return True


def update_project(service, task_id, digest, item_id, project):
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE unit_derivations SET project=? WHERE unit_task_id=? AND unit_digest=? AND item_id=?',
            (str(project or ''), int(task_id), str(digest), int(item_id)))


def supersede(service, item_id, reason) -> None:
    """Hide one unit-derived item from current retrieval, keeping its history."""
    knowledge = service.knowledge()
    row = knowledge.detail(item_id)
    if row['status'] == 'deleted':
        return
    knowledge.transition(item_id, {'expected_revision': row['revision'], 'action': 'archive'})
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?',
            (str(reason)[:500], int(item_id)))
