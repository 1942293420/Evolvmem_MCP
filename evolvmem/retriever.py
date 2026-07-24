"""Hybrid retrieval orchestration: FTS5/trigram exact search + HNSW vector semantic search."""

import numpy as np
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


class Retriever:
    """Hybrid retrieval orchestrator.

    Query flow:
    1. FTS5/trigram exact search
    2. Vector semantic search (when embedding engine is available)
    3. Deduplicate, weighted merge
    4. Fetch full records from SQLite (all merged results)
    5. Filter by status → sort by score → truncate to top_k
    6. Update access_count
    """

    def __init__(self, config: Config, memory_store: MemoryStore,
                 vector_index: VectorIndex, embedding_engine=None):
        self.config = config
        self.store = memory_store
        self.vidx = vector_index
        self.engine = embedding_engine

    def search(self, query: str, top_k: int = 10,
               status_filter: str = "active") -> list[dict]:
        """Execute hybrid search, returns full memory record list."""

        # 1. FTS5 search
        fts_results = self.store.search_fts(query, self.config.fts_top_k)

        # 2. Vector search (only when embedding is available and index is non-empty)
        vector_results = []
        if self._can_vector_search():
            try:
                vec = self.engine.encode(query)
                vector_results = self.vidx.search(
                    np.array(vec, dtype=np.float32),
                    self.config.vector_top_k,
                )
            except Exception:
                pass  # gracefully degrade to pure FTS5 on embedding failure

        # 3. Deduplicate + weighted merge
        merged = self._merge(fts_results, vector_results)

        # 4. Fetch full records from SQLite (all merged results, no truncation)
        ids = [m["id"] for m in merged]
        if not ids:
            return []
        records = self.store.get_by_ids(ids)
        record_map = {r["id"]: r for r in records}

        # 5. Filter status → sort by score → truncate top_k
        results = []
        for m in merged:
            record = record_map.get(m["id"])
            if record and record.get("status") == status_filter:
                record["score"] = m["score"]
                record["match_type"] = m["match_type"]
                results.append(record)

        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:top_k]

        # 6. Update access_count
        for r in results:
            self.store.update_access(r["id"])

        return results

    def _can_vector_search(self) -> bool:
        return (self.engine is not None
                and self.engine.is_loaded
                and self.vidx.count() > 0)

    def _merge(self, fts_results: list[dict],
               vector_results: list[dict]) -> list[dict]:
        """Merge FTS5 and vector results, compute weighted composite score."""
        scores: dict[int, dict] = {}

        # Normalize FTS5 results
        if fts_results:
            max_rank = max(r.get("rank", 0) for r in fts_results) or 1.0
            for r in fts_results:
                normalized = r.get("rank", 0) / max_rank
                scores[r["id"]] = {
                    "id": r["id"],
                    "score": normalized * self.config.fts_weight,
                    "match_type": "fts",
                }

        # Normalize vector results (cosine distance [0,2] → lower is better → convert to similarity [0,1])
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
