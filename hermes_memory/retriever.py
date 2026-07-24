"""混合检索编排：FTS5/trigram 精确搜索 + HNSW 向量语义搜索。"""

import numpy as np
from hermes_memory.config import Config
from hermes_memory.memory_store import MemoryStore
from hermes_memory.vector_index import VectorIndex


class Retriever:
    """混合检索编排器。

    查询流程:
    1. FTS5/trigram 精确搜索
    2. 向量语义搜索（embedding 引擎可用时）
    3. 去重、加权合并、排序
    4. 回 SQLite 取完整记录
    5. 更新 access_count
    """

    def __init__(self, config: Config, memory_store: MemoryStore,
                 vector_index: VectorIndex, embedding_engine=None):
        self.config = config
        self.store = memory_store
        self.vidx = vector_index
        self.engine = embedding_engine

    def search(self, query: str, top_k: int = 10,
               status_filter: str = "active") -> list[dict]:
        """执行混合检索，返回完整记忆记录列表。"""

        # 1. FTS5 搜索
        fts_results = self.store.search_fts(query, self.config.fts_top_k)

        # 2. 向量搜索（仅在 embedding 可用且索引非空时）
        vector_results = []
        if self._can_vector_search():
            try:
                vec = self.engine.encode(query)
                vector_results = self.vidx.search(
                    np.array(vec, dtype=np.float32),
                    self.config.vector_top_k,
                )
            except Exception:
                pass  # embedding 失败时静默降级到纯 FTS5

        # 3. 去重 + 加权合并
        merged = self._merge(fts_results, vector_results)

        # 4. 按 score 排序，截断
        merged.sort(key=lambda x: x["score"], reverse=True)
        merged = merged[:top_k]

        # 5. 回 SQLite 取完整记录 + 过滤状态
        ids = [m["id"] for m in merged]
        if not ids:
            return []
        records = self.store.get_by_ids(ids)
        record_map = {r["id"]: r for r in records}

        results = []
        for m in merged:
            record = record_map.get(m["id"])
            if record and record.get("status") == status_filter:
                record["score"] = m["score"]
                record["match_type"] = m["match_type"]
                results.append(record)

        # 6. 更新 access_count
        for r in results:
            self.store.update_access(r["id"])

        return results

    def _can_vector_search(self) -> bool:
        return (self.engine is not None
                and self.engine.is_loaded
                and self.vidx.count() > 0)

    def _merge(self, fts_results: list[dict],
               vector_results: list[dict]) -> list[dict]:
        """合并 FTS5 和向量结果，加权计算综合分。"""
        scores: dict[int, dict] = {}

        # FTS5 结果归一化
        if fts_results:
            max_rank = max(r.get("rank", 0) for r in fts_results) or 1.0
            for r in fts_results:
                normalized = r.get("rank", 0) / max_rank
                scores[r["id"]] = {
                    "id": r["id"],
                    "score": normalized * self.config.fts_weight,
                    "match_type": "fts",
                }

        # 向量结果归一化（余弦距离 [0,2] → 越小越好 → 转相似度 [0,1]）
        if vector_results:
            for v in vector_results:
                similarity = max(0.0, 1.0 - v["distance"] / 2.0)
                vec_score = similarity * self.config.vector_weight
                vid = v["id"]
                if vid in scores:
                    scores[vid]["score"] += vec_score
                    scores[vid]["match_type"] += "+vector"
                else:
                    scores[vid] = {
                        "id": vid,
                        "score": vec_score,
                        "match_type": "vector",
                    }

        return list(scores.values())
