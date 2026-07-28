"""Tests for the web memory management console API.

Runs against a temporary data_dir; the real production database is never
touched. Covers both the plain API functions and the HTTP layer end-to-end.
"""

import json
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.web_server import (
    api_archive,
    api_delete,
    api_memories,
    api_restore,
    api_stats,
    api_update,
    make_handler,
)


@pytest.fixture
def store(test_config):
    s = MemoryStore(test_config)
    s.initialize()
    # seed: 高频 decision / 低频 preference / 零访问
    a = s.add("proj:decision:db", "使用 SQLite 作为存储", attribute="decision",
              tags=["db"], importance=7.0, tier="pinned")
    b = s.add("user:pref:color", "喜欢柔和紫色界面", attribute="preference",
              tags=["ui"], importance=6.0, tier="normal")
    c = s.add("proj:fact:idle", "从未被调用过的记忆", attribute="fact")
    for _ in range(5):
        s.update_access(a)
    s.update_access(b)
    s.archive(c)  # c 处于 archived 状态
    yield s, {"hot": a, "warm": b, "cold": c}
    s.close()


# ---- api_stats ----

def test_stats_counts(store):
    s, ids = store
    st = api_stats(s)
    assert st["total_active"] == 2  # c archived 不计入
    assert st["by_tier"]["pinned"] == 1
    assert st["by_tier"]["normal"] == 1
    assert st["by_tier"]["reference"] == 0
    assert st["by_attribute"]["decision"] == 1
    assert st["by_attribute"]["preference"] == 1
    assert st["never_accessed"] == 0  # 两个 active 都被访问过
    assert st["top_accessed"][0] == {
        "id": ids["hot"], "key": "proj:decision:db", "access_count": 5,
    }
    assert len(st["top_accessed"]) <= 10


def test_stats_never_accessed(store):
    s, _ = store
    s.add("proj:fact:never", "零访问记忆", attribute="fact")
    st = api_stats(s)
    assert st["never_accessed"] == 1
    assert st["total_active"] == 3


# ---- api_memories ----

def test_memories_default_sort_by_access_desc(store):
    s, ids = store
    rows = api_memories(s, {})
    assert [r["id"] for r in rows] == [ids["hot"], ids["warm"]]
    assert rows[0]["access_count"] == 5
    # 字段完整
    for field in ("id", "key", "value", "attribute", "tags", "tier",
                  "importance", "access_count", "last_accessed",
                  "created_at", "updated_at", "expires_at"):
        assert field in rows[0]


def test_memories_sort_and_order(store):
    s, ids = store
    rows = api_memories(s, {"sort": "access_count", "order": "asc"})
    assert [r["access_count"] for r in rows] == [1, 5]
    rows = api_memories(s, {"sort": "importance", "order": "desc"})
    assert rows[0]["importance"] == 7.0
    rows = api_memories(s, {"sort": "created_at", "order": "desc"})
    assert len(rows) == 2
    # 非法 sort 列回退到 access_count，不报错
    rows = api_memories(s, {"sort": "access_count; DROP TABLE memories"})
    assert rows[0]["access_count"] == 5


def test_memories_filters(store):
    s, ids = store
    # status
    rows = api_memories(s, {"status": "archived"})
    assert [r["id"] for r in rows] == [ids["cold"]]
    # tier
    rows = api_memories(s, {"tier": "pinned"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # attribute
    rows = api_memories(s, {"attribute": "preference"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 value
    rows = api_memories(s, {"q": "紫色"})
    assert [r["id"] for r in rows] == [ids["warm"]]
    # q 命中 key
    rows = api_memories(s, {"q": "decision"})
    assert [r["id"] for r in rows] == [ids["hot"]]
    # LIKE 通配符按字面处理
    assert api_memories(s, {"q": "%"}) == []


# ---- update / archive / restore / delete ----

def test_update_metadata_fields(store):
    s, ids = store
    res = api_update(s, ids["warm"], {
        "importance": 9.0, "tier": "pinned",
        "attribute": "user_profile", "tags": ["ui", "color"],
    })
    assert res["ok"]
    m = s.get_by_id(ids["warm"])
    assert m["importance"] == 9.0
    assert m["tier"] == "pinned"
    assert m["attribute"] == "user_profile"
    assert m["tags"] == "ui,color"


def test_update_validation(store):
    s, ids = store
    assert not api_update(s, ids["warm"], {"importance": 99})["ok"]
    assert not api_update(s, ids["warm"], {"tier": "bogus"})["ok"]
    assert not api_update(s, 9999, {"importance": 5})["ok"]
    # 校验失败后数据未变
    m = s.get_by_id(ids["warm"])
    assert m["importance"] == 6.0 and m["tier"] == "normal"


def test_archive_restore_delete_flow(store):
    s, ids = store
    # restore: archived -> active
    assert api_restore(s, ids["cold"])["ok"]
    assert s.get_by_id(ids["cold"])["status"] == "active"
    # archive: active -> archived
    assert api_archive(s, ids["warm"])["ok"]
    assert s.get_by_id(ids["warm"])["status"] == "archived"
    # delete: 软删
    assert api_delete(s, ids["hot"])["ok"]
    assert s.get_by_id(ids["hot"])["status"] == "deleted"
    # 软删后不出现在默认列表
    assert all(r["id"] != ids["hot"] for r in api_memories(s, {}))
    # 不存在的 id
    assert not api_archive(s, 9999)["ok"]
    assert not api_restore(s, 9999)["ok"]
    assert not api_delete(s, 9999)["ok"]


# ---- HTTP 端到端 ----

@pytest.fixture
def http_server(store):
    s, ids = store
    holder = {}
    ready = threading.Event()

    def serve():
        # sqlite 连接不能跨线程使用：服务线程内独立持有一个 store
        ts = MemoryStore(s.config)
        ts.initialize()
        srv = HTTPServer(("127.0.0.1", 0), make_handler(ts))
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()
        ts.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(timeout=5)
    yield f"http://127.0.0.1:{holder['srv'].server_address[1]}", ids
    holder["srv"].shutdown()
    holder["srv"].server_close()
    t.join(timeout=5)


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
    base, ids = http_server
    st = _get(base + "/api/stats")
    assert st["total_active"] == 2
    rows = _get(base + "/api/memories?status=active&sort=access_count&order=desc")
    assert rows[0]["id"] == ids["hot"]
    rows = _get(base + "/api/memories?q=%E7%B4%AB%E8%89%B2")  # q=紫色
    assert [r["id"] for r in rows] == [ids["warm"]]


def test_http_write_flow(http_server):
    base, ids = http_server
    assert _post(f"{base}/api/memory/{ids['warm']}/update",
                 {"importance": 8.0, "tier": "pinned"})["ok"]
    assert _post(f"{base}/api/memory/{ids['warm']}/archive")["ok"]
    rows = _get(base + "/api/memories?status=archived")
    assert any(r["id"] == ids["warm"] for r in rows)
    assert _post(f"{base}/api/memory/{ids['warm']}/restore")["ok"]
    assert _post(f"{base}/api/memory/{ids['cold']}/delete")["ok"]
    # 未知端点 / 错误 id 返回错误 JSON
    try:
        _post(f"{base}/api/memory/{ids['warm']}/nonsense")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_http_index_served(http_server):
    base, _ = http_server
    with urllib.request.urlopen(base + "/") as resp:
        html = resp.read().decode("utf-8")
    assert resp.headers.get_content_type() == "text/html"
    assert "EvolvMem" in html
