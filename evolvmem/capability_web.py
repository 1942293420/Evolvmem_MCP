"""Read/write models for the capability and project-archive workbench."""
from __future__ import annotations

import hashlib
import json
import re

from evolvmem import capability_assessment
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.capability_recall_eligibility import recall_facts
from evolvmem.context_store import _now_iso
from evolvmem.project_archive_web import literal_pattern, page_number, project_archive


def _intake_public(row) -> dict:
    result = dict(row)
    result["keywords"] = json.loads(result["keywords"] or "[]")
    return result


def _current_projects(fact):
    return sorted({project for source in fact.get("sources", []) if source["valid"]
                   for project in source["projects"]})


def _project_candidates(store, project):
    # Labels/JSON are candidate indexes only. Current provenance is the authority.
    rows = store.conn.execute(
        "SELECT memory_id FROM capability_memories WHERE source_project=? UNION "
        "SELECT s.memory_id FROM capability_sources s "
        "LEFT JOIN organization_units u ON u.task_id=s.task_id AND u.digest=s.unit_digest "
        "LEFT JOIN context_items c ON c.id=s.context_item_id "
        "LEFT JOIN context_project_resolutions r ON r.item_id=s.context_item_id "
        "WHERE u.project=? OR c.project=? OR r.resolved_project=? "
        "OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(u.projects) THEN u.projects ELSE '[]' END) WHERE value=?) "
        "OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(c.projects) THEN c.projects ELSE '[]' END) WHERE value=?) "
        "OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(s.projects) THEN s.projects ELSE '[]' END) WHERE value=?)",
        [project] * 7)
    identities = [row[0] for row in rows]
    return recall_facts(store.context_store, identities) if identities else {}


def _capability_filters(store, query, category, source, *, status="active", source_ids=None, source_scope="current"):
    clauses = ["1=1"] if status == "all" else ["status=?"]
    values = [] if status == "all" else [status]
    if category:
        clauses.append("category=?")
        values.append(category)
    if source:
        if source_scope == "historical":
            clauses.append("source_project=?")
            values.append(source)
        else:
            if source_ids is None:
                source_ids = [identity for identity, fact in _project_candidates(store, source).items()
                              if source in _current_projects(fact)]
            clauses.append("memory_id IN (SELECT value FROM json_each(?))")
            values.append(json.dumps(source_ids))
    if query:
        clauses.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\' OR source_project LIKE ? ESCAPE '\\')")
        values.extend([literal_pattern(query)] * 4)
    return " AND ".join(clauses), values


def filtered_capability_rows(store, *, query, category, source_project, status="active", source_scope="current"):
    where, values = _capability_filters(store, query, category, source_project, status=status, source_scope=source_scope)
    return [store._public(row) for row in store.conn.execute(
        "SELECT * FROM capability_memories WHERE " + where + " ORDER BY updated_at DESC,memory_id DESC", values)]


def capability_counts(store, *, query, source_project, status="active", source_ids=None, source_scope="current"):
    where, values = _capability_filters(store, query, "", source_project, status=status,
                                        source_ids=source_ids, source_scope=source_scope)
    return {row["category"]: row["n"] for row in store.conn.execute(
        "SELECT category,count(*) AS n FROM capability_memories WHERE " + where + " GROUP BY category", values)}


_REASON_LABELS = {
    "task_superseded": "这份来源已被新版本替代。", "unit_withdrawn": "来源片段已退出当前知识范围。",
    "archive_unavailable": "原始档案当前不可用。", "source_revision_changed": "来源内容已更新，需要重新核对。",
    "unit_content_changed": "来源正文已更改，需要重新核对。", "source_range_changed": "来源引用范围已更改。",
    "context_not_active": "原始记录已停用或被替代。", "context_revision_changed": "原始记录内容已更新。",
    "context_not_eligible": "原始记录暂不符合当前引用要求。", "context_expired": "原始记录已过适用期。",
    "untraceable_legacy_source": "历史来源尚未建立可核对的关联。", "unanchored_reference": "来源关联尚待核对。",
    "source_not_found": "尚未找到对应来源。", "unit_missing_or_ambiguous": "来源片段缺失或存在歧义。",
    "model_response_invalid": "本次 AI 评估未返回有效结果，内容已保留，等待重新评估。",
    "missing_evidence": "缺少支持内容的原文引用。", "unsupported_claim": "现有引用尚不能支持全部结论。",
    "missing_action_context": "方法的触发条件或操作步骤尚不完整。", "generic_or_progress_only": "内容主要是口号或进度记录。",
    "supported_scoped_action": "原文支持具体条件和操作。", "supported_reference": "原文支持这条事实或决定。",
}


