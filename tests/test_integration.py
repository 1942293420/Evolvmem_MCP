"""端到端集成测试——从写入到检索的完整链路。"""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import pytest
import numpy as np
import evolvmem.kimi_hooks as hooks
from evolvmem.config import Config
from evolvmem.consolidator import Consolidator
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.memory_store import MemoryStore
from evolvmem.session_archive import SessionArchiver
from evolvmem.vector_index import VectorIndex
from evolvmem.retriever import Retriever
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.forgetting import ForgettingEngine
from evolvmem.auto_extractor import AutoExtractor


@pytest.fixture
def server(test_config):
    """MemoryMCPServer wired to a temp-dir store plus an injected compat-mode
    ContextService (embedding engine stays unloaded)."""
    from evolvmem.mcp_server import MemoryMCPServer
    with MemoryStore(test_config):
        pass  # legacy 投影 schema 引导；只有测试允许持有裸 store
    srv = MemoryMCPServer(config=test_config)
    service = ContextService(test_config, embedding_engine=srv.engine)
    service._legacy_vector = srv.vidx  # 共享 legacy 投影向量索引实例
    service.initialize(mode=ContextMode.COMPAT, adapter="test")
    srv.context_service = service
    srv.conflict_detector = ConflictDetector(service.legacy_facade())
    yield srv
    service.close()


@pytest.fixture
def degraded_primary_server(test_config):
    """Server whose injected primary-mode service failed the invariant gate."""
    from evolvmem.mcp_server import MemoryMCPServer
    with MemoryStore(test_config):
        pass  # legacy 投影 schema 引导
    srv = MemoryMCPServer(config=test_config)
    service = ContextService(test_config, embedding_engine=srv.engine)
    service._legacy_vector = srv.vidx
    service.initialize(mode=ContextMode.PRIMARY, adapter="test")
    assert service.status().ready is False  # 未初始化 context 向量索引 → 降级
    assert service.status().reason_codes == ("degraded_legacy",)
    srv.context_service = service
    srv.conflict_detector = ConflictDetector(service.legacy_facade())
    yield srv
    service.close()


