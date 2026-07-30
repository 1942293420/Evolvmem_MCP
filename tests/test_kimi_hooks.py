"""kimi_hooks persist tests."""
import numpy as np
import pytest
from evolvmem.kimi_hooks import _persist_candidates
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from tests.test_semantic_merge import FakeEngine


def _add(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


def test_semantically_similar_candidate_merges(test_config):
    store = MemoryStore(test_config); store.initialize()
    vidx = VectorIndex(test_config); vidx.initialize(dim=8)
    engine = FakeEngine()
    old = _add(store, vidx, engine, "p:t:decision:db", "数据库选用 MySQL")

    cands = [CandidateMemory(key="other:key:fact:x",
                             value="数据库选用 MySQL",
                             attribute="fact", importance=8.0)]
    n = _persist_candidates(test_config, store, vidx, engine, cands, "sess")
    assert n == 1
    # 旧 key 被 supersede，而不是新增一条并列记忆
    assert store.count_active() == 1
    active = store.get_active()[0]
    assert active["key"] == "p:t:decision:db"
    assert active["value"] == "数据库选用 MySQL"
    assert store.get_by_id(old)["status"] == "superseded"
    vidx.close(); store.close()
