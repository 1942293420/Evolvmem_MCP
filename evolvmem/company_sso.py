"""Web-only company admission; native EvolvMem authorization remains authoritative.

No OAuth credentials, persistent sessions, account provisioning, or library grants.
Enabled explicitly by the Web unit; the independent MCP server never imports it.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from evolvmem.web_auth import WebAuth

_CENTRAL_ORIGIN = 'http://127.0.0.1:9600'
_APP_ID = 'cli_aaa39bd67b7c9ceb'
_LIMIT = 16384
_DEADLINE = 2.0
_COOKIE_TOKEN = re.compile(r'^[A-Za-z0-9_.~\-]{1,512}$')


class AdmissionError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _safe_next(query):
    values = parse_qs(query, keep_blank_values=True)
    if set(values) - {'next'} or len(values.get('next', ['/'])) != 1:
        raise AdmissionError('invalid_next', 400)
    target = values.get('next', ['/'])[0]
    if not target or len(target) > 2048:
        raise AdmissionError('invalid_next', 400)
    # Reject ambiguous encoded paths at every decode layer, including browsers'
    # backslash interpretation and an encoded second leading slash.
    check = target
    for _ in range(4):
        if (not check.startswith('/') or check.startswith('//') or '\\' in check
                or any(ord(c) < 32 or ord(c) == 127 for c in check)):
            raise AdmissionError('invalid_next', 400)
        parsed = urlparse(check)
        if parsed.scheme or parsed.netloc:
            raise AdmissionError('invalid_next', 400)
        decoded = unquote(check)
        if decoded == check:
            break
        check = decoded
    else:
        raise AdmissionError('invalid_next', 400)
    if urlparse(target).path in ('/login', '/auth/login', '/auth/feishu/callback'):
        raise AdmissionError('invalid_next', 400)
    return target


class CompanyAuth(WebAuth):
    """Request-scoped adapter for the existing WebAuth/ConsoleService contract."""

    def __init__(self, settings):
        if settings is None or settings.app_id != _APP_ID:
            raise ValueError('company_sso_requires_existing_same_app_and_origin')
        super().__init__(settings)
        trusted = urlparse(settings.redirect_uri)
        self.system_origin = f'{trusted.scheme}://{trusted.netloc}'
        public = os.environ.get('EVOLVMEM_COMPANY_SSO_PUBLIC_ORIGIN', 'http://localhost:9600')
        parsed = urlparse(public)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or any(c.isspace() for c in public)):
            raise ValueError('invalid_company_sso_public_origin')
        try:
            parsed.port
        except ValueError:
            raise ValueError('invalid_company_sso_public_origin') from None
        self.central_browser_origin = public
        self.system_id = hashlib.sha256(self.system_origin.encode()).hexdigest()[:16]
        self.native_status = None
        self._memory_key = secrets.token_bytes(32)

    def _opaque(self, label, value):
        return hmac.new(self._memory_key, (label + ':' + value).encode(), hashlib.sha256).hexdigest()

    def _headers(self, handler):
        hosts = handler.headers.get_all('Host', [])
        origins = handler.headers.get_all('Origin', [])
        cookies = handler.headers.get_all('Cookie', [])
        csrf = handler.headers.get_all('X-CSRF-Token', [])
        if (hosts != [urlparse(self.system_origin).netloc] or len(origins) > 1
                or len(cookies) > 1 or len(csrf) > 1):
            raise AdmissionError('invalid_request_headers', 400)
        if origins and origins[0] != self.system_origin:
            raise AdmissionError('invalid_origin', 403)
        raw = cookies[0] if cookies else ''
        if len(raw) > _LIMIT:
            raise AdmissionError('invalid_cookie', 400)
        tokens = []
        for part in raw.split(';'):
            name, separator, value = part.strip().partition('=')
            if name == 'ops_session':
                if not separator or not _COOKIE_TOKEN.fullmatch(value):
                    raise AdmissionError('invalid_cookie', 400)
                tokens.append(value)
        if len(tokens) > 1:
            raise AdmissionError('invalid_cookie', 400)
        return tokens[0] if tokens else ''

    @staticmethod
    def _request(path, token, deadline, method='GET', csrf=''):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionError('sso_unavailable', 503)
        parsed = urlparse(_CENTRAL_ORIGIN)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise AdmissionError('sso_unavailable', 503)
        connection = http.client.HTTPConnection('127.0.0.1', parsed.port or 80, timeout=remaining)
        timer = None
        active_socket = None
        try:
            connection.connect()
            # HTTP/1.0 may detach/close HTTPConnection.sock after the headers.
            # A duplicate descriptor keeps the deadline able to interrupt both
            # a drip-fed header and a body even after that ownership change.
            active_socket = connection.sock.dup()

            def expire():
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            headers = {'Accept': 'application/json', 'Cookie': 'ops_session=' + token}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            if method == 'POST':
                headers['Content-Type'] = 'application/json'
            # Direct loopback HTTPConnection: no environment proxy or redirects.
            connection.request(method, path, body=b'{}' if method == 'POST' else None, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise AdmissionError('authentication_required' if response.status == 401 else 'system_access_denied', response.status)
            if response.status != 200:
                raise ValueError('upstream_status')
            payload = bytearray()
            while True:
                block = response.read1(min(4096, _LIMIT + 1 - len(payload)))
                if time.monotonic() >= deadline:
                    raise ValueError('deadline')
                if not block:
                    break
                payload.extend(block)
                if len(payload) > _LIMIT:
                    raise ValueError('upstream_size')
            result = json.loads(payload, object_pairs_hook=_unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
            if not isinstance(result, dict):
                raise ValueError('upstream_json')
            return result
        except AdmissionError:
            raise
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            raise AdmissionError('sso_unavailable', 503) from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()
            if active_socket is not None:
                active_socket.close()

    def _identity(self, token, deadline):
        if not token:
            raise AdmissionError('authentication_required', 401)
        decision = self._request('/api/access/check?' + urlencode({'system': self.system_id}), token, deadline)
        if decision.get('allowed') is not True:
            raise AdmissionError('system_access_denied', 403)
        data = self._request('/api/auth/status', token, deadline)
        user = data.get('user')
        if data.get('authenticated') is not True or not isinstance(user, dict):
            raise AdmissionError('authentication_required', 401)
        if any(not isinstance(user.get(key), str) or not user[key] or len(user[key]) > 512
               or any(ord(c) < 32 for c in user[key]) for key in ('open_id', 'name', 'csrf')):
            raise AdmissionError('sso_unavailable', 503)
        return {'open_id': user['open_id'], 'name': user['name'][:100],
                'csrf': self._opaque('csrf', token + ':' + user['open_id']),
                '_central_csrf': user['csrf']}

    def session(self, handler):
        return getattr(handler, '_company_identity', None)

    def handle_get(self, handler, parsed):
        # Admission and auth routes have already run before every router.
        return False

    def _status(self, handler, identity, native):
        handler._send_json({'enabled': True, 'central_sso': True, 'authenticated': True,
            'name': identity['name'], 'csrf_token': identity['csrf'],
            'user_scope': hashlib.sha256((self.system_origin + ':' + identity['open_id']).encode()).hexdigest(),
            'can_write': native == 'ready' and self.can_write(identity),
            'native_status': native, 'login_url': '/auth/login',
            'central_console_url': self.central_browser_origin})

    def _logout(self, handler, identity, token, deadline):
        result = self._request('/api/ops/logout', token, deadline, 'POST', identity['_central_csrf'])
        if result.get('ok') is not True:
            raise AdmissionError('sso_unavailable', 503)
        handler._send_json({'ok': True, 'authenticated': False}, headers=[
            self._cookie(name, '', 0) for name in ('ops_session', 'evolvmem_session', 'evolvmem_oauth_state')])

    def admit(self, handler, parsed):
        """Return True after an auth response/denial, before data router access."""
        handler._company_identity = None
        try:
            token = self._headers(handler)
            path, method = parsed.path, handler.command
            status_path = path in ('/auth/ops/status', '/api/auth/me')
            auth_path = status_path or path in ('/login', '/auth/login', '/auth/feishu/callback')
            if method in ('HEAD', 'OPTIONS') or (auth_path and method != 'GET') or (path == '/auth/logout' and method != 'POST'):
                raise AdmissionError('method_not_allowed', 405)
            if path == '/login':
                target = _safe_next(parsed.query)
                href = '/auth/login?' + urlencode({'next': target})
                nonce = secrets.token_urlsafe(18)
                script_target = json.dumps(target).replace('<', '\\u003c')
                # URL fragments are not sent to HTTP servers. Browsers retain
                # the original fragment through the local login redirect; add
                # it to the safe relative return immediately before clicking.
                script = ('const target=' + script_target + ';'
                          'const next=target.includes("#")?target:target+location.hash;'
                          'document.getElementById("company-login").href="/auth/login?"+'
                          'new URLSearchParams({next}).toString();')
                handler._send_html('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>EvolvMem 登录</title><main><h1>EvolvMem</h1><p>使用公司飞书账号登录</p><a id="company-login" href="' + html.escape(href, quote=True) + '">飞书登录</a></main><script nonce="' + nonce + '">' + script + '</script></html>', headers=[('Content-Security-Policy', "default-src 'none'; script-src 'nonce-" + nonce + "'; base-uri 'none'; frame-ancestors 'none'")])
                return True
            if path == '/auth/feishu/callback':
                self._redirect(handler, '/login', [self._cookie('evolvmem_oauth_state', '', 0)])
                return True
            target = _safe_next(parsed.query) if path == '/auth/login' else None
            deadline = time.monotonic() + _DEADLINE
            try:
                identity = self._identity(token, deadline)
            except AdmissionError as exc:
                if exc.status == 401 and path == '/auth/login':
                    self._redirect(handler, self.central_browser_origin + '/api/ops/login?' + urlencode({'next': self.system_origin + target}))
                    return True
                raise
            handler._company_identity = identity
            # Only this explicit login action may observe a new native pending
            # account. Status/static/API polling never creates native state.
            if path == '/auth/login':
                try:
                    self.login_observer(identity['open_id'], identity['name'])
                except Exception:
                    raise AdmissionError('pg_control_unavailable', 503) from None
            native = self.native_status(identity['open_id'])
            if status_path:
                self._status(handler, identity, native)
                return True
            if path == '/auth/login':
                if native == 'disabled' or not self.session_validator(identity['open_id']):
                    raise AdmissionError('account_disabled' if native == 'disabled' else 'account_pending', 403)
                self._redirect(handler, target)
                return True
            if method not in ('GET', 'HEAD', 'OPTIONS'):
                if handler.headers.get('Origin') != self.system_origin:
                    raise AdmissionError('invalid_origin', 403)
                if not self._valid_csrf(handler, identity):
                    raise AdmissionError('invalid_csrf', 403)
            if path == '/auth/logout':
                self._logout(handler, identity, token, deadline)
                return True
            if path.startswith('/api/') and native != 'ready':
                raise AdmissionError('account_' + native, 403)
            if self.settings.owner_only and not self.can_write(identity):
                raise AdmissionError('owner_only', 403)
            return False
        except AdmissionError as exc:
            # JSON keeps every failure usable by status polling; pages go to a
            # public local login page only when the parent session is absent.
            if exc.status == 401 and handler.command == 'GET' and not parsed.path.startswith(('/api/', '/auth/')):
                self._redirect(handler, '/login?' + urlencode({'next': handler.path}))
            else:
                handler._send_json({'ok': False, 'central_sso': True, 'authenticated': False, 'error': exc.code}, exc.status)
            return True
