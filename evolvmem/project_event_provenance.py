"""Current project-event support, sharing source checks but never memory IDs."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from evolvmem.capability_provenance import ProvenanceRead, context_revision


def _legacy_sources(store, event, read, *, context_only=False):
    conn = store._connection()
    ids = {r[0] for r in conn.execute(
        "SELECT legacy_item_id FROM knowledge_restructure_map WHERE target_kind='event' AND target_id=?",
        (str(event["event_id"]),))}
    match = re.fullmatch(r"context(?:_item)?:([1-9][0-9]*)", event["source_ref"])
    if match:
        ids.add(int(match[1]))
    sources = [{"source_kind": "context", "source_key": f"context:{item_id}",
                "source_ref": f"context_item:{item_id}", "context_item_id": item_id,
                "source_revision": context_revision(conn, item_id)} for item_id in sorted(ids)]
    if context_only:
        return sources
    parsed = re.fullmatch(r"((?:conversation|archive|item):[^#]+)#([^@#]+)(?:@([0-9]+)-([0-9]+))?",
                          event["source_ref"])
    if parsed:
        key, digest = parsed[1], parsed[2]
        rows = conn.execute(
            "SELECT t.id,t.source_revision,u.revision FROM organization_tasks t JOIN organization_units u "
            "ON u.task_id=t.id WHERE t.source_key=? AND u.digest=? AND t.status!='superseded'",
            (key, digest)).fetchall()
        if len(rows) == 1:
            row = rows[0]
            sources.append({"source_kind": "unit", "source_key": f"{key}#{digest}",
                            "source_ref": event["source_ref"], "source_revision": row["source_revision"],
                            "task_id": row["id"], "unit_digest": digest, "unit_revision": row["revision"]})
        elif rows:
            sources.append({"source_kind": "reference", "source_key": event["source_ref"],
                            "source_ref": event["source_ref"], "legacy_reason": "ambiguous_current_unit"})
    return sources or [{"source_kind": "reference", "source_key": event["source_ref"],
                        "source_ref": event["source_ref"], "legacy_reason": "untraceable_legacy_source"}]


def annotate(store, events):
    """Batch annotate supplied event rows using one transient provenance reader."""
    read = ProvenanceRead(store)
    conn = store._connection()
    exists = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_event_sources'").fetchone())
    bound = {}
    ids = [e["event_id"] for e in events]
    if exists:
        for start in range(0, len(ids), 500):
            batch = ids[start:start+500]
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(f"SELECT * FROM project_event_sources WHERE event_id IN ({marks}) ORDER BY source_id", batch):
                source = dict(row)
                source["projects"] = json.loads(source["projects"])
                bound.setdefault(row["event_id"], []).append(source)
    out = {}
    for event in events:
        sources = list(bound.get(event["event_id"], []))
        identities = {(s["source_kind"], s["source_key"]) for s in sources}
        inferred = [s for s in _legacy_sources(store, event, read, context_only=bool(sources))
                    if (s["source_kind"], s["source_key"]) not in identities]
        sources.extend(inferred)
        diagnostics = []
        for source in sources:
            if source["source_kind"] == "manual":
                supported = (source.get("evidence_status") == "confirmed"
                             and source.get("knowledge_scope") == "project"
                             and source.get("projects") == [event["project"]]
                             and event["project"] in read.active_projects)
                fact = {"valid": supported, "ownership_confirmed": supported,
                        "projects": [event["project"]] if supported else [],
                        "reason": "" if supported else "manual_scope_unconfirmed"}
            else:
                fact = read.evaluate(source)
                supported = (fact["valid"] and fact["ownership_confirmed"]
                             and event["project"] in fact["projects"])
                if supported and source["source_kind"] == "unit":
                    from evolvmem.project_archive_pipeline import history_projects
                    unit = conn.execute("SELECT * FROM organization_units WHERE task_id=? AND digest=?",
                                        (source["task_id"], source["unit_digest"])).fetchone()
                    supported = bool(unit and event["project"] in history_projects(
                        SimpleNamespace(store=store), dict(unit)))
                if fact["valid"] and not supported:
                    fact = {**fact, "reason": "project_ownership_changed"}
            diagnostics.append({**fact, "current_support": supported,
                                "source_kind": source["source_kind"], "source_ref": source["source_ref"],
                                "source_key": source["source_key"]})
        out[event["event_id"]] = {
            "current_support": event["status"] == "active" and any(s["current_support"] for s in diagnostics),
            "source_diagnostics": diagnostics,
            "sources_count": len({(s["source_kind"], s["source_key"]) for s in sources}),
            "source_bindings_count": len(sources),
            "legacy_source_fallback": bool(inferred)}
    return out
