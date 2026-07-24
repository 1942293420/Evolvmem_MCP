"""MemoryStore tests."""

import pytest
from hermes_memory.memory_store import MemoryStore


class TestMemoryStore:
    def test_initialize_creates_tables(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        # Verify memories table exists
        rows = store._execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
        assert len(rows) == 1
        store.close()

    def test_add_and_get_active(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(
            key="project:test:fact:sample",
            value="test memory content",
            category="fact",
            tags=["test", "example"],
            source_session="sess_001",
        )
        assert mem_id == 1

        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["key"] == "project:test:fact:sample"
        assert actives[0]["status"] == "active"
        store.close()

    def test_replace_supersedes_old(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        old_id = store.add(key="project:test:decision:x", value="plan A", category="decision")
        new_id = store.replace(key="project:test:decision:x", new_value="plan B, abandoned plan A")

        # Old record is superseded
        old = store.get_by_id(old_id)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == new_id

        # New record is active
        new = store.get_by_id(new_id)
        assert new["status"] == "active"
        assert new["supersedes"] == old_id

        # get_active returns only one
        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["value"] == "plan B, abandoned plan A"
        store.close()

    def test_remove_soft_delete(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="project:test:temp", value="temporary content")
        store.remove(mem_id)
        record = store.get_by_id(mem_id)
        assert record["status"] == "deleted"
        # get_active does not return deleted
        assert len(store.get_active()) == 0
        store.close()

    def test_fts_search_finds_chinese(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        assert store._has_cjk("退款") is True

        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["售后"])
        store.add(key="p:a:fact:2", value="用户偏好暗色主题界面", tags=["偏好"])
        store.add(key="p:a:fact:3", value="Python 版本需要 3.10 以上", tags=["技术"])

        results = store.search_fts("退款")
        assert len(results) == 1
        assert "破损商品" in results[0]["value"]
        store.close()

    def test_trigram_search_finds_chinese_substring(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["售后"])

        results = store.search_fts("破损")         # trigram can match
        assert len(results) == 1
        results2 = store.search_fts("直接退款")     # phrase substring
        assert len(results2) == 1
        store.close()

    def test_fts_falls_back_to_like_when_trigram_unavailable(self, test_config):
        """When trigram tokenizer is unavailable, fall back to LIKE search."""
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["test"])

        # Verify CJK detection works
        assert store._has_cjk("退款") is True

        # Simulate trigram unavailable: force LIKE path directly
        results = store._search_like("退款")
        assert len(results) == 1
        assert "破损商品" in results[0]["value"]
        store.close()

    def test_search_like_excludes_deleted(self, test_config):
        """_search_like should not return soft-deleted records."""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:del:test", value="破损商品直接退款", tags=["test"])
        store.remove(mem_id)
        # Deleted record should not appear in LIKE results
        results = store._search_like("退款")
        assert len(results) == 0
        store.close()

    def test_duplicate_key_skip(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:dup:test", value="first entry")
        # Same key, same value add_if_changed should skip
        result = store.add_if_changed(key="p:dup:test", value="first entry")
        assert result is None  # not written
        assert len(store.get_active()) == 1
        store.close()

    def test_update_access_count(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:acc:test", value="access test")
        store.update_access(mem_id)
        record = store.get_by_id(mem_id)
        assert record["access_count"] == 1
        assert record["last_accessed"] is not None
        store.close()
