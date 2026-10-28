"""Hybrid development-capability recall without project-summary injection."""
from __future__ import annotations

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityStore


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
            self.service.store, config, getattr(self.service, "embedding_engine", None)
        )

    def search(self, query: str, categories=(), top_k: int = 10) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("invalid_query")
        top_k = max(1, min(int(top_k), 20))
        lexical = CapabilityStore(self.service.store).search(query, categories, top_k * 3)
        index = self._index()
        vector = index.search(query, categories, top_k * 3) if index is not None else []
        return merge_ranked(lexical, vector, top_k=top_k)

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
            source = f"\n来源项目：{hit['source_project']}" if hit["source_project"] else ""
            block = f"〔{hit['category']} · 记忆 #{hit['memory_id']}〕\n{hit['title']}\n{hit['content']}{source}"
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
