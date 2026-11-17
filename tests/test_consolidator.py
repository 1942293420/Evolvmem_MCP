"""consolidator tests."""
import hashlib

import numpy as np
import pytest
from evolvmem.consolidator import Consolidator
from evolvmem.context_models import ContextMode, ContextStatus
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
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
        _add_with_vector(store, vidx, engine, "project:demo:db:choice", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "demo:db:choice", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "c:z", "完全不同的东西")
        pairs = c.find_candidates()
        assert len(pairs) == 1
        assert pairs[0]["similarity"] > 0.99

    def test_higher_score_is_keep(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "project:demo:db:choice", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "demo:db:choice", "相同内容", importance=9.0)
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

    def test_reference_tier_never_merged(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "project:demo:db:choice", "相同内容",
                         tier="reference")
        _add_with_vector(store, vidx, engine, "demo:db:choice", "相同内容",
                         tier="reference")
        _add_with_vector(store, vidx, engine, "c:z", "相同内容")
        # 两条 reference 不参与合并；normal 的 c:z 也没有可配对对象
        assert c.find_candidates() == []
        assert store.count_active() == 3


class TestConsolidate:
    def test_dry_run_changes_nothing(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "project:demo:db:choice", "相同内容")
        _add_with_vector(store, vidx, engine, "demo:db:choice", "相同内容")
        result = c.consolidate(dry_run=True)
        assert len(result["pairs"]) == 1
        assert store.count_active() == 2

    def test_apply_archives_drop(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "project:demo:db:choice", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "demo:db:choice", "相同内容", importance=9.0)
        result = c.consolidate(dry_run=False)
        assert result["merged"] == 1
        assert store.count_active() == 1
        active = store.get_active()[0]
        assert active["importance"] == 9.0


def _make_compat_facade(config, *, store=None):
    """compat 模式的 ContextService + 兼容门面（legacy 投影 schema 先就位）。"""
    with MemoryStore(config):
        pass
    service = (
        ContextService(config, store=store)
        if store is not None
        else ContextService(config)
    )
    service.initialize(mode=ContextMode.COMPAT, adapter="test")
    return service, service.legacy_facade()


class TestConsolidatorThroughFacade:
    """compat 模式下，合并的 access/archive 经门面落到映射双侧。"""

    def _env(self, test_config, *, store=None):
        test_config.embedding_dim = 8  # 与 FakeEngine 对齐
        service, facade = _make_compat_facade(test_config, store=store)
        vidx = VectorIndex(test_config)
        vidx.initialize(dim=8)
        return service, facade, vidx, FakeEngine()

    def test_consolidate_mirrors_access_and_archive_on_both_sides(
            self, test_config):
        service, facade, vidx, engine = self._env(test_config)
        first = _add_with_vector(facade, vidx, engine, "project:demo:db:choice", "相同内容",
                                 importance=3.0)
        second = _add_with_vector(facade, vidx, engine, "demo:db:choice", "相同内容",
                                  importance=9.0)
        first_ctx = service.store.resolve_legacy_mapping(first)
        second_ctx = service.store.resolve_legacy_mapping(second)
        assert first_ctx is not None and second_ctx is not None

        result = Consolidator(test_config, facade, vidx, engine).consolidate(
            dry_run=False)

        assert result["merged"] == 1
        assert result["pairs"][0]["keep"]["id"] == second
        # 赢家 access +1 双侧
        assert facade.get_by_id(second)["access_count"] == 1
        assert service.store.get_item(second_ctx).access_count == 1
        # 输家双侧归档
        assert facade.get_by_id(first)["status"] == "archived"
        assert service.store.get_item(first_ctx).status is (
            ContextStatus.ARCHIVED)
        vidx.close()
        service.close()

    def test_dry_run_through_facade_changes_nothing(self, test_config):
        service, facade, vidx, engine = self._env(test_config)
        first = _add_with_vector(facade, vidx, engine, "project:demo:db:choice", "相同内容")
        second = _add_with_vector(facade, vidx, engine, "demo:db:choice", "相同内容")

        result = Consolidator(test_config, facade, vidx, engine).consolidate(
            dry_run=True)

        assert result["dry_run"] is True and result["merged"] == 0
        assert len(result["pairs"]) == 1
        for legacy_id in (first, second):
            assert facade.get_by_id(legacy_id)["status"] == "active"
            assert facade.get_by_id(legacy_id)["access_count"] == 0
            ctx = service.store.resolve_legacy_mapping(legacy_id)
            assert service.store.get_item(ctx).status is ContextStatus.ACTIVE
            assert service.store.get_item(ctx).access_count == 0
        vidx.close()
        service.close()

    def test_pair_failure_rolls_back_both_sides(self, test_config):
        """输家归档在 Core 侧失败时整对回滚：投影与 ContextItem 都保持 active。"""

        class FailingStore(ContextStore):
            def set_item_status(self, item_id, status):
                raise RuntimeError("injected core failure")

        service, facade, vidx, engine = self._env(
            test_config, store=FailingStore(test_config))
        first = _add_with_vector(facade, vidx, engine, "project:demo:db:choice", "相同内容",
                                 importance=3.0)
        _add_with_vector(facade, vidx, engine, "demo:db:choice", "相同内容",
                         importance=9.0)
        first_ctx = service.store.resolve_legacy_mapping(first)

        with pytest.raises(RuntimeError, match="injected core failure"):
            Consolidator(test_config, facade, vidx, engine).consolidate(
                dry_run=False)

        assert facade.get_by_id(first)["status"] == "active"
        assert service.store.get_item(first_ctx).status is ContextStatus.ACTIVE
        vidx.close()
        service.close()
