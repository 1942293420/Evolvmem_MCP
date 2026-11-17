"""Real Handler + loopback central HTTP; isolated native control fake only."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evolvmem.product_policy import ProductPolicy
from evolvmem.web_server import make_handler
from evolvmem.web_sessions import SessionStoreError

ORIGIN = 'http://192.168.1.135:9377'


class CentralHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        state = self.server.state
        state['calls'].append((self.command, self.path, dict(self.headers)))
        if state.get('delay'):
            time.sleep(state['delay'])
        status = state['status'] if self.headers.get('Cookie') == 'ops_session=test-parent' else 401
        if self.path.startswith('/api/access/check?system='):
            data = {'allowed': status == 200}
        elif self.path == '/api/auth/status':
            data = {'authenticated': status == 200, 'user': {
                'open_id': state['identity'], 'name': 'Synthetic employee',
                'role': state['role'], 'csrf': 'central-secret-csrf'}}
        else:
            status, data = 404, {}
        payload = state.get('raw') or json.dumps(data).encode()
        self.send_response(state.get('force_status') or status)
        if state.get('force_status') == 302:
            self.send_header('Location', '/must-not-follow')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass

    def do_POST(self):
        self.server.state['calls'].append((self.command, self.path, dict(self.headers)))
        raw = self.rfile.read(int(self.headers.get('Content-Length',0)))
        json_body = self.headers.get('Content-Type') == 'application/json' and raw == b'{}'
        valid = json_body and self.path == '/api/ops/logout' and self.headers.get('Cookie') == 'ops_session=test-parent' and self.headers.get('X-CSRF-Token') == 'central-secret-csrf' and not self.headers.get('Origin')
        if valid:
            self.server.state['status'] = 401
        payload = b'{"ok":true}' if valid else b'{"ok":false}'
        self.send_response(200 if valid else 403)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class NativeFake:
    pg_mode = True
    _expected_owner_id = 'native-owner'

    def __init__(self):
        self._pg_store = self
        self.native = 'ready'
        self.logins = []
        self.dispatches = []
        self.fail = False

    def registry_ready(self):
        if self.fail:
            raise SessionStoreError('pg_control_unavailable')
        return True

    def actor_by_open_id(self, expected, identity):
        self.registry_ready()
        if self.native == 'pending':
            return None
        return {'open_id': identity, 'enabled': self.native != 'disabled', 'is_admin': False}

    def login_allowed(self, identity):
        self.registry_ready()
        return self.native != 'disabled'

    def observe_login(self, identity, name):
        if self.fail:
            raise RuntimeError('isolated native login failure')
        self.logins.append((identity, name))

    def dispatch(self, method, path, params, session):
        self.dispatches.append((method, path, params, session))
        return 200, 'application/json', {'ok': True, 'native_admin': False}


class CompanySSOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.central = ThreadingHTTPServer(('127.0.0.1', 0), CentralHandler)
        cls.central.daemon_threads = True
        cls.thread = threading.Thread(target=cls.central.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.central.shutdown()
        cls.central.server_close()

    def setUp(self):
        self.central.state = dict(status=200, identity='test-employee', role='superadmin', calls=[])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        (directory / 'web_auth.json').write_text(json.dumps(dict(app_id='cli_aaa39bd67b7c9ceb', app_secret='test-only', owner_open_id='test-owner', redirect_uri=ORIGIN + '/auth/feishu/callback')))
        self.native = NativeFake()
        self.session_store_calls = []
        session_store = SimpleNamespace(initialize=lambda: self.session_store_calls.append('initialize'), prune=lambda: self.session_store_calls.append('prune'), get=lambda token: None)
        self.patches = [patch.dict(os.environ, {'EVOLVMEM_COMPANY_SSO':'1', 'EVOLVMEM_COMPANY_SSO_PUBLIC_ORIGIN':'http://192.168.1.135:9600', 'EVOLVMEM_CONTROL_BACKEND':'postgres'}),
                        patch('evolvmem.product_policy.load_policy', return_value=ProductPolicy(console_enabled=True, legacy_web_enabled=False, ai_processing_enabled=False)),
                        patch('evolvmem.console_access.configured_console', return_value=self.native),
                        patch('evolvmem.console_access.configured_session_store', return_value=session_store),
                        patch('evolvmem.manual_experience_search.configured_search', return_value=None)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        try:
            from evolvmem import company_sso
        except ImportError:
            pass  # RED baseline has no central admission adapter.
        else:
            item = patch.object(company_sso, '_CENTRAL_ORIGIN', f'http://127.0.0.1:{self.central.server_port}')
            item.start()
            self.addCleanup(item.stop)
        service = SimpleNamespace(config=SimpleNamespace(data_dir=directory), store=object(), legacy_facade=lambda: object())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service))
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path='/auth/ops/status', method='GET', cookie='ops_session=test-parent', headers=()):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=6)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader('Host', '192.168.1.135:9377')
        if cookie is not None:
            conn.putheader('Cookie', cookie)
        for key, value in headers:
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.headers), data
        conn.close()
        return result

    def test_real_handler_checks_central_and_native_without_new_session_or_pending_write(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data['authenticated'])
        self.assertFalse(data['can_write'])
        self.assertEqual(data['native_status'], 'ready')
        self.assertNotIn('central-secret-csrf', body.decode())
        self.assertNotIn('test-employee', body.decode())
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertEqual(self.native.logins, [])
        self.assertEqual(self.session_store_calls, [])

    def test_old_cookie_and_forged_bearer_never_bypass_central(self):
        for path in ['/api/console-v2/me', '/api/knowledge-console/me', '/v2/main.js', '/unknown']:
            self.assertIn(self.request(path, cookie='evolvmem_session=old', headers=[('Authorization','Bearer fake')])[0], (401,302))
        self.assertEqual(self.native.dispatches, [])

    def test_revocation_is_seen_on_next_request(self):
        self.assertEqual(self.request('/api/knowledge-console/me')[0], 200)
        self.central.state['status'] = 403
        self.assertEqual(self.request('/api/knowledge-console/me')[0], 403)
        self.assertEqual(len(self.native.dispatches), 1)

    def test_pending_and_disabled_do_not_reach_business(self):
        for state in ['pending','disabled']:
            self.native.native = state
            self.assertEqual(json.loads(self.request()[2])['native_status'], state)
            status, _, body = self.request('/api/console-v2/me')
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)['error'], 'account_' + state)
        self.assertEqual(self.native.dispatches, [])
        self.assertEqual(self.native.logins, [])

    def test_explicit_login_preserves_hash_and_is_only_observer_entry(self):
        status, headers, _ = self.request('/auth/login?next=%2Fv2%2F%3Fx%3D1%23history')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/v2/?x=1#history')
        self.assertEqual(len(self.native.logins), 1)
        for path in ['/auth/login?next=//evil.test','/auth/login?next=%252f%252fevil.test','/auth/login?next=/&next=/v2']:
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(len(self.native.logins), 1)

    def test_anonymous_login_uses_fixed_central_return_and_callback_cannot_accept_code(self):
        status, headers, _ = self.request('/auth/login?next=%2Fv2%2F%23history', cookie=None)
        self.assertEqual(status, 302)
        self.assertTrue(headers['Location'].startswith('http://192.168.1.135:9600/api/ops/login?next='))
        self.assertEqual(self.request('/auth/feishu/callback?code=old&state=old', cookie=None)[1]['Location'], '/login')
        self.assertEqual(self.request('/login', cookie=None)[0], 200)

    def test_duplicate_headers_host_origin_and_unsafe_methods_fail_closed(self):
        for headers in [[('Host','evil.test')],[('Origin',ORIGIN),('Origin',ORIGIN)],[('Cookie','ops_session=other')]]:
            self.assertEqual(self.request(headers=headers)[0], 400)
        self.assertEqual(self.request(headers=[('Origin','http://evil.test')])[0],403)
        for method in ['HEAD','OPTIONS']:
            self.assertIn(self.request('/api/console-v2/me',method)[0], (401,405))
        self.assertEqual(self.request('/auth/login','POST')[0],405)
        self.assertEqual(self.native.dispatches, [])

    def test_native_pg_outage_and_central_failures_are_503_no_stale_success(self):
        self.assertEqual(self.request()[0],200)
        self.native.fail = True
        status, _, body = self.request()
        self.assertEqual(status,503)
        self.assertEqual(json.loads(body)['error'],'pg_control_unavailable')
        self.assertTrue(json.loads(body)['central_sso'])
        self.native.fail = False
        for state in [dict(force_status=302),dict(raw=b'{"allowed":true,"allowed":true}'),dict(raw=b'x'*17000)]:
            self.central.state.update(state)
            self.assertEqual(self.request()[0],503)
            self.central.state.pop('force_status',None)
            self.central.state.pop('raw',None)

    def test_central_absolute_deadline(self):
        self.central.state['delay'] = 2.5
        start = time.monotonic()
        self.assertEqual(self.request()[0],503)
        self.assertLess(time.monotonic()-start,2.35)

    def test_two_central_calls_share_one_absolute_deadline(self):
        self.central.state['delay'] = 1.1
        start = time.monotonic()
        self.assertEqual(self.request()[0],503)
        self.assertLess(time.monotonic()-start,2.35)

    def test_explicit_login_native_failure_is_bounded_503(self):
        self.native.fail = True
        status, _, body = self.request('/auth/login?next=/v2/')
        self.assertEqual(status,503)
        self.assertTrue(json.loads(body)['central_sso'])

    def test_owner_exact_identity_scope_and_csrf_change_on_identity_switch(self):
        employee = json.loads(self.request()[2])
        self.central.state['identity'] = 'test-owner'
        owner = json.loads(self.request()[2])
        self.assertTrue(owner['can_write'])
        self.assertNotEqual(owner['user_scope'], employee['user_scope'])
        self.assertNotEqual(owner['csrf_token'], employee['csrf_token'])
        self.assertEqual(self.request('/auth/logout','POST',headers=[('Origin',ORIGIN),('X-CSRF-Token',employee['csrf_token'])])[0],403)
        self.native.native = 'disabled'
        self.assertFalse(json.loads(self.request()[2])['can_write'])

    def test_duplicate_cookie_values_and_proxy_environment_cannot_change_identity(self):
        self.assertEqual(self.request(cookie='ops_session=test-parent; ops_session=other')[0],400)
        with patch.dict(os.environ, {'HTTP_PROXY':'http://127.0.0.1:1','http_proxy':'http://127.0.0.1:1','ALL_PROXY':'http://127.0.0.1:1','NO_PROXY':''}):
            status, _, body = self.request()
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['native_status'],'ready')

    def test_user_scope_survives_auth_adapter_restart_but_csrf_does_not(self):
        from evolvmem.company_sso import CompanyAuth
        from evolvmem.web_auth import AuthSettings
        settings = AuthSettings('cli_aaa39bd67b7c9ceb','test-only','test-owner',ORIGIN + '/auth/feishu/callback')
        payloads = []
        for adapter in (CompanyAuth(settings), CompanyAuth(settings)):
            identity = dict(open_id='test-employee',name='Synthetic',csrf=adapter._opaque('csrf','same-parent-cookie'))
            adapter._status(SimpleNamespace(_send_json=lambda payload: payloads.append(payload)), identity, 'ready')
        self.assertEqual(payloads[0]['user_scope'],payloads[1]['user_scope'])
        self.assertNotEqual(payloads[0]['csrf_token'],payloads[1]['csrf_token'])

    def test_logout_requires_exact_origin_memory_csrf_and_revokes_parent(self):
        csrf = json.loads(self.request()[2])['csrf_token']
        self.assertEqual(self.request('/auth/logout','POST')[0],403)
        self.assertEqual(self.request('/auth/logout','POST',headers=[('Origin',ORIGIN),('X-CSRF-Token','wrong')])[0],403)
        status, headers, _ = self.request('/auth/logout','POST',headers=[('Origin',ORIGIN),('X-CSRF-Token',csrf)])
        self.assertEqual(status,200)
        self.assertIn('ops_session=',headers['Set-Cookie'])
        self.assertEqual(self.request()[0],401)
        self.assertEqual(self.native.dispatches,[])

    def test_central_role_does_not_grant_native_owner_write(self):
        csrf = json.loads(self.request()[2])['csrf_token']
        status, _, body = self.request('/api/unallocated','POST',headers=[('Origin',ORIGIN),('X-CSRF-Token',csrf)])
        self.assertIn(status,(403,410))
        self.assertEqual(self.native.dispatches,[])


if __name__ == '__main__':
    unittest.main(verbosity=2)
