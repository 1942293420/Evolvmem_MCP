"""Read-only project candidate recall; scores never establish ownership."""
from __future__ import annotations

import hashlib
import math
import re
import time

from evolvmem import knowledge_scope as ks
from evolvmem.context_models import ContextLayer, ContextStatus
from evolvmem.context_store import _now_iso

VERSION = "ownership-candidates-v2-bounded"
POOL_LIMIT = 100
SOURCE_POOL_LIMIT = 256
QUERY_LIMIT = 3
MAX_QUERY_CHARS = 10000
MAX_SOURCE_CHARS = 16000
RECALL_SECONDS = 2.0
_GENERIC = {
    "api", "app", "web", "sql", "ui", "ai", "test", "data", "demo", "project",
    "项目", "任务", "数据", "系统", "服务", "接口", "开发", "前端", "后端", "测试",
    "功能", "更新", "新增", "增加", "修改", "修复", "问题", "用户", "要求", "通用",
    "规则", "处理", "页面", "代码", "技术", "模块", "平台", "管理", "查询", "结果",
}


def _match(text, term):
    value = str(term).strip()
    if value.casefold() in _GENERIC or len(value) < 2:
        return None
    if not re.search(r"[\u4e00-\u9fff]", value) and len(value) < 3:
        return None
    # Latin identifiers retain boundaries next to Chinese prose; meaningful
    # Chinese aliases may occur before a noun, e.g. 蓝鲸平台.
    left = r"(?<![A-Za-z0-9_-])" if re.match(r"[A-Za-z0-9_]", value) else ""
    right = r"(?![A-Za-z0-9_-])" if re.search(r"[A-Za-z0-9_]$", value) else ""
    return re.search(left + re.escape(value) + right, text, re.I)


def _terms(text):
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text)
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(sequence) <= 8:
            terms.append(sequence)
        terms.extend(sequence[i:i+2] for i in range(len(sequence)-1))
    return tuple(dict.fromkeys(t.casefold() for t in terms
                               if len(t) >= 2 and t.casefold() not in _GENERIC))[:24]


def _overlap(text, terms):
    return [term for term in terms if _match(text, term)]


def _snippet(text, terms):
    hits = [hit.start() for term in terms if (hit := _match(text, term))]
    start = max(0, min(hits) - 100) if hits else 0
    return text[start:start+500]