def _facade(server):
    """测试读取断言统一走兼容门面（与生产读取同一路径）。"""
    return server.context_service.legacy_facade()


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
        """确定性随机向量（基于 text hash），保证相同 text 返回相同向量。"""
        h = hashlib.md5(text.encode()).digest()
        seed = int.from_bytes(h[:4], 'big')
        rng = np.random.RandomState(seed)
        v = rng.randn(self._dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def encode_batch(self, texts):
        return [self.encode(t) for t in texts]

    def encode_query(self, text):
        return self.encode(text)

    def encode_document(self, text):
        return self.encode(text)


class FailingVectorIndex:
    def add(self, memory_id, embedding):
        raise RuntimeError("synthetic vector failure")

    def save(self):
        raise AssertionError("save must not run after add failure")


class LoadedFakeEngine:
    is_loaded = True

    def encode_document(self, value):
        return [0.0, 1.0]


class TestIntegration:
    """完整的记忆生命周期测试。"""

    def test_full_lifecycle_write_search_replace(self, test_config):
        """完整生命周期：写入 → 检索 → 替换 → 验证历史。"""
        # 初始化
        store = MemoryStore(test_config)
        store.initialize()
        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)
        engine = FakeEmbeddingEngine()
        retriever = Retriever(test_config, store, vidx, engine)
        detector = ConflictDetector(store)

        # 1. 写入记忆
        mem1_id = store.add(
            key="project:shop:decision:after_sales",
            value="破损商品直接退款，不再补发",
            attribute="decision",
            tags=["售后", "退款"],
        )
        mem2_id = store.add(
            key="user:preference:theme",
            value="偏好暗色主题界面",
            attribute="preference",
            tags=["UI"],
        )
        mem3_id = store.add(
            key="project:db:fact:version",
            value="PostgreSQL 版本需 15 以上",
            attribute="fact",
            tags=["数据库"],
        )

        # 更新向量索引
        for mid in [mem1_id, mem2_id, mem3_id]:
            rec = store.get_by_id(mid)
            vec = np.array(engine.encode(rec["value"]), dtype=np.float32)
            vidx.add(mid, vec)
        vidx.save()

        # 2. FTS5 精确搜索（混合检索会返回多项，但正确结果应在其中）
        results = retriever.search("退款", top_k=5)
        assert len(results) >= 1
        result_keys = [r["key"] for r in results]
        assert "project:shop:decision:after_sales" in result_keys

        # 3. 语义搜索（换了表达方式）
        results = retriever.search("用户喜欢什么颜色", top_k=5)
        # 应命中 "暗色主题"
        keys = [r["key"] for r in results]
        assert "user:preference:theme" in keys

        # 4. 替换记忆
        decision = detector.check(
            "project:shop:decision:after_sales",
            "破损商品直接退款，不再补发；VIP 客户额外补偿优惠券",
        )
        assert decision.action == "replace"

        new_id = store.replace(
            key="project:shop:decision:after_sales",
            new_value="破损商品直接退款，不再补发；VIP 客户额外补偿优惠券",
        )
        new_vec = np.array(
            engine.encode(
                "破损商品直接退款，不再补发；VIP 客户额外补偿优惠券"
            ),
            dtype=np.float32,
        )
        vidx.add(new_id, new_vec)
        vidx.save()

        # 5. 验证：旧值 superseded，新值 active
        old = store.get_by_id(mem1_id)
        assert old["status"] == "superseded"
        new = store.get_by_id(new_id)
        assert new["status"] == "active"

        # get_active 只返回新的
        actives = store.get_active()
        after_sales_memories = [
            m for m in actives
            if m["key"] == "project:shop:decision:after_sales"
        ]
        assert len(after_sales_memories) == 1
        assert "VIP" in after_sales_memories[0]["value"]

        # 6. 历史查询仍能找到旧值
        history = store.get_by_key("project:shop:decision:after_sales")
        assert len(history) == 2  # 旧 (superseded) + 新 (active)

        # 清理
        store.close()
        vidx.close()

    def test_vector_failure_after_commit_keeps_sqlite_records(
            self, monkeypatch, test_config):
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)
        with MemoryStore(test_config) as store:
            memory_id = store.add(
                "project:x:decision:api",
                "采用统一接口，因为它减少重复实现。",
            )
            hooks._sync_candidate_vectors(
                store,
                FailingVectorIndex(),
                LoadedFakeEngine(),
                [memory_id],
            )
            assert store.get_by_id(memory_id)["status"] == "active"
        assert logs == ["vector sync skipped: RuntimeError"]
        assert "统一接口" not in "\n".join(logs)

    def test_memory_add_with_importance_tier(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:constraint:db",
            "value": "禁止直接操作生产数据库",
            "attribute": "constraint",
            "importance": 9.0,
            "tier": "pinned",
        })
        assert result["status"] == "added"
        # 可选增量字段：context_id 与 available_layers
        assert result["context_id"] is not None
        assert result["available_layers"] == ["l0", "l1", "l2"]
        rec = _facade(server).get_by_id(result["id"])
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
        # 双侧：mapping + 三层 + 状态
        context_store = server.context_service.store
        assert context_store.resolve_legacy_mapping(result["id"]) == (
            result["context_id"]
        )
        item = context_store.get_item(result["context_id"])
        assert item.status is ContextStatus.ACTIVE
        assert item.layers is not None
        assert item.layers.l1 == "禁止直接操作生产数据库"
        assert item.importance == 9.0
        assert item.tier is ContextTier.PINNED

    def test_memory_add_nan_importance_uses_default(self, server):
        """min(10.0, nan) 返回 10.0 —— NaN importance 必须走默认值路径落库为 5.0。"""
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:nan",
            "value": "nan importance 测试",
            "importance": float("nan"),
        })
        assert result["status"] == "added"
        rec = _facade(server).get_by_id(result["id"])
        assert rec["importance"] == 5.0

    def test_memory_add_rejects_overlong_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:long", "value": "x" * 501,
        })
        assert "error" in result
        assert "too long" in result["error"]
        assert _facade(server).count_active() == 0

    def test_memory_add_accepts_value_at_limit(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:ok", "value": "x" * 500,
        })
        assert result["status"] == "added"

    def test_memory_add_rejects_trivial_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:trivial", "value": "等待用户指令。",
        })
        assert "error" in result
        assert _facade(server).count_active() == 0

    def test_memory_add_rejects_too_short(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:short", "value": "短",
        })
        assert "error" in result

    def test_memory_add_accepts_normal_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:ok", "value": "供应商合同必须双人复核后归档",
        })
        assert result["status"] == "added"

    def test_memory_add_skips_merge_when_engine_not_loaded(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:x", "value": "供应商合同必须双人复核后归档",
        })
        assert result["status"] == "added"

    def test_memory_add_merges_semantic_duplicate(self, server, test_config):
        """跨 key 语义合并：不同 key、同 value → merged，active 只剩一条，旧记录 supersede。"""
        test_config.embedding_dim = 512  # 与 FakeEmbeddingEngine/索引维度一致
        server.vidx = VectorIndex(test_config)
        server.vidx.initialize(dim=512)
        server.engine = FakeEmbeddingEngine()
        # 服务与处理器共享同一 legacy 索引/引擎实例，合并命中才能彼此可见
        server.context_service._legacy_vector = server.vidx
        server.context_service.embedding_engine = server.engine
        try:
            first = server.handle_tool_call("memory_add", {
                "key": "p:t:decision:db", "value": "数据库选用 MySQL",
            })
            assert first["status"] == "added"
            second = server.handle_tool_call("memory_add", {
                "key": "project:p:t:decision:db", "value": "数据库选用 MySQL",
            })
            assert second["status"] == "merged"
            assert second["merged_into"] == first["id"]
            assert second["key"] == "p:t:decision:db"
            # 可选增量字段
            assert second["context_id"] is not None
            assert second["old_context_id"] == first["context_id"]
            assert second["available_layers"] == ["l0", "l1", "l2"]
            assert _facade(server).count_active() == 1
            active = _facade(server).get_active()[0]
            assert active["key"] == "p:t:decision:db"
            assert active["value"] == "数据库选用 MySQL"
            assert _facade(server).get_by_id(first["id"])["status"] == "superseded"
            # 双侧：旧 ContextItem superseded，新两侧映射一致
            context_store = server.context_service.store
            assert context_store.get_item(first["context_id"]).status is (
                ContextStatus.SUPERSEDED
            )
            assert context_store.get_item(second["context_id"]).status is (
                ContextStatus.ACTIVE
            )
            assert context_store.resolve_legacy_mapping(second["new_id"]) == (
                second["context_id"]
            )
        finally:
            server.vidx.close()

    def test_memory_add_reference_tier_skips_merge(self, server, test_config):
        """tier="reference" 的新值不参与语义合并——永不 supersede 别人。"""
        test_config.embedding_dim = 512  # 与 FakeEmbeddingEngine/索引维度一致
        server.vidx = VectorIndex(test_config)
        server.vidx.initialize(dim=512)
        server.engine = FakeEmbeddingEngine()
        # 服务与处理器共享同一 legacy 索引/引擎实例
        server.context_service._legacy_vector = server.vidx
        server.context_service.embedding_engine = server.engine
        try:
            first = server.handle_tool_call("memory_add", {
                "key": "p:t:decision:db", "value": "数据库选用 MySQL",
            })
            assert first["status"] == "added"
            second = server.handle_tool_call("memory_add", {
                "key": "p:t:arch:doc", "value": "数据库选用 MySQL",
                "tier": "reference",
            })
            assert second["status"] == "added"
            assert _facade(server).count_active() == 2
        finally:
            server.vidx.close()

    def test_memory_add_accepts_pattern_mid_sentence(self, server):
        """模式词出现在句中不算低信息——整句语义合格必须入库。"""
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:window", "value": "部署窗口需等待用户确认后再排期",
        })
        assert result["status"] == "added"

    def test_memory_add_rejects_low_info_prefix_variant(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:trivial2", "value": "等待用户下一步指令。",
        })
        assert "error" in result

    def test_memory_add_rejects_low_info_case_variant(self, server):
        """大小写变体同样拒收。"""
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:trivial3", "value": "No action required.",
        })
        assert "error" in result

    def test_memory_replace_rejects_low_info_value(self, server):
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:rl", "value": "供应商合同必须双人复核后归档",
        })
        assert seed["status"] == "added"
        result = server.handle_tool_call("memory_replace", {
            "key": "p:t:fact:rl", "value": "等待用户确认后再继续推进。",
        })
        assert "error" in result

    def test_memory_replace_rejects_overlong_value(self, server):
        seed = server.handle_tool_call("memory_add", {"key": "p:t:fact:r", "value": "待替换的初始值，内容足够长"})
        assert seed["status"] == "added"
        result = server.handle_tool_call("memory_replace", {
            "key": "p:t:fact:r", "value": "y" * 501,
        })
        assert "error" in result

    def test_memory_add_conflict_replace_keeps_legacy_shape(self, server):
        """冲突检测判定 replace：旧 new_id/old_id 字段不变，old_context_id 仅增量。"""
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:decision:api", "value": "采用旧接口实现方案。",
        })
        assert seed["status"] == "added"

        result = server.handle_tool_call("memory_add", {
            "key": "p:t:decision:api",
            "value": "采用统一接口，因为它能够长期减少重复实现。",
        })

        assert result["status"] == "replaced"
        assert result["new_id"] != seed["id"]
        assert result["old_id"] == seed["id"]
        # 可选增量字段
        assert result["context_id"] is not None
        assert result["old_context_id"] == seed["context_id"]
        assert result["available_layers"] == ["l0", "l1", "l2"]
        assert _facade(server).get_by_id(seed["id"])["status"] == "superseded"
        assert _facade(server).get_by_id(result["new_id"])["status"] == "active"
        context_store = server.context_service.store
        assert context_store.get_item(result["old_context_id"]).status is (
            ContextStatus.SUPERSEDED
        )
        assert context_store.get_item(result["context_id"]).status is (
            ContextStatus.ACTIVE
        )
        assert context_store.resolve_legacy_mapping(result["new_id"]) == (
            result["context_id"]
        )

    def test_memory_add_undecidable_conflict_reports_without_writing(self, server):
        """不可判定的同 key 冲突：旧 conflict 形状不变，不写任何一侧。"""
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:c", "value": "供应商合同必须双人复核后归档",
        })
        assert seed["status"] == "added"

        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:c", "value": "供应商合同改为三人复核后归档",
        })

        assert result["status"] == "conflict"
        assert result["existing_id"] == seed["id"]
        assert "reason" in result
        assert "context_id" not in result
        assert _facade(server).count_active() == 1
        assert server.context_service.store.count_by_status() == {"active": 1}

    def test_memory_add_skip_duplicate_keeps_legacy_shape(self, server):
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:s", "value": "供应商合同必须双人复核后归档",
        })
        assert seed["status"] == "added"

        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:s", "value": "供应商合同必须双人复核后归档",
        })

        assert result["status"] == "skipped"
        assert "reason" in result
        assert "id" not in result
        assert _facade(server).count_active() == 1

    def test_memory_replace_keeps_legacy_shape_and_writes_both_sides(self, server):
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:rl", "value": "供应商合同必须双人复核后归档",
        })
        assert seed["status"] == "added"

        result = server.handle_tool_call("memory_replace", {
            "key": "p:t:fact:rl", "value": "供应商合同必须双人复核并当场归档。",
        })

        # 旧形状只有 status/new_id；context_id/old_context_id 为可选增量
        assert result["status"] == "replaced"
        assert isinstance(result["new_id"], int)
        assert result["new_id"] != seed["id"]
        assert "old_id" not in result
        assert result["old_context_id"] == seed["context_id"]
        assert result["context_id"] is not None
        assert _facade(server).get_by_id(seed["id"])["status"] == "superseded"
        assert _facade(server).get_by_id(result["new_id"])["value"] == (
            "供应商合同必须双人复核并当场归档。"
        )
        context_store = server.context_service.store
        assert context_store.get_item(seed["context_id"]).status is (
            ContextStatus.SUPERSEDED
        )
        assert context_store.resolve_legacy_mapping(result["new_id"]) == (
            result["context_id"]
        )

    def test_memory_remove_marks_both_sides_deleted(self, server):
        seed = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:rm", "value": "供应商合同必须双人复核后归档",
        })
        assert seed["status"] == "added"

        result = server.handle_tool_call("memory_remove", {"id": seed["id"]})

        assert result["status"] == "deleted"
        assert result["id"] == seed["id"]
        assert result["context_id"] == seed["context_id"]
        assert _facade(server).get_by_id(seed["id"])["status"] == "deleted"
        context_store = server.context_service.store
        assert context_store.get_item(seed["context_id"]).status is (
            ContextStatus.DELETED
        )
        # 映射保留，供历史追溯
        assert context_store.resolve_legacy_mapping(seed["id"]) == (
            seed["context_id"]
        )

    def _seed_consolidate_pair(self, server, test_config):
        """经兼容边界写入一对近重复，并把向量直接加进共享 legacy 索引。"""
        server.vidx.initialize(dim=512)
        server.engine = FakeEmbeddingEngine()
        server.context_service._legacy_vector = server.vidx
        server.context_service.embedding_engine = server.engine
        facade = server.context_service.legacy_facade()
        first_id = facade.add(
            key="p:t:fact:duplicate", value="完全相同的内容", importance=3.0
        )
        second_id = facade.add(
            key="project:p:t:fact:duplicate", value="完全相同的内容", importance=9.0
        )
        vector = np.array(
            server.engine.encode_document("完全相同的内容"), dtype=np.float32
        )
        server.vidx.add(first_id, vector)
        server.vidx.add(second_id, vector)
        server.consolidator = Consolidator(
            test_config, facade, server.vidx, server.engine
        )
        return first_id, second_id

    def test_memory_consolidate_dry_run_remains_pure_diagnostic(
            self, server, test_config):
        first_id, second_id = self._seed_consolidate_pair(server, test_config)
        context_store = server.context_service.store
        first_context_id = context_store.resolve_legacy_mapping(first_id)
        before_items = context_store.count_by_status()

        result = server.handle_tool_call("memory_consolidate", {"dry_run": True})

        assert result["dry_run"] is True
        assert result["merged"] == 0
        assert len(result["pairs"]) == 1
        pair = result["pairs"][0]
        assert set(pair["keep"]) == {"id", "key", "preview", "importance"}
        assert pair["keep"]["id"] == second_id  # 高分者保留
        assert pair["drop"]["id"] == first_id
        # 纯诊断：两侧状态与访问计数一律不变
        assert _facade(server).get_by_id(first_id)["status"] == "active"
        assert _facade(server).get_by_id(second_id)["status"] == "active"
        assert _facade(server).get_by_id(second_id)["access_count"] == 0
        assert context_store.count_by_status() == before_items
        assert context_store.get_item(first_context_id).access_count == 0

    def test_memory_consolidate_apply_changes_both_sides_per_pair(
            self, server, test_config):
        first_id, second_id = self._seed_consolidate_pair(server, test_config)
        context_store = server.context_service.store
        first_context_id = context_store.resolve_legacy_mapping(first_id)
        second_context_id = context_store.resolve_legacy_mapping(second_id)

        result = server.handle_tool_call("memory_consolidate", {"dry_run": False})

        assert result["dry_run"] is False
        assert result["merged"] == 1
        # keep：access +1 双侧；drop：archived 双侧
        assert _facade(server).get_by_id(second_id)["access_count"] == 1
        assert context_store.get_item(second_context_id).access_count == 1
        assert _facade(server).get_by_id(first_id)["status"] == "archived"
        assert context_store.get_item(first_context_id).status is (
            ContextStatus.ARCHIVED
        )
        assert _facade(server).count_active() == 1

    def test_degraded_primary_rejects_all_mutation_tools(
            self, degraded_primary_server, test_config):
        server = degraded_primary_server
        with MemoryStore(test_config) as legacy_store:
            # 未映射的旧行：投影在、Core 侧无（降级态不补迁移）
            legacy_id = legacy_store.add(
                key="p:t:fact:seed", value="既有的长期事实记录。"
            )

        add = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:x", "value": "供应商合同必须双人复核后归档",
        })
        replace = server.handle_tool_call("memory_replace", {
            "key": "p:t:fact:seed", "value": "尝试改写既有的长期事实记录。",
        })
        remove = server.handle_tool_call("memory_remove", {"id": legacy_id})

        for outcome in (add, replace, remove):
            assert "error" in outcome
            assert "degraded" in outcome["error"]
        # 全部拒绝，两侧均无变化
        assert _facade(server).get_by_id(legacy_id)["status"] == "active"
        assert _facade(server).count_active() == 1
        assert server.context_service.store.count_by_status() == {}

    def test_degraded_primary_keeps_dry_run_consolidate_diagnostic(
            self, degraded_primary_server, test_config):
        server = degraded_primary_server
        server.engine = FakeEmbeddingEngine()
        server.consolidator = Consolidator(
            test_config,
            server.context_service.legacy_facade(),
            server.vidx,
            server.engine,
        )
        with MemoryStore(test_config) as legacy_store:
            # 未映射的旧行：投影在、Core 侧无（降级态不补迁移）
            legacy_id = legacy_store.add(
                key="p:t:fact:seed", value="既有的长期事实记录。"
            )

        dry = server.handle_tool_call("memory_consolidate", {"dry_run": True})
        assert dry["dry_run"] is True
        assert dry["merged"] == 0

        applied = server.handle_tool_call(
            "memory_consolidate", {"dry_run": False}
        )
        assert "error" in applied
        assert "degraded" in applied["error"]
        assert _facade(server).get_by_id(legacy_id)["status"] == "active"

    def test_memory_add_vector_failure_returns_committed_id_and_degraded_state(
            self, server, test_config):
        test_config.embedding_dim = 2

        class FailingIndex:
            """add 即失败的 legacy 索引 fake；dirty 标记如实存活。"""

            def __init__(self):
                self.dirty = False

            def is_dirty(self):
                return self.dirty

            def mark_dirty(self):
                self.dirty = True

            def preserve_dirty(self):
                pass

            def clear_dirty(self):
                self.dirty = False

            def count(self):
                return 0

            def initialize(self, dim=2):
                pass

            def add(self, mem_id, embedding):
                raise RuntimeError("synthetic vector failure")

            def remove(self, mem_id):
                return False

            def save(self):
                raise AssertionError("save must not run after add failure")

            def close(self):
                pass

        class LoadedEngine:
            is_loaded = True

            def encode_document(self, value):
                return [0.0, 1.0]

        service = server.context_service
        service._legacy_vector = FailingIndex()
        service.embedding_engine = LoadedEngine()

        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:v", "value": "供应商合同必须双人复核后归档",
        })

        # 返回已提交 legacy ID + 非敏感降级状态，而不是假回滚
        assert result["status"] == "added"
        assert isinstance(result["id"], int)
        assert result["index_state"] == "degraded"
        assert result["context_id"] is not None
        assert _facade(server).get_by_id(result["id"])["status"] == "active"
        context_store = server.context_service.store
        assert context_store.resolve_legacy_mapping(result["id"]) == (
            result["context_id"]
        )
        assert context_store.get_item(result["context_id"]).status is (
            ContextStatus.ACTIVE
        )
        assert service.status().legacy_vector_dirty is True

    def test_auto_extractor_realistic_conversation(self):
        """从真实对话中提取记忆。"""
        extractor = AutoExtractor()
        conversation = [
            {"role": "user", "content": "我们用 PostgreSQL 代替 MySQL 吧，性能更好"},
            {"role": "assistant", "content": "好的，我来调整配置"},
        ]
        prompt = extractor.build_extraction_prompt(conversation)
        assert "PostgreSQL" in prompt
        assert "MySQL" in prompt

        # 模拟 Claude 返回的 JSON
        fake_response = """```json
[
  {
    "key": "project:db:decision:engine",
    "value": "数据库选用 PostgreSQL，替代 MySQL",
    "attribute": "decision",
    "tags": ["数据库", "PostgreSQL", "架构"],
    "confidence": 0.95
  }
]
```"""
        candidates = extractor.parse_response(fake_response)
        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"
        assert extractor.should_persist(candidates[0]) is True

    def test_forgetting_does_not_archive_recently_used(self, test_config):
        """遗忘引擎不归档最近使用的记忆。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:f:1", value="活跃记忆")
        store.update_access(mem_id)

        engine = ForgettingEngine(test_config, store)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2

        candidates = engine.find_candidates()
        # access_count=1 但刚被访问，last_accessed 很新
        # 由于 forget_rate_limit_days 默认为 7，刚更新的记忆不会成为候选
        assert len(candidates) == 0

        store.close()

    def test_mcp_tool_schemas_match_design(self, test_config):
        """legacy 默认仅注册 6 个旧工具；codex/kimi+shadow 追加八个 context
        工具与五个续接工具（续接不过 Core serving gate）。"""
        from evolvmem.mcp_server import MemoryMCPServer
        server = MemoryMCPServer(config=test_config)
        # 伪造 initialize request 后直接查询工具列表
        response = server._handle_request({
            "method": "tools/list", "id": 1, "jsonrpc": "2.0",
        })
        tools = response["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        expected = {
            "memory_search", "memory_status", "memory_add",
            "memory_replace", "memory_remove", "memory_consolidate",
        }
        assert tool_names == expected

        # Codex/Kimi shadow：同一注册表按 adapter/mode/health 暴露 context
        # 与 continuity 工具
        test_config.context_mode = "shadow"
        for adapter in ("codex", "kimi"):
            test_config.adapter = adapter
            adapter_server = MemoryMCPServer(config=test_config)
            service = ContextService(
                test_config, embedding_engine=adapter_server.engine
            )
            service.initialize(mode=ContextMode.SHADOW, adapter=adapter)
            adapter_server.context_service = service
            try:
                response = adapter_server._handle_request({
                    "method": "tools/list", "id": 2, "jsonrpc": "2.0",
                })
                adapter_names = {t["name"] for t in response["result"]["tools"]}
                assert adapter_names == expected | {
                    "context_session_start", "context_search",
                    "context_read", "context_status",
                    "context_confirm", "context_record_outcome",
                    "context_archive_project", "context_sweep",
                    "experience_recall", "experience_record",
                    "continuity_resume", "continuity_checkpoint",
                    "continuity_list", "continuity_begin", "continuity_find",
                }
            finally:
                service.close()

    def test_memory_consolidate_requires_embedding(self, server):
        result = server.handle_tool_call("memory_consolidate", {})
        assert "error" in result

    def test_mcp_notifications_get_no_response(self):
        """JSON-RPC notification（无 id）不应产生响应。"""
        from evolvmem.mcp_server import MemoryMCPServer
        server = MemoryMCPServer()
        assert server._handle_request({
            "method": "notifications/initialized", "jsonrpc": "2.0",
        }) is None

    def test_mcp_ping(self):
        from evolvmem.mcp_server import MemoryMCPServer
        server = MemoryMCPServer()
        resp = server._handle_request({
            "method": "ping", "id": 1, "jsonrpc": "2.0",
        })
        assert resp["id"] == 1
        assert resp["result"] == {}

    def test_mcp_initialize_echoes_protocol_version(self):
        from evolvmem.mcp_server import MemoryMCPServer
        server = MemoryMCPServer()
        resp = server._handle_request({
            "method": "initialize", "id": 1, "jsonrpc": "2.0",
            "params": {"protocolVersion": "2025-03-26"},
        })
        assert resp["result"]["protocolVersion"] == "2025-03-26"

    def test_unknown_mcp_tool_does_not_wait_for_initialization(self, monkeypatch):
        """未知工具无需任何组件，必须立即返回而不是等待初始化门闩。"""
        from evolvmem.mcp_server import MemoryMCPServer
        server = MemoryMCPServer()
        monkeypatch.setattr(
            server,
            "_init_gate_error",
            lambda: (_ for _ in ()).throw(
                AssertionError("unknown tool unexpectedly waited for initialization")
            ),
        )
        resp = server._handle_request({
            "method": "tools/call", "id": 2, "jsonrpc": "2.0",
            "params": {"name": "no_such_tool", "arguments": {}},
        })
        assert resp["result"]["isError"] is True

    def test_usearch_rebuild_from_sqlite(self, test_config):
        """向量索引崩溃后从 SQLite 重建。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(
            key="p:rebuild:test", value="重建测试记忆", tags=["测试"]
        )
        store.close()

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)
        engine = FakeEmbeddingEngine()

        # 模拟重建
        store2 = MemoryStore(test_config)
        store2.initialize()
        all_ids = store2.all_ids()
        records = store2.get_by_ids(all_ids)

        ids = []
        embeddings = []
        for r in records:
            vec = np.array(engine.encode(r["value"]), dtype=np.float32)
            ids.append(r["id"])
            embeddings.append(vec)

        vidx.rebuild(ids, embeddings)
        assert vidx.count() == 1
        assert vidx.check_consistency(len(all_ids))

        store2.close()
        vidx.close()


