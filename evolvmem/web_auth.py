"""Optional Feishu login for the single-process Web console."""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

_AUTHORIZE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
_TOKEN = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_USER_INFO = "https://open.feishu.cn/open-apis/authen/v1/user_info"
_SESSION_COOKIE, _STATE_COOKIE = "evolvmem_session", "evolvmem_oauth_state"
_SESSION_SECONDS, _STATE_SECONDS = 8 * 60 * 60, 5 * 60
_LOGIN_PAGE = Path(__file__).parent / "web_static" / "login.html"
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthSettings:
    app_id: str
    app_secret: str = field(repr=False)
    owner_open_id: str = ""
    redirect_uri: str = ""
    tenant_key: str = ""
    owner_only: bool = False

    @classmethod
    def load(cls, data_dir: Path):
        explicit = os.environ.get("EVOLVMEM_WEB_AUTH_FILE")
        path = Path(explicit).expanduser() if explicit else data_dir / "web_auth.json"
        if not path.exists() and not explicit:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("无法读取 Web 登录配置，请检查 web_auth.json") from None
        if not isinstance(data, dict) or not isinstance(data.get("enabled", True), bool):
            raise ValueError("Web 登录配置 enabled 必须是布尔值")
        if data.get("enabled") is False:
            return None
        for key in ("app_id", "app_secret", "owner_open_id", "redirect_uri"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"Web 登录配置缺少 {key}")
        if not isinstance(data.get("tenant_key", ""), str) or not isinstance(data.get("owner_only", False), bool):
            raise ValueError("Web 登录配置 tenant_key / owner_only 类型无效")
        redirect = urlparse(data["redirect_uri"].strip())
        if (redirect.scheme not in ("http", "https") or not redirect.hostname or redirect.username
                or redirect.password or redirect.query or redirect.fragment or redirect.path != "/auth/feishu/callback"
                or any(c.isspace() for c in data["redirect_uri"].strip())):
            raise ValueError("redirect_uri 必须是完整的 HTTP(S) /auth/feishu/callback 地址")
        try:
            redirect.port
        except ValueError:
            raise ValueError("redirect_uri 端口无效") from None
        values = {key: data.get(key, "").strip() for key in ("app_id", "app_secret", "owner_open_id", "redirect_uri", "tenant_key")}
        values["owner_only"] = data.get("owner_only", False)
        return cls(**values)


class LoginError(Exception):
    pass