class CandidateRead:
    """One reasoning chunk's bounded, read-only provenance caches.

    Never a global/service cache. A DB change invalidates every cached proof
    before the next recall, including changes on another connection.
    """
    def __init__(self, service):
        self.service = service
        self.conn = service.store._connection()
        self.token = None
        self.diagnostics = None
        self.begin({})

    def begin(self, diagnostics):
        token = (self.conn.total_changes, self.conn.execute("PRAGMA data_version").fetchone()[0])
        if token != self.token:
            from evolvmem.capability_provenance import ProvenanceRead
            self.reader = ProvenanceRead(self.service.store)
            self.registry = [{k: p.get(k) for k in ("project", "display_name", "aliases", "revision")}
                             for p in self.service.knowledge().registry() if p["status"] == "active"]
            self.registry.sort(key=lambda p: p["project"])
            self.projects = {p["project"] for p in self.registry}
            self.tasks, self.contexts = {}, {}
            self._units, self._context_ids = None, None
            self._pool_flags = set()
            self.token = token
        self.diagnostics = diagnostics
        self.examined = set()
        self.deadline = time.monotonic() + RECALL_SECONDS
        return self

    def budget(self, reason):
        reasons = self.diagnostics.setdefault("budget_reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        self.diagnostics["pool_truncated"] = True
        self._pool_flags.add(reason)

    def allowed(self):
        if time.monotonic() >= self.deadline:
            self.budget("deadline")
            return False
        return True

    def units(self):
        if self._units is None:
            # Select bounded identities first. There is no whole-table text
            # predicate; potentially long body reads happen only by exact PK.
            ids = self.conn.execute(
                "SELECT u.task_id,u.ordinal FROM organization_tasks t INDEXED BY idx_organization_tasks_status "
                "CROSS JOIN organization_units u ON u.task_id=t.id "
                "WHERE u.decision IN ('auto','manual') AND u.disposition='keep' AND t.status!='superseded' "
                "ORDER BY t.id DESC,u.ordinal LIMIT ?", (SOURCE_POOL_LIMIT + 1,)).fetchall()
            if len(ids) > SOURCE_POOL_LIMIT:
                self._pool_flags.add("unit_pool")
            self._units = []
            for identity in ids[:SOURCE_POOL_LIMIT]:
                if not self.allowed():
                    self._pool_flags.add("unit_read_deadline")
                    break
                row = self.conn.execute(
                    "SELECT task_id,ordinal,digest,source_start,source_end,project,projects,knowledge_scope,"
                    "decision,disposition,revision,CASE WHEN length(text)<=? THEN text END AS text "
                    "FROM organization_units WHERE task_id=? AND ordinal=?",
                    (MAX_SOURCE_CHARS, identity["task_id"], identity["ordinal"])).fetchone()
                if row["text"] is None:
                    self._pool_flags.add("source_body_over_budget")
                else:
                    self._units.append(dict(row))
        for flag in self._pool_flags:
            self.budget(flag)
        self.diagnostics["unit_rows_examined"] = len(self._units)
        return self._units

    def context_ids(self):
        if self._context_ids is None:
            rows = self.conn.execute(
                "SELECT id FROM context_items WHERE status='active' AND scope='project' "
                "ORDER BY id DESC LIMIT ?", (SOURCE_POOL_LIMIT + 1,)).fetchall()
            if len(rows) > SOURCE_POOL_LIMIT:
                self._pool_flags.add("context_pool")
            self._context_ids = []
            for row in rows[:SOURCE_POOL_LIMIT]:
                if not self.allowed():
                    self._pool_flags.add("context_read_deadline")
                    break
                size = self.conn.execute(
                    "SELECT coalesce(max(length(content)),0) FROM context_layers "
                    "WHERE item_id=? AND layer IN ('l0','l1')", (row["id"],)).fetchone()[0]
                if size <= MAX_SOURCE_CHARS:
                    self._context_ids.append(row["id"])
                else:
                    self._pool_flags.add("source_body_over_budget")
        for flag in self._pool_flags:
            self.budget(flag)
        return tuple(self._context_ids)

    def exclude(self, kind, ref, reason):
        item = {"source": kind, "ref": ref, "reason": reason}
        if len(self.diagnostics["excluded"]) < POOL_LIMIT and item not in self.diagnostics["excluded"]:
            self.diagnostics["excluded"].append(item)

    def current_task(self, task_id):
        if task_id not in self.tasks or self.tasks[task_id][1] == "source_verification_budget":
            row = self.conn.execute(
                "SELECT id,source_key,source_revision,rule_revision,status,superseded_by,error_code "
                "FROM organization_tasks WHERE id=?", (task_id,)).fetchone()
            task = dict(row) if row else None
            reason = ""
            if task is None or task["status"] == "superseded" or task["superseded_by"]:
                reason = "source_superseded"
            elif not self.allowed():
                reason = "source_verification_budget"
            else:
                revision, failure = self.reader.source_revision(task["source_key"])
                if failure or revision != task["source_revision"] or task["error_code"] == "source_no_dialogue":
                    reason = "source_changed"
            self.tasks[task_id] = (task, reason)
        return self.tasks[task_id]

    def unit_projects(self, unit):
        _, reason = self.current_task(unit["task_id"])
        ref = f"unit:{unit['task_id']}:{unit['digest']}"
        if reason:
            self.exclude("unit", ref, reason)
            return []
        projects = ks.projects(unit)
        if (unit["decision"] not in ("manual", "auto") or unit["disposition"] != "keep"
                or ks.scope(unit) != "project" or not projects
                or not set(projects) <= self.projects):
            self.exclude("unit", ref, "ownership_unconfirmed")
            return []
        return projects

    def context(self, item_id):
        if item_id not in self.examined:
            if len(self.examined) >= POOL_LIMIT or not self.allowed():
                self.budget("context_candidate_limit")
                return None
            self.examined.add(item_id)
            self.diagnostics["contexts_examined"] = len(self.examined)
        if item_id in self.contexts:
            return self.contexts[item_id]
        self.contexts[item_id] = None
        row = self.conn.execute("SELECT * FROM context_items WHERE id=?", (item_id,)).fetchone()
        ref = f"context:{item_id}"
        if row is None or row["status"] != "active":
            self.exclude("context", ref, "context_inactive")
            return None
        item = dict(row)
        now = _now_iso()
        if ((item.get("expires_at") and item["expires_at"] <= now)
                or (item.get("effective_until") and item["effective_until"] <= now)
                or (item.get("effective_from") and item["effective_from"] > now)):
            self.exclude("context", ref, "context_outside_validity")
            return None
        projects = ks.projects(item)
        if item["scope"] != "project" or not projects or not set(projects) <= self.projects:
            self.exclude("context", ref, "ownership_unconfirmed")
            return None
        resolution = self.conn.execute(
            "SELECT * FROM context_project_resolutions WHERE item_id=?", (item_id,)).fetchone()
        if resolution and (resolution["resolution_state"] != "resolved"
                           or resolution["review_state"] in ("pending", "rejected")):
            self.exclude("context", ref, "ownership_unconfirmed")
            return None
        units = self.conn.execute(
            "SELECT u.task_id,u.digest,u.project,u.projects,u.knowledge_scope,u.decision,u.disposition "
            "FROM unit_derivations d JOIN organization_units u "
            "ON u.task_id=d.unit_task_id AND u.digest=d.unit_digest WHERE d.item_id=? LIMIT ?", (item_id, POOL_LIMIT + 1)).fetchall()
        if len(units) > POOL_LIMIT:
            self.budget("derivation_pool")
        units = units[:POOL_LIMIT]
        has_derivations = self.conn.execute(
            "SELECT 1 FROM unit_derivations WHERE item_id=? LIMIT 1", (item_id,)).fetchone()
        confirmed = set()
        for unit in units:
            confirmed.update(set(self.unit_projects(dict(unit))) & set(projects))
        # Explicit independent manual evidence can continue to support a shared
        # record after its automatic derivation is withdrawn.
        if (resolution and resolution["decision_source"] == "human"
                and resolution["resolved_project"] in projects):
            sources = self.conn.execute(
                "SELECT s.source_kind,s.archive_id,a.state FROM context_sources s LEFT JOIN session_archives a "
                "ON a.id=s.archive_id WHERE s.item_id=?", (item_id,)).fetchall()
            independent = any(s["source_kind"] == "manual" and
                              (s["archive_id"] is None or s["state"] == "available") for s in sources)
            native = not has_derivations and (not sources or any(
                s["archive_id"] is not None and s["state"] == "available" for s in sources))
            if independent or native:
                confirmed.add(resolution["resolved_project"])
        if not confirmed:
            self.exclude("context", ref, "ownership_unconfirmed")
            return None
        from evolvmem.memory_eligibility import eligible
        if not eligible(self.service.store, item_id):
            self.exclude("context", ref, "context_ineligible")
            return None
        size = self.conn.execute("SELECT coalesce(max(length(content)),0) FROM context_layers "
                                 "WHERE item_id=? AND layer IN ('l0','l1')", (item_id,)).fetchone()[0]
        if size > MAX_SOURCE_CHARS:
            self.budget("source_body_over_budget")
            return None
        text = self.service.store.get_layer(item_id, ContextLayer.L0) or ""
        result = {"projects": sorted(confirmed), "text": text,
                  "source": {"kind": "context", "id": item_id,
                             "resolution_revision": resolution["revision"] if resolution else None,
                             "updated_at": item["updated_at"]}}
        self.contexts[item_id] = result
        return result


def _vectors(service, text, diagnostics):
    engine = getattr(service, "embedding_engine", None)
    index = getattr(service, "vector_index", None)
    diagnostics["vector_status"] = "unavailable"
    if engine is None or not getattr(engine, "is_loaded", False) or index is None:
        return []
    # IDs from the capability/legacy cache must never resolve as Context IDs.
    if getattr(index, "path", None) != service.config.context_vector_path.resolve():
        diagnostics["vector_status"] = "incompatible_namespace"
        return []
    try:
        from evolvmem.context_retriever import ContextRetriever
        retriever = ContextRetriever(service.config, service.store, index, engine)
        prepared = {}
        hits = retriever._vector_candidates(text, POOL_LIMIT, prepared=prepared)
        state = prepared.get("state")
        if state is not None and state.available(engine):
            diagnostics["vector_status"] = "available"
        return hits
    except Exception:
        diagnostics["vector_status"] = "failed"
        return []


def recall(service, unit_or_text, *, limit=8, use_vectors=True, read=None):
    """Return evidence and a safe registry for subsequent ownership reasoning.

    No database or vector-cache writes. limit is a preferred shortlist size, not
    permission to discard exact matches or ambiguities. Scores measure recall.
    """
    text = str(unit_or_text.get("text", "") if isinstance(unit_or_text, dict) else unit_or_text).strip()
    if not text:
        raise ValueError("ownership_candidate_text_required")
    limit = max(2, min(int(limit), 50))
    diagnostics = {"vector_status": "disabled", "excluded": [], "pool_truncated": False,
                   "budget_reasons": [], "fts_queries": 0, "contexts_examined": 0, "unit_rows_examined": 0}
    read = (read or CandidateRead(service)).begin(diagnostics)
    if read.service is not service:
        raise ValueError("ownership_reader_service_mismatch")
    registry = read.registry
    names = read.projects
    found = {}

    def add(project, origin, source, ref, score, quote):
        if project not in names:
            return
        row = found.setdefault(project, {"project": project, "score": 0.0, "evidence": []})
        evidence = {"origin": origin, "source": source, "ref": ref,
                    "score": round(score, 6), "quote": quote[:500]}
        if not any(e["origin"] == origin and e["ref"] == ref and e["quote"] == evidence["quote"]
                   for e in row["evidence"]):
            row["evidence"].append(evidence)
        row["score"] = max(row["score"], evidence["score"])

    exact_projects = set()
    for project in registry:
        labels = [(project["project"], "project_exact", 1.0),
                  (project.get("display_name") or "", "display_exact", .95)]
        labels.extend((alias, "alias_exact", .98) for alias in project.get("aliases") or [])
        for label, origin, score in labels:
            hit = _match(text, label)
            if hit:
                exact_projects.add(project["project"])
                add(project["project"], origin,
                    {"kind": "registry", "project": project["project"], "revision": project["revision"]},
                    "project:" + project["project"], score, hit.group())

    overlong = len(text) > MAX_QUERY_CHARS
    if overlong:
        read.budget("input_over_budget")
    terms = _terms(text) if not overlong else ()
    if terms:
        for unit in read.units():
            if not read.allowed():
                break
            hits = _overlap(unit["text"], terms)
            if not hits:
                continue
            for project in read.unit_projects(unit):
                task, _ = read.current_task(unit["task_id"])
                ref = f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}"
                add(project, "unit_lexical",
                    {"kind": "unit", "task_id": unit["task_id"], "digest": unit["digest"],
                     "source_revision": task["source_revision"], "unit_revision": unit["revision"]},
                    ref, .7 * len(hits) / len(terms), _snippet(unit["text"], terms))
        lexical = {}
        queries = tuple(dict.fromkeys([text, *terms[:12]]))
        if len(queries) > QUERY_LIMIT:
            read.budget("query_limit")
        item_ids = read.context_ids() if read.allowed() else ()
        for query in queries[:QUERY_LIMIT]:
            if not item_ids or not read.allowed() or len(lexical) >= POOL_LIMIT:
                break
            remaining = POOL_LIMIT - len(lexical)
            hits = service.store.search_fts(query, top_k=remaining, statuses=(ContextStatus.ACTIVE,),
                                            now=_now_iso(), item_ids=item_ids)
            diagnostics["fts_queries"] += 1
            if len(hits) >= remaining:
                read.budget("lexical_candidate_limit")
            for hit in hits:
                if hit.item_id not in lexical and len(lexical) >= POOL_LIMIT:
                    read.budget("lexical_candidate_limit")
                    break
                lexical.setdefault(hit.item_id, set()).update(hit.match_layers)
        for item_id, layers in lexical.items():
            context = read.context(item_id)
            if not context:
                continue
            for layer in sorted(layers, key=lambda layer: layer.value):
                content = service.store.get_layer(item_id, layer) or ""
                hits = _overlap(content, terms)
                if not hits:
                    continue
                score = .65 * len(hits) / len(terms)
                source = {**context["source"], "layer": layer.value,
                          "content_hash": hashlib.sha256(content.encode()).hexdigest()}
                for project in context["projects"]:
                    add(project, "context_fts", source, f"context:{item_id}", score, _snippet(content, terms))

    if use_vectors and not overlong and read.allowed() and len(read.examined) < POOL_LIMIT:
        for hit in _vectors(service, text, diagnostics):
            similarity = 1.0 - float(hit["distance"]) / 2.0
            if not math.isfinite(similarity) or not 0 <= similarity <= 1 or similarity < .7:
                continue
            context = read.context(int(hit["id"]))
            if context:
                for project in context["projects"]:
                    add(project, "context_vector", context["source"], f"context:{hit['id']}",
                        .65 * similarity, context["text"])

    elif use_vectors:
        diagnostics["vector_status"] = "budget_skipped"

    # A synchronous vector call may itself overrun the soft scheduling budget.
    # Its result never authorizes a narrowed registry after the deadline.
    read.allowed()
    ranked = sorted(found.values(), key=lambda row: (-row["score"], row["project"]))
    reasons = []
    if len(ranked) < 2:
        reasons.append("insufficient_candidates")
    if not ranked or ranked[0]["score"] < .75:
        reasons.append("low_recall_confidence")
    if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < .1:
        reasons.append("ambiguous_scores")
    if ranked and len({e["source"]["kind"] for e in ranked[0]["evidence"]}) < 2:
        reasons.append("uncorroborated_match")
    if len(ranked) > limit or diagnostics["pool_truncated"]:
        reasons.append("candidate_limit")
    chosen = ranked[:limit]
    chosen.extend(row for row in ranked[limit:] if row["project"] in exact_projects)
    fallback = bool(reasons)
    selected = {row["project"] for row in chosen}
    return {"version": VERSION, "requires_reasoning": True, "candidates": chosen,
            "registry": registry if fallback else [p for p in registry if p["project"] in selected],
            "fallback": fallback, "fallback_reasons": reasons, "diagnostics": diagnostics}
