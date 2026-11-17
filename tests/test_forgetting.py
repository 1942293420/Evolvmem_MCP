"""ForgettingEngine 测试。"""

import pytest
from evolvmem.context_models import ContextMode, ContextStatus
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
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

    def test_pinned_never_candidate(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:f:pinned", value="常驻规则", tier="pinned")
        store.add(key="p:f:normal", value="普通记忆")

        engine = ForgettingEngine(test_config, store)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2
        engine.config.forget_rate_limit_days = 0
        candidates = engine.find_candidates()
        # pinned 永不归档，只剩 normal 一条候选
        assert len(candidates) == 1
        assert candidates[0]["key"] == "p:f:normal"
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

    def test_progress_log_key_with_non_fact_attribute_is_not_a_summary(
            self, test_config):
        with MemoryStore(test_config) as store:
            mid = store.add(
                key="project:eva:progress:log:decision",
                value="这个同形状条目明确属于项目决定。",
                attribute="decision",
                expires_at="2020-01-01 00:00:00",
            )

            archived = ForgettingEngine(test_config, store).run()

            assert archived == 1
            assert store.get_by_id(mid)["status"] == "archived"


def _make_compat_facade(config, *, store=None):
    """compat 模式的 ContextService + 兼容门面（legacy 投影 schema 先就位）。

    生产切换前数据库已带 memories 表；测试里用 MemoryStore 建出同一 schema。
    """
    with MemoryStore(config):
        pass
    service = (
        ContextService(config, store=store)
        if store is not None
        else ContextService(config)
    )
    service.initialize(mode=ContextMode.COMPAT, adapter="test")
    return service, service.legacy_facade()


class TestForgettingThroughFacade:
    """compat 模式下，遗忘维护的 archive 变更经门面落到双侧（Core + 投影）。"""

    def test_run_archives_expired_on_both_sides(self, test_config):
        service, facade = _make_compat_facade(test_config)
        legacy_id = facade.add(key="p:t:fact:exp", value="已过期记忆",
                               expires_at="2020-01-01 00:00:00")
        context_id = service.store.resolve_legacy_mapping(legacy_id)
        assert context_id is not None

        archived = ForgettingEngine(test_config, facade).run()

        assert archived >= 1
        assert facade.get_by_id(legacy_id)["status"] == "archived"
        assert service.store.get_item(context_id).status is ContextStatus.ARCHIVED
        service.close()

    def test_run_leaves_expired_session_summary_to_retention_gate(self, test_config):
        from evolvmem.project_store import ProjectStore

        service, facade = _make_compat_facade(test_config)
        with service.store.transaction():
            ProjectStore(
                service.store._connection(),
                service.store._require_transaction,
                generic_names=(),
            ).register_project("eva")
        legacy_id = facade.add(
            key="project:eva:progress:log:t1",
            value="本次完成摘要覆盖门控接线。",
            attribute="fact",
            expires_at="2020-01-01 00:00:00",
        )
        context_id = service.store.resolve_legacy_mapping(legacy_id)

        archived = ForgettingEngine(test_config, facade).run()

        assert archived == 0
        assert facade.get_by_id(legacy_id)["status"] == "active"
        assert service.store.get_item(context_id).status is ContextStatus.ACTIVE
        service.close()

    def test_decay_candidate_archives_both_sides(self, test_config):
        service, facade = _make_compat_facade(test_config)
        legacy_id = facade.add(key="p:t:fact:cold", value="久未访问的冷记忆")
        context_id = service.store.resolve_legacy_mapping(legacy_id)

        engine = ForgettingEngine(test_config, facade)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2
        engine.config.forget_rate_limit_days = 0

        assert engine.run() == 1
        assert facade.get_by_id(legacy_id)["status"] == "archived"
        assert service.store.get_item(context_id).status is ContextStatus.ARCHIVED
        service.close()

    def test_pinned_survives_forgetting_on_both_sides(self, test_config):
        service, facade = _make_compat_facade(test_config)
        pinned_id = facade.add(key="p:t:rule:pinned", value="常驻规则",
                               tier="pinned")
        normal_id = facade.add(key="p:t:fact:normal", value="普通记忆")
        pinned_ctx = service.store.resolve_legacy_mapping(pinned_id)

        engine = ForgettingEngine(test_config, facade)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2
        engine.config.forget_rate_limit_days = 0

        assert engine.run() == 1
        assert facade.get_by_id(pinned_id)["status"] == "active"
        assert service.store.get_item(pinned_ctx).status is ContextStatus.ACTIVE
        assert facade.get_by_id(normal_id)["status"] == "archived"
        service.close()

    def test_archive_failure_rolls_back_both_sides(self, test_config):
        """Core 侧写入失败时整对回滚：投影与 ContextItem 都保持 active。"""

        class FailingStore(ContextStore):
            def set_item_status(self, item_id, status):
                raise RuntimeError("injected core failure")

        service, facade = _make_compat_facade(
            test_config, store=FailingStore(test_config)
        )
        legacy_id = facade.add(key="p:t:fact:exp", value="已过期记忆",
                               expires_at="2020-01-01 00:00:00")
        context_id = service.store.resolve_legacy_mapping(legacy_id)

        with pytest.raises(RuntimeError, match="injected core failure"):
            ForgettingEngine(test_config, facade).run()

        assert facade.get_by_id(legacy_id)["status"] == "active"
        assert service.store.get_item(context_id).status is ContextStatus.ACTIVE
        service.close()
