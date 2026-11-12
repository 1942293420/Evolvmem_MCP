"""Task 1.2: Behavioral tests for em_control.agents, MCP clientInfo, hook --agent/--model, and removing forced codex."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
import uuid

import pytest

from tests.library_foundation.test_ddl_contract import security_findings
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


def test_static_sql_contract_008():
    sql_008 = ROOT / 'evolvmem/library_catalog/sql/008_agents_and_model_reputation.sql'
    assert sql_008.is_file(), '008_agents_and_model_reputation.sql missing'
    assert security_findings(sql_008.read_text(encoding='utf-8')) == []


def test_agents_table_registration_and_timestamp_progression(pg_control_v8, tmp_path):
    from evolvmem.experience_v2.control.agent_identity import PgAgentRegistry

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    dev_id = str(uuid.uuid4())
    store.register_trusted_device(
        'user_a', device_id=dev_id, external_device_id='macbook-pro-a', token='tok-agent-a'
    )

    registry = PgAgentRegistry(store)
    rec_1 = registry.record_seen(
        actor='user_a',
        device_id=dev_id,
        client_info={'name': 'claude-code', 'version': '1.0.5', 'model': 'claude-opus-4-6'},
    )
    assert rec_1.actor == 'user_a'
    assert rec_1.client == 'claude'
    assert rec_1.model == 'claude-opus-4-6'
    assert rec_1.device_id == dev_id

    stored_1 = registry.get_agent(rec_1.agent_id)
    assert stored_1 is not None
    assert stored_1['client'] == 'claude'
    assert stored_1['model'] == 'claude-opus-4-6'

    time.sleep(0.02)
    rec_2 = registry.record_seen(
        actor='user_a',
        device_id=dev_id,
        client_info={'name': 'claude-code', 'model': 'claude-opus-4-6'},
    )
    stored_2 = registry.get_agent(rec_2.agent_id)
    assert stored_2['first_seen'] == stored_1['first_seen']
    assert stored_2['last_seen'] >= stored_1['last_seen']

    listed = registry.list_for_device(dev_id)
    assert len(listed) == 1 and listed[0]['agent_id'] == rec_1.agent_id


def test_lan_runtime_no_longer_forces_codex_adapter(tmp_path):
    from evolvmem.lan_config import LanSettings
    from evolvmem.lan_runtime import LanRuntime

    lan_py = (ROOT / 'evolvmem/lan_runtime.py').read_text(encoding='utf-8')
    assert 'config.adapter = "codex"' not in lan_py
    assert "config.adapter = 'codex'" not in lan_py

    owner_dir = init_sqlite_source(tmp_path / 'owner')
    lan_dir = tmp_path / 'lan'
    lan_dir.mkdir()
    settings = LanSettings(
        data_dir=lan_dir,
        owner_data_dir=owner_dir,
        token_hashes={
            'jiangli': hashlib.sha256(b'tok-1').hexdigest(),
            'kane': hashlib.sha256(b'tok-2').hexdigest(),
        },
        embedding_enabled=False,
    )
    rt = LanRuntime(settings)
    rt.initialize()
    try:
        ns_cfg = rt._namespace_config(owner_dir)
        assert ns_cfg.adapter != 'codex'
        srv = rt.server_for('jiangli')
        status_res = srv.handle_tool_call('context_status', {})
        assert isinstance(status_res, dict) and 'error' not in status_res
    finally:
        rt.close()


def test_mcp_initialize_captures_client_info_across_mcp_lan_and_library_host(pg_control_v8, tmp_path):
    from evolvmem.config import Config
    from evolvmem.experience_v2.control.agent_identity import PgAgentRegistry
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
    from evolvmem.lan_config import LanSettings
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_tools import LanTools
    from evolvmem.library_catalog import ControlBinding
    from evolvmem.library_foundation import ConnectionBinding
    from evolvmem.library_host import LibraryHost
    from evolvmem.library_startup import FileSecretResolver
    from evolvmem.mcp_server import MemoryMCPServer

    _, store, cfg = build_pg_console(pg_control_v8, tmp_path)
    registry = PgAgentRegistry(store)
    dev_a, cred_a = str(uuid.uuid4()), str(uuid.uuid4())
    p_a = store.register_trusted_device(
        'user_a', device_id=dev_a, external_device_id='box-a', token='tok-libhost-a', credential_id=cred_a
    )

    # 1. Standalone MemoryMCPServer initialize
    data_dir = init_sqlite_source(tmp_path / 'standalone')
    mcp_cfg = Config(data_dir=data_dir, apply_environment=False)
    mcp_srv = MemoryMCPServer(mcp_cfg, embedding_engine=None, agent_registry=registry, actor_id='user_a', device_id=dev_a)
    mcp_srv.initialize()
    mcp_srv._init_done.set()
    try:
        resp = mcp_srv._handle_request({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2024-11-05',
                'clientInfo': {'name': 'kimi-cli', 'version': '2.1.0', 'model': 'kimi-k2.5'},
            },
        })
        assert resp['result']['protocolVersion'] == '2024-11-05'
        attr = mcp_srv.current_attribution()
        assert (attr.actor, attr.client, attr.model) == ('user_a', 'kimi', 'kimi-k2.5')
        assert registry.get_agent(attr.agent_id) is not None
    finally:
        mcp_srv.shutdown()

    # 2. LAN LanTools initialize per authenticated user
    lan_dir = tmp_path / 'lan_mcp'
    lan_dir.mkdir()
    settings = LanSettings(
        data_dir=lan_dir,
        owner_data_dir=data_dir,
        token_hashes={
            'jiangli': hashlib.sha256(b't1').hexdigest(),
            'kane': hashlib.sha256(b't2').hexdigest(),
        },
        embedding_enabled=False,
    )
    rt = LanRuntime(settings)
    rt.initialize()
    try:
        lan_tools = LanTools(rt, agent_registry=registry)
        init_res = lan_tools.handle_request(
            'jiangli',
            {
                'jsonrpc': '2.0',
                'id': 2,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'Codex', 'version': '0.9.0', 'model': 'gpt-5.4'},
                },
            },
        )
        assert 'result' in init_res
        lan_attr = lan_tools.attribution_for('jiangli')
        assert (lan_attr.actor, lan_attr.client, lan_attr.model) == ('jiangli', 'codex', 'gpt-5.4')
    finally:
        rt.close()

    # 3. LibraryHost initialize with authenticated RequestIdentity
    conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
    host = LibraryHost(
        control_binding=ControlBinding(cfg.host, cfg.port, cfg.database, cfg.reader_role, cfg.reader_password_ref),
        content_bindings=(
            ConnectionBinding('content.dummy', str(uuid.uuid4()), p_a, cred_a, dev_a, TEST_PG_HOST, TEST_PG_PORT, 'postgres', cfg.reader_role, cfg.reader_password_ref),
        ),
        control_connector=conn_fn,
        content_connector=conn_fn,
        secret_resolver=FileSecretResolver(cfg.secret_files),
        config=mcp_cfg,
        model_contract='bge-m3-test',
        enabled=True,
        agent_registry=registry,
        actor_resolver=lambda principal_id: 'user_a' if principal_id == p_a else principal_id,
    )
    published = []
    try:
        with host.request_scope():
            ident = host.authenticate('tok-libhost-a')
            host.serve_rpc(
                ident,
                {
                    'jsonrpc': '2.0',
                    'id': 3,
                    'method': 'initialize',
                    'params': {
                        'protocolVersion': '2025-03-26',
                        'clientInfo': {'name': 'claude-code', 'version': '1.2.0', 'model': 'claude-sonnet-4-6'},
                    },
                },
                publish=published.append,
            )
        assert published and published[0]['result']['protocolVersion'] == '2025-03-26'
        host_attr = host.last_attribution(dev_a)
        assert (host_attr.actor, host_attr.client, host_attr.model) == ('user_a', 'claude', 'claude-sonnet-4-6')
        assert registry.get_agent(host_attr.agent_id) is not None
    finally:
        host.close()


def test_hooks_cli_accept_agent_and_model_arguments():
    from evolvmem.dsh_bridge import parse_agent_model_args as dsh_parse
    from evolvmem.hooks import parse_agent_model_args as hooks_parse
    from evolvmem.kimi_hooks import parse_agent_model_args as kimi_parse

    h_attr, h_rest = hooks_parse(['--agent', 'claude-code', '--model', 'claude-sonnet-4-6', 'session-start'])
    assert (h_attr.client, h_attr.model) == ('claude', 'claude-sonnet-4-6')
    assert h_rest == ['session-start']

    k_attr, k_rest = kimi_parse(['--agent', 'kimi', '--model', 'kimi-k2-thinking', 'session-start'])
    assert (k_attr.client, k_attr.model) == ('kimi', 'kimi-k2-thinking')
    assert k_rest == ['session-start']

    d_attr, d_rest = dsh_parse(['--agent', 'dsh', '--model', 'deepseek-r1', 'inject'])
    assert (d_attr.client, d_attr.model) == ('dsh', 'deepseek-r1')
    assert d_rest == ['inject']

    intent_script = ROOT / 'scripts/linux/codex-memory-intent.py'
    proc = subprocess.run(
        [sys.executable, str(intent_script), '--agent', 'codex', '--model', 'gpt-5.4', '--help'],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    ps1_text = (ROOT / 'scripts/windows/evolvmem-codex.ps1').read_text(encoding='utf-8')
    assert '[string]$Agent' in ps1_text and '[string]$Model' in ps1_text
