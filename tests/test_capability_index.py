"""Independent, rebuildable capability vector cache."""
import numpy as np

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore


class FakeEngine:
    is_loaded = True

    def encode_document(self, text):
        return np.asarray([1.0, 0.0], dtype=np.float32)

    def encode_query(self, text):
        return np.asarray([1.0, 0.0], dtype=np.float32)


def test_capability_vector_sync_and_search_use_separate_cache(test_config):
    test_config.embedding_dim = 2
    with ContextStore(test_config) as store:
        memory_id = CapabilityStore(store).upsert(CapabilityMemoryDraft(
            category="frontend", title="Vue表单", content="提交前校验。", source_project="bluewhale"))
        index = CapabilityIndex(store, test_config, FakeEngine())

        assert index.sync(memory_id, "Vue表单 提交前校验") == "synchronized"
        assert test_config.capability_vector_path.exists()
        assert not test_config.context_vector_path.exists()
        assert [hit["memory_id"] for hit in index.search("前端校验")] == [memory_id]


def test_capability_vector_unavailable_keeps_lexical_store_usable(test_config):
    with ContextStore(test_config) as store:
        memory_id = CapabilityStore(store).upsert(CapabilityMemoryDraft(
            category="frontend", title="Vue表单", content="提交前校验。"))
        index = CapabilityIndex(store, test_config, None)

        assert index.sync(memory_id, "Vue表单") == "unavailable"
        assert index.search("前端校验") == []
        assert CapabilityStore(store).search("Vue表单")
