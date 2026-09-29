"""Exercise login and read/write permissions through real HTTP and a temporary DB.

Only Feishu's external endpoints are simulated. Removing the request guards,
trusting browser roles, or allowing a forged/replayed callback must fail here.
"""
import base64
import hashlib
import http.client
import io
import json
import logging
import threading
import urllib.error
from http.cookies import SimpleCookie
from http.server import HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from evolvmem.web_server import make_handler
from tests.test_web_server import _make_service, _seed


class Browser:
    def __init__(self, base):
        self.base = base
        self.cookies = {}

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(urlparse(self.base).netloc, timeout=5)
        fields = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        if body is not None:
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=fields)
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        response_headers = response.getheaders()
        for key, value in response_headers:
            if key.lower() == "set-cookie":
                for name, cookie in SimpleCookie(value).items():
                    if cookie["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = cookie.value
        status = response.status
        connection.close()
        return status, dict(response_headers), data

    def begin_login(self):
        status, headers, _ = self.request("/auth/login")
        assert status == 302
        return parse_qs(urlparse(headers["Location"]).query)

    def login(self, account="owner"):
        query = self.begin_login()
        status, headers, _ = self.request("/auth/feishu/callback?" + urlencode({
            "code": account, "state": query["state"][0]}))
        assert status == 302 and headers["Location"] == "/"
        status, _, body = self.request("/api/auth/me")
        assert status == 200
        return json.loads(body)


@pytest.fixture
def protected_web(test_config, monkeypatch, request):
    import urllib.request

    exchanges = []
    verifier_challenges = []

    def feishu(request, timeout=None):
        if request.full_url == "https://accounts.feishu.cn/oauth/v3/token":
            # Reproduce the real v3 PKCE failure observed on 2026-09-07.
            body = io.BytesIO(json.dumps({"code": 20049, "error": "invalid_grant",
                "error_description": "PKCE code challenge failed."}).encode())
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)
        if request.full_url == "https://open.feishu.cn/open-apis/authen/v2/oauth/token":
            assert request.get_method() == "POST"
            assert request.get_header("Content-type").startswith("application/json")
            values = json.loads(request.data)
            assert values["client_id"] == "cli_test"
            assert values["client_secret"] == "test-secret-never-send-to-browser"
            assert values["grant_type"] == "authorization_code"
            assert values["redirect_uri"].endswith("/auth/feishu/callback")
            verifier_challenges.append(base64.urlsafe_b64encode(hashlib.sha256(
                values["code_verifier"].encode()).digest()).rstrip(b"=").decode())
            exchanges.append(values["code"])
            if values["code"] == "failed":
                return io.BytesIO(json.dumps({"code": 20003, "error": "invalid_grant",
                    "error_description": "test-secret-never-send-to-browser"}).encode())
            payload = {"code": 0, "access_token": "u-" + values["code"],
                       "expires_in": 7200, "token_type": "Bearer", "scope": ""}
        elif request.full_url == "https://open.feishu.cn/open-apis/authen/v1/user_info":
            account = request.get_header("Authorization").removeprefix("Bearer u-")
            payload = {"code": 0, "msg": "success", "data": {
                "open_id": "ou_owner" if account in ("owner", "foreign") else "ou_viewer",
                "tenant_key": "other-tenant" if account == "foreign" else "our-tenant",
                "name": "本人" if account == "owner" else "访客",
                "avatar_url": "https://example.com/avatar.png"}}
        else:
            raise AssertionError("Unexpected external request")
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", feishu)
    service = _make_service(test_config)
    ids = _seed(service.legacy_facade())
    holder = {}
    ready = threading.Event()

    def serve():
        inner = _make_service(test_config)
        server = HTTPServer(("127.0.0.1", 0), make_handler(inner))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        config = {"enabled": True, "app_id": "cli_test",
                  "app_secret": "test-secret-never-send-to-browser",
                  "owner_open_id": "ou_owner", "tenant_key": "our-tenant",
                  "owner_only": getattr(request, "param", False),
                  "redirect_uri": base + "/auth/feishu/callback"}
        (test_config.data_dir / "web_auth.json").write_text(json.dumps(config))
        server.RequestHandlerClass = make_handler(inner)
        holder.update(server=server, base=base)
        ready.set()
        try:
            server.serve_forever(poll_interval=0.01)
        finally:
            inner.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield holder["base"], ids, service, exchanges, verifier_challenges
    holder["server"].shutdown()
    holder["server"].server_close()
    thread.join(5)
    service.close()


def test_anonymous_cannot_read_data_or_mutate(protected_web):
    base, ids, service, _, _ = protected_web
    browser = Browser(base)
    status, headers, _ = browser.request("/")
    assert status == 302 and headers["Location"] == "/login"
    for path in ("/api/stats", "/api/memories", "/api/insights", "/api/projects"):
        assert browser.request(path)[0] == 401
    assert browser.request(f"/api/memory/{ids['warm']}/delete", "POST")[0] == 401
    assert service.legacy_facade().get_by_id(ids["warm"])["status"] == "active"
    status, _, body = browser.request("/login")
    assert status == 200 and "飞书登录" in body


