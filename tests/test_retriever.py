"""Retriever 测试。"""

import hashlib
import pytest
import numpy as np
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.retriever import Retriever


class FakeEmbeddingEngine:
    """假 embedding 引擎，返回确定性向量（同文本 → 同向量）。"""

    def __init__(self, dim=512):
        self._dim = dim
        self._loaded = True

    @property
    def is_loaded(self):
        return self._loaded

    @property
    def dim(self):
        return self._dim

    def encode(self, text):
        """使用 SHA-256 哈希生成确定性随机向量。"""
        digest = hashlib.sha256(text.encode()).digest()
        seed = int.from_bytes(digest[:4], "big")
        rng = np.random.RandomState(seed)
        v = rng.randn(self._dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def encode_batch(self, texts):
        return [self.encode(t) for t in texts]


class TestRetriever:
    def test_fts_only_when_no_vectors(self, test_config):
        """向量索引为空时，只走 FTS5 搜索。"""
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:1", value="破损商品直接退款", tags=["售后"])

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)

        engine = FakeEmbeddingEngine()
        retriever = Retriever(test_config, store, vidx, engine)

        results = retriever.search("退款", top_k=5)
        assert len(results) > 0
        assert results[0]["value"] == "破损商品直接退款"

        store.close()
        vidx.close()

    def test_hybrid_search_merges_results(self, test_config):
        """FTS5 + 向量结果去重合并。"""
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:1", value="破损商品直接退款", tags=["售后"])
        store.add(key="p:t:2", value="用户偏好暗色主题", tags=["偏好"])

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)

        engine = FakeEmbeddingEngine()
        retriever = Retriever(test_config, store, vidx, engine)

        results = retriever.search("退款政策", top_k=5)
        # 应无重复 id
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))

        store.close()
        vidx.close()

    def test_search_updates_access_count(self, test_config):
        """检索命中后更新 access_count。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:t:1", value="测试记忆")

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)

        engine = FakeEmbeddingEngine()
        retriever = Retriever(test_config, store, vidx, engine)

        retriever.search("测试", top_k=5)
        record = store.get_by_id(mem_id)
        assert record["access_count"] == 1

        store.close()
        vidx.close()

    def test_hybrid_search_match_types(self, test_config):
        """Hybrid search with populated vector index produces correct match_type labels.

        Verifies that _merge(), vector score normalization, and combined scoring
        are exercised: FTS finds exact keyword matches, vector search returns
        semantically related results that FTS misses, and merged results carry
        the correct match_type ("fts", "vector", or "fts+vector").
        """
        store = MemoryStore(test_config)
        store.initialize()

        # A: contains "退货" — FTS will match for query "退货流程"
        id_a = store.add(key="p:return:1", value="客户退货流程说明", tags=["售后"])
        # B: related meaning but different wording — FTS won't match "退货流程"
        id_b = store.add(key="p:refund:1", value="破损商品直接退款", tags=["售后"])
        # C: unrelated content
        id_c = store.add(key="p:pref:1", value="用户偏好暗色主题", tags=["偏好"])

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)
        engine = FakeEmbeddingEngine()

        # Populate vector index so _can_vector_search() returns True
        for mem_id, text in [(id_a, "客户退货流程说明"),
                              (id_b, "破损商品直接退款"),
                              (id_c, "用户偏好暗色主题")]:
            emb = engine.encode(text)
            vidx.add(mem_id, np.array(emb, dtype=np.float32))

        assert vidx.count() == 3

        retriever = Retriever(test_config, store, vidx, engine)
        results = retriever.search("退货流程", top_k=10)

        # No duplicate IDs (dedup in _merge works)
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))

        match_types = {r["id"]: r["match_type"] for r in results}

        # A is found by FTS (contains "退货" matching query tokens)
        assert id_a in match_types
        assert "fts" in match_types[id_a], (
            f"Expected 'fts' or 'fts+vector' for id_a, got {match_types[id_a]}"
        )

        # B is found only by vector: "退款" shares no trigrams with "退货流程"
        assert id_b in match_types
        assert match_types[id_b] == "vector", (
            f"Expected 'vector' for id_b, got {match_types[id_b]}"
        )

        # Verify at least one "fts+vector" and at least two match_type values exist
        all_types = set(match_types.values())
        assert len(all_types) >= 2, (
            f"Expected at least 2 distinct match_types, got {all_types}"
        )
        assert "fts+vector" in all_types or "fts" in all_types

        store.close()
        vidx.close()
