"""Retriever 测试。"""

import hashlib
import pytest
import numpy as np
from hermes_memory.memory_store import MemoryStore
from hermes_memory.vector_index import VectorIndex
from hermes_memory.retriever import Retriever


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
