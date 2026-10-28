"""Read/write models for the capability and project-archive workbench."""
from __future__ import annotations

import hashlib
import json

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import _now_iso
from evolvmem.project_archive_pipeline import get_project_archive, list_project_events


def _intake_public(row) -> dict:
    result = dict(row)
    result["keywords"] = json.loads(result["keywords"] or "[]")
    return result


def capabilities(service, params) -> dict:
    store = CapabilityStore(service.store)
    query = str(params.get("q", "")).strip()
    category = str(params.get("category", "")).strip()
    if query:
        items = store.search(query, (category,) if category else (), top_k=100)
    else:
        sql = "SELECT * FROM capability_memories WHERE status='active'"
        values = []
        if category:
            sql += " AND category=?"
            values.append(category)
        sql += " ORDER BY updated_at DESC,memory_id DESC LIMIT 200"
        items = [store._public(row) for row in store.conn.execute(sql, values)]
    intake = [_intake_public(row) for row in store.conn.execute(
        "SELECT * FROM capability_intake WHERE status='pending' ORDER BY created_at,intake_id LIMIT 200"
    )]
    suggestions = [dict(row) for row in store.conn.execute(
        "SELECT * FROM capability_category_suggestions WHERE status='pending' ORDER BY created_at,suggestion_id"
    )]
    return {
        "categories": store.categories(),
        "items": items,
        "intake": intake,
        "suggestions": suggestions,
        "total": len(items),
    }


def create_category_suggestion(service, body) -> int:
    return CapabilityStore(service.store).suggest_category(
        str(body.get("label", "")), str(body.get("reason", "")), body.get("source_memory_id")
    )


def review_category_suggestion(service, suggestion_id: int, action: str) -> dict:
    if action not in {"accept", "reject"}:
        raise ValueError("invalid_category_suggestion_action")
    conn = service.store._connection()
    with service.store.transaction():
        row = conn.execute(
            "SELECT * FROM capability_category_suggestions WHERE suggestion_id=? AND status='pending'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            raise ValueError("category_suggestion_not_found")
        category_id = ""
        if action == "accept":
            category_id = "custom_" + hashlib.sha256(row["label"].encode()).hexdigest()[:12]
            now = _now_iso()
            conn.execute(
                "INSERT OR IGNORE INTO capability_categories(id,label,created_at,updated_at) VALUES(?,?,?,?)",
                (category_id, row["label"], now, now),
            )
        conn.execute(
            "UPDATE capability_category_suggestions SET status=?,reviewed_at=? WHERE suggestion_id=?",
            ("accepted" if action == "accept" else "rejected", _now_iso(), suggestion_id),
        )
    return {"suggestion_id": suggestion_id, "status": "accepted" if action == "accept" else "rejected",
            "category_id": category_id}


def assign_intake(service, intake_id: int, body) -> dict:
    category = str(body.get("category", "")).strip()
    expected_revision = body.get("expected_revision")
    conn = service.store._connection()
    capabilities = CapabilityStore(service.store)
    capabilities.categories()
    with service.store.transaction():
        row = conn.execute(
            "SELECT * FROM capability_intake WHERE intake_id=?", (intake_id,)
        ).fetchone()
        if row is None:
            raise ValueError("capability_intake_not_found")
        if row["status"] != "pending" or row["revision"] != expected_revision:
            raise ValueError("revision_conflict")
        enabled = conn.execute(
            "SELECT enabled FROM capability_categories WHERE id=?", (category,)
        ).fetchone()
        if enabled is None or not enabled[0]:
            raise ValueError("capability_category_not_found")
        memory_id = CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
            category=category,
            title=row["title"],
            content=row["content"],
            keywords=tuple(json.loads(row["keywords"] or "[]")),
            source_project=row["source_project"],
            source_ref=row["source_ref"],
            occurred_at=row["occurred_at"],
        ))
        cursor = conn.execute(
            "UPDATE capability_intake SET status='assigned',revision=revision+1,updated_at=? "
            "WHERE intake_id=? AND revision=? AND status='pending'",
            (_now_iso(), intake_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ValueError("revision_conflict")
    index_factory = getattr(service, "capability_index", None)
    if callable(index_factory):
        memory = CapabilityStore(service.store).get(memory_id)
        index_factory().sync(memory_id, memory["title"] + "\n" + memory["content"])
    return {"memory_id": memory_id, "intake_id": intake_id, "status": "assigned"}


def project_archive(service, project: str) -> dict:
    archive = get_project_archive(service, project)
    archive["events"] = list_project_events(service, project)
    return archive
