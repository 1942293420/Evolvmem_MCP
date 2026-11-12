"""Independent real HTTP session tests; only Feishu endpoints are simulated.

Every handler generation constructs a new WebAuth with an empty session dict.
No cookie/session seed is copied into the new object. Synthetic knowledge data
is built by the existing console fixture's source/history constructors.
"""
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import pytest

from tests.test_web_auth import Browser
from tests.browser import launch_console_fixture as synthetic

PREFIX = '/api/knowledge-console'
OWNER_ID = 'test-owner-open-id'
MEMBER_ID = 'test-member-open-id'
PENDING_ID = 'test-pending-open-id-1'
TENANT = 'synthetic-fixture-tenant'
COOKIE = 'evolvmem_session'


class ConsoleBrowser(Browser):
    def request(self, path, method='GET', body=None, headers=None):
        return super().request(path, method, body, {'Host': 'fixture.test', **(headers or {})})


class LoginFixture:
    def __init__(self, directory, monkeypatch):
        from evolvmem import web_server, web_auth
        from evolvmem.knowledge_console import ConsoleService, HISTORY_KEYS
        self.directory = Path(directory)
        self.directory.mkdir()
        self.clock = [1_800_000_000.0]
        monkeypatch.setattr(time, 'time', lambda: self.clock[0])
        self.owner, self.experience_id = synthetic._source(self.directory / 'owner', 'Owner')
        member, _ = synthetic._source(self.directory / 'lan' / 'users' / 'kane', 'Member')
        member.close()
        env, self.image_sha, self.image = synthetic._history(self.directory / 'history', 'Owner')
        policy = self.directory / 'policy.json'
        policy.write_text(json.dumps({'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False}))
        self.registry_path = self.directory / 'metadata' / 'users.db'
        for key, value in {'EVOLVMEM_PRODUCT_POLICY_FILE': str(policy),
                           'EVOLVMEM_CONSOLE_REGISTRY': str(self.registry_path),
                           'EVOLVMEM_CONSOLE_OWNER_ID': 'jiangli',
                           'EVOLVMEM_LAN_DATA_DIR': str(self.directory / 'lan')}.items():
            monkeypatch.setenv(key, value)
        for key in HISTORY_KEYS:
            if env.get(key) is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, env[key])
        self.console = ConsoleService(self.registry_path, 'jiangli', self.owner.config.data_dir,
                                      self.directory / 'lan', env)
        self.console.bootstrap(OWNER_ID, 'Synthetic owner')
        self.console.observe_login(MEMBER_ID, 'Synthetic member')
        assert self.console.dispatch('POST', '/users/kane', {'open_id': MEMBER_ID}, {'open_id': OWNER_ID})[0] == 200
        self.settings = {'enabled': True, 'app_id': 'cli_synthetic_persistence',
                         'app_secret': 'synthetic-config-secret', 'owner_open_id': OWNER_ID,
                         'tenant_key': TENANT, 'owner_only': False,
                         'redirect_uri': 'http://fixture.test/auth/feishu/callback'}
        self.write_settings()
        self.auths, self.initial_session_counts, self.exchanges, self.responses = [], [], [], []
        self.servers = []
        real_auth = web_auth.WebAuth
        def fresh_auth(settings):
            auth = real_auth(settings)
            self.initial_session_counts.append(len(auth.sessions))
            self.auths.append(auth)
            return auth
        monkeypatch.setattr(web_server, 'WebAuth', fresh_auth)
        monkeypatch.setattr(web_auth.urllib.request, 'urlopen', self.feishu)
        self.start()

    def write_settings(self):
        (self.owner.config.data_dir / 'web_auth.json').write_text(json.dumps(self.settings))

    def feishu(self, request, timeout=None):
        if request.full_url.endswith('/authen/v2/oauth/token'):
            body = json.loads(request.data)
            account = body['code']
            self.exchanges.append(account)
            token = 'fixture-feishu-access-' + account
            payload = {'code': 0, 'access_token': token,
                       'refresh_token': 'fixture-feishu-refresh-' + account,
                       'expires_in': 7200, 'refresh_expires_in': 2_592_000}
            self.responses.append(payload)
        elif request.full_url.endswith('/authen/v1/user_info'):
            account = request.get_header('Authorization').removeprefix('Bearer fixture-feishu-access-')
            identity = {'owner': OWNER_ID, 'member': MEMBER_ID, 'pending': PENDING_ID}[account]
            payload = {'code': 0, 'data': {'open_id': identity, 'tenant_key': TENANT,
                                          'name': 'Synthetic ' + account}}
        else:
            raise AssertionError('unexpected external endpoint')
        return io.BytesIO(json.dumps(payload).encode())

    def start(self):
        from evolvmem import web_server
        server = web_server.make_http_server(self.owner, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        entry = {'server': server, 'thread': thread, 'auth': self.auths[-1],
                 'base': f'http://127.0.0.1:{server.server_port}'}
        self.servers.append(entry)
        self.current = entry
        assert self.initial_session_counts[-1] == 0
        return entry

    def restart(self, browser=None):
        from evolvmem.context_models import ContextMode
        from evolvmem.context_service import ContextService
        self.stop(self.current)
        config = self.owner.config
        self.owner.close()
        self.owner = ContextService(config)
        self.owner.initialize(mode=ContextMode.SHADOW, adapter='codex')
        entry = self.start()
        if browser is not None:
            browser.base = entry['base']
        return entry

    def stop(self, entry):
        if not entry.get('closed'):
            entry['server'].shutdown()
            entry['server'].server_close()
            entry['thread'].join(3)
            entry['closed'] = True

    def browser(self, entry=None, cookies=None):
        browser = ConsoleBrowser((entry or self.current)['base'])
        browser.cookies = dict(cookies or {})
        return browser

    def login(self, account='owner', entry=None):
        browser = self.browser(entry)
        me = browser.login(account)
        assert COOKIE in browser.cookies and not browser.cookies[COOKIE].startswith('test-')
        return browser, me

    def mutate_user(self, owner, me, **body):
        return owner.request(PREFIX + '/users/kane', 'POST', body, {'X-CSRF-Token': me['csrf_token']})

    def close(self):
        for entry in self.servers:
            self.stop(entry)
        self.owner.close()


@pytest.fixture
def login_web(tmp_path, monkeypatch):
    fixture = LoginFixture(tmp_path / 'real-login', monkeypatch)
    try:
        yield fixture
    finally:
        fixture.close()


def auth_me(browser):
    status, headers, body = browser.request('/api/auth/me')
    assert status == 200
    return headers, json.loads(body)


def assert_logged_out(browser):
    assert auth_me(browser)[1]['authenticated'] is False
    assert browser.request(PREFIX + '/me')[0] == 401


def test_same_cookie_and_csrf_survive_real_handler_and_service_restart(login_web):
    f = login_web
    browser, me = f.login()
    cookies = dict(browser.cookies)
    f.restart(browser)
    assert browser.cookies == cookies
    assert auth_me(browser)[1]['csrf_token'] == me['csrf_token']
    assert f.mutate_user(browser, me, display_name='Renamed after restart')[0] == 200
    assert browser.request(PREFIX + '/users/kane', 'POST', {'display_name': 'Bad'},
                           {'X-CSRF-Token': 'incorrect-csrf'})[0] == 403
    assert f.initial_session_counts == [0, 0]
    assert f.exchanges == ['owner']


def test_absolute_eight_hour_expiry_refresh_never_extends_it(login_web):
    f = login_web
    browser, me = f.login()
    issued_at = f.clock[0]
    for offset in (60, 3600, 7 * 3600, 8 * 3600 - 1):
        f.clock[0] = issued_at + offset
        headers, current = auth_me(browser)
        assert current['authenticated'] is True and current['csrf_token'] == me['csrf_token']
        assert COOKIE + '=' not in headers.get('Set-Cookie', '')
        assert browser.request(PREFIX + '/me')[0] == 200
    f.restart(browser)
    assert auth_me(browser)[1]['authenticated'] is True
    f.clock[0] = issued_at + 8 * 3600
    assert_logged_out(browser)
    assert f.exchanges == ['owner']


def test_logout_permanently_revokes_copied_cookie_across_instances_and_restart(login_web):
    f = login_web
    browser, me = f.login()
    copied = dict(browser.cookies)
    second = f.start()
    other = f.browser(second, copied)
    assert auth_me(other)[1]['authenticated'] is True
    assert browser.request('/auth/logout', 'POST', headers={'X-CSRF-Token': 'wrong-csrf'})[0] == 403
    assert auth_me(other)[1]['authenticated'] is True
    assert browser.request('/auth/logout', 'POST', headers={'X-CSRF-Token': me['csrf_token']})[0] == 200
    assert_logged_out(other)
    f.restart(other)
    assert_logged_out(other)
    assert f.exchanges == ['owner']


def test_disable_then_enable_without_intermediate_request_never_revives_cookie(login_web):
    f = login_web
    owner, owner_me = f.login()
    member, _ = f.login('member', entry=f.start())
    copied = dict(member.cookies)
    assert f.mutate_user(owner, owner_me, enabled=False)[0] == 200
    # The old member cookie is intentionally never presented while disabled.
    assert f.mutate_user(owner, owner_me, enabled=True)[0] == 200
    member.cookies = copied
    assert_logged_out(member)
    f.restart(member)
    assert_logged_out(member)
    fresh, _ = f.login('member')
    assert auth_me(fresh)[1]['authenticated'] is True


def test_disabled_account_cannot_issue_a_fresh_session(login_web):
    f = login_web
    owner, me = f.login()
    assert f.mutate_user(owner, me, enabled=False)[0] == 200
    member = f.browser()
    query = member.begin_login()
    status, _, _ = member.request('/auth/feishu/callback?' + urlencode({'code': 'member', 'state': query['state'][0]}))
    assert status == 403
    assert COOKIE not in member.cookies
    assert_logged_out(member)


@pytest.mark.parametrize('field,value', [
    ('app_id', 'cli_changed_identity'), ('app_secret', 'changed-synthetic-secret'),
    ('owner_open_id', 'changed-owner'), ('tenant_key', 'changed-tenant'),
    ('redirect_uri', 'http://another.fixture.test/auth/feishu/callback'), ('owner_only', True)])
def test_auth_identity_scope_change_rejects_old_sessions(login_web, field, value):
    f = login_web
    browser, _ = f.login()
    f.settings[field] = value
    f.write_settings()
    f.restart(browser)
    assert_logged_out(browser)


def test_request_host_does_not_change_persistent_identity_scope(login_web):
    f = login_web
    browser, _ = f.login()
    f.restart(browser)
    status, _, body = browser.request('/api/auth/me', headers={'Host': 'alternate.fixture.test'})
    assert status == 200 and json.loads(body)['authenticated'] is True


def test_logout_of_old_scope_cookie_cannot_revive_if_app_settings_are_restored(login_web):
    f = login_web
    browser, _ = f.login()
    copied = dict(browser.cookies)
    old_app = f.settings['app_id']
    f.settings['app_id'] = 'cli_changed_logout_scope'
    f.write_settings()
    f.restart(browser)
    assert_logged_out(browser)
    # Logout is also allowed to clear an already invalid client cookie.
    assert browser.request('/auth/logout', 'POST')[0] == 200
    f.settings['app_id'] = old_app
    f.write_settings()
    f.restart(browser)
    browser.cookies = copied
    assert_logged_out(browser)


def test_trusted_pending_login_persists_but_never_bypasses_knowledge_acl(login_web):
    f = login_web
    pending, _ = f.login('pending')
    f.restart(pending)
    assert auth_me(pending)[1]['authenticated'] is True
    for path in ('/me', '/users', '/libraries', '/libraries/jiangli/experiences',
                 '/libraries/jiangli/history/projects', '/libraries/jiangli/history/attachment/' + f.image_sha):
        assert pending.request(PREFIX + path)[0] == 403, path
    owner, me = f.login()
    status, _, body = owner.request(PREFIX + '/users')
    assert status == 200 and any(row['open_id'] == PENDING_ID for row in json.loads(body)['pending'])


def test_feishu_access_refresh_tokens_and_config_secret_never_enter_session_database(login_web):
    f = login_web
    browser, _ = f.login()
    f.restart(browser)
    assert auth_me(browser)[1]['authenticated'] is True
    with sqlite3.connect(f.registry_path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert 'web_sessions' in tables
        stored = '\n'.join(repr(row) for table in tables for row in conn.execute('SELECT * FROM "' + table.replace('"', '""') + '"'))
    for forbidden in ('fixture-feishu-access-', 'fixture-feishu-refresh-', 'synthetic-config-secret'):
        assert forbidden not in stored
        for file in f.registry_path.parent.iterdir():
            if file.is_file():
                assert forbidden.encode() not in file.read_bytes()
    for file in f.registry_path.parent.iterdir():
        if file.is_file():
            assert browser.cookies[COOKIE].encode() not in file.read_bytes()


def test_disable_committed_between_identity_check_and_session_insert_is_rejected(login_web, monkeypatch):
    f = login_web
    auth = f.current['auth']
    store = getattr(auth, 'session_store', None)
    assert store is not None, 'durable session store is missing'
    original = store.put
    called = []
    def put_after_disable(*args, **kwargs):
        called.append(True)
        result = f.console.dispatch('POST', '/users/kane', {'enabled': False}, {'open_id': OWNER_ID})
        assert result[0] == 200
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'put', put_after_disable)
    member = f.browser()
    query = member.begin_login()
    status, _, _ = member.request('/auth/feishu/callback?' + urlencode({'code': 'member', 'state': query['state'][0]}))
    assert called and status == 403
    assert COOKIE not in member.cookies


def test_callback_sets_absolute_eight_hour_http_only_lax_cookie(login_web):
    f = login_web
    browser = f.browser()
    query = browser.begin_login()
    status, headers, _ = browser.request('/auth/feishu/callback?' + urlencode({
        'code': 'owner', 'state': query['state'][0]}))
    assert status == 302 and headers['Location'] == '/'
    cookies = SimpleCookie()
    cookies.load(headers['Set-Cookie'])
    issued = cookies[COOKIE]
    assert issued['max-age'] == '28800'
    assert issued['httponly'] is True
    assert issued['samesite'].lower() == 'lax'
    assert issued['path'] == '/'
    assert auth_me(browser)[1]['authenticated'] is True
    assert COOKIE + '=' not in auth_me(browser)[0].get('Set-Cookie', '')
    assert f.exchanges == ['owner']


def test_store_failure_never_falls_back_to_old_memory_session_or_renews_expiry(login_web, monkeypatch):
    from evolvmem import web_reads
    from evolvmem.web_sessions import SessionStoreError
    f = login_web
    browser, me = f.login()
    auth = f.current['auth']
    token = browser.cookies[COOKIE]
    original = auth.session_store.get(token)
    assert original['expires'] == f.clock[0] + 8 * 3600
    # Intentional fault fixture only: a stale in-memory copy must not become
    # an alternate authentication source when durable storage is unavailable.
    auth.sessions[token] = dict(original)
    snapshots = []
    snapshot = web_reads.snapshot
    def tracked_snapshot(*args, **kwargs):
        snapshots.append(True)
        return snapshot(*args, **kwargs)
    monkeypatch.setattr(web_reads, 'snapshot', tracked_snapshot)
    f.clock[0] += 3600
    def unavailable(token):
        raise SessionStoreError()
    with monkeypatch.context() as failure:
        failure.setattr(auth.session_store, 'get', unavailable)
        for path in ('/api/auth/me', PREFIX + '/me', PREFIX + '/libraries/jiangli/experiences'):
            status, _, body = browser.request(path)
            assert status == 503, path
            assert json.loads(body) == {'ok': False, 'error': 'session_store_unavailable'}
            assert len(body.encode()) < 128
        assert snapshots == []
    current = auth_me(browser)[1]
    assert current['authenticated'] is True and current['csrf_token'] == me['csrf_token']
    assert auth.session_store.get(token)['expires'] == original['expires']
    f.clock[0] = original['expires'] - 1
    assert auth_me(browser)[1]['authenticated'] is True
    f.clock[0] = original['expires']
    assert_logged_out(browser)
    assert f.exchanges == ['owner']