def _governance_summaries(store, items, facts):
    ids = [item["memory_id"] for item in items]
    grouped = {identity: [] for identity in ids}
    if ids:
        for row in store.conn.execute(
            "SELECT a.memory_id,a.source_id,a.value,a.score,a.verification_status,"
            "json_extract(a.details,'$.structure.kind') AS kind,json_extract(a.details,'$.reasons') AS reasons "
            "FROM capability_assessments a WHERE a.memory_id IN (SELECT value FROM json_each(?)) "
            "AND a.assessment_id=(SELECT MAX(b.assessment_id) FROM capability_assessments b WHERE b.source_id=a.source_id)",
            (json.dumps(ids),)):
            grouped[row["memory_id"]].append(dict(row))
    for item in items:
        fact = facts[item["memory_id"]]
        sources = fact["sources"]
        current = [source for source in sources if source["valid"]]
        broken = [source for source in sources if not source["valid"]]
        codes = list(dict.fromkeys(source["reason"] for source in broken if source.get("reason")))
        untraceable = {"untraceable_legacy_source", "unanchored_reference", "source_not_found", "unit_missing_or_ambiguous"}
        source_state = ("partial" if broken else "current") if current else (
            "untraceable" if not codes or set(codes) <= untraceable else "invalid")
        summaries = grouped[item["memory_id"]]
        approved = set(fact["assessment"]["eligible_source_ids"])
        current_values = [row for row in summaries if row["source_id"] in approved]
        chosen = next((row for row in current_values if row["value"] == "reusable"),
                      next(iter(current_values), next(iter(summaries), None)))
        value = chosen["value"] if chosen else "unassessed"
        if summaries and not current_values and fact["assessment"]["stale_count"] == len(summaries):
            value = "stale"
        assessment_codes = json.loads(chosen["reasons"] or "[]") if chosen else []
        reasons = [_REASON_LABELS.get(code, "来源或内容状态发生变化，需要重新核对。") for code in codes]
        reasons.extend(_REASON_LABELS.get(code, "这条内容需要进一步核对。") for code in assessment_codes)
        projects = _current_projects(fact)
        explicit_global = any(source.get("knowledge_scope") == "global" for source in current)
        if not projects and not explicit_global:
            reasons.append("项目归属尚未确认。")
        if item["status"] == "pending":
            reasons.append("资料已保存到待核对，尚未用于自动召回。")
        if not fact["recall_eligible"] and item["status"] == "active":
            reasons.append("记录仍可浏览，当前暂不用于自动召回。")
        item["source_projects"] = projects
        item["current_source_projects"] = projects
        item["recall_facts"] = fact
        item["governance"] = {"value": value, "verification_status": "unverified",
            "source_state": source_state, "ownership_state": "confirmed" if projects else "global" if explicit_global else "unconfirmed",
            "reasons": list(dict.fromkeys(reasons)), "reason_codes": codes,
            "assessment_count": fact["assessment"]["assessments_count"]}


