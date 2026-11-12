"""Stage 1 Task 1.2 REV remediation tests: concurrent session-scoped MCP attribution,
tools/call active attribution context, strict CLI --agent/--model validation, and
end-to-end execution of all 5 CLI hooks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import uuid

import pytest

from evolvmem.config import Config
from evolvmem.experience_v2.control import agent_identity as ai_mod
from evolvmem.experience_v2.control.agent_identity import (
    PgAgentRegistry,
    compute_agent_id,
    parse_cli_agent_model,
)
from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_server import make_http_server
from evolvmem.lan_tools import LanTools
from evolvmem.library_catalog import ControlBinding
from evolvmem.library_foundation import ConnectionBinding
from evolvmem.library_host import LibraryHost
from evolvmem.library_startup import FileSecretResolver
from evolvmem.mcp_server import MemoryMCPServer
from tests.stage1.pg_stage1_harness import (
    ROOT,
    TEST_PG_HOST,
    TEST_PG_PORT,
    create_isolated_control_db,
    drop_isolated_db,
)
from tests.stage1.test_task11_identity_acl import build_pg_console, init_sqlite_source


@pytest.fixture()
def pg_control_v8(tmp_path):
    env = create_isolated_control_db(tmp_path, up_to_version=8)
    try:
        yield env
    finally:
        drop_isolated_db(env.database, (env.coordinator_role, env.reader_role, env.owner_role))


def _http_post_json(server, path: str, payload: dict, headers: dict[str, str]):
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req_headers = {'Content-Type': 'application/json', **headers}
        conn.request('POST', path, body=raw, headers=req_headers)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode('utf-8'))
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, resp_headers
    finally:
        conn.close()


def test_mcp_server_binds_active_attribution_during_tool_call(tmp_path, pg_control_v8, monkeypatch):
    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    registry = PgAgentRegistry(store)
    dev_a, cred_a = str(uuid.uuid4()), str(uuid.uuid4())
    store.register_trusted_device(
        'user_a', device_id=dev_a, external_device_id='box-mcp-a', token='tok-mcp-a', credential_id=cred_a
    )
    get_active = getattr(ai_mod, 'get_active_attribution', lambda: None)

    data_dir = init_sqlite_source(tmp_path / 'mcp_standalone')
    mcp_cfg = Config(data_dir=data_dir, apply_environment=False)
    mcp_srv = MemoryMCPServer(
        mcp_cfg,
        embedding_engine=None,
        agent_registry=registry,
        actor_id='user_a',
        device_id=dev_a,
    )
    mcp_srv.initialize()
    mcp_srv._init_done.set()
    seen_in_mcp_tool = []
    orig_mcp_handle_tool = mcp_srv.handle_tool_call

    def spy_mcp_tool(name, args):
        seen_in_mcp_tool.append(get_active())
        return orig_mcp_handle_tool(name, args)

    monkeypatch.setattr(mcp_srv, 'handle_tool_call', spy_mcp_tool)
    try:
        mcp_srv._handle_request(
            {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'claude-code', 'version': '1.2.0', 'model': 'claude-opus-4-6'},
                },
            }
        )
        assert get_active() is None
        mcp_srv._handle_request(
            {
                'jsonrpc': '2.0',
                'id': 2,
                'method': 'tools/call',
                'params': {'name': 'memory_status', 'arguments': {}},
            }
        )
        assert len(seen_in_mcp_tool) == 1 and seen_in_mcp_tool[0] is not None
        assert (seen_in_mcp_tool[0].actor, seen_in_mcp_tool[0].client, seen_in_mcp_tool[0].model) == (
            'user_a',
            'claude',
            'claude-opus-4-6',
        )
        assert get_active() is None
    finally:
        mcp_srv.shutdown()


def test_http_concurrent_multisession_isolation_for_lan_and_library_host(tmp_path, pg_control_v8, monkeypatch):
    _, store, cfg = build_pg_console(pg_control_v8, tmp_path)
    registry = PgAgentRegistry(store)
    dev_a, cred_a = str(uuid.uuid4()), str(uuid.uuid4())
    p_a = store.register_trusted_device(
        'user_a', device_id=dev_a, external_device_id='box-conc-a', token='tok-libhost-conc', credential_id=cred_a
    )
    get_active = getattr(ai_mod, 'get_active_attribution', lambda: None)

    data_dir = init_sqlite_source(tmp_path / 'owner_sqlite')
    mcp_cfg = Config(data_dir=data_dir, apply_environment=False)

    lan_dir = tmp_path / 'lan_mcp'
    lan_dir.mkdir()
    settings = LanSettings(
        data_dir=lan_dir,
        owner_data_dir=data_dir,
        token_hashes={
            'user_a': hashlib.sha256(b'tok-lan-a').hexdigest(),
            'user_c': hashlib.sha256(b'tok-lan-c').hexdigest(),
        },
        embedding_enabled=False,
    )
    rt = LanRuntime(settings)
    rt.initialize()
    observed_lan_calls: list[tuple[str, str, str]] = []
    obs_lock = threading.Lock()
    orig_call_tool = LanTools.call_tool

    def spy_lan_call_tool(self, user, name, arguments, *, owner=False):
        active = get_active()
        with obs_lock:
            observed_lan_calls.append(
                (
                    user,
                    active.client if active else 'NONE',
                    active.model if active else 'NONE',
                )
            )
        return orig_call_tool(self, user, name, arguments, owner=owner)

    monkeypatch.setattr(LanTools, 'call_tool', spy_lan_call_tool)

    conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
    host = LibraryHost(
        control_binding=ControlBinding(cfg.host, cfg.port, cfg.database, cfg.reader_role, cfg.reader_password_ref),
        content_bindings=(
            ConnectionBinding(
                'content.dummy',
                str(uuid.uuid4()),
                p_a,
                cred_a,
                dev_a,
                TEST_PG_HOST,
                TEST_PG_PORT,
                'postgres',
                cfg.reader_role,
                cfg.reader_password_ref,
            ),
        ),
        control_connector=conn_fn,
        content_connector=conn_fn,
        secret_resolver=FileSecretResolver(cfg.secret_files),
        config=mcp_cfg,
        model_contract='bge-m3-test',
        enabled=True,
        agent_registry=registry,
        actor_resolver=lambda pid: 'user_a' if pid == p_a else pid,
    )

    server = make_http_server(
        rt,
        host='127.0.0.1',
        port=0,
        library_host=host,
    )
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    try:
        # Two simultaneous clients under the SAME authenticated user_a
        st1, _, h1 = _http_post_json(
            server,
            '/mcp',
            {
                'jsonrpc': '2.0',
                'id': 10,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'Codex', 'model': 'gpt-5.4'},
                },
            },
            {'Authorization': 'Bearer tok-lan-a'},
        )
        st2, _, h2 = _http_post_json(
            server,
            '/mcp',
            {
                'jsonrpc': '2.0',
                'id': 11,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'claude-code', 'model': 'claude-opus-4-6'},
                },
            },
            {'Authorization': 'Bearer tok-lan-a'},
        )
        assert st1 == 200 and st2 == 200
        sess_codex = h1.get('mcp-session-id')
        sess_claude = h2.get('mcp-session-id')
        assert sess_codex and sess_claude and sess_codex != sess_claude

        # 16-thread concurrent tool calls across both sessions + stateless X-EvolvMem-Agent/Model headers
        def worker(i: int):
            if i % 3 == 0:
                st, body, _ = _http_post_json(
                    server,
                    '/mcp',
                    {'jsonrpc': '2.0', 'id': 100 + i, 'method': 'tools/call', 'params': {'name': 'memory_status', 'arguments': {}}},
                    {'Authorization': 'Bearer tok-lan-a', 'Mcp-Session-Id': sess_codex},
                )
                return ('codex', 'gpt-5.4', st, 'result' in body)
            if i % 3 == 1:
                st, body, _ = _http_post_json(
                    server,
                    '/mcp',
                    {'jsonrpc': '2.0', 'id': 100 + i, 'method': 'tools/call', 'params': {'name': 'memory_status', 'arguments': {}}},
                    {'Authorization': 'Bearer tok-lan-a', 'Mcp-Session-Id': sess_claude},
                )
                return ('claude', 'claude-opus-4-6', st, 'result' in body)
            st, body, _ = _http_post_json(
                server,
                '/mcp',
                {'jsonrpc': '2.0', 'id': 100 + i, 'method': 'tools/call', 'params': {'name': 'memory_status', 'arguments': {}}},
                {
                    'Authorization': 'Bearer tok-lan-a',
                    'X-EvolvMem-Agent': 'dsh-cli',
                    'X-EvolvMem-Model': 'deepseek-r1',
                },
            )
            return ('dsh', 'deepseek-r1', st, 'result' in body)

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(worker, range(24)))
        assert all(st == 200 and ok for _, _, st, ok in outcomes)
        seen_pairs = {(c, m) for _, c, m in observed_lan_calls}
        assert seen_pairs == {
            ('codex', 'gpt-5.4'),
            ('claude', 'claude-opus-4-6'),
            ('dsh', 'deepseek-r1'),
        }

        # LibraryHost multi-session isolation on the same device_id over HTTP /library/mcp
        lh1_st, _, lh1_h = _http_post_json(
            server,
            '/library/mcp',
            {
                'jsonrpc': '2.0',
                'id': 201,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'claude-code', 'model': 'claude-sonnet-4-6'},
                },
            },
            {'Authorization': 'Bearer tok-libhost-conc', 'X-EvolvMem-Backend': 'postgres'},
        )
        lh2_st, _, lh2_h = _http_post_json(
            server,
            '/library/mcp',
            {
                'jsonrpc': '2.0',
                'id': 202,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'Codex', 'model': 'gpt-5.4'},
                },
            },
            {'Authorization': 'Bearer tok-libhost-conc', 'X-EvolvMem-Backend': 'postgres'},
        )
        assert lh1_st == 200 and lh2_st == 200
        lh_s1 = lh1_h.get('mcp-session-id')
        lh_s2 = lh2_h.get('mcp-session-id')
        assert lh_s1 and lh_s2 and lh_s1 != lh_s2
        a1 = host.last_attribution(dev_a, session_id=lh_s1)
        a2 = host.last_attribution(dev_a, session_id=lh_s2)
        assert (a1.actor, a1.client, a1.model) == ('user_a', 'claude', 'claude-sonnet-4-6')
        assert (a2.actor, a2.client, a2.model) == ('user_a', 'codex', 'gpt-5.4')
    finally:
        server.shutdown()
        srv_thread.join(3)
        server.server_close()
        host.close()
        rt.close()


def test_parse_cli_agent_model_strict_validation():
    a1, r1 = parse_cli_agent_model(['--agent', 'dsh', '--model', 'deepseek-r1', 'inject'])
    assert (a1.client, a1.model, r1) == ('dsh', 'deepseek-r1', ['inject'])

    a2, r2 = parse_cli_agent_model(['inject', '--agent', 'dsh', '--model', 'deepseek-r1'])
    assert (a2.client, a2.model, r2) == ('dsh', 'deepseek-r1', ['inject'])

    for bad_argv in (
        ['--agent'],
        ['--model'],
        ['--agent='],
        ['--model='],
        ['--agent', '--model', 'gpt-5.4'],
        ['--model', '--agent', 'codex'],
        ['--agent', 'inject'],
        ['--agent', 'session-start'],
        ['--agent', 'session-end'],
        ['--model', 'recall'],
        ['--model', 'extract'],
    ):
        with pytest.raises(ValueError):
            parse_cli_agent_model(bad_argv)


def test_five_cli_hooks_wire_agent_and_model_end_to_end(tmp_path, pg_control_v8):
    _, store, cfg = build_pg_console(pg_control_v8, tmp_path)
    pg_cfg_path = tmp_path / 'pg-control.json'
    pg_cfg_path.write_text(
        json.dumps(
            {
                'host': cfg.host,
                'port': cfg.port,
                'database': cfg.database,
                'coordinator_role': cfg.coordinator_role,
                'coordinator_password_ref': cfg.coordinator_password_ref,
                'reader_role': cfg.reader_role,
                'reader_password_ref': cfg.reader_password_ref,
                'secret_files': {k: str(v) for k, v in cfg.secret_files.items()},
                'allow_loopback_disable_ssl': True,
            }
        ),
        encoding='utf-8',
    )
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    data_dir = init_sqlite_source(tmp_path / 'hook_data')
    policy_path = tmp_path / 'product-policy.json'
    policy_path.write_text('{"ai_processing_enabled": false, "legacy_web_enabled": true}', encoding='utf-8')
    env = {
        **os.environ,
        'HOME': str(fake_home),
        'EVOLVMEM_DATA_DIR': str(data_dir),
        'EVOLVMEM_PRODUCT_POLICY': str(policy_path),
        'EVOLVMEM_CONSOLE_PG_CONFIG': str(pg_cfg_path),
        'EVOLVMEM_AGENT_ACTOR': 'user_a',
        'PYTHONPATH': str(ROOT),
    }

    p_hooks = subprocess.run(
        [sys.executable, '-m', 'evolvmem.hooks', '--agent', 'claude-code', '--model', 'claude-opus-4-6', 'session-start'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_hooks.returncode == 0, p_hooks.stderr

    p_kimi = subprocess.run(
        [sys.executable, '-m', 'evolvmem.kimi_hooks', 'session-start', '--agent', 'kimi-code', '--model', 'kimi-k2-thinking'],
        input='{"session_id": "s-kimi-1"}',
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_kimi.returncode == 0, p_kimi.stderr

    p_dsh = subprocess.run(
        [sys.executable, '-m', 'evolvmem.dsh_bridge', '--agent', 'dsh-cli', '--model', 'deepseek-r1', 'inject'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_dsh.returncode == 0, p_dsh.stderr

    intent_script = ROOT / 'scripts/linux/codex-memory-intent.py'
    p_codex = subprocess.run(
        [sys.executable, str(intent_script), '--agent', 'codex-cli', '--model', 'gpt-5.4'],
        input=json.dumps(
            {
                'hook_event_name': 'UserPromptSubmit',
                'session_id': 's-cx-1',
                'turn_id': 't1',
                'prompt': '请记住：月底统一按入账日核对。',
            },
            ensure_ascii=False,
        ),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_codex.returncode == 0, p_codex.stderr
    codex_out = json.loads(p_codex.stdout)
    assert set(codex_out) == {'hookSpecificOutput'}
    hint = codex_out['hookSpecificOutput']['additionalContext']
    assert 'agent=codex' in hint and 'model=gpt-5.4' in hint

    registry = PgAgentRegistry(store)
    for expected_client, expected_model in (
        ('claude', 'claude-opus-4-6'),
        ('kimi', 'kimi-k2-thinking'),
        ('dsh', 'deepseek-r1'),
        ('codex', 'gpt-5.4'),
    ):
        aid = compute_agent_id(actor='user_a', device_id=None, client=expected_client, model=expected_model)
        row = registry.get_agent(aid)
        assert row is not None, f'missing agent row for {(expected_client, expected_model)}'
        assert (row['client'], row['model']) == (expected_client, expected_model)

    ps1_text = (ROOT / 'scripts/windows/evolvmem-codex.ps1').read_text(encoding='utf-8')
    assert 'X-EvolvMem-Agent' in ps1_text
    assert 'X-EvolvMem-Model' in ps1_text
    assert 'EVOLVMEM_AGENT_CLIENT' in ps1_text
