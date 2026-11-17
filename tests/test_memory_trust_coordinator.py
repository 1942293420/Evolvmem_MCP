"""Independent historical decision probes with synthetic stores only."""
import pytest
import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
from evolvmem.context_models import ContextContentType
from evolvmem.trust_acceptance import _Env, _item, _register, _review, _search, _supersede


@pytest.fixture
def env(tmp_path):
    instance = _Env(tmp_path)
    _register(instance, "evolvmem")
    yield instance
    instance.close()


def test_historical_overlap_uses_latest_applicable_decision(env):
    key = "project:evolvmem:decision:overlap"
    old = _item(env, key, project="evolvmem", l0="zebra 原决定", content_type=ContextContentType.DECISION,
                effective_from="2026-01-01", effective_until="2026-12-31")
    new = _supersede(env, key, project="evolvmem", l0="zebra 新决定", effective_from="2026-06-01")
    assert [r.id for r in _search(env, "zebra", "evolvmem", as_of="2026-08-01")] == [new.id]
    assert env.store.get_item(old.id, include_layers=False).effective_until == "2026-12-31 00:00:00"


def test_delayed_intermediate_decision_orders_by_effective_time(env):
    key = "project:evolvmem:decision:backfill"
    _item(env, key, project="evolvmem", l0="zebra 最早决定", content_type=ContextContentType.DECISION,
          effective_from="2025-01-01")
    current = _supersede(env, key, project="evolvmem", l0="zebra 当前决定", effective_from="2026-01-01")
    late = _supersede(env, key, project="evolvmem", l0="zebra 迟到的中间决定", effective_from="2025-03-01")
    assert [r.id for r in _search(env, "zebra", "evolvmem", as_of="2025-04-01")] == [late.id]
    assert [r.id for r in _search(env, "zebra", "evolvmem", as_of="2026-05-01")] == [current.id]


def test_historical_winner_is_independent_of_matching_wording(env):
    key = "project:evolvmem:decision:wording"
    _item(env, key, project="evolvmem", l0="zebra 原决定", l1="zebra 旧文字",
          content_type=ContextContentType.DECISION, effective_from="2026-01-01", effective_until="2026-12-31")
    _supersede(env, key, project="evolvmem", l0="lion 新决定", effective_from="2026-06-01")
    assert _search(env, "zebra", "evolvmem", as_of="2026-08-01") == ()


def test_delayed_historical_winner_is_independent_of_matching_wording(env):
    key = "project:evolvmem:decision:delayed-wording"
    _item(env, key, project="evolvmem", l0="zebra 最早决定", l1="zebra 旧文字",
          content_type=ContextContentType.DECISION, effective_from="2025-01-01")
    _supersede(env, key, project="evolvmem", l0="lion 当前决定", effective_from="2026-01-01")
    _supersede(env, key, project="evolvmem", l0="otter 迟到的中间决定", effective_from="2025-03-01")
    assert _search(env, "zebra", "evolvmem", as_of="2025-04-01") == ()


@pytest.fixture
def http_review(tmp_path):
    from evolvmem.web_server import make_handler
    ready = threading.Event()
    holder = {}
    def serve():
        instance = _Env(tmp_path)
        _register(instance, "evolvmem")
        instance.service._legacy_backend()
        first = _item(instance, "project:evolvmem:fact:reject", project="evolvmem", l0="待拒绝归属")
        second = _item(instance, "project:evolvmem:fact:confirm", project="evolvmem", l0="待确认归属")
        server = HTTPServer(("127.0.0.1", 0), make_handler(instance.service))
        holder.update(server=server, ids=(first.id, second.id), env=instance)
        ready.set()
        server.serve_forever()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(8)
    yield f"http://127.0.0.1:{holder['server'].server_port}", holder['ids']
    holder['server'].shutdown()
    holder['server'].server_close()
    thread.join(5)
    holder['env'].close()


def _http_json(base, path, body=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=4)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_real_http_review_matches_ui_routes_and_removes_queue_rows(http_review):
    base, (rejected, confirmed) = http_review
    status, payload = _http_json(base, f"/api/resolutions/{rejected}/reject", {"expected_revision": 0})
    assert status == 200 and payload["ok"]
    status, payload = _http_json(base, f"/api/resolutions/{confirmed}/accept", {"expected_revision": 0, "project": "evolvmem"})
    assert status == 200 and payload["ok"]
    _, page = _http_json(base, "/api/knowledge?project=evolvmem")
    assert page["open_issues"]["review_backlog"]["total"] == 0
    status, payload = _http_json(base, f"/api/resolutions/{rejected}/reject", {"expected_revision": 0})
    assert status == 400 and payload["error"] == "revision_conflict"
    # Existing singular callers retain their route.
    status, payload = _http_json(base, f"/api/resolution/{confirmed}/accept", {"expected_revision": 1, "project": "evolvmem"})
    assert status == 200 and payload["ok"]


def test_unreviewed_api_lists_held_facts_as_well_as_checkpoints(http_review):
    base, ids = http_review
    status, rows = _http_json(base, "/api/resolutions?state=unreviewed")
    assert status == 200
    assert set(ids).issubset({row["item_id"] for row in rows})


def test_project_knowledge_review_backlog_does_not_mix_other_projects(env):
    # Primary approved goal: an EvolvMem project page must not include unrelated
    # inventory progress/review rows. A global review API can still expose them.
    from evolvmem.trust_views import TrustViews
    _register(env, "inventory")
    unrelated = _item(env, "project:inventory:fact:pending", project="inventory", l0="库存待审核事实")
    _review(env, unrelated.id, state="conflict", review="")
    page = TrustViews(env.service).knowledge("evolvmem")
    assert unrelated.id not in {row["item_id"] for row in page["open_issues"]["review_backlog"]["items"]}