def test_owner_login_retains_editing_and_logout_revokes_session(protected_web):
    base, ids, service, _, _ = protected_web
    browser = Browser(base)
    me = browser.login()
    assert me["can_write"] is True and me["role"] == "owner"
    assert me["name"] == "本人"
    assert "test-secret" not in json.dumps(me) and "access_token" not in me
    headers = {"X-CSRF-Token": me["csrf_token"]}
    assert browser.request(f"/api/memory/{ids['warm']}/update", "POST",
                           {"importance": 9.0}, headers)[0] == 200
    assert service.legacy_facade().get_by_id(ids["warm"])["importance"] == 9.0
    copied_session = dict(browser.cookies)
    assert browser.request("/auth/logout", "POST", headers=headers)[0] == 200
    browser.cookies = copied_session
    assert browser.request("/api/stats")[0] == 401


def test_viewer_reads_same_data_but_all_post_routes_are_forbidden(protected_web):
    base, ids, service, exchanges, _ = protected_web
    browser = Browser(base)
    me = browser.login("viewer")
    assert me["role"] == "viewer" and me["can_write"] is False
    status, _, body = browser.request("/api/memories")
    assert status == 200 and any(row["id"] == ids["warm"] for row in json.loads(body)["rows"])
    paths = [f"/api/memory/{ids['warm']}/{action}" for action in (
        "update", "archive", "restore", "delete", "hard_delete")]
    item_id = service.store.resolve_legacy_mapping(ids["warm"])
    paths += [f"/api/resolution/{item_id}/{action}" for action in ("accept", "reject")]
    paths += ["/api/resolutions/batch_accept", "/api/projects/register",
              "/api/projects/display_name", "/api/projects/display_names",
              "/api/projects/suggest_display_names", "/api/memories/organize_suggest",
              "/api/future-write-route"]
    before = service.legacy_facade().get_by_id(ids["warm"])
    for path in paths:
        status, _, body = browser.request(path, "POST", {"importance": 1}, {
            "X-CSRF-Token": me["csrf_token"], "X-EvolvMem-Role": "owner"})
        assert status == 403, (path, body)
    assert service.legacy_facade().get_by_id(ids["warm"]) == before
    assert exchanges == ["viewer"]
    assert browser.request("/auth/logout", "POST", headers={"X-CSRF-Token": me["csrf_token"]})[0] == 200


@pytest.mark.parametrize("protected_web", [True], indirect=True)
def test_owner_only_rejects_viewer_callback_and_keeps_owner_personal_data(protected_web):
    base, ids, service, exchanges, _ = protected_web
    owner = Browser(base)
    me = owner.login("owner")
    assert me["can_write"] is True
    assert owner.request("/api/stats")[0] == 200
    assert owner.request(f"/api/memory/{ids['warm']}/update", "POST",
                         {"importance": 9}, {"X-CSRF-Token": me["csrf_token"]})[0] == 200
    viewer = Browser(base)
    query = viewer.begin_login()
    status, _, _ = viewer.request("/auth/feishu/callback?" + urlencode({
        "code": "viewer", "state": query["state"][0]}))
    assert status == 403
    for path in ("/api/stats", "/api/memories", "/api/insights", "/api/projects"):
        assert viewer.request(path)[0] == 401
    assert exchanges == ["owner", "viewer"]
    assert service.legacy_facade().get_by_id(ids["warm"])["importance"] == 9.0


def test_owner_write_requires_session_csrf_token(protected_web):
    base, ids, service, _, _ = protected_web
    browser = Browser(base)
    browser.login()
    path = f"/api/memory/{ids['warm']}/delete"
    for headers in ({}, {"X-CSRF-Token": "forged"}):
        assert browser.request(path, "POST", headers=headers)[0] == 403
    assert service.legacy_facade().get_by_id(ids["warm"])["status"] == "active"