class TestMigrateClaudeMem:
    """claude-mem 迁移工具：按配置 mode 经 ContextService 写入。"""

    @staticmethod
    def _summaries() -> list[dict]:
        return [
            {"chroma_id": 1, "created_at_epoch": 1700000000,
             "project": "eva",
             "doc": "EVA 部署了 TEI 嵌入服务并验证了中文检索效果"},
            {"chroma_id": 2, "created_at_epoch": 1700000100,
             "project": "hermes",
             "doc": "hermes 插件改用 SQLite 存储记忆元数据与 FTS 索引"},
        ]

    def test_legacy_mode_import_preserves_old_job(self, test_config):
        """切换前 legacy：只写 memories 行，返回 legacy ID，无映射无 Core。"""
        test_config.context_mode = "legacy"
        from migrate_claude_mem import import_summaries

        result = import_summaries(test_config, self._summaries())

        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert len(result["new_ids"]) == 2  # 旧向量处理仍拿 legacy ID
        with MemoryStore(test_config) as store:
            row = store.get_by_key("claude-mem:summary:1")[0]
            assert row["status"] == "active"
            assert row["attribute"] == "claude-mem-migration"
            assert "migrated" in row["tags"]
            assert "project:eva" in row["tags"]
        conn = sqlite3.connect(test_config.db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM context_items").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM legacy_memory_migrations"
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_compat_mode_import_creates_mappings_and_layers(self, test_config):
        """切换后 compat：每行同事务创建映射与三层，而不是未映射行。"""
        test_config.context_mode = "compat"
        from migrate_claude_mem import import_summaries

        result = import_summaries(test_config, self._summaries())

        assert result["imported"] == 2
        ctx_store = ContextStore(test_config)
        ctx_store.initialize()
        try:
            for legacy_id in result["new_ids"]:
                context_id = ctx_store.resolve_legacy_mapping(legacy_id)
                assert context_id is not None
                item = ctx_store.get_item(context_id)
                assert item.status is ContextStatus.ACTIVE
                for layer in (ContextLayer.L0, ContextLayer.L1,
                              ContextLayer.L2):
                    assert ctx_store.get_layer(context_id, layer)
        finally:
            ctx_store.close()
        # 投影行仍是旧形状
        with MemoryStore(test_config) as store:
            row = store.get_by_key("claude-mem:summary:2")[0]
            assert row["attribute"] == "claude-mem-migration"

    def test_import_skips_existing_keys(self, test_config):
        """重复迁移幂等：已存在的 key 跳过，不产生重复行。"""
        test_config.context_mode = "compat"
        from migrate_claude_mem import import_summaries

        first = import_summaries(test_config, self._summaries())
        second = import_summaries(test_config, self._summaries())

        assert first["imported"] == 2
        assert second["imported"] == 0
        assert second["skipped"] == 2
        with MemoryStore(test_config) as store:
            assert len(store.get_by_key("claude-mem:summary:1")) == 1


class TestContextLifecycleIntegration:
    """P4a 端到端：candidate 隔离 → outcome/confirm → archive purge（MCP 面）。"""

    @staticmethod
    def _shadow_server(test_config):
        from evolvmem.mcp_server import MemoryMCPServer
        with MemoryStore(test_config):
            pass  # legacy 投影 schema 引导；只有测试允许持有裸 store
        test_config.context_mode = "shadow"
        test_config.adapter = "codex"
        srv = MemoryMCPServer(config=test_config)
        srv._init_done.set()  # 注入服务的服务器不经过初始化门闩
        service = ContextService(test_config, embedding_engine=srv.engine)
        service._legacy_vector = srv.vidx
        service.initialize(mode=ContextMode.SHADOW, adapter="codex")
        srv.context_service = service
        return srv

    @staticmethod
    def _call(srv, name, arguments, req_id=7):
        resp = srv._handle_request({
            "method": "tools/call", "id": req_id, "jsonrpc": "2.0",
            "params": {"name": name, "arguments": arguments},
        })
        result = resp["result"]
        return result, json.loads(result["content"][0]["text"])

    def test_candidate_lifecycle_and_archive_purge_end_to_end(
            self, test_config):
        srv = self._shadow_server(test_config)
        service = srv.context_service
        try:
            candidate = service.store.create_item(
                ContextItemDraft(
                    identity_key="experience:proj:stdio-hang",
                    content_type=ContextContentType.EXPERIENCE,
                    layers=ContextLayers(
                        l0="MCP stdio 握手卡住时先检查 stdin 预读竞争。",
                        l1="症状是 initialize 无响应；改为单一读取路径。",
                        l2="完整排查与修复证据。",
                        generator="test-suite",
                    ),
                    project="proj",
                    scope=ContextScope.PROJECT,
                    status=ContextStatus.CANDIDATE,
                    tier=ContextTier.NORMAL,
                    confidence=0.8,
                )
            )

            # 注册表暴露四个新写工具
            listed = {
                tool["name"]
                for tool in srv._handle_request({
                    "method": "tools/list", "id": 3, "jsonrpc": "2.0",
                })["result"]["tools"]
            }
            assert {
                "context_confirm", "context_record_outcome",
                "context_archive_project", "context_sweep",
                    "experience_recall", "experience_record",
            } <= listed

            # candidate 隔离：精确读取被策略拒绝，绝不进入服务面
            result, payload = self._call(
                srv, "context_read", {"id": candidate.id}
            )
            assert result["isError"] is True
            assert payload["error"] == "not_readable"

            # outcome → confirm：candidate 经显式确认进入 active
            _, recorded = self._call(srv, "context_record_outcome", {
                "id": candidate.id, "outcome": "success",
                "note": "改为单一读取路径后握手回归通过",
            })
            assert recorded["outcome"] == "success"
            _, confirmed = self._call(
                srv, "context_confirm", {"id": candidate.id}
            )
            assert confirmed["status"] == "active"

            # 晋升后同一精确 ID 可读 L1
            _, read = self._call(srv, "context_read", {"id": candidate.id})
            assert read["layer"] == "l1"
            assert "单一读取路径" in read["content"]

            # 到期 archive 经 context_sweep 真实删除且幂等
            expired = SessionArchiver(
                test_config, service.store
            ).archive_session(
                "proj", "codex", "sess-old", "过期的原始会话正文",
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            expired_file = test_config.data_dir / expired.payload_path
            _, swept = self._call(srv, "context_sweep", {})
            assert swept["purged"] == 1
            assert not expired_file.exists()
            assert self._call(srv, "context_sweep", {})[1]["purged"] == 0

            # 项目 purge：路径形入参归一化；active item 不受影响
            fresh = SessionArchiver(
                test_config, service.store
            ).archive_session("proj", "codex", "sess-new", "未过期的原始会话正文")
            _, purged = self._call(
                srv, "context_archive_project", {"project": "/worktrees/proj"}
            )
            assert purged["purged"] == 1
            assert purged["purged_archive_ids"] == [fresh.id]
            reloaded = service.store.get_item(
                candidate.id, include_layers=False
            )
            assert reloaded.status is ContextStatus.ACTIVE
        finally:
            service.close()


class TestKimiSessionArchiveIntegration:
    """P4b 端到端：kimi session_end 归档 → 候选隔离 → 确认晋升。"""

    def test_kimi_session_end_archives_isolates_then_confirms(
            self, test_config, monkeypatch, tmp_path):
        from evolvmem.auto_extractor import CandidateMemory
        from evolvmem.context_models import ContextReadRequest

        test_config.context_mode = "shadow"
        wire = tmp_path / "wire.jsonl"
        text = "排查并修复了 MCP stdio 握手卡死问题。" + "甲" * 250
        events = [
            {"type": "turn.prompt",
             "input": [{"type": "text", "text": text}]},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part",
                       "part": {"type": "text", "text": "已修复并回归验证"}}},
        ]
        wire.write_text(
            "\n".join(
                json.dumps(event, ensure_ascii=False) for event in events
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(hooks, "_find_wire", lambda _sid: str(wire))
        monkeypatch.setattr(
            Config,
            "from_file",
            classmethod(lambda cls, path=None: test_config),
        )
        with MemoryStore(test_config):
            pass  # shadow 模式同样以既有 legacy 库为前提
        monkeypatch.setattr(
            hooks,
            "_load_llm_config",
            lambda: hooks.LLMConfig(
                provider="deepseek",
                api_key="test-key",
                base_url="https://api.deepseek.com/chat/completions",
                model="deepseek-v4-flash",
            ),
        )
        # P5 起合约为真实放行：experience attribute 无需再打补丁
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda *_: [
                CandidateMemory(
                    key="project:proj:experience:stdio-hang",
                    value="MCP 握手卡住时先检查 stdin 预读竞争，改为单一读取路径。",
                    attribute="experience",
                    confidence=0.8,
                    importance=7.0,
                ),
                CandidateMemory(
                    key="SESSION_SUMMARY",
                    value="本次会话修复了 MCP 握手卡死并完成回归验证。",
                    confidence=0.9,
                    tags=["日志"],
                ),
            ],
        )

        result = hooks.session_end({"session_id": "session_e2e_archive"})

        assert result.status == "completed"
        assert result.persisted == 2  # summary + 隔离的 experience

        service = ContextService(test_config)
        service.initialize(mode=ContextMode.SHADOW, adapter="kimi")
        try:
            # 候选隔离：只在显式审阅 API 中可见
            candidates = service.list_candidates()
            assert [entry.identity_key for entry in candidates] == [
                "project:proj:experience:stdio-hang"
            ]
            candidate = candidates[0]
            # 来源链接：candidate 可追溯到加密归档，payload 可解密
            sources = service.store.list_item_sources(candidate.id)
            assert len(sources) == 1
            assert sources[0]["source_kind"] == "session"
            assert sources[0]["extraction_version"] == "kimi-extraction-v1"
            archive = service.store.get_session_archive(
                sources[0]["archive_id"]
            )
            assert archive["adapter"] == "kimi"
            assert archive["external_session_id"] == "session_e2e_archive"
            payload = SessionArchiver(
                test_config, service.store
            ).read_payload(archive["id"])
            assert "握手卡死" in payload  # 原始消息原文（本地证据）
            # candidate 不可精确读取；确认晋升后 L1 可读
            denied = service.read(
                ContextReadRequest(id=candidate.id, layer=ContextLayer.L1)
            )
            assert denied.error_code == "not_readable"
            service.confirm(candidate.id)
            read = service.read(
                ContextReadRequest(id=candidate.id, layer=ContextLayer.L1)
            )
            assert read.error_code is None
            assert "预读竞争" in read.content
        finally:
            service.close()
