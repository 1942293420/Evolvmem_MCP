"""Hybrid development-capability recall without project-summary injection."""
from __future__ import annotations

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityStore
from evolvmem.capability_recall_eligibility import recall_facts, eligible_ids


CATEGORY_QUERY_TERMS = {
    "frontend": ("前端", "vue", "react", "页面", "组件"),
    "backend": ("后端", "django", "api", "接口", "队列"),
    "business_logic": ("业务逻辑", "业务规则", "业务流程", "审批", "状态流转"),
    "database": ("数据库", "mysql", "sql", "索引", "事务"),
    "architecture": ("架构", "技术选型", "模块划分", "系统设计"),
    "quality": ("测试", "报错", "故障", "性能", "调试", "修复"),
    "delivery": ("部署", "docker", "运维", "发布", "构建"),
}


def merge_ranked(lexical, vector, *, top_k: int) -> list[dict]:
    merged = {}
    for rank, item in enumerate(lexical):
        key = int(item["memory_id"])
        merged[key] = dict(item, _score=1.0 / (rank + 1))
    for rank, item in enumerate(vector):
        key = int(item["memory_id"])
        vector_score = float(item.get("vector_score", 0.0))
        if key in merged:
            merged[key]["_score"] += vector_score
        else:
            merged[key] = dict(item, _score=vector_score / (rank + 1))
    return sorted(merged.values(), key=lambda item: (-item["_score"], item["memory_id"]))[:top_k]


class CapabilityRetriever:
    def __init__(self, service):
        self.service = service

    def _index(self):
        factory = getattr(self.service, "capability_index", None)
        if callable(factory):
            return factory()
        config = getattr(self.service, "config", None)
        if config is None:
            return None
        return CapabilityIndex(
            self.service.store, config, getattr(self.service, "embedding_engine", None),
            eligible_ids=lambda ids: eligible_ids(self.service.store, ids)
        )

    def search(self, query: str, categories=(), top_k: int = 10, *, project=None, module_id=None) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("invalid_query")
        from evolvmem.project_modules import ProjectModuleStore
        modules = ProjectModuleStore(self.service.store)
        if project is not None:
            if not isinstance(project, str):
                raise ValueError("invalid_project")
            modules._project(project)
        if module_id is not None:
            if not isinstance(module_id, str):
                raise ValueError("invalid_module")
            module = modules.get(module_id)
            if module["status"] != "active" or (project is not None and module["project"] != project):
                raise ValueError("invalid_module")
        top_k = max(1, min(int(top_k), 20))
        store = CapabilityStore(self.service.store)
        index = self._index()
        # Encode the vector query once. Backfill only the lexical window when
        # current-source or explicit project/module filters remove early hits.
        budget = 500
        page_size = min(100, max(20, top_k * 3))
        vector = index.search(query, categories, min(200, max(60, top_k * 6))) if index is not None else []
        lexical, facts, module_facts = [], {}, {}
        for offset in range(0, budget, page_size):
            page = store.search(query, categories, min(page_size, budget - offset), offset=offset)
            lexical.extend(page)
            candidates = merge_ranked(lexical, vector, top_k=len(lexical) + len(vector))
            missing = [r["memory_id"] for r in candidates if r["memory_id"] not in facts]
            if missing:
                facts.update(recall_facts(self.service.store, missing))
            result = []
            for row in candidates:
                ident = row["memory_id"]
                fact = facts[ident]
                if not fact["recall_eligible"]:
                    continue
                projects = fact["source_projects"]
                if project is not None and project not in projects:
                    continue
                if ident not in module_facts:
                    module_facts[ident] = modules.modules_for_memory(ident, allowed_sources=fact["eligible_sources"])
                current_modules = module_facts[ident]
                if module_id is not None and not any(m["module_id"] == module_id for m in current_modules):
                    continue
                row["modules"] = [{k: m[k] for k in ("module_id", "project", "name")} for m in current_modules]
                row.update(source_projects=projects, source_project=projects[0] if len(projects) == 1 else "",
                           ownership_confirmed=fact["ownership_confirmed"],
                           provenance=fact, verification_status="unverified")
                result.append(row)
                if len(result) >= top_k:
                    break
            if len(result) >= top_k or len(page) < page_size:
                return result
        return result

    def infer_categories(self, query: str) -> tuple[str, ...]:
        text = str(query).casefold()
        return tuple(
            category
            for category, words in CATEGORY_QUERY_TERMS.items()
            if any(word in text for word in words)
        )

    def render(self, query: str, *, max_chars: int, max_items: int = 12) -> tuple[str, tuple[int, ...]]:
        categories = self.infer_categories(query)
        hits = self.search(query, categories=categories, top_k=max_items)
        blocks = []
        selected = []
        used = 0
        for hit in hits:
            source = "\n来源项目：" + "、".join(hit["source_projects"]) if hit["source_projects"] else "\n项目归属：待确认"
            if hit.get("modules"):
                source += "\n业务模块：" + "、".join(m["project"] + "/" + m["name"] for m in hit["modules"])
            block = f"〔{hit['category']} · 开发知识 capability:{hit['memory_id']} · 方法结果未独立验证〕\n{hit['title']}\n{hit['content']}{source}"
            if blocks and used + len(block) + 2 > max_chars:
                continue
            if len(block) > max_chars:
                continue
            blocks.append(block)
            selected.append(int(hit["memory_id"]))
            used += len(block) + (2 if len(blocks) > 1 else 0)
            if used >= max_chars:
                break
        return "\n\n".join(blocks), tuple(selected)
