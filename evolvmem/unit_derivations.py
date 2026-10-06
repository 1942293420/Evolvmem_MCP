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