class WebAuth:
    def __init__(self, settings):
        self.settings, self.sessions, self.pending = settings, {}, {}

    @property
    def enabled(self):
        return self.settings is not None

    def _cookie(self, name, value, seconds):
        cookie = SimpleCookie(); cookie[name] = value
        cookie[name]["path"] = "/"; cookie[name]["httponly"] = True; cookie[name]["samesite"] = "Lax"; cookie[name]["max-age"] = str(seconds)
        if self.settings and self.settings.redirect_uri.startswith("https://"):
            cookie[name]["secure"] = True
        return "Set-Cookie", cookie[name].OutputString()

    @staticmethod
    def _cookie_value(handler, name):
        try:
            values = SimpleCookie(handler.headers.get("Cookie", ""))
            return values[name].value if name in values else ""
        except CookieError:
            return ""

    def _prune(self):
        now = time.time()
        for items in (self.sessions, self.pending):
            for key in [key for key, value in items.items() if value["expires"] <= now]:
                del items[key]

    def session(self, handler):
        self._prune()
        return self.sessions.get(self._cookie_value(handler, _SESSION_COOKIE))

    def can_write(self, session):
        return bool(session and session["open_id"] == self.settings.owner_open_id)

    @staticmethod
    def _redirect(handler, location, cookies=()):
        handler.send_response(302); handler.send_header("Location", location); handler.send_header("Content-Length", "0"); handler.send_header("Cache-Control", "no-store")
        for name, value in cookies: handler.send_header(name, value)
        handler.end_headers()

    @staticmethod
    def _login_page(handler, message="", status=200, cookies=()):
        page = _LOGIN_PAGE.read_text(encoding="utf-8")
        handler._send_html(page.replace("{{message}}", html.escape(message)), status, headers=cookies)

    @staticmethod
    def _json_request(request):
        stage, status = ("token" if request.full_url == _TOKEN else "user_info"), 0
        try:
            try: response = urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError as error: response = error
            with response:
                status, data = getattr(response, "status", 200), json.loads(response.read(1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError) as error:
            _LOG.warning("Feishu login failed: stage=%s http_status=%s error_type=%s", stage, status, type(error).__name__); raise LoginError() from None
        code = data.get("code") if isinstance(data, dict) else None
        if code != 0 or not 200 <= status < 300:
            _LOG.warning("Feishu login failed: stage=%s http_status=%s api_code=%s", stage, status, code if type(code) is int else "invalid"); raise LoginError()
        return data

    def _identity(self, code, verifier):
        values = {"grant_type": "authorization_code", "client_id": self.settings.app_id, "client_secret": self.settings.app_secret, "code": code, "redirect_uri": self.settings.redirect_uri, "code_verifier": verifier}
        token = self._json_request(urllib.request.Request(_TOKEN, data=json.dumps(values).encode(), headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"))
        access = token.get("access_token")
        if not isinstance(access, str) or not access or "\n" in access or "\r" in access:
            _LOG.warning("Feishu login failed: stage=token invalid_access_token"); raise LoginError()
        response = self._json_request(urllib.request.Request(_USER_INFO, headers={"Authorization": "Bearer " + access}))
        user = response.get("data")
        if not isinstance(user, dict) or not isinstance(user.get("open_id"), str) or not user["open_id"] or not isinstance(user.get("tenant_key"), str) or not user["tenant_key"]:
            _LOG.warning("Feishu login failed: stage=user_info missing_identity_fields"); raise LoginError()
        return user

    def handle_get(self, handler, parsed):
        path, session = parsed.path, self.session(handler) if self.enabled else None
        if path == "/api/auth/me":
            owner = self.can_write(session) if self.enabled else True
            handler._send_json({"enabled": self.enabled, "authenticated": bool(session) or not self.enabled, "can_write": owner, "role": "owner" if owner else "viewer" if session else None, "name": session["name"] if session else "本地使用" if not self.enabled else "", "csrf_token": session["csrf"] if session else ""}); return True
        if path in ("/login", "/auth/login", "/auth/feishu/callback") and not self.enabled:
            self._redirect(handler, "/"); return True
        if path == "/login":
            self._redirect(handler, "/") if session else self._login_page(handler); return True
        if path == "/auth/login":
            redirect = urlparse(self.settings.redirect_uri); default_port = 443 if redirect.scheme == "https" else 80
            try:
                requested = urlparse("//" + handler.headers.get("Host", "")); same_host = requested.hostname == redirect.hostname and (requested.port or default_port) == (redirect.port or default_port) and not requested.username and not requested.password
            except ValueError: same_host = False
            if not same_host:
                self._redirect(handler, f"{redirect.scheme}://{redirect.netloc}/auth/login"); return True
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self.pending[state] = {"verifier": verifier, "expires": time.time() + _STATE_SECONDS}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            query = urlencode({"client_id": self.settings.app_id, "response_type": "code", "redirect_uri": self.settings.redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            self._redirect(handler, _AUTHORIZE + "?" + query, [self._cookie(_STATE_COOKIE, state, _STATE_SECONDS)]); return True
        if path == "/auth/feishu/callback": self._callback(handler, parse_qs(parsed.query)); return True
        if self.enabled and not session:
            if path.startswith("/api/"): handler._send_json({"ok": False, "error": "login_required"}, 401)
            else: self._redirect(handler, "/login")
            return True
        if self.enabled and self.settings.owner_only and not self.can_write(session):
            if path.startswith("/api/"): handler._send_json({"ok": False, "error": "owner_only"}, 403)
            else: self._redirect(handler, "/login")
            return True
        return False

    def _callback(self, handler, query):
        states = query.get("state", []); state = states[0] if len(states) == 1 else ""; cookie = self._cookie_value(handler, _STATE_COOKIE)
        if not state or not cookie or not state.isascii() or not cookie.isascii() or not hmac.compare_digest(state, cookie) or state not in self.pending:
            self._login_page(handler, "登录请求已过期或不匹配，请重新登录。", 400); return
        pending = self.pending.pop(state); cookies = [self._cookie(_STATE_COOKIE, "", 0)]
        if "error" in query: self._login_page(handler, "你已取消飞书授权，可以重新登录。", 403, cookies); return
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0]: self._login_page(handler, "没有收到有效的授权码，请重新登录。", 400, cookies); return
        try: user = self._identity(codes[0], pending["verifier"])
        except LoginError: self._login_page(handler, "飞书登录暂未完成，请重新登录；持续失败时请联系应用管理员。", 502, cookies); return
        if (self.settings.tenant_key and user["tenant_key"] != self.settings.tenant_key) or (self.settings.owner_only and user["open_id"] != self.settings.owner_open_id):
            self._login_page(handler, "此飞书账号不允许访问该工作台。", 403, cookies); return
        self.sessions.pop(self._cookie_value(handler, _SESSION_COOKIE), None); session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = {"open_id": user["open_id"], "name": str(user.get("name") or "飞书用户")[:100], "csrf": secrets.token_urlsafe(32), "expires": time.time() + _SESSION_SECONDS}
        cookies.append(self._cookie(_SESSION_COOKIE, session_id, _SESSION_SECONDS)); self._redirect(handler, "/", cookies)

    def require_write(self, handler):
        if not self.enabled: return True
        session = self.session(handler)
        if not session: handler._send_json({"ok": False, "error": "login_required"}, 401)
        elif not self.can_write(session): handler._send_json({"ok": False, "error": "read_only"}, 403)
        elif not self._valid_csrf(handler, session): handler._send_json({"ok": False, "error": "invalid_csrf"}, 403)
        else: return True
        return False

    @staticmethod
    def _valid_csrf(handler, session):
        token = handler.headers.get("X-CSRF-Token", "")
        return bool(token and token.isascii() and hmac.compare_digest(token, session["csrf"]))

    def logout(self, handler):
        session = self.session(handler)
        if session and not self._valid_csrf(handler, session): handler._send_json({"ok": False, "error": "invalid_csrf"}, 403); return
        self.sessions.pop(self._cookie_value(handler, _SESSION_COOKIE), None); handler._send_json({"ok": True}, headers=[self._cookie(_SESSION_COOKIE, "", 0)])
