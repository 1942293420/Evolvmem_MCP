"""Tests for the web memory management console API.

Runs against a temporary data_dir; the real production database is never
touched. Covers both the plain API functions and the HTTP layer end-to-end.
Post-cutover the API owns a ContextService + compatibility facade: every
lifecycle mutation mirrors onto the mapped Context side in one transaction.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from evolvmem.context_models import (
    ContextMode,
    ContextServiceError,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.memory_store import MemoryStore
from evolvmem.web_server import (
    _bounded_error,
    api_archive,
    api_delete,
    api_hard_delete,
    api_memories,
    api_restore,
    api_stats,
    api_update,
    make_handler,
)


def _make_service(config, *, mode=ContextMode.COMPAT, store=None):
    """Mode-selected ContextService over the temp DB.

    The legacy projection schema is created first, mirroring a pre-cutover
    production database (ContextStore only owns Context tables).
    """
    with MemoryStore(config):
        pass
    service = (
        ContextService(config, store=store)
        if store is not None
        else ContextService(config)
    )
    service.initialize(mode=mode, adapter="test-web")
    return service


def _seed(facade):
    """The historical seed: hot decision / warm preference / idle archived."""
    a = facade.add("proj:decision:db", "使用 SQLite 作为存储",
                   attribute="decision", tags=["db"], importance=7.0,
                   tier="pinned")
    b = facade.add("user:pref:color", "喜欢柔和紫色界面",
                   attribute="preference", tags=["ui"], importance=6.0,
                   tier="normal")
    c = facade.add("proj:fact:idle", "从未被调用过的记忆", attribute="fact")
    for _ in range(5):
        facade.update_access(a)
    facade.update_access(b)
    facade.archive(c)  # c 处于 archived 状态
    return {"hot": a, "warm": b, "cold": c}


@pytest.fixture
def backend(test_config):
    """Compat-mode facade + residual legacy store + service, seeded."""
    service = _make_service(test_config)
    facade = service.legacy_facade()
    legacy = MemoryStore(test_config)  # attribute/tags 残留写入与校验读
    legacy.initialize()
    ids = _seed(facade)
    yield facade, legacy, service, ids
    service.close()
    legacy.close()


# ---- api_stats ----

def test_stats_counts(backend):
    facade, _, _, ids = backend
    st = api_stats(facade)
    assert st["total_active"] == 2  # c archived 不计入
    assert st["by_tier"]["pinned"] == 1
    assert st["by_tier"]["normal"] == 1
    assert st["by_tier"]["reference"] == 0
    assert st["by_attribute"]["decision"] == 1
    assert st["by_attribute"]["preference"] == 1
    assert st["never_accessed"] == 0  # 两个 active 都被访问过
    assert st["top_accessed"][0] == {
        "id": ids["hot"], "key": "proj:decision:db", "access_count": 5,
        "importance": 7.0,
    }
    assert len(st["top_accessed"]) <= 10


def test_stats_never_accessed(backend):
    facade, _, _, _ = backend
    facade.add("proj:fact:never", "零访问记忆", attribute="fact")
    st = api_stats(facade)
    assert st["never_accessed"] == 1
    assert st["total_active"] == 3


def test_top_accessed_ranked_by_composite_heat(test_config):
    service = _make_service(test_config)
    facade = service.legacy_facade()
    # 高频低分：4 次命中但 importance 1 → 综合 1*(4+1)=5
    junk = facade.add(key="p:t:junk", value="空摘要", importance=1.0)
    for _ in range(4):
        facade.update_access(junk)
    # 低频高分：1 次命中 importance 9 → 综合 9*(1+1)=18
    gem = facade.add(key="p:t:gem", value="核心规则", importance=9.0)
    facade.update_access(gem)
    top = api_stats(facade)["top_accessed"]
    assert top[0]["id"] == gem  # 高分低频应排在高频低分之前
    service.close()


# ---- api_memories ----

def test_memories_default_sort_by_access_desc(backend):
    facade, _, _, ids = backend
    rows = api_memories(facade, {})
    assert [r["id"] for r in rows] == [ids["hot"], ids["warm"]]
    assert rows[0]["access_count"] == 5
    # 字段完整
    for field in ("id", "key", "value", "attribute", "tags", "tier",
                  "importance", "access_count", "last_accessed",
                  "created_at", "updated_at", "expires_at"):
        assert field in rows[0]


def test_memories_sort_and_order(backend):
    facade, _, _, ids = backend
    rows = api_memories(facade, {"sort": "access_count", "order": "asc"})
    assert [r["access_count"] for r in rows] == [1, 5]
    rows = api_memories(facade, {"sort": "importance", "order": "desc"})
    assert rows[0]["importance"] == 7.0
    rows = api_memories(facade, {"sort": "created_at", "order": "desc"})
    assert len(rows) == 2
    # 非法 sort 列回退到 access_count，不报错
    rows = api_memories(facade, {"sort": "access_count; DROP TABLE memories"})
    assert rows[0]["access_count"] == 5


def test_memories_filters(backend):
    facade, _, _, ids = backend
    # status
    rows = api_memories(facade, {"status": "archived"})
    assert [r["id"] for r in rows] == [ids["cold"]]
    # tier
    rows = api_memories(facade, {"tier": "pinned"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # attribute
    rows = api_memories(facade, {"attribute": "preference"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 value
    rows = api_memories(facade, {"q": "紫色"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 key
    rows = api_memories(facade, {"q": "decision"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # LIKE 通配符按字面处理
    assert api_memories(facade, {"q": "%"}) == []


# ---- update / archive / restore / delete ----

def test_update_metadata_fields(backend):
    facade, legacy, _, ids = backend
    res = api_update(facade, legacy, ids["warm"], {
        "importance": 9.0, "tier": "pinned",
        "attribute": "user_profile", "tags": ["ui", "color"],
    })
    assert res["ok"]
    m = legacy.get_by_id(ids["warm"])
    assert m["importance"] == 9.0
    assert m["tier"] == "pinned"
    assert m["attribute"] == "user_profile"
    assert m["tags"] == "ui,color"


def test_update_mirrors_importance_tier_to_context(backend):
    """importance/tier 经门面在同事务同步到映射的 ContextItem。"""
    facade, legacy, service, ids = backend
    res = api_update(facade, legacy, ids["warm"],
                     {"importance": 9.0, "tier": "pinned"})
    assert res["ok"]
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert context_id is not None
    item = service.store.get_item(context_id)
    assert item.importance == 9.0
    assert item.tier is ContextTier.PINNED
    # 投影行同步
    m = facade.get_by_id(ids["warm"])
    assert m["importance"] == 9.0 and m["tier"] == "pinned"


def test_update_attribute_tags_projection_consistent(backend):
    """attribute/tags 就地更新投影行且 id 不变；Core 映射与条目保持一致。"""
    facade, legacy, service, ids = backend
    res = api_update(facade, legacy, ids["warm"],
                     {"attribute": "user_profile", "tags": ["ui", "color"]})
    assert res["ok"]
    m = res["memory"]
    assert m["id"] == ids["warm"]
    assert m["attribute"] == "user_profile"
    assert m["tags"] == "ui,color"
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert context_id is not None
    item = service.store.get_item(context_id)
    assert item is not None
    assert item.status is ContextStatus.ACTIVE


def test_update_validation(backend):
    facade, legacy, _, ids = backend
    assert not api_update(facade, legacy, ids["warm"],
                          {"importance": 99})["ok"]
    assert not api_update(facade, legacy, ids["warm"], {"tier": "bogus"})["ok"]
    assert not api_update(facade, legacy, 9999, {"importance": 5})["ok"]
    # 校验失败后数据未变
    m = legacy.get_by_id(ids["warm"])
    assert m["importance"] == 6.0 and m["tier"] == "normal"


def test_archive_restore_mirror_context_status(backend):
    facade, _, service, ids = backend
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert context_id is not None
    assert api_archive(facade, ids["warm"])["ok"]
    assert facade.get_by_id(ids["warm"])["status"] == "archived"
    assert service.store.get_item(context_id).status is ContextStatus.ARCHIVED
    assert api_restore(facade, ids["warm"])["ok"]
    assert facade.get_by_id(ids["warm"])["status"] == "active"
    assert service.store.get_item(context_id).status is ContextStatus.ACTIVE


def test_archive_restore_delete_flow(backend):
    facade, _, _, ids = backend
    # restore: archived -> active
    assert api_restore(facade, ids["cold"])["ok"]
    assert facade.get_by_id(ids["cold"])["status"] == "active"
    # archive: active -> archived
    assert api_archive(facade, ids["warm"])["ok"]
    assert facade.get_by_id(ids["warm"])["status"] == "archived"
    # delete: 软删
    assert api_delete(facade, ids["hot"])["ok"]
    assert facade.get_by_id(ids["hot"])["status"] == "deleted"
    # 软删后不出现在默认列表
    assert all(r["id"] != ids["hot"] for r in api_memories(facade, {}))
    # 不存在的 id
    assert not api_archive(facade, 9999)["ok"]
    assert not api_restore(facade, 9999)["ok"]
    assert not api_delete(facade, 9999)["ok"]


def test_soft_delete_mirrors_context(backend):
    facade, _, service, ids = backend
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert api_delete(facade, ids["warm"])["ok"]
    assert facade.get_by_id(ids["warm"])["status"] == "deleted"
    assert service.store.get_item(context_id).status is ContextStatus.DELETED


# ---- api_hard_delete ----

def test_hard_delete_removes_row_permanently(test_config):
    service = _make_service(test_config)
    facade = service.legacy_facade()
    mid = facade.add(key="p:t:temp", value="临时记忆")
    result = api_hard_delete(facade, mid)
    assert result["ok"] is True
    assert facade.get_by_id(mid) is None  # 物理消失，不是 status 标记
    service.close()


def test_hard_delete_removes_exact_triple_atomically(backend):
    """映射/投影/ContextItem 三元组在同一事务删除；邻居不受影响。"""
    facade, _, service, ids = backend
    context_id = service.store.resolve_legacy_mapping(ids["cold"])
    assert context_id is not None

    assert api_hard_delete(facade, ids["cold"])["ok"]

    assert facade.get_by_id(ids["cold"]) is None
    assert service.store.resolve_legacy_mapping(ids["cold"]) is None
    assert service.store.get_item(context_id) is None
    hot_ctx = service.store.resolve_legacy_mapping(ids["hot"])
    assert hot_ctx is not None
    assert service.store.get_item(hot_ctx).status is ContextStatus.ACTIVE


def test_hard_delete_not_found(test_config):
    service = _make_service(test_config)
    facade = service.legacy_facade()
    assert api_hard_delete(facade, 999)["ok"] is False
    service.close()


def test_legacy_mode_writes_stay_legacy_only(test_config):
    """切换前 legacy 模式：Web 写保持旧行为，Core 表保持空。"""
    service = _make_service(test_config, mode=ContextMode.LEGACY)
    facade = service.legacy_facade()
    legacy = MemoryStore(test_config)
    legacy.initialize()
    mem_id = facade.add("p:t:legacy", "旧模式记忆", attribute="fact")

    assert api_update(facade, legacy, mem_id,
                      {"importance": 8.0, "attribute": "decision",
                       "tags": ["x"]})["ok"]
    m = facade.get_by_id(mem_id)
    assert m["importance"] == 8.0 and m["attribute"] == "decision"
    assert m["tags"] == "x"
    assert api_archive(facade, mem_id)["ok"]
    assert api_restore(facade, mem_id)["ok"]
    assert api_delete(facade, mem_id)["ok"]
    assert facade.get_by_id(mem_id)["status"] == "deleted"
    assert api_hard_delete(facade, mem_id)["ok"]
    assert facade.get_by_id(mem_id) is None
    # legacy 模式不声称 Core 变化
    assert service.store.count_by_status() == {}
    service.close()
    legacy.close()


# ---- bounded 500 surface ----

def test_bounded_error_surface():
    """HTTP 500 只暴露稳定的 Context 错误码或有界的异常类名。"""
    assert _bounded_error(
        ContextServiceError("degraded_legacy", "internal detail")
    ) == "context_error:degraded_legacy"
    assert _bounded_error(RuntimeError("boom /secret/path")) == "RuntimeError"


# ---- HTTP 端到端 ----

@pytest.fixture
def http_server(test_config):
    """Serving thread owns its own service + residual store (SQLite threads)."""
    service = _make_service(test_config)  # 外层：播种 + Core 侧断言
    facade = service.legacy_facade()
    ids = _seed(facade)

    holder = {}
    ready = threading.Event()

    def serve():
        # sqlite 连接不能跨线程使用：服务线程内独立持有 service 与 store
        inner = _make_service(test_config)
        inner_legacy = MemoryStore(test_config)
        inner_legacy.initialize()
        srv = HTTPServer(("127.0.0.1", 0), make_handler(inner, inner_legacy))
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()
        inner.close()
        inner_legacy.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(timeout=5)
    yield f"http://127.0.0.1:{holder['srv'].server_address[1]}", ids, service
    holder["srv"].shutdown()
    holder["srv"].server_close()
    t.join(timeout=5)
    service.close()


def _get(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_http_stats_and_memories(http_server):
    base, ids, _ = http_server
    st = _get(base + "/api/stats")
    assert st["total_active"] == 2
    rows = _get(base + "/api/memories?status=active&sort=access_count&order=desc")
    assert rows[0]["id"] == ids["hot"]
    rows = _get(base + "/api/memories?q=%E7%B4%AB%E8%89%B2")  # q=紫色
    assert [r["id"] for r in rows] == [ids["warm"]]


def test_http_write_flow(http_server):
    base, ids, service = http_server
    warm_ctx = service.store.resolve_legacy_mapping(ids["warm"])
    assert _post(f"{base}/api/memory/{ids['warm']}/update",
                 {"importance": 8.0, "tier": "pinned"})["ok"]
    assert service.store.get_item(warm_ctx).importance == 8.0
    assert _post(f"{base}/api/memory/{ids['warm']}/archive")["ok"]
    rows = _get(base + "/api/memories?status=archived")
    assert any(r["id"] == ids["warm"] for r in rows)
    assert service.store.get_item(warm_ctx).status is ContextStatus.ARCHIVED
    assert _post(f"{base}/api/memory/{ids['warm']}/restore")["ok"]
    assert service.store.get_item(warm_ctx).status is ContextStatus.ACTIVE
    assert _post(f"{base}/api/memory/{ids['cold']}/delete")["ok"]
    # 未知端点 / 错误 id 返回错误 JSON
    try:
        _post(f"{base}/api/memory/{ids['warm']}/nonsense")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_http_hard_delete_removes_both_sides(http_server):
    base, ids, service = http_server
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert context_id is not None

    assert _post(f"{base}/api/memory/{ids['warm']}/hard_delete")["ok"]

    assert service.store.resolve_legacy_mapping(ids["warm"]) is None
    assert service.store.get_item(context_id) is None
    # 邻居不受影响，仍出现在列表
    rows = _get(base + "/api/memories?status=active")
    assert [r["id"] for r in rows] == [ids["hot"]]


def test_http_index_served(http_server):
    base, _, _ = http_server
    with urllib.request.urlopen(base + "/") as resp:
        html = resp.read().decode("utf-8")
    assert resp.headers.get_content_type() == "text/html"
    assert "EvolvMem" in html


def test_http_context_failure_500_bounded_no_partial_state(test_config):
    """Core 侧失败：HTTP 500 + 有界错误类/码 + 双侧无部分状态。"""

    class FailingStore(ContextStore):
        def set_item_status(self, item_id, status):
            raise RuntimeError("injected failure with /abs/path detail")

    outer = _make_service(test_config)
    facade = outer.legacy_facade()
    mem_id = facade.add(key="p:t:victim", value="将被归档的记忆条目")
    context_id = outer.store.resolve_legacy_mapping(mem_id)
    assert context_id is not None

    holder = {}
    ready = threading.Event()

    def serve():
        inner = _make_service(test_config, store=FailingStore(test_config))
        inner_legacy = MemoryStore(test_config)
        inner_legacy.initialize()
        srv = HTTPServer(("127.0.0.1", 0), make_handler(inner, inner_legacy))
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()
        inner.close()
        inner_legacy.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(timeout=5)
    base = f"http://127.0.0.1:{holder['srv'].server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"{base}/api/memory/{mem_id}/archive")
        err = exc_info.value
        assert err.code == 500
        body = json.loads(err.read().decode("utf-8"))
        # 有界：只有错误类名，绝不泄露内部消息/路径
        assert body == {"ok": False, "error": "RuntimeError"}
        # 无部分状态：双侧都保持 active
        assert facade.get_by_id(mem_id)["status"] == "active"
        assert outer.store.get_item(context_id).status is ContextStatus.ACTIVE
    finally:
        holder["srv"].shutdown()
        holder["srv"].server_close()
        t.join(timeout=5)
        outer.close()
