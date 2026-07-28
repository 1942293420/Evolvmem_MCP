"""consolidator tests."""
import hashlib

import numpy as np
import pytest
from evolvmem.consolidator import Consolidator
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


class FakeEngine:
    is_loaded = True
    def __init__(self, dim=8):
        self.dim = dim
    def encode_document(self, text):
        # 相同文本 → 相同向量；不同文本 → 按 hash 散列后归一化（近似正交）
        # 与 test_integration.FakeEmbeddingEngine 一致：md5 定种子（内建 hash() 随
        # PYTHONHASHSEED 加盐，跨进程不稳定）+ randn（rand 全正象限，余弦虚高会误判近重复）
        seed = int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "big")
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def setup(test_config):
    store = MemoryStore(test_config)
    store.initialize()
    vidx = VectorIndex(test_config)
    vidx.initialize(dim=8)
    engine = FakeEngine()
    c = Consolidator(test_config, store, vidx, engine)
    yield store, vidx, engine, c
    vidx.close()
    store.close()


def _add_with_vector(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


class TestFindCandidates:
    def test_identical_values_flagged(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "b:y", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "c:z", "完全不同的东西")
        pairs = c.find_candidates()
        assert len(pairs) == 1
        assert pairs[0]["similarity"] > 0.99

    def test_higher_score_is_keep(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "b:y", "相同内容", importance=9.0)
        pairs = c.find_candidates()
        assert pairs[0]["keep"]["importance"] == 9.0
        assert pairs[0]["drop"]["importance"] == 3.0

    def test_engine_not_loaded_returns_empty(self, test_config):
        store = MemoryStore(test_config); store.initialize()
        vidx = VectorIndex(test_config); vidx.initialize(dim=8)
        engine = FakeEngine(); engine.is_loaded = False
        c = Consolidator(test_config, store, vidx, engine)
        assert c.find_candidates() == []
        vidx.close(); store.close()


class TestConsolidate:
    def test_dry_run_changes_nothing(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容")
        _add_with_vector(store, vidx, engine, "b:y", "相同内容")
        result = c.consolidate(dry_run=True)
        assert len(result["pairs"]) == 1
        assert store.count_active() == 2

    def test_apply_archives_drop(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "b:y", "相同内容", importance=9.0)
        result = c.consolidate(dry_run=False)
        assert result["merged"] == 1
        assert store.count_active() == 1
        active = store.get_active()[0]
        assert active["importance"] == 9.0
