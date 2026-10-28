"""Independent USearch cache for active development-capability memories."""
from __future__ import annotations

import numpy as np

from evolvmem.capability_store import CapabilityStore
from evolvmem.vector_index import VectorIndex


class CapabilityIndex:
    def __init__(self, context_store, config, embedding_engine=None):
        self.context_store = context_store
        self.config = config
        self.embedding_engine = embedding_engine
        self.vector_index = VectorIndex(config, path=config.capability_vector_path)

    def _ensure_index(self) -> None:
        try:
            self.vector_index.count()
        except Exception:
            self.vector_index.initialize(dim=self.config.embedding_dim)

    def sync(self, memory_id: int, text: str) -> str:
        store = CapabilityStore(self.context_store)
        memory = store.get(int(memory_id))
        try:
            self._ensure_index()
            if memory is None or memory["status"] != "active":
                self.vector_index.remove(int(memory_id))
                self.vector_index.save()
                return "synchronized"
            self.vector_index.mark_dirty()
            if self.embedding_engine is None or not getattr(self.embedding_engine, "is_loaded", False):
                self.vector_index.preserve_dirty()
                return "unavailable"
            embedding = np.asarray(self.embedding_engine.encode_document(text), dtype=np.float32)
            if embedding.ndim != 1 or embedding.shape[0] != self.config.embedding_dim:
                raise ValueError("embedding dimension mismatch")
            self.vector_index.remove(int(memory_id))
            self.vector_index.add(int(memory_id), embedding)
            self.vector_index.save()
            self.vector_index.clear_dirty()
            return "synchronized"
        except Exception:
            self.vector_index.preserve_dirty()
            return "failed"

    def search(self, query: str, categories=(), top_k: int = 10) -> list[dict]:
        if self.embedding_engine is None or not getattr(self.embedding_engine, "is_loaded", False):
            return []
        try:
            self._ensure_index()
            if self.vector_index.count() == 0:
                return []
            embedding = np.asarray(self.embedding_engine.encode_query(query), dtype=np.float32)
            matches = self.vector_index.search(embedding, k=max(1, int(top_k)))
        except Exception:
            return []
        store = CapabilityStore(self.context_store)
        categories = set(categories)
        results = []
        for match in matches:
            memory = store.get(int(match["id"]))
            if memory is None or memory["status"] != "active":
                continue
            if categories and memory["category"] not in categories:
                continue
            memory["vector_score"] = 1.0 - float(match["distance"]) / 2.0
            results.append(memory)
        return results