def capabilities(service, params) -> dict:
    store = CapabilityStore(service.store)
    query = str(params.get("q", "")).strip()
    category = str(params.get("category", "")).strip()
    source = str(params.get("source_project", "")).strip()
    status = str(params.get("status", "active")).strip()
    if status not in {"active", "pending", "all"}:
        raise ValueError("invalid_capability_status_filter")
    source_scope = str(params.get("source_scope", "current"))
    if source_scope not in {"current", "historical"}:
        raise ValueError("invalid_capability_source_scope_filter")
    prefetched = _project_candidates(store, source) if source and source_scope == "current" else {}
    source_ids = [identity for identity, fact in prefetched.items() if source in _current_projects(fact)]
    page_size = page_number(params.get("page_size", 20), "page_size", maximum=50)
    requested_page = page_number(params.get("page", 1), "page")
    where, values = _capability_filters(store, query, category, source, status=status,
                                        source_ids=source_ids, source_scope=source_scope)
    # Facet counts use the same query/source filters and are already required
    # by the response. Reuse them instead of scanning LIKE predicates again.
    counts = capability_counts(store, query=query, source_project=source, status=status,
                               source_ids=source_ids, source_scope=source_scope)
    total = counts.get(category, 0) if category else sum(counts.values())
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(requested_page, pages)
    start = (page - 1) * page_size
    # Page only IDs in the sort: deep OFFSETs must not build a temporary
    # ordering table containing every full memory body before discarding it.
    items = [] if not total else [store._public(row) for row in store.conn.execute(
        "SELECT * FROM capability_memories WHERE memory_id IN ("
        "SELECT memory_id FROM capability_memories WHERE " + where +
        " ORDER BY updated_at DESC,memory_id DESC LIMIT ? OFFSET ?)"
        " ORDER BY updated_at DESC,memory_id DESC",
        [*values, page_size, start],
    )]
    selected = [item["memory_id"] for item in items]
    facts = {identity: prefetched[identity] for identity in selected if identity in prefetched}
    missing = [identity for identity in selected if identity not in facts]
    if missing:
        facts.update(recall_facts(service.store, missing))
    _governance_summaries(store, items, facts)
    facet_status = "1=1" if status == "all" else "status=?"
    facet_args = () if status == "all" else (status,)
    source_projects = sorted({row[0] for row in store.conn.execute(
        "SELECT DISTINCT source_project FROM capability_memories WHERE " + facet_status + " AND source_project<>''", facet_args
    )} | {row[0] for row in store.conn.execute("SELECT project FROM context_project_registry WHERE status='active'")})
    pending_total = store.conn.execute("SELECT COUNT(*) FROM capability_memories WHERE status='pending'").fetchone()[0]
    intake_all_total = store.conn.execute(
        "SELECT count(*) FROM capability_intake WHERE status='pending'"
    ).fetchone()[0]
    intake_source_projects = [row[0] for row in store.conn.execute(
        "SELECT DISTINCT source_project FROM capability_intake "
        "WHERE status='pending' AND source_project<>'' ORDER BY source_project"
    )]
    intake_where, intake_values = ["status='pending'"], []
    for param, column in (("intake_category", "suggested_category"), ("intake_source_project", "source_project")):
        value = str(params.get(param, "")).strip()
        if value:
            intake_where.append(column + "=?")
            intake_values.append(value)
    intake_query = str(params.get("intake_q", "")).strip()
    if intake_query:
        intake_where.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\' OR source_project LIKE ? ESCAPE '\\')")
        intake_values.extend([literal_pattern(intake_query)] * 4)
    intake_where = " AND ".join(intake_where)
    intake_total = store.conn.execute("SELECT count(*) FROM capability_intake WHERE " + intake_where, intake_values).fetchone()[0]
    intake_size = page_number(params.get("intake_page_size", 200), "intake_page_size", maximum=200)
    intake_pages = max(1, (intake_total + intake_size - 1) // intake_size)
    intake_page = min(page_number(params.get("intake_page", 1), "intake_page"), intake_pages)
    suggestions_total = store.conn.execute(
        "SELECT count(*) FROM capability_category_suggestions WHERE status='pending'"
    ).fetchone()[0]
    intake = [_intake_public(row) for row in store.conn.execute(
        "SELECT * FROM capability_intake WHERE " + intake_where + " ORDER BY created_at,intake_id LIMIT ? OFFSET ?",
        [*intake_values, intake_size, (intake_page - 1) * intake_size]
    )]
    suggestions = [dict(row) for row in store.conn.execute(
        "SELECT * FROM capability_category_suggestions WHERE status='pending' ORDER BY created_at,suggestion_id"
    )]
    return {
        "categories": store.categories(),
        "items": items,
        "intake": intake,
        "intake_total": intake_total,
        "intake_all_total": intake_all_total,
        "intake_source_projects": intake_source_projects,
        "intake_page": intake_page,
        "intake_page_size": intake_size,
        "intake_pages": intake_pages,
        "suggestions": suggestions,
        "suggestions_total": suggestions_total,
        "source_projects": source_projects,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "counts": counts, "status": status, "source_scope": source_scope,
        "pending_total": pending_total,
    }


def overview(service) -> dict:
    conn = service.store._connection()
    return {
        "capabilities": conn.execute("SELECT count(*) FROM capability_memories WHERE status='active'").fetchone()[0],
        "project_archives": conn.execute("SELECT count(*) FROM project_archives").fetchone()[0],
        "intake": conn.execute("SELECT count(*) FROM capability_intake WHERE status='pending'").fetchone()[0],
        "events": conn.execute("SELECT count(*) FROM project_events WHERE status='active'").fetchone()[0],
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


def _intake_evaluation(service, row, category):
    """Only a current, exact v3 receipt can supply value evidence at assignment.

    Uses stored unit text and metadata; never calls a model or decrypts archives.
    """
    from evolvmem.capability_pipeline import _fingerprint
    from evolvmem.capability_provenance import ProvenanceRead
    from evolvmem.pipeline_state import unit_scope, validate_occurred_at
    from evolvmem.auto_organization import unit_digest
    match = re.fullmatch(r"(.+)#([^#@]+)@([0-9]+)-([0-9]+)", row["source_ref"])
    if not match:
        return None, None
    conn = service.store._connection()
    units = conn.execute(
        "SELECT u.* FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
        "WHERE t.source_key=? AND t.status!='superseded' AND t.superseded_by IS NULL AND u.digest=? "
        "AND u.source_start=? AND u.source_end=? AND u.disposition='keep' LIMIT 2",
        (match[1], match[2], int(match[3]), int(match[4]))).fetchall()
    if len(units) != 1:
        return None, None
    unit = dict(units[0])
    task = dict(conn.execute("SELECT * FROM organization_tasks WHERE id=?", (unit["task_id"],)).fetchone())
    current, reason = ProvenanceRead(service.store).source_revision(task["source_key"])
    if reason or current != task["source_revision"] or unit_digest(task["source_key"], unit["source_start"], unit["source_end"], unit["text"]) != unit["digest"]:
        return None, None
    policy = "capability:v3:" + capability_assessment.POLICY_VERSION
    key = _fingerprint(policy, task["source_key"], task["source_revision"], task["rule_revision"],
                       unit["digest"], unit["text"], unit["source_start"], unit["source_end"])
    receipt = conn.execute("SELECT payload FROM extraction_receipts WHERE input_key=? AND pipeline=?", (key, policy)).fetchone()
    if receipt is None or not receipt[0]:
        return None, None
    try:
        candidate = json.loads(receipt[0])
        if (not isinstance(candidate, dict) or candidate.get("action") != "keep"
                or candidate.get("title", "").strip() != row["title"]
                or candidate.get("content", "").strip() != row["content"]
                or candidate.get("category") != row["suggested_category"]):
            return None, None
        capability_assessment.validate(candidate, unit["text"])
        validate_occurred_at(candidate.get("occurred_at"))
        evaluated = capability_assessment.evaluate({**candidate, "category": category}, source_text=unit["text"])
    except (ValueError, TypeError, AttributeError, KeyError):
        return None, None
    source = CapabilitySourceDraft(source_kind="unit", source_key=f"{task['source_key']}#{unit['digest']}",
        source_ref=row["source_ref"], source_revision=task["source_revision"], task_id=task["id"],
        unit_digest=unit["digest"], unit_revision=unit["revision"], **unit_scope(service, unit))
    return source, evaluated


def assign_intake(service, intake_id: int, body) -> dict:
    category = str(body.get("category", "")).strip()
    expected_revision = body.get("expected_revision")
    conn = service.store._connection()
    capabilities = CapabilityStore(service.store)
    with service.store.transaction():
        row = conn.execute("SELECT * FROM capability_intake WHERE intake_id=?", (intake_id,)).fetchone()
        if row is None:
            raise ValueError("capability_intake_not_found")
        if row["status"] != "pending" or row["revision"] != expected_revision:
            raise ValueError("revision_conflict")
        enabled = conn.execute("SELECT enabled FROM capability_categories WHERE id=?", (category,)).fetchone()
        if enabled is None or not enabled[0]:
            raise ValueError("capability_category_not_found")
        source, evaluated = _intake_evaluation(service, row, category)
        project = (source.projects[0] if len(source.projects) == 1 else "") if source else row["source_project"]
        draft = CapabilityMemoryDraft(category=category, title=row["title"], content=row["content"],
            keywords=tuple(json.loads(row["keywords"] or "[]")), source_project=project,
            source_ref=row["source_ref"], occurred_at=row["occurred_at"])
        prior = conn.execute("SELECT status FROM capability_memories WHERE fingerprint=?", (draft.fingerprint(),)).fetchone()
        memory_id = capabilities.upsert(draft, source=source)
        if evaluated is not None:
            source_id = capabilities.bind_source(memory_id, source)
            capability_assessment.persist(service.store, memory_id, source_id, evaluated,
                                          existing_status=prior["status"] if prior else None)
        elif prior is None:
            capabilities.set_status(memory_id, "pending")
        cursor = conn.execute(
            "UPDATE capability_intake SET status='assigned',revision=revision+1,updated_at=? "
            "WHERE intake_id=? AND revision=? AND status='pending'", (_now_iso(), intake_id, expected_revision))
        if cursor.rowcount != 1:
            raise ValueError("revision_conflict")
        memory = capabilities.get(memory_id)
    index_factory = getattr(service, "capability_index", None)
    if callable(index_factory):
        index_factory().sync(memory_id, memory["title"] + "\n" + memory["content"])
    notice = ("已归类，资料已加入待核对，可在待核对列表继续查看。" if memory["status"] == "pending" else
              "已归类，可在已整理资料查看；方法结果仍未独立验证。" if memory["status"] == "active" else
              "已归类，保留原有停用状态，可在全部记录查看。")
    return {"memory_id": memory_id, "intake_id": intake_id, "status": "assigned",
            "memory_status": memory["status"], "assessment_value": evaluated["value"] if evaluated else "unassessed",
            "notice": notice}
