"""semantic_merge tests."""
import numpy as np
import pytest
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.semantic_merge import find_semantic_match


class FakeEngine:
    is_loaded = True
    def __init__(self, dim=8):
        self.dim = dim
    def encode_document(self, text):
        import hashlib
        seed = int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "big")
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def setup(test_config):
    store = MemoryStore(test_config); store.initialize()
    vidx = VectorIndex(test_config); vidx.initialize(dim=8)
    engine = FakeEngine()
    yield store, vidx, engine
    vidx.close(); store.close()


def _add(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


class TestFindSemanticMatch:
    def test_identical_value_matches(self, setup):
        store, vidx, engine = setup
        mid = _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "数据库选用 PostgreSQL", 0.95, key="p:t:fact:a")
        assert hit is not None and hit["id"] == mid
        assert hit["similarity"] > 0.99

    def test_different_value_no_match(self, setup):
        store, vidx, engine = setup
        _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "用户偏好中文界面", 0.95)
        assert hit is None

    def test_reference_tier_never_matches(self, setup):
        store, vidx, engine = setup
        _add(store, vidx, engine, "p:t:arch:doc", "长文档内容", tier="reference")
        hit = find_semantic_match(store, vidx, engine, "长文档内容", 0.95)
        assert hit is None

    def test_engine_not_loaded_returns_none(self, setup):
        store, vidx, engine = setup
        engine.is_loaded = False
        _add(store, vidx, engine, "p:t:fact:a", "任意内容")
        assert find_semantic_match(store, vidx, engine, "任意内容", 0.95) is None

    def test_exclude_id_skips_self(self, setup):
        store, vidx, engine = setup
        mid = _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "数据库选用 PostgreSQL", 0.95, exclude_id=mid)
        assert hit is None


def test_semantic_merge_requires_same_project_type_and_entity(setup):
    store, vidx, engine = setup
    value = '数据库选用 PostgreSQL'
    mid = _add(store, vidx, engine, 'project:alpha:db:choice', value, attribute='decision')
    for key, attribute in [('project:beta:db:choice', 'decision'),
                           ('project:alpha:cache:choice', 'decision'),
                           ('project:alpha:db:choice', 'fact')]:
        assert find_semantic_match(store, vidx, engine, value, .95,
                                   key=key, attribute=attribute) is None
    hit = find_semantic_match(store, vidx, engine, value, .95,
                              key='alpha:db:choice', attribute='decision')
    assert hit['id'] == mid


def test_semantic_merge_unknown_identity_never_overwrites(setup):
    store, vidx, engine = setup
    _add(store, vidx, engine, 'unknown', '数据库选用 PostgreSQL')
    assert find_semantic_match(store, vidx, engine, '数据库选用 PostgreSQL', .95) is None
