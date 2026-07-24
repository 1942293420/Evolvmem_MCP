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
        store.add(key="p:a:fact:1", value="damaged goods get direct refund, no resend", tags=["aftersales"])
        store.add(key="p:a:fact:2", value="user prefers dark theme interface", tags=["preference"])
        store.add(key="p:a:fact:3", value="Python version requires 3.10 or higher", tags=["tech"])

        results = store.search_fts("refund")
        assert len(results) == 1
        assert "damaged goods" in results[0]["value"]
        store.close()

    def test_trigram_search_finds_chinese_substring(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="damaged goods get direct refund, no resend", tags=["aftersales"])

        results = store.search_fts("damaged")         # trigram can match
        assert len(results) == 1
        results2 = store.search_fts("direct refund")     # phrase substring
        assert len(results2) == 1
        store.close()

    def test_fts_falls_back_to_like_when_trigram_unavailable(self, test_config):
        """When trigram tokenizer is unavailable, fall back to LIKE search."""
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="damaged goods direct refund", tags=["test"])

        # Simulate trigram unavailable: force LIKE path directly
        results = store._search_like("refund")
        assert len(results) == 1
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
