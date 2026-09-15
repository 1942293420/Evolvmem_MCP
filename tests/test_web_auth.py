"""Real HTTP checks for the optional owner-only Web boundary."""
import io
import json
import threading
import urllib.request
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from evolvmem.web_server import make_handler
from tests.test_web_server import _make_service, _seed


class Browser:
    def __init__(self, base):
        self.base, self.cookies = base, {}

    def request(self, path, method="GET", body=None, headers=None):
        connection = HTTPConnection(urlparse(self.base).netloc, timeout=5)
        request_headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        request_headers.update(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, json.dumps(body) if body is not None else None, request_headers)
        response = connection.getresponse()
        payload, response_headers = response.read().decode(), response.getheaders()
        for name, value in response_headers:
            if name.lower() == "set-cookie":
                for key, cookie in SimpleCookie(value).items():
                    if cookie["max-age"] == "0":
                        self.cookies.pop(key, None)
                    else:
                        self.cookies[key] = cookie.value
        status = response.status
        connection.close()
        return status, dict(response_headers), payload

    def login(self, account):
        status, headers, _ = self.request("/auth/login")
        assert status == 302
        state = parse_qs(urlparse(headers["Location"]).query)["state"][0]
        return self.request("/auth/feishu/callback?" + urlencode({"state": state, "code": account}))


@pytest.fixture
def protected_web(test_config, monkeypatch, request):
    owner_only = getattr(request, "param", True)
    def feishu(request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            account = json.loads(request.data)["code"]
            return io.BytesIO(json.dumps({"code": 0, "access_token": "token-" + account}).encode())
        account = request.get_header("Authorization").removeprefix("Bearer token-")
        return io.BytesIO(json.dumps({"code": 0, "data": {"open_id": "ou_owner" if account == "owner" else "ou_viewer", "tenant_key": "tenant", "name": account}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", feishu)
    service = _make_service(test_config)
    ids = _seed(service.legacy_facade())
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    base = f"http://127.0.0.1:{server.server_port}"
    (test_config.data_dir / "web_auth.json").write_text(json.dumps({
        "enabled": True, "app_id": "test", "app_secret": "private",
        "owner_open_id": "ou_owner", "tenant_key": "tenant",
        "redirect_uri": base + "/auth/feishu/callback", "owner_only": owner_only,
    }))
    # AuthSettings is loaded when the handler is constructed, after the port is known.
    server.RequestHandlerClass = make_handler(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base, ids
    server.shutdown(); thread.join(5); server.server_close(); service.close()


def test_owner_only_web_policy_keeps_owner_access_and_rejects_viewer_reads(protected_web):
    base, ids = protected_web
    owner = Browser(base)
    assert owner.login("owner")[0] == 302
    assert "/auth.js" in owner.request("/")[2]
    status, _, me = owner.request("/api/auth/me")
    assert status == 200 and json.loads(me)["can_write"] is True
    assert owner.request("/api/stats")[0] == 200
    csrf = json.loads(me)["csrf_token"]
    assert owner.request(f"/api/memory/{ids['warm']}/update", "POST", {"importance": 9}, {"X-CSRF-Token": csrf})[0] == 200

    viewer = Browser(base)
    assert viewer.login("viewer")[0] == 403
    for path in ("/api/stats", "/api/memories", "/api/insights", "/api/projects"):
        assert viewer.request(path)[0] == 401


@pytest.mark.parametrize("protected_web", [False], indirect=True)
def test_default_web_policy_keeps_authenticated_viewer_reads(protected_web):
    base, _ = protected_web
    viewer = Browser(base)
    assert viewer.login("viewer")[0] == 302
    assert viewer.request("/api/stats")[0] == 200
