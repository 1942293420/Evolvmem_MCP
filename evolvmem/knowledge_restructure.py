"""Non-destructive migration from legacy Context items to the new two-lane store."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from evolvmem.capability_models import CAPABILITY_CATEGORIES
from evolvmem.capability_pipeline import _call_provider
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft, source_progress_date


HISTORICAL_TYPES = {"session_summary", "project_summary", "workstream_checkpoint"}
CAPABILITY_TYPES = {"fact", "decision", "experience", "playbook", "constraint", "preference", "reference"}
_ALL_MIGRATION_TYPES = CAPABILITY_TYPES | HISTORICAL_TYPES


def load_state(store) -> dict:
    row = store._connection().execute(
        "SELECT value FROM knowledge_restructure_state WHERE key='migration'"
    ).fetchone()
    return json.loads(row[0]) if row else {"last_item_id": 0, "version": 1}


def save_state(store, state) -> None:
    with store.transaction():
        store._connection().execute(
            "INSERT INTO knowledge_restructure_state(key,value) VALUES('migration',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(state, sort_keys=True),),
        )


def _save_mapping(store, legacy_item_id, target_kind, target_id, outcome) -> None:
    with store.transaction():
        store._connection().execute(
            "INSERT INTO knowledge_restructure_map(legacy_item_id,target_kind,target_id,outcome,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(legacy_item_id) DO UPDATE SET "
            "target_kind=excluded.target_kind,target_id=excluded.target_id,"
            "outcome=excluded.outcome,updated_at=excluded.updated_at",
            (legacy_item_id, target_kind, str(target_id), outcome, _now_iso()),
        )


def _content(store, item_id) -> tuple[str, str]:
    rows = {row["layer"]: row["content"] for row in store._connection().execute(
        "SELECT layer,content FROM context_layers WHERE item_id=?", (item_id,)
    )}
    return rows.get("l2") or rows.get("l1") or rows.get("l0") or "", rows.get("l0") or ""


def _source_project(store, row) -> str:
    if row["project"]:
        return str(row["project"])
    projects = json.loads(row["projects"] or "[]") if "projects" in row.keys() else []
    if projects:
        return str(projects[0])
    resolution = store._connection().execute(
        "SELECT resolved_project FROM context_project_resolutions WHERE item_id=?", (row["id"],)
    ).fetchone()
    if resolution and resolution[0]:
        return str(resolution[0])
    source = store._connection().execute(
        "SELECT archive_id FROM context_sources WHERE item_id=? AND archive_id IS NOT NULL "
        "ORDER BY id LIMIT 1", (row["id"],)
    ).fetchone()
    if source:
        archive = store._connection().execute(
            "SELECT project FROM session_archives WHERE id=?", (source[0],)
        ).fetchone()
        if archive:
            return str(archive[0])
    return ""


def _source_ref(store, item_id) -> str:
    row = store._connection().execute(
        "SELECT archive_id,source_ref FROM context_sources WHERE item_id=? ORDER BY id LIMIT 1",
        (item_id,),
    ).fetchone()
    if row is None:
        return f"context_item:{item_id}"
    return row["source_ref"] or (f"archive:{row['archive_id']}" if row["archive_id"] else f"context_item:{item_id}")


def _ensure_project(store, project) -> None:
    if not project:
        return
    now = _now_iso()
    with store.transaction():
        store._connection().execute(
            "INSERT OR IGNORE INTO context_project_registry(project,display_name,created_at,updated_at) "
            "VALUES(?,?,?,?)", (project, project, now, now)
        )


def _save_intake(store, row, candidate, source_project, source_ref) -> int:
    now = _now_iso()
    title = str(candidate.get("title", "")).strip()
    content = str(candidate.get("content", "")).strip()
    if not title or not content:
        raise ValueError("invalid_capability_memory")
    fingerprint = __import__("hashlib").sha256(
        json.dumps([title, content], ensure_ascii=False).encode()
    ).hexdigest()
    with store.transaction():
        store._connection().execute(
            "INSERT OR IGNORE INTO capability_intake(title,content,keywords,suggested_category,"
            "source_project,source_ref,occurred_at,fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (title, content, json.dumps(candidate.get("keywords") or [], ensure_ascii=False),
             str(candidate.get("category", "")), source_project, source_ref, row["occurred_at"],
             fingerprint, now, now),
        )
        saved = store._connection().execute(
            "SELECT intake_id FROM capability_intake WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
    return int(saved[0])


CATEGORY_TERMS = {
    "frontend": ("前端", "vue", "react", "页面", "组件", "样式", "浏览器", "设计偏好", "css"),
    "backend": ("后端", "django", "api", "接口", "缓存", "队列", "python", "hook", "cli", "execute_code"),
    "business_logic": ("业务", "采购", "订单", "审批", "流程", "状态", "节点", "规则", "供应商"),
    "database": ("mysql", "数据库", "sql", "索引", "事务", "表结构", "多维表格", "base"),
    "architecture": ("架构", "技术选型", "模块划分", "系统设计"),
    "quality": ("测试", "test", "报错", "修复", "故障", "性能", "调试", "验证", "陷阱"),
    "delivery": ("部署", "docker", "运维", "发布", "config", "配置", "环境", "密钥", "api key", "secrets"),
}


def infer_capability_category(value) -> str:
    text = str(value).casefold()
    scores = {category: sum(term in text for term in terms) for category, terms in CATEGORY_TERMS.items()}
    best = max(scores.values(), default=0)
    winners = [category for category, score in scores.items() if score == best and score > 0]
    return winners[0] if len(winners) == 1 else ""


def _offline_capability(store, row, body, summary) -> dict:
    metadata = store._connection().execute(
        "SELECT title FROM knowledge_metadata WHERE item_id=?", (row["id"],)
    ).fetchone()
    learning = store._connection().execute(
        "SELECT category FROM learning_memories WHERE item_id=?", (row["id"],)
    ).fetchone()
    return {
        "action": "keep",
        "category": infer_capability_category("\n".join((summary, body))),
        "title": (metadata[0] if metadata and metadata[0] else summary) or f"旧资料 #{row['id']}",
        "content": body,
        "keywords": [learning[0]] if learning else [],
        "occurred_at": row["occurred_at"],
    }


def migrate_capability(service, row, provider=None) -> tuple[str, str, int, str]:
    store = service.store
    body, summary = _content(store, row["id"])
    prompt = "CAPABILITY\n只迁移开发记忆，不判断项目归属或跨项目通用性。\n" + body
    candidate = _offline_capability(store, row, body, summary) if provider is None else _call_provider(prompt, provider)
    source_project = _source_project(store, row)
    source_ref = _source_ref(store, row["id"])
    if candidate.get("action") == "ignore":
        return "skipped", "none", 0, "ignored"
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    description = str(candidate.get("description") or "").strip()
    title = str(candidate.get("title") or description or candidate.get("name") or "").strip()
    content = str(candidate.get("content") or description).strip()
    if not candidate.get("keywords") and metadata.get("tags"):
        candidate = {**candidate, "keywords": metadata["tags"]}
    category = str(candidate.get("category", ""))
    if not category:
        category = infer_capability_category("\n".join((title, content, body)))
    if not title or not content:
        category = ""
    candidate = {
        **candidate,
        "title": title or summary or f"旧资料 #{row['id']}",
        "content": content or body,
        "category": category,
    }
    if not str(candidate["content"]).strip():
        return "skipped", "none", 0, "invalid_candidate"
    if category not in CAPABILITY_CATEGORIES:
        intake_id = _save_intake(store, row, candidate, source_project, source_ref)
        return "intake", str(intake_id), intake_id, "pending"
    memory_id = CapabilityStore(store).upsert(CapabilityMemoryDraft(
        category=category,
        title=str(candidate.get("title", "")),
        content=str(candidate.get("content", "")),
        keywords=tuple(candidate.get("keywords") or ()),
        source_project=source_project,
        source_ref=source_ref,
        occurred_at=row["occurred_at"],
    ))
    index_factory = getattr(service, "capability_index", None)
    if callable(index_factory):
        memory = CapabilityStore(store).get(memory_id)
        index_factory().sync(memory_id, memory["title"] + "\n" + memory["content"])
    return "capability", str(memory_id), memory_id, "migrated"


def migrate_project_event(service, row, provider=None) -> tuple[str, str, int, str]:
    store = service.store
    project = _source_project(store, row)
    if not project:
        return "skipped", "none", 0, "missing_project"
    body, title = _content(store, row["id"])
    prompt = "PROJECT_HISTORY\n只迁移项目历史事件。\n" + body
    candidate = ({"event_type": "milestone", "title": title, "content": body}
                if provider is None else _call_provider(prompt, provider))
    metadata = store._connection().execute(
        "SELECT title FROM knowledge_metadata WHERE item_id=?", (row["id"],)
    ).fetchone()
    event = ProjectEventDraft(
        project=project,
        event_type=str(candidate.get("event_type", "milestone")),
        title=str(candidate.get("title") or (metadata[0] if metadata else title) or f"项目记录 #{row['id']}"),
        content=str(candidate.get("content") or body),
        source_ref=_source_ref(store, row["id"]),
        occurred_at=row["occurred_at"] or source_progress_date(row["identity_key"]),
    )
    _ensure_project(store, project)
    event_id = ProjectArchiveStore(store).record_event(event)
    return "event", str(event_id), event_id, project


def migrate_batch(service, batch_size=100, provider=None) -> dict:
    batch_size = max(1, min(int(batch_size), 1000))
    store = service.store
    state = load_state(store)
    rows = store._connection().execute(
        "SELECT * FROM context_items WHERE id>? AND status IN ('active','superseded') "
        "AND content_type IN ('fact','decision','experience','playbook','constraint','preference',"
        "'reference','session_summary','project_summary','workstream_checkpoint') ORDER BY id LIMIT ?",
        (state["last_item_id"], batch_size),
    ).fetchall()
    result = {"scanned": 0, "capabilities": 0, "events": 0, "pending": 0,
              "skipped": 0, "last_item_id": state["last_item_id"]}
    touched = set()
    for row in rows:
        result["scanned"] += 1
        result["last_item_id"] = int(row["id"])
        existing = store._connection().execute(
            "SELECT outcome FROM knowledge_restructure_map WHERE legacy_item_id=?", (row["id"],)
        ).fetchone()
        if existing:
            save_state(store, {"last_item_id": result["last_item_id"], "version": 1})
            continue
        if row["content_type"] in CAPABILITY_TYPES and row["status"] == "active":
            kind, target_id, numeric_id, outcome = migrate_capability(service, row, provider)
            if outcome == "migrated":
                result["capabilities"] += 1
            elif outcome == "pending":
                result["pending"] += 1
            else:
                result["skipped"] += 1
        elif row["content_type"] in HISTORICAL_TYPES:
            kind, target_id, numeric_id, outcome = migrate_project_event(service, row, provider)
            if kind == "event":
                result["events"] += 1
                touched.add(outcome)
            else:
                result["skipped"] += 1
        else:
            kind, target_id, outcome = "skipped", "0", "inactive"
            result["skipped"] += 1
        _save_mapping(store, row["id"], kind, target_id, outcome)
        save_state(store, {"last_item_id": result["last_item_id"], "version": 1})
    archives = ProjectArchiveStore(store)
    for project in sorted(touched):
        archives.rebuild(project)
        with store.transaction():
            store._connection().execute(
                "UPDATE project_archives SET source_watermark=?,updated_at=? WHERE project=?",
                (f"legacy_item:{result['last_item_id']}", _now_iso(), project),
            )
    return result


def verify_migration(service) -> dict:
    conn = service.store._connection()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_errors = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
    invalid_categories = conn.execute(
        "SELECT count(*) FROM capability_memories m LEFT JOIN capability_categories c "
        "ON c.id=m.category WHERE c.id IS NULL"
    ).fetchone()[0]
    missing_registry = conn.execute(
        "SELECT count(*) FROM project_archives a LEFT JOIN context_project_registry r "
        "ON r.project=a.project WHERE r.project IS NULL"
    ).fetchone()[0]
    last_item_id = load_state(service.store)["last_item_id"]
    latest = conn.execute(
        "SELECT COALESCE(max(id),0) FROM context_items WHERE content_type IN "
        "('fact','decision','experience','playbook','constraint','preference','reference',"
        "'session_summary','project_summary','workstream_checkpoint')"
    ).fetchone()[0]
    errors = []
    if integrity != "ok":
        errors.append("integrity_check_failed")
    if foreign_errors:
        errors.append("foreign_key_check_failed")
    if invalid_categories:
        errors.append("invalid_capability_category")
    if missing_registry:
        errors.append("project_registry_missing")
    if last_item_id < latest:
        errors.append("migration_incomplete")
    return {
        "ok": not errors,
        "integrity_check": integrity,
        "foreign_key_errors": len(foreign_errors),
        "invalid_categories": invalid_categories,
        "missing_registry": missing_registry,
        "last_item_id": last_item_id,
        "latest_item_id": latest,
        "capabilities": conn.execute("SELECT count(*) FROM capability_memories").fetchone()[0],
        "events": conn.execute("SELECT count(*) FROM project_events").fetchone()[0],
        "pending": conn.execute("SELECT count(*) FROM capability_intake WHERE status='pending'").fetchone()[0],
        "errors": errors,
    }


def set_structure_mode(service, mode, allow_incomplete=False) -> dict:
    if mode not in {"legacy", "dual", "primary"}:
        raise ValueError("invalid_knowledge_structure_mode")
    if mode == "primary" and not allow_incomplete and not verify_migration(service)["ok"]:
        raise ValueError("migration_not_verified")
    previous = service.config.knowledge_structure_mode
    try:
        service.config.knowledge_structure_mode = mode
        service.config.save()
    except Exception:
        service.config.knowledge_structure_mode = previous
        raise
    return {"mode": mode, "verified": verify_migration(service)["ok"]}


def backup_database(config, target: Path) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError("backup_integrity_failed")
        finally:
            destination.close()
    finally:
        source.close()
    return target


def dry_run(copy_path: Path) -> dict:
    conn = sqlite3.connect(Path(copy_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {
            "capability_candidates": conn.execute(
                "SELECT count(*) FROM context_items WHERE status='active' AND content_type IN "
                "('fact','decision','experience','playbook','constraint','preference','reference')"
            ).fetchone()[0],
            "historical_candidates": conn.execute(
                "SELECT count(*) FROM context_items WHERE status IN ('active','superseded') "
                "AND content_type IN ('session_summary','project_summary','workstream_checkpoint')"
            ).fetchone()[0],
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        conn.close()
