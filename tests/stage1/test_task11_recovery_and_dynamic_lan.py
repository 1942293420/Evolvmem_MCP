"""Behavioral regression tests for PG startup outage auto-recovery and dynamic LAN users."""
from __future__ import annotations

import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_server import make_http_server
from evolvmem.memory_store import MemoryStore
from evolvmem.web_auth import AuthSettings, WebAuth
from evolvmem.web_server import make_handler
from tests.stage1.pg_stage1_harness import create_isolated_control_db, drop_isolated_db
from tests.stage1.test_task11_identity_acl import ADMIN_SESSION, build_pg_console


@pytest.fixture
def pg_control_env(tmp_path):
    env = create_isolated_control_db(tmp_path, up_to_version=7)
    try:
        yield env
    finally:
        drop_isolated_db(env.database, (env.coordinator_role, env.reader_role, env.owner_role))


def _bootstrap_admin_owner(env, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    svc, store, _ = build_pg_console(env, root)
    return svc, store


def test_web_server_recovers_after_startup_pg_outage_without_owner_env(pg_control_env, tmp_path, monkeypatch):
    """Startup PG outage returns 503 without SQLite fallback and auto-recovers once PG is reachable, even with EVOLVMEM_CONSOLE_OWNER_ID unset."""
    owner_dir = tmp_path / 'owner_recover'
    owner_dir.mkdir()
    decoy_sqlite = tmp_path / 'decoy_recover' / 'users.db'

    _, store = _bootstrap_admin_owner(pg_control_env, tmp_path / 'boot_recover')
    auth_file = owner_dir / 'web_auth.json'
    auth_file.write_text(
        json.dumps({
            'enabled': True,
            'app_id': 'cli_stage1_recover',
            'app_secret': 'sec_stage1_recover_secret',
            'redirect_uri': 'http://127.0.0.1:19377/auth/feishu/callback',
            'owner_open_id': ADMIN_SESSION['open_id'],
            'owner_only': False,
        }),
        encoding='utf-8',
    )
    os.chmod(auth_file, 0o600)
    scope = WebAuth(AuthSettings.load(owner_dir)).persistence_scope()
    cookie_tok = 'cookie-recover-token-0001'
    pg_sessions = PgPersistentSessions(store, scope, expected_owner_id=None, console_mode=True)
    pg_sessions.initialize()
    pg_sessions.put(cookie_tok, {**ADMIN_SESSION, 'expires': time.time() + 3600})

    policy_file = tmp_path / 'policy_recover.json'
    policy_file.write_text(
        json.dumps({'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False}),
        encoding='utf-8',
    )

    runtime_pg_cfg = tmp_path / 'runtime_pg.json'
    live_cfg_data = json.loads(pg_control_env.config_file.read_text(encoding='utf-8'))
    down_cfg_data = {**live_cfg_data, 'port': 59999}
    runtime_pg_cfg.write_text(json.dumps(down_cfg_data), encoding='utf-8')
    os.chmod(runtime_pg_cfg, 0o600)

    monkeypatch.delenv('EVOLVMEM_CONSOLE_OWNER_ID', raising=False)
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_REGISTRY', str(decoy_sqlite))
    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(runtime_pg_cfg))

    app_cfg = Config(data_dir=owner_dir, apply_environment=False)
    with MemoryStore(app_cfg):
        pass
    ctx = ContextService(app_cfg)
    ctx.initialize(mode=ContextMode.SHADOW, adapter='codex')
    try:
        handler_cls = make_handler(ctx)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/knowledge-console/me',
                headers={'Cookie': f'evolvmem_session={cookie_tok}'},
            )
            with pytest.raises(urllib.error.HTTPError) as down_exc:
                urllib.request.urlopen(req, timeout=5)
            assert down_exc.value.code == 503
            down_body = json.loads(down_exc.value.read())
            assert down_body['error'] == 'pg_control_unavailable'
            assert 'PG 不可用' in down_body.get('message', '')
            assert not decoy_sqlite.exists()
            assert not (owner_dir / 'web-sessions.db').exists()

            runtime_pg_cfg.write_text(json.dumps(live_cfg_data), encoding='utf-8')
            os.chmod(runtime_pg_cfg, 0o600)

            with urllib.request.urlopen(req, timeout=5) as up_resp:
                assert up_resp.status == 200
                up_body = json.loads(up_resp.read())
                assert up_body['current_user']['id'] == 'admin_owner'
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        ctx.close()


def test_dynamic_pg_user_lan_mcp_end_to_end(pg_control_env, tmp_path, monkeypatch):
    """Dynamically provisioned PG console users (non-jiangli/kane) authenticate and execute LAN MCP tools end-to-end."""
    svc, _ = _bootstrap_admin_owner(pg_control_env, tmp_path / 'lan_dyn')
    svc.observe_login('feishu-user-d', 'User D')
    status, _, _ = svc.dispatch(
        'POST', '/users',
        {'id': 'user_d', 'display_name': 'User D', 'open_id': 'feishu-user-d'},
        ADMIN_SESSION,
    )
    assert status == 201
    svc.observe_login('feishu-user-disabled', 'Disabled User')
    status, _, _ = svc.dispatch(
        'POST', '/users',
        {'id': 'user_disabled', 'display_name': 'Disabled User', 'open_id': 'feishu-user-disabled'},
        ADMIN_SESSION,
    )
    assert status == 201
    assert svc.dispatch('POST', '/users/user_disabled', {'enabled': False}, ADMIN_SESSION)[0] == 200

    policy_file = tmp_path / 'policy_lan.json'
    policy_file.write_text(
        json.dumps({'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False}),
        encoding='utf-8',
    )
    monkeypatch.delenv('EVOLVMEM_CONSOLE_OWNER_ID', raising=False)
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(pg_control_env.config_file))

    tokens = {
        'admin_owner': 'tok-admin-owner-stage1',
        'user_d': 'tok-user-d-stage1',
        'user_disabled': 'tok-user-disabled-stage1',
    }
    settings = LanSettings(
        data_dir=tmp_path / 'lan-data',
        owner_data_dir=tmp_path / 'owner-data',
        token_hashes={u: hashlib.sha256(t.encode('utf-8')).hexdigest() for u, t in tokens.items()},
        embedding_enabled=False,
    )
    runtime = LanRuntime(settings)
    runtime.initialize()
    httpd = make_http_server(runtime, host='127.0.0.1', port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def post_mcp(bearer: str, rpc: dict):
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/mcp',
                data=json.dumps(rpc).encode('utf-8'),
                headers={'Authorization': f'Bearer {bearer}', 'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())

        st, init_res = post_mcp(tokens['user_d'], {
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-03-26', 'clientInfo': {'name': 'claude-code', 'model': 'claude-sonnet-4-6'}},
        })
        assert st == 200 and 'result' in init_res

        st, list_res = post_mcp(tokens['user_d'], {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        assert st == 200
        tool_names = {t['name'] for t in list_res['result']['tools']}
        assert 'memory_status' in tool_names

        st, call_res = post_mcp(tokens['user_d'], {
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'memory_status', 'arguments': {'detail': 'revision'}},
        })
        assert st == 200 and call_res['result']['isError'] is False
        status_payload = json.loads(call_res['result']['content'][0]['text'])
        assert status_payload['authenticated_user'] == 'user_d'

        with pytest.raises(urllib.error.HTTPError) as dis_exc:
            post_mcp(tokens['user_disabled'], {'jsonrpc': '2.0', 'id': 4, 'method': 'ping', 'params': {}})
        assert dis_exc.value.code == 403
        assert json.loads(dis_exc.value.read())['error'] == 'account_disabled'
    finally:
        httpd.shutdown()
        httpd.server_close()
        runtime.close()
