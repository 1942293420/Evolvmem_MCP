"""ForgettingEngine 测试。"""

import pytest
from evolvmem.memory_store import MemoryStore
from evolvmem.forgetting import ForgettingEngine


class TestForgettingEngine:
    def test_recently_accessed_not_candidate(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:f:1", value="频繁访问的记忆")
        store.update_access(mem_id)  # 刚被访问

        engine = ForgettingEngine(test_config, store)
        candidates = engine.find_candidates()
        assert len(candidates) == 0  # 刚访问过，不降级
        store.close()

    def test_never_accessed_is_candidate(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:f:1", value="从未被访问的记忆")

        engine = ForgettingEngine(test_config, store)
        engine.config.forget_days_threshold = 0     # 立即生效
        engine.config.forget_access_count_threshold = 2
        engine.config.forget_rate_limit_days = 0    # 立即生效
        candidates = engine.find_candidates()
        # 从未被访问，access_count=0 ≤ 2，是候选
        assert len(candidates) == 1
        store.close()

    def test_archive_moves_to_archived_status(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:f:1", value="待降级记忆")

        engine = ForgettingEngine(test_config, store)
        engine.archive(mem_id)

        record = store.get_by_id(mem_id)
        assert record["status"] == "archived"
        # active 列表不再包含
        assert len(store.get_active()) == 0
        store.close()

    def test_run_full_cycle(self, test_config):
        """完整遗忘周期：找到候选 → 降级。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:f:1", value="冷记忆")

        engine = ForgettingEngine(test_config, store)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2
        engine.config.forget_rate_limit_days = 0

        archived_count = engine.run()
        assert archived_count == 1
        assert store.get_by_id(mem_id)["status"] == "archived"
        store.close()

    def test_run_archives_expired(self, test_config):
        from evolvmem.memory_store import MemoryStore
        from evolvmem.forgetting import ForgettingEngine
        with MemoryStore(test_config) as store:
            mid = store.add(key="p:t:fact:exp", value="已过期",
                            expires_at="2020-01-01 00:00:00")
            archived = ForgettingEngine(test_config, store).run()
            assert archived >= 1
            assert store.get_by_id(mid)["status"] == "archived"
