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
    ContextContentType,
    ContextMode,
    ContextScope,
    ContextServiceError,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.memory_store import MemoryStore
from evolvmem.web_server import (
    _bounded_error,
    _context_mode,
    api_archive,
    api_delete,
    api_hard_delete,
    api_memories,
    api_restore,
    api_stats,
    api_update,
    make_handler,
)


def _mem_rows(*args, **kwargs):
    """api_memories 的分页行访问器（返回形状为 {rows, total, page, page_size}）。"""
    return api_memories(*args, **kwargs)["rows"]


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
    """Compat-mode facade + service, seeded; legacy store for read checks."""
    service = _make_service(test_config)
    facade = service.legacy_facade()
    legacy = MemoryStore(test_config)  # 独立连接校验投影行内容
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
    rows = _mem_rows(facade, {})
    assert [r["id"] for r in rows] == [ids["hot"], ids["warm"]]
    assert rows[0]["access_count"] == 5
    # 字段完整
    for field in ("id", "key", "value", "attribute", "tags", "tier",
                  "importance", "access_count", "last_accessed",
                  "created_at", "updated_at", "expires_at"):
        assert field in rows[0]


def test_memories_sort_and_order(backend):
    facade, _, _, ids = backend
    rows = _mem_rows(facade, {"sort": "access_count", "order": "asc"})
    assert [r["access_count"] for r in rows] == [1, 5]
    rows = _mem_rows(facade, {"sort": "importance", "order": "desc"})
    assert rows[0]["importance"] == 7.0
    rows = _mem_rows(facade, {"sort": "created_at", "order": "desc"})
    assert len(rows) == 2
    # 非法 sort 列回退到 access_count，不报错
    rows = _mem_rows(facade, {"sort": "access_count; DROP TABLE memories"})
    assert rows[0]["access_count"] == 5


