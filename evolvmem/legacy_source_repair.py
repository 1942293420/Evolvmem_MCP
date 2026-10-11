"""Bounded local repair for uploads made before native sub-session validation.

Explicit preview/apply only: decrypting historical payloads is never part of a
recall request or background tick. Originals and human/shared knowledge remain.
"""
import json

from evolvmem.codex_transcript import SUBAGENT_SESSION_REASON, payload_is_subagent
from evolvmem.context_store import _now_iso


def _outputs(service, excluded):
    from evolvmem.auto_organization import _independent_source_link
    conn = service.store._connection()
    if not excluded:
        return [], []
    keys = {f'archive:{i}' for i in excluded}
    candidates = {r['item_id'] for r in conn.execute(
        "SELECT s.item_id,s.archive_id FROM context_sources s JOIN context_items i ON i.id=s.item_id "
        "WHERE i.status='active' AND s.archive_id IS NOT NULL") if r['archive_id'] in excluded}
    downgrade, preserve = [], []
    for iid in sorted(candidates):
        human = conn.execute("SELECT 1 FROM context_project_resolutions WHERE item_id=? "
                             "AND decision_source='human'", (iid,)).fetchone()
        nonautomatic = conn.execute("SELECT 1 FROM context_sources WHERE item_id=? "
                                   "AND source_kind NOT IN ('session','organization','extraction') LIMIT 1",
                                   (iid,)).fetchone()
        units = conn.execute(
            "SELECT u.decision,t.source_key,t.status FROM organization_units u "
            "JOIN organization_tasks t ON t.id=u.task_id WHERE u.item_id=? "
            "UNION ALL SELECT u.decision,t.source_key,t.status FROM unit_derivations d "
            "JOIN organization_units u ON u.task_id=d.unit_task_id AND u.digest=d.unit_digest "
            "JOIN organization_tasks t ON t.id=u.task_id WHERE d.item_id=?", (iid,iid)).fetchall()
        protected = human or nonautomatic or any(r['decision']=='manual' or
                                 (r['status']!='superseded' and r['source_key'] not in keys) for r in units)
        independent = _independent_source_link(conn, iid, own_keys=keys, own_archives=excluded)
        (preserve if protected or independent else downgrade).append(iid)
    return downgrade, preserve


def repair_subagents(service, *, after_id=0, limit=100, apply=False):
    """Preview (default) or repair a cursor page; return ids, never source text.

    Existing upload receipts are updated from verified native metadata. An old
    summary without an organization task is still an automatic output. All
    already-known excluded sources participate in the ownership check so two
    system sources cannot corroborate each other. Unavailable evidence is
    reported and left untouched. Repeating apply is idempotent.
    """
    from evolvmem.history_organization import _excluded_archive_ids, _has_uploads
    from evolvmem.auto_organization import _mirror_legacy_candidate, reconcile_sources
    from evolvmem.session_archive import SessionArchiver
    conn = service.store._connection()
    limit = min(200, max(1, int(limit)))
    after_id = max(0, int(after_id))
    report = dict(examined=0, after_id=after_id, has_more=False, applied=bool(apply),
                  excluded_archive_ids=[], unavailable_archive_ids=[],
                  downgraded_item_ids=[], preserved_item_ids=[], model_calls=0)
    if not _has_uploads(service):
        return report
    rows = conn.execute(
        "SELECT a.id FROM session_archives a WHERE a.adapter='codex' AND a.id>? "
        "AND EXISTS(SELECT 1 FROM lan_session_uploads u WHERE u.archive_id=a.id) "
        "ORDER BY a.id LIMIT ?", (after_id,limit+1)).fetchall()
    report['has_more'] = len(rows)>limit
    known = _excluded_archive_ids(service)
    for row in rows[:limit]:
        aid = row['id']
        report['after_id'] = aid
        report['examined'] += 1
        if aid not in known:
            raw = SessionArchiver(service.config, service.store).read_payload(aid)
            try:
                payload = json.loads(raw) if raw else None
            except (ValueError,TypeError):
                payload = None
            if not isinstance(payload, dict):
                report['unavailable_archive_ids'].append(aid)
                continue
            if not payload_is_subagent(payload):
                continue
        report['excluded_archive_ids'].append(aid)
    excluded = known | set(report['excluded_archive_ids'])
    # Recheck protection and apply in one transaction; previews never write.
    if not apply:
        report['downgraded_item_ids'], report['preserved_item_ids'] = _outputs(service, excluded)
        return report
    with service.store.transaction():
        for aid in report['excluded_archive_ids']:
            conn.execute("UPDATE lan_session_uploads SET attribution_reason=?,project='',"
                         "extraction_status='excluded',backfill_status='excluded' WHERE archive_id=?",
                         (SUBAGENT_SESSION_REASON,aid))
            human = conn.execute("SELECT 1 FROM knowledge_cleaning_reviews WHERE source_key=? "
                                 "AND state IN ('ready','assigned')", (f'archive:{aid}',)).fetchone()
            if not human:
                conn.execute("UPDATE session_archives SET project='' WHERE id=?", (aid,))
        downgrade, preserve = _outputs(service, excluded)
        for iid in downgrade:
            conn.execute("UPDATE context_items SET status='candidate',updated_at=? WHERE id=? AND status='active'",
                         (_now_iso(),iid))
        _mirror_legacy_candidate(service, downgrade)
    if downgrade:
        service.knowledge()._sync(downgrade)
    report.update(downgraded_item_ids=downgrade, preserved_item_ids=preserve)
    report['reconciliation'] = reconcile_sources(service, limit=limit)
    return report