def test_callback_is_browser_bound_one_time_and_uses_pkce(protected_web):
    base, _, _, exchanges, challenges = protected_web
    browser = Browser(base)
    query = browser.begin_login()
    assert query["client_id"] == ["cli_test"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query and "offline_access" not in query.get("scope", [])
    callback = "/auth/feishu/callback?" + urlencode({"state": query["state"][0], "code": "owner"})
    assert Browser(base).request(callback)[0] == 400
    assert exchanges == []
    status, headers, _ = browser.request(callback)
    assert status == 302 and challenges == query["code_challenge"]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
    assert browser.request(callback)[0] == 400
    assert exchanges == ["owner"]


def test_login_pkce_matches_rfc7636_s256_vector(protected_web, monkeypatch):
    # RFC 7636 Appendix B: a fixed external vector, independent of our encoder.
    base, _, _, _, challenges = protected_web
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    monkeypatch.setattr("evolvmem.web_auth.secrets.token_urlsafe",
                        lambda length: verifier if length == 48 else "rfc-test-state")
    browser = Browser(base)
    query = browser.begin_login()
    assert query["code_challenge"] == [expected]
    status, _, _ = browser.request("/auth/feishu/callback?" + urlencode({
        "state": query["state"][0], "code": "owner"}))
    assert status == 302 and challenges == [expected]


@pytest.mark.parametrize("account,status", [("foreign", 403), ("failed", 502)])
def test_invalid_identity_and_provider_failure_do_not_issue_session(protected_web, account, status):
    base, _, _, _, _ = protected_web
    browser = Browser(base)
    query = browser.begin_login()
    actual, _, body = browser.request("/auth/feishu/callback?" + urlencode({
        "state": query["state"][0], "code": account}))
    assert actual == status
    assert "test-secret" not in body
    assert browser.request("/api/stats")[0] == 401


def test_missing_owner_configuration_fails_closed(test_config):
    service = _make_service(test_config)
    (test_config.data_dir / "web_auth.json").write_text(json.dumps({
        "enabled": True, "app_id": "cli_test", "app_secret": "secret",
        "redirect_uri": "http://localhost:9377/auth/feishu/callback"}))
    try:
        with pytest.raises(ValueError, match="owner_open_id"):
            make_handler(service)
    finally:
        service.close()


def test_malformed_state_is_rejected_without_consuming_valid_login(protected_web):
    base, _, _, exchanges, _ = protected_web
    browser = Browser(base)
    query = browser.begin_login()
    malformed = "/auth/feishu/callback?" + urlencode({"state": "不匹配", "code": "owner"})
    assert browser.request(malformed)[0] == 400
    assert exchanges == []
    valid = "/auth/feishu/callback?" + urlencode({"state": query["state"][0], "code": "owner"})
    assert browser.request(valid)[0] == 302


@pytest.mark.parametrize("phase,seconds", [("state", 301), ("session", 28801)])
def test_expired_login_state_or_session_requires_login(protected_web, monkeypatch, phase, seconds):
    from evolvmem import web_auth
    base, _, _, _, _ = protected_web
    browser = Browser(base)
    if phase == "session":
        browser.login()
    else:
        query = browser.begin_login()
    now = web_auth.time.time()
    monkeypatch.setattr(web_auth.time, "time", lambda: now + seconds)
    if phase == "state":
        callback = "/auth/feishu/callback?" + urlencode({"state": query["state"][0], "code": "owner"})
        assert browser.request(callback)[0] == 400
    assert browser.request("/api/stats")[0] == 401


def test_forged_session_and_unsupported_write_methods_cannot_change_data(protected_web):
    base, ids, service, _, _ = protected_web
    browser = Browser(base)
    browser.cookies["evolvmem_session"] = "owner"
    assert browser.request("/api/stats")[0] == 401
    me = browser.login("viewer")
    for method in ("PUT", "PATCH", "DELETE"):
        assert browser.request(f"/api/memory/{ids['warm']}/delete", method,
                               headers={"X-CSRF-Token": me["csrf_token"]})[0] == 403
    assert service.legacy_facade().get_by_id(ids["warm"])["status"] == "active"


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_default_port_in_callback_does_not_loop_login_redirect(scheme, port):
    from evolvmem.web_auth import AuthSettings, WebAuth

    class Response:
        headers = {"Host": "memory.local"}

        def __init__(self):
            self.sent = {}

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.sent[name] = value

        def end_headers(self):
            pass

    auth = WebAuth(AuthSettings("cli_test", "secret", "ou_owner",
                               f"{scheme}://memory.local:{port}/auth/feishu/callback"))
    handler = Response()
    assert auth.handle_get(handler, urlparse("/auth/login"))
    assert handler.status == 302
    assert handler.sent["Location"].startswith("https://accounts.feishu.cn/")


@pytest.mark.parametrize("http_status", [200, 400])
def test_provider_failure_logs_stage_and_code_without_credentials(monkeypatch, caplog, http_status):
    from evolvmem.web_auth import AuthSettings, LoginError, WebAuth

    private = "credential-must-not-appear-in-logs"
    payload = {"code": 20049, "error": "invalid_grant", "error_description": private,
               "access_token": private}

    def failed(request, timeout=None):
        stream = io.BytesIO(json.dumps(payload).encode())
        if http_status == 400:
            raise urllib.error.HTTPError(request.full_url, 400, private, {}, stream)
        return stream

    monkeypatch.setattr("urllib.request.urlopen", failed)
    auth = WebAuth(AuthSettings("cli_test", private, "ou_owner",
                               "http://localhost:9377/auth/feishu/callback"))
    with caplog.at_level(logging.WARNING), pytest.raises(LoginError):
        auth._identity(private, private)
    assert "stage=token" in caplog.text and "api_code=20049" in caplog.text
    assert private not in caplog.text