def test_memories_filters(backend):
    facade, _, _, ids = backend
    # status
    rows = _mem_rows(facade, {"status": "archived"})
    assert [r["id"] for r in rows] == [ids["cold"]]
    # tier
    rows = _mem_rows(facade, {"tier": "pinned"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # attribute
    rows = _mem_rows(facade, {"attribute": "preference"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 value
    rows = _mem_rows(facade, {"q": "紫色"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 key
    rows = _mem_rows(facade, {"q": "decision"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # LIKE 通配符按字面处理
    assert _mem_rows(facade, {"q": "%"}) == []


def test_memories_q_filter_is_case_insensitive(backend):
    """SQL LIKE 时代的大小写不敏感语义在 Python 过滤下保持不变。"""
    facade, _, _, _ = backend
    mem_id = facade.add("proj:ABC:deploy", "Production DEPLOY notes",
                        attribute="fact")
    rows = _mem_rows(facade, {"q": "abc"})
    assert [r["id"] for r in rows] == [mem_id]
    rows = _mem_rows(facade, {"q": "deploy notes"})
    assert [r["id"] for r in rows] == [mem_id]


# ---- update / archive / restore / delete ----

def test_update_metadata_fields(backend):
    facade, legacy, _, ids = backend
    res = api_update(facade, ids["warm"], {
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
    res = api_update(facade, ids["warm"],
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
    """attribute/tags 就地更新投影行且 id 不变；派生字段同事务镜像到 Core。"""
    facade, legacy, service, ids = backend
    res = api_update(facade, ids["warm"],
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
    # content_type/scope/tags 由迁移器公开策略从新投影行推导
    assert item.content_type is ContextContentType.USER_PROFILE
    assert item.scope is ContextScope.GLOBAL
    assert item.tags == ("ui", "color")


def test_update_attribute_tags_failure_rolls_back_projection_and_context(
    test_config,
):
    """Core 侧失败时投影行的 attribute/tags 编辑一并回滚（不再两连接两提交）。"""

    class FailingStore(ContextStore):
        def update_item_from_legacy(self, *args, **kwargs):
            raise RuntimeError("injected failure with /abs/path detail")

    service = _make_service(test_config, store=FailingStore(test_config))
    facade = service.legacy_facade()
    mem_id = facade.add("user:pref:color", "喜欢柔和紫色界面",
                        attribute="preference", tags=["ui"])
    context_id = service.store.resolve_legacy_mapping(mem_id)

    with pytest.raises(RuntimeError, match="injected failure"):
        api_update(facade, mem_id, {"attribute": "decision", "tags": ["x"]})

    row = facade.get_by_id(mem_id)
    assert row["attribute"] == "preference"
    assert row["tags"] == "ui"
    item = service.store.get_item(context_id)
    assert item.content_type is ContextContentType.PREFERENCE
    assert item.tags == ("ui",)
    service.close()


def test_update_validation(backend):
    facade, legacy, _, ids = backend
    assert not api_update(facade, ids["warm"],
                          {"importance": 99})["ok"]
    assert not api_update(facade, ids["warm"], {"tier": "bogus"})["ok"]
    assert not api_update(facade, 9999, {"importance": 5})["ok"]
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
    assert all(r["id"] != ids["hot"] for r in _mem_rows(facade, {}))
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
    mem_id = facade.add("p:t:legacy", "旧模式记忆", attribute="fact")

    assert api_update(facade, mem_id,
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


def test_invalid_context_mode_fails_closed_to_legacy(test_config):
    """非法 context_mode（如 typo）：handler 可构造，读写全部走 legacy 后端。"""
    test_config.context_mode = "compatt"
    assert _context_mode(test_config) is ContextMode.LEGACY

    service = ContextService(test_config)
    service.initialize(mode=_context_mode(test_config), adapter="web")
    handler = make_handler(service)
    assert handler is not None
    facade = service.legacy_facade()
    mem_id = facade.add("p:t:legacy", "旧模式记忆", attribute="fact")
    assert facade.get_by_id(mem_id)["value"] == "旧模式记忆"
    assert api_update(facade, mem_id,
                      {"importance": 8.0, "attribute": "decision"})["ok"]
    assert facade.get_by_id(mem_id)["attribute"] == "decision"
    assert service.store.count_by_status() == {}
    service.close()


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
    """Serving thread owns its own service (SQLite connections are per-thread)."""
    service = _make_service(test_config)  # 外层：播种 + Core 侧断言
    facade = service.legacy_facade()
    ids = _seed(facade)

    holder = {}
    ready = threading.Event()

    def serve():
        # sqlite 连接不能跨线程使用：服务线程内独立持有 service
        inner = _make_service(test_config)
        srv = HTTPServer(("127.0.0.1", 0), make_handler(inner))
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()
        inner.close()

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
    assert st["today_new"] == 3
    assert st["skill_candidates"] == 1
    rows = _get(base + "/api/memories?status=active&sort=access_count&order=desc")["rows"]
    assert rows[0]["id"] == ids["hot"]
    rows = _get(base + "/api/memories?q=%E7%B4%AB%E8%89%B2")["rows"]  # q=紫色
    assert [r["id"] for r in rows] == [ids["warm"]]
    rows = _get(base + "/api/memories?preset=skill")["rows"]
    assert [r["id"] for r in rows] == [ids["hot"]]


def test_http_write_flow(http_server):
    base, ids, service = http_server
    warm_ctx = service.store.resolve_legacy_mapping(ids["warm"])
    assert _post(f"{base}/api/memory/{ids['warm']}/update",
                 {"importance": 8.0, "tier": "pinned"})["ok"]
    assert service.store.get_item(warm_ctx).importance == 8.0
    assert _post(f"{base}/api/memory/{ids['warm']}/archive")["ok"]
    rows = _get(base + "/api/memories?status=archived")["rows"]
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


def test_http_update_attribute_tags_mirrors_context(http_server):
    """HTTP shape 不变：attribute/tags 更新经类型化边界同事务镜像到 Core。"""
    base, ids, service = http_server
    warm_ctx = service.store.resolve_legacy_mapping(ids["warm"])

    res = _post(f"{base}/api/memory/{ids['warm']}/update",
                {"attribute": "decision", "tags": ["ui", "color"]})

    assert res["ok"]
    assert res["memory"]["id"] == ids["warm"]
    assert res["memory"]["attribute"] == "decision"
    assert res["memory"]["tags"] == "ui,color"
    item = service.store.get_item(warm_ctx)
    assert item.content_type is ContextContentType.DECISION
    assert item.scope is ContextScope.PROJECT
    assert item.tags == ("ui", "color")


def test_http_hard_delete_removes_both_sides(http_server):
    base, ids, service = http_server
    context_id = service.store.resolve_legacy_mapping(ids["warm"])
    assert context_id is not None

    assert _post(f"{base}/api/memory/{ids['warm']}/hard_delete")["ok"]

    assert service.store.resolve_legacy_mapping(ids["warm"]) is None
    assert service.store.get_item(context_id) is None
    # 邻居不受影响，仍出现在列表
    rows = _get(base + "/api/memories?status=active")["rows"]
    assert [r["id"] for r in rows] == [ids["hot"]]


def test_http_index_served(http_server):
    base, _, _ = http_server
    with urllib.request.urlopen(base + "/") as resp:
        html = resp.read().decode("utf-8")
    assert resp.headers.get_content_type() == "text/html"
    assert "EvolvMem" in html


def test_http_architecture_served(http_server):
    """架构图静态页：/architecture 与 /architecture.html 均返回 200 + HTML。"""
    base, _, _ = http_server
    for route in ("/architecture", "/architecture.html"):
        with urllib.request.urlopen(base + route) as resp:
            html = resp.read().decode("utf-8")
        assert resp.headers.get_content_type() == "text/html"
        assert "evolvmem 记忆系统流程框架" in html


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
        srv = HTTPServer(("127.0.0.1", 0), make_handler(inner))
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()
        inner.close()

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


# ---- 项目归属与审核队列 ----

from evolvmem.project_models import ProjectResolutionDecision  # noqa: E402
from evolvmem.project_store import ProjectStore  # noqa: E402
from evolvmem.web_server import (  # noqa: E402
    api_memories_organize_suggest,
    api_memory_context,
    api_project_register,
    api_project_set_display_name,
    api_projects,
    api_projects_set_display_names,
    api_projects_suggest_display_names,
    api_resolution_accept,
    api_resolution_reject,
    api_resolutions,
    api_resolutions_batch_accept,
)

_CONFLICT_EVIDENCE = (
    {"source": "key", "type": "canonical_key", "source_version": "legacy-v1",
     "normalized_value": "webproj"},
    {"source": "tags", "type": "category_tag", "source_version": "legacy-v1",
     "normalized_value": "otherproj"},
)


def _project_store(service):
    store = service.store
    return ProjectStore(
        store._connection(), store._require_transaction, generic_names=()
    )


@pytest.fixture
def review_backend(backend):
    """Seeded facade plus a resolution queue in mixed review states.

    COMPAT 模式下 facade.add 已为每条记忆自动记录 unresolved/pending 决议
    （revision 1）。这里：warm 重录为 conflict（revision → 2，两个证据候选）；
    cold（已归档）与 extra 被采纳进 webproj；hot 保持 pending。
    """
    facade, _, service, ids = backend
    store = service.store
    extra = facade.add("proj:fact:extra", "归属审核用例记忆", attribute="fact")
    ctx = {
        key: store.resolve_legacy_mapping(legacy_id)
        for key, legacy_id in {**ids, "extra": extra}.items()
    }
    with store.transaction():
        ps = _project_store(service)
        ps.register_project("webproj")
        ps.register_project("otherproj")
        ps.record_resolution(
            ctx["warm"],
            ProjectResolutionDecision.conflict("test-v1", _CONFLICT_EVIDENCE),
        )
        ps.accept_resolution(ctx["cold"], "webproj", expected_revision=1)
        ps.accept_resolution(ctx["extra"], "webproj", expected_revision=1)
    yield facade, service, ids, ctx, extra


def test_projects_registry_counts_and_pending(review_backend):
    facade, service, _, _, _ = review_backend
    payload = api_projects(facade, service.store)
    by_name = {p["project"]: p for p in payload["projects"]}
    assert by_name["webproj"]["status"] == "active"
    assert by_name["webproj"]["active_items"] == 1  # 仅 extra；cold 已归档
    assert by_name["otherproj"]["active_items"] == 0
    assert payload["pending_review"] == 2


def test_stats_attribution_counters(review_backend):
    facade, service, _, _, _ = review_backend
    st = api_stats(facade, service.store)
    assert st["attributed_active"] == 1
    assert st["unattributed_active"] == 2  # hot、warm 未归属
    assert st["by_attribution"] == {"webproj": 1}
    assert st["pending_review"] == 2
    # 既有字段保持
    assert st["total_active"] == 3


def test_memories_attribution_enrichment_and_filter(review_backend):
    facade, service, ids, ctx, extra = review_backend
    rows = _mem_rows(facade, {}, service.store)
    by_id = {r["id"]: r for r in rows}
    assert by_id[extra]["project"] == "webproj"
    assert by_id[ids["hot"]]["project"] == ""
    # 行内改派/批量映射所需的决议元数据随行返回
    assert by_id[extra]["item_id"] == ctx["extra"]
    assert by_id[extra]["resolution_revision"] == 2  # 采纳后 revision+1
    assert by_id[ids["hot"]]["item_id"] == ctx["hot"]
    assert by_id[ids["hot"]]["resolution_revision"] == 1

    rows = _mem_rows(facade, {"attribution": "webproj"}, service.store)
    assert [r["id"] for r in rows] == [extra]
    rows = _mem_rows(facade, {"attribution": "__none__"}, service.store)
    assert sorted(r["id"] for r in rows) == sorted([ids["hot"], ids["warm"]])


def test_memories_row_drives_reassign(review_backend):
    """浏览列表行自带的 item_id/revision 可直接驱动改派（同一 CAS 通道）。"""
    facade, service, ids, ctx, extra = review_backend
    row = next(r for r in _mem_rows(facade, {}, service.store)
               if r["id"] == extra)
    res = api_resolution_accept(
        service, row["item_id"],
        {"project": "otherproj",
         "expected_revision": row["resolution_revision"]},
    )
    assert res["ok"]
    assert service.store.get_item(ctx["extra"]).project == "otherproj"
    rows = _mem_rows(facade, {"attribution": "otherproj"}, service.store)
    assert [r["id"] for r in rows] == [extra]


def test_memories_review_filter_and_metadata(review_backend):
    """审核队列 = 同一列表的 review 过滤视图；行带决议状态与有界证据。

    审核视图带 status="all"：待审队列里有 archived 条目，不能被默认的
    active 状态过滤掉。
    """
    facade, service, ids, ctx, extra = review_backend
    payload = api_memories(
        facade, {"review": "pending", "status": "all"}, service.store)
    assert sorted(r["id"] for r in payload["rows"]) == sorted(
        [ids["hot"], ids["warm"]])
    by_id = {r["id"]: r for r in payload["rows"]}
    assert by_id[ids["hot"]]["resolution_state"] == "unresolved"
    assert by_id[ids["hot"]]["review_state"] == "pending"
    warm = by_id[ids["warm"]]
    assert warm["resolution_state"] == "conflict"
    assert warm["evidence"] == [dict(e) for e in _CONFLICT_EVIDENCE]
    # 已采纳视图（含 archived 的 cold）
    accepted = api_memories(
        facade, {"review": "accepted", "status": "all"}, service.store)["rows"]
    assert sorted(r["id"] for r in accepted) == sorted([ids["cold"], extra])


def test_memories_pagination(review_backend):
    """分页：total 是筛选后总数，rows 为当前页切片，页码越界回空页。"""
    facade, service, ids, _, extra = review_backend
    all_rows = api_memories(facade, {"status": "all"}, service.store)
    assert all_rows["total"] == 4
    page1 = api_memories(
        facade, {"status": "all", "page_size": 2, "page": 1,
                 "sort": "created_at", "order": "asc"}, service.store)
    page2 = api_memories(
        facade, {"status": "all", "page_size": 2, "page": 2,
                 "sort": "created_at", "order": "asc"}, service.store)
    assert page1["total"] == 4 and page1["page_size"] == 2
    assert len(page1["rows"]) == 2 and len(page2["rows"]) == 2
    # 两页并集 = 全集，且不重叠
    ids_p1 = {r["id"] for r in page1["rows"]}
    ids_p2 = {r["id"] for r in page2["rows"]}
    assert ids_p1.isdisjoint(ids_p2)
    assert ids_p1 | ids_p2 == set(ids.values()) | {extra}
    # 非法参数回退默认；页码越界给空页
    bad = api_memories(
        facade, {"status": "all", "page": "abc", "page_size": "999"},
        service.store)
    assert bad["page"] == 1 and bad["page_size"] == 200
    beyond = api_memories(
        facade, {"status": "all", "page": 99}, service.store)
    assert beyond["rows"] == [] and beyond["total"] == 4


def test_resolutions_pending_listing_with_preview(review_backend):
    facade, service, ids, ctx, _ = review_backend
    rows = api_resolutions(facade, service.store, {})
    assert [r["item_id"] for r in rows] == sorted([ctx["hot"], ctx["warm"]])
    hot, warm = rows
    assert hot["legacy_id"] == ids["hot"]
    assert hot["key"] == "proj:decision:db"
    assert hot["value"] == "使用 SQLite 作为存储"
    assert hot["review_state"] == "pending"
    assert hot["resolution_state"] == "unresolved"
    assert hot["revision"] == 1
    assert warm["resolution_state"] == "conflict"
    # evidence 只含解析器的有界公开行
    assert warm["evidence"] == [dict(e) for e in _CONFLICT_EVIDENCE]


def test_resolutions_dangling_legacy_mapping(backend, test_config):
    """legacy 投影行被物理删但映射残留：预览回退 identity_key/l0，标记不可删。"""
    facade, _, service, ids = backend
    import sqlite3

    conn = sqlite3.connect(str(test_config.db_path))
    conn.execute("DELETE FROM memories WHERE id=?", (ids["warm"],))
    conn.commit()
    conn.close()

    rows = api_resolutions(facade, service.store, {"state": "all"})
    warm = next(r for r in rows if r["legacy_id"] == ids["warm"])
    assert warm["legacy_available"] is False
    warm_ctx = service.store.resolve_legacy_mapping(ids["warm"])
    assert warm["key"] == service.store.get_item(warm_ctx).identity_key
    assert warm["value"]  # l0 层兜底预览
    hot = next(r for r in rows if r["legacy_id"] == ids["hot"])
    assert hot["legacy_available"] is True


def test_resolutions_state_filter(review_backend):
    facade, service, _, ctx, _ = review_backend
    accepted = api_resolutions(facade, service.store, {"state": "accepted"})
    assert sorted(r["item_id"] for r in accepted) == sorted(
        [ctx["cold"], ctx["extra"]]
    )
    assert all(r["resolved_project"] == "webproj" for r in accepted)
    assert all(r["reviewed_at"] for r in accepted)
    everything = api_resolutions(facade, service.store, {"state": "all"})
    assert len(everything) == 4


def test_resolution_accept_happy_path(review_backend):
    facade, service, ids, ctx, _ = review_backend
    res = api_resolution_accept(
        service, ctx["hot"], {"project": "webproj", "expected_revision": 1}
    )
    assert res == {"ok": True, "item_id": ctx["hot"],
                   "resolved_project": "webproj"}
    assert service.store.get_item(ctx["hot"]).project == "webproj"
    rows = api_resolutions(facade, service.store, {"state": "pending"})
    assert [r["item_id"] for r in rows] == [ctx["warm"]]
    rows = _mem_rows(facade, {"attribution": "webproj"}, service.store)
    assert ids["hot"] in [r["id"] for r in rows]


def test_resolution_accept_failures(review_backend):
    _, service, _, ctx, _ = review_backend
    assert api_resolution_accept(
        service, ctx["hot"], {"project": "", "expected_revision": 1}
    )["error"] == "invalid_project"
    assert api_resolution_accept(
        service, ctx["hot"], {"project": "webproj"}
    )["error"] == "invalid_revision"
    assert api_resolution_accept(
        service, ctx["hot"],
        {"project": "webproj", "expected_revision": 99},
    )["error"] == "revision_conflict"
    assert api_resolution_accept(
        service, ctx["hot"],
        {"project": "ghost", "expected_revision": 1},
    )["error"] == "project_not_found"
    assert api_resolution_accept(
        service, 9999, {"project": "webproj", "expected_revision": 1}
    )["error"] == "resolution_not_found"
    # 全部失败均未落地
    assert service.store.get_item(ctx["hot"]).project == ""


def test_resolution_reject(review_backend):
    facade, service, _, ctx, _ = review_backend
    # warm 在夹具里被重录为 conflict，revision 已升到 2
    res = api_resolution_reject(service, ctx["warm"], {"expected_revision": 2})
    assert res["ok"] and res["review_state"] == "rejected"
    assert service.store.get_item(ctx["warm"]).project == ""
    rows = api_resolutions(facade, service.store, {"state": "rejected"})
    assert [r["item_id"] for r in rows] == [ctx["warm"]]
    assert api_resolution_reject(
        service, ctx["hot"], {"expected_revision": 42}
    )["error"] == "revision_conflict"


def test_batch_accept_partial_success(review_backend):
    facade, service, ids, ctx, _ = review_backend
    res = api_resolutions_batch_accept(service, {
        "project": "otherproj",
        "items": [
            {"item_id": ctx["hot"], "expected_revision": 1},
            {"item_id": ctx["warm"], "expected_revision": 99},  # 过期 revision
        ],
    })
    assert res["ok"] and res["accepted"] == 1 and res["failed"] == 1
    by_item = {r["item_id"]: r for r in res["results"]}
    assert by_item[ctx["hot"]]["ok"]
    assert by_item[ctx["warm"]]["error"] == "revision_conflict"
    assert service.store.get_item(ctx["hot"]).project == "otherproj"
    assert service.store.get_item(ctx["warm"]).project == ""


def test_batch_accept_validation(review_backend):
    _, service, _, ctx, _ = review_backend
    assert api_resolutions_batch_accept(
        service, {"project": "", "items": []}
    )["error"] == "invalid_project"
    assert api_resolutions_batch_accept(
        service, {"project": "webproj", "items": []}
    )["error"] == "invalid_items"
    assert api_resolutions_batch_accept(
        service, {"project": "webproj",
                  "items": [{"item_id": ctx["hot"], "expected_revision": "1"}]}
    )["error"] == "invalid_items"
    assert api_resolutions_batch_accept(
        service, {"project": "ghost",
                  "items": [{"item_id": ctx["hot"], "expected_revision": 1}]}
    )["error"] == "project_not_found"


def test_project_register(review_backend):
    _, service, _, _, _ = review_backend
    assert api_project_register(service, {"name": "newproj"}) == {
        "ok": True, "project": "newproj", "display_name": ""}
    # 幂等：重复注册仍 ok
    assert api_project_register(service, {"name": "newproj"})["ok"]
    assert api_project_register(
        service, {"name": " "})["error"] == "invalid_project"
    assert api_project_register(
        service, {"name": "x" * 65})["error"] == "invalid_project"
    assert api_project_register(
        service, {"name": "a\x00b"})["error"] == "invalid_project"
    assert api_project_register(
        service, {"name": "ok", "display_name": "x" * 33}
    )["error"] == "invalid_display_name"


def test_project_display_name_flow(review_backend):
    """中文显示名：注册携带 / CAS 设置 / 列表返回 / 冲突与校验。"""
    facade, service, _, _, _ = review_backend
    res = api_project_register(
        service, {"name": "cnproj", "display_name": "中文项目"})
    assert res == {"ok": True, "project": "cnproj", "display_name": "中文项目"}

    res = api_project_set_display_name(
        service, {"project": "webproj", "display_name": "网项目",
                  "expected_revision": 1})
    assert res["ok"] and res["display_name"] == "网项目"
    # 过期 revision / 未知项目 / 非法显示名
    assert api_project_set_display_name(
        service, {"project": "webproj", "display_name": "X",
                  "expected_revision": 1})["error"] == "revision_conflict"
    assert api_project_set_display_name(
        service, {"project": "ghost", "display_name": "X",
                  "expected_revision": 1})["error"] == "project_not_found"
    assert api_project_set_display_name(
        service, {"project": "webproj", "display_name": "a\x01b",
                  "expected_revision": 2})["error"] == "invalid_display_name"

    payload = api_projects(facade, service.store)
    by_name = {p["project"]: p for p in payload["projects"]}
    assert by_name["webproj"]["display_name"] == "网项目"
    assert by_name["cnproj"]["display_name"] == "中文项目"
    assert by_name["webproj"]["revision"] == 2


def test_memory_context(backend):
    """来源上下文：会话摘要 + 同会话兄弟记忆（含归属），无来源时诚实为空。"""
    facade, _, service, ids = backend
    # 播种的 _seed 没有 source_session → 诚实空上下文
    res = api_memory_context(facade, service.store, ids["hot"])
    assert res == {"ok": True, "source_session": "", "session_summary": None,
                   "siblings": []}
    assert api_memory_context(facade, service.store, 9999) == {
        "ok": False, "error": "not found"}

    # 同一次会话产出：摘要 + 两条原子记忆
    s = "session_ctx_test"
    facade.add("project:jiangli:progress:log:2026-09-03-1200",
               "这次会话在排查蓝鲸选品的模板渲染问题", source_session=s)
    m1 = facade.add("project:bluewhale:fact:a", "事实A", source_session=s)
    m2 = facade.add("project:bluewhale:fact:b", "事实B", source_session=s)

    res = api_memory_context(facade, service.store, m1)
    assert res["ok"] and res["source_session"] == s
    assert res["session_summary"]["value"] == "这次会话在排查蓝鲸选品的模板渲染问题"
    sib_ids = [x["id"] for x in res["siblings"]]
    # 兄弟记忆只有 m2（摘要条目单独展示，不进兄弟列表），不含 m1 自身
    assert sib_ids == [m2]
    assert all("project" in x for x in res["siblings"])


def test_organize_suggest_key_first(review_backend, monkeypatch):
    """key 前缀 project:<已注册slug>: 直判不过 LLM；只有看不出来的才问 AI。"""
    facade, service, ids, _, _ = review_backend
    import evolvmem.kimi_hooks as kh

    kid = facade.add("project:webproj:fact:direct", "key 直判条目",
                     attribute="fact")
    # LLM 不可用也不影响 key 直判
    monkeypatch.setattr(kh, "_load_llm_config", lambda: None)
    res = api_memories_organize_suggest(service, facade, {"legacy_ids": [kid]})
    assert res["ok"]
    assert res["assignments"] == {str(kid): "webproj"}
    assert res["via"] == {str(kid): "key"}
    assert res["key_decided"] == 1

    # 混合：直判 1 条 + AI 1 条（hot 的 key 前缀 proj 未注册，走 AI）
    class FakeCfg:
        provider = "deepseek"
        model = "deepseek-v4-flash"

    captured = {}

    def fake_call(prompt, cfg, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"assignments": {str(ids["hot"]): "otherproj"}})

    monkeypatch.setattr(kh, "_load_llm_config", lambda: FakeCfg())
    monkeypatch.setattr(kh, "_call_llm", fake_call)
    res = api_memories_organize_suggest(
        service, facade, {"legacy_ids": [kid, ids["hot"]]})
    assert res["assignments"] == {str(kid): "webproj",
                                  str(ids["hot"]): "otherproj"}
    assert res["via"][str(kid)] == "key"
    assert res["via"][str(ids["hot"])] == "ai"
    # 直判行不进 prompt
    assert "project:webproj:fact:direct" not in captured["prompt"]
    assert "proj:decision:db" in captured["prompt"]


def test_organize_suggest(review_backend, monkeypatch):
    """AI 一键整理：降级诚实、校验过滤、建议不落库。"""
    facade, service, ids, ctx, _ = review_backend
    import evolvmem.kimi_hooks as kh

    monkeypatch.setattr(kh, "_load_llm_config", lambda: None)
    res = api_memories_organize_suggest(
        service, facade, {"legacy_ids": [ids["hot"]]})
    # 降级时仍带回 key 直判的部分结果（本条没有，故为空）
    assert res == {"ok": False, "error": "llm_unavailable",
                   "assignments": {}, "via": {}}
    assert api_memories_organize_suggest(
        service, facade, {"legacy_ids": []})["error"] == "invalid_items"
    assert api_memories_organize_suggest(
        service, facade, {"legacy_ids": ["x"]})["error"] == "invalid_items"

    class FakeCfg:
        provider = "deepseek"
        model = "deepseek-v4-flash"

    captured = {}

    def fake_call(prompt, cfg, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"assignments": {
            str(ids["hot"]): "webproj",        # 现有项目
            str(ids["warm"]): "new-bucket",    # 合法新 slug
            str(ids["cold"]): "Bad Slug!!",    # 非法新 slug 丢弃
            "9999": "webproj",                 # 未请求的 id 丢弃
        }})

    monkeypatch.setattr(kh, "_load_llm_config", lambda: FakeCfg())
    monkeypatch.setattr(kh, "_call_llm", fake_call)
    res = api_memories_organize_suggest(
        service, facade,
        {"legacy_ids": [ids["hot"], ids["warm"], ids["cold"]]})
    assert res["ok"]
    assert res["assignments"] == {
        str(ids["hot"]): "webproj", str(ids["warm"]): "new-bucket"}
    assert res["new_projects"] == ["new-bucket"]
    # prompt 携带注册表与记忆 key/截断 value
    assert "webproj" in captured["prompt"]
    assert "proj:decision:db" in captured["prompt"]
    # 只给建议不落库
    assert service.store.get_item(ctx["hot"]).project == ""


def test_display_names_batch(review_backend):
    """批量保存显示名：逐条 CAS，单条冲突不影响其他。"""
    facade, service, _, _, _ = review_backend
    res = api_projects_set_display_names(service, {"items": [
        {"project": "webproj", "display_name": "网项目", "expected_revision": 1},
        {"project": "otherproj", "display_name": "另一个", "expected_revision": 99},
    ]})
    assert res["ok"] and res["saved"] == 1 and res["failed"] == 1
    by_proj = {r["project"]: r for r in res["results"]}
    assert by_proj["webproj"]["ok"]
    assert by_proj["otherproj"]["error"] == "revision_conflict"
    payload = api_projects(facade, service.store)
    names = {p["project"]: p["display_name"] for p in payload["projects"]}
    assert names["webproj"] == "网项目"
    assert names["otherproj"] == ""

    assert api_projects_set_display_names(
        service, {"items": []})["error"] == "invalid_items"
    assert api_projects_set_display_names(service, {"items": [
        {"project": "webproj", "display_name": "x" * 33,
         "expected_revision": 2}]})["error"] == "invalid_items"


def test_suggest_display_names(review_backend, monkeypatch):
    """AI 一键填充：无凭据诚实降级；有响应时按校验规则过滤。"""
    _, service, _, _, _ = review_backend
    import evolvmem.kimi_hooks as kh

    monkeypatch.setattr(kh, "_load_llm_config", lambda: None)
    assert api_projects_suggest_display_names(service) == {
        "ok": False, "error": "llm_unavailable"}

    class FakeCfg:
        provider = "deepseek"
        model = "deepseek-v4-flash"

    captured = {}

    def fake_call(prompt, cfg, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"names": {
            "webproj": "网项目",
            "otherproj": "x" * 33,   # 超长按校验丢弃
            "ghost": "不在注册表",    # 未知 slug 丢弃
        }})

    monkeypatch.setattr(kh, "_load_llm_config", lambda: FakeCfg())
    monkeypatch.setattr(kh, "_call_llm", fake_call)
    res = api_projects_suggest_display_names(service)
    assert res == {"ok": True, "names": {"webproj": "网项目"}}
    # prompt 携带 slug 与其活跃条目的样本 key
    assert "webproj" in captured["prompt"]
    assert "proj:fact:extra" in captured["prompt"]

    monkeypatch.setattr(kh, "_call_llm", lambda *a, **k: "不是 JSON")
    assert api_projects_suggest_display_names(
        service)["error"] == "llm_bad_response"


# ---- 今日概览与 preset 筛选 ----

def _age_memory(config, mem_id: int) -> None:
    """把一条投影行改成久未访问且超出限速窗口（成为遗忘候选）。"""
    import sqlite3

    conn = sqlite3.connect(str(config.db_path))
    conn.execute(
        "UPDATE memories SET last_accessed='2000-01-01 00:00:00',"
        " updated_at='2000-01-01 00:00:00' WHERE id=?",
        (mem_id,),
    )
    conn.commit()
    conn.close()


def test_stats_daily_overview(backend, test_config):
    facade, _, _, ids = backend
    st = api_stats(facade, None, test_config)
    assert st["today_new"] == 3  # 播种的三条（含已归档的 cold）都是今天产生
    assert st["skill_candidates"] == 1  # 仅 hot（5 次命中 ≥ 3）
    assert st["forgetting_candidates"] == 0  # 刚更新过，都在限速窗口内

    _age_memory(test_config, ids["warm"])
    st = api_stats(facade, None, test_config)
    assert st["forgetting_candidates"] == 1
    # hot 是 pinned，即使久置也不会成为候选
    _age_memory(test_config, ids["hot"])
    assert api_stats(facade, None, test_config)["forgetting_candidates"] == 1


def test_memories_preset_filters(backend, test_config):
    facade, _, _, ids = backend
    # skill：高频命中（客户端复选框同款口径的服务端版本）
    rows = _mem_rows(facade, {"preset": "skill"}, None, test_config)
    assert [r["id"] for r in rows] == [ids["hot"]]
    # today：默认 status=active，cold（archived）不计
    rows = _mem_rows(facade, {"preset": "today"}, None, test_config)
    assert sorted(r["id"] for r in rows) == sorted([ids["hot"], ids["warm"]])
    # forgetting：遗忘引擎同口径
    assert _mem_rows(facade, {"preset": "forgetting"}, None, test_config) == []
    _age_memory(test_config, ids["warm"])
    rows = _mem_rows(facade, {"preset": "forgetting"}, None, test_config)
    assert [r["id"] for r in rows] == [ids["warm"]]


def test_project_endpoints_in_legacy_mode(test_config):
    """LEGACY 模式（无 Core 数据）：新端点返回空集而不是报错。"""
    service = _make_service(test_config, mode=ContextMode.LEGACY)
    facade = service.legacy_facade()
    facade.add("p:t:legacy", "旧模式记忆", attribute="fact")
    payload = api_projects(facade, service.store)
    assert payload == {"projects": [], "pending_review": 0}
    assert api_resolutions(facade, service.store, {}) == []
    rows = _mem_rows(facade, {}, service.store)
    assert rows[0]["project"] == ""
    service.close()


def test_http_review_flow(http_server):
    """HTTP 端到端：队列列表 → 采纳 → 项目筛选可见。"""
    base, ids, service = http_server
    store = service.store
    # COMPAT 下 add 已自动记录 unresolved/pending 决议（revision 1）
    with store.transaction():
        _project_store(service).register_project("webproj")

    projects = _get(base + "/api/projects")
    # 三条播种记忆里 warm 是 global 域（preference）无需审核；hot/cold 待审
    assert projects["pending_review"] == 2
    assert [p["project"] for p in projects["projects"]] == ["webproj"]

    pending = _get(base + "/api/resolutions?state=pending")
    assert len(pending) == 2
    row = next(r for r in pending if r["legacy_id"] == ids["hot"])
    assert row["key"] == "proj:decision:db"
    assert row["revision"] == 1

    res = _post(f"{base}/api/resolution/{row['item_id']}/accept",
                {"project": "webproj", "expected_revision": 1})
    assert res["ok"]
    assert _get(base + "/api/projects")["pending_review"] == 1
    rows = _get(base + "/api/memories?attribution=webproj")["rows"]
    assert [r["id"] for r in rows] == [ids["hot"]]

    # 过期 revision 与未知项目的错误形状
    try:
        _post(f"{base}/api/resolution/{row['item_id']}/accept",
              {"project": "webproj", "expected_revision": 1})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read().decode())["error"] == "revision_conflict"
    try:
        _post(f"{base}/api/resolutions/batch_accept",
              {"project": "ghost",
               "items": [{"item_id": row["item_id"], "expected_revision": 2}]})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert json.loads(e.read().decode())["error"] == "project_not_found"

    assert _post(base + "/api/projects/register", {"name": "httpproj"})["ok"]
    names = [p["project"] for p in _get(base + "/api/projects")["projects"]]
    assert names == ["httpproj", "webproj"]

    # 中文显示名：设置 → 列表返回 → 过期 revision 冲突
    httpproj = next(p for p in _get(base + "/api/projects")["projects"]
                    if p["project"] == "httpproj")
    res = _post(base + "/api/projects/display_name",
                {"project": "httpproj", "display_name": "HTTP 项目",
                 "expected_revision": httpproj["revision"]})
    assert res["ok"]
    shown = next(p for p in _get(base + "/api/projects")["projects"]
                 if p["project"] == "httpproj")
    assert shown["display_name"] == "HTTP 项目"
    try:
        _post(base + "/api/projects/display_name",
              {"project": "httpproj", "display_name": "X",
               "expected_revision": httpproj["revision"]})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert json.loads(e.read().decode())["error"] == "revision_conflict"
