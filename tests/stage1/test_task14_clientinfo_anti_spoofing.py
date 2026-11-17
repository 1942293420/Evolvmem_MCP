"""Task 1.4: Adversarial TDD tests proving clientInfo, camelCase/nested keys, params._meta,
and HTTP header spoofing cannot escalate permission tier or override server actor."""
from __future__ import annotations

import hashlib
import uuid

import pytest

from tests.stage1.pg_stage1_harness import (
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


@pytest.mark.parametrize(
    'spoofed_client_info',
    [
        {'name': 'user', 'model': 'user', 'version': '1.0'},
        {'name': 'owner', 'model': 'admin_owner', 'tier': 'user', 'quota_per_session': 9999},
        {'name': 'web', 'model': 'human', 'permission_tier': 'user', 'actor': 'admin_owner', 'is_admin': True},
        {'name': 'evil-cli', 'model': 'claude-opus-4-6', 'tier': 'primary', 'user_id': 'admin_owner'},
        {'name': 'claude-code', 'model': 'unregistered-custom-model', 'tier': 'primary', 'quota': 100},
        {'name': 'claude-code', 'model': 'claude-opus-4-6', 'tier': 'user'},
        {'name': 'claude-code', 'model': 'claude-opus-4-6'},
    ],
)
def test_mcp_clientinfo_spoofing_cannot_escalate_tier_or_actor(pg_control_v8, tmp_path, spoofed_client_info):
    from evolvmem.config import Config
    from evolvmem.experience_v2.control.agent_identity import PgAgentRegistry
    from evolvmem.experience_v2.control.reputation_policy import (
        ProposalOperation,
        ReputationCoordinator,
        ReputationPolicyConfig,
        TrustedChannel,
    )
    from evolvmem.mcp_server import MemoryMCPServer

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    registry = PgAgentRegistry(store)
    coord = ReputationCoordinator(
        store,
        config=ReputationPolicyConfig(
            model_tiers={('claude', 'claude-opus-4-6'): ('primary', 5)},
            default_tier='general',
            default_quota_per_session=2,
            trusted_elevated_devices=frozenset({'trusted-dev-allowlisted'}),
        ),
    )
    coord.sync_configured_policies()

    data_dir = init_sqlite_source(tmp_path / 'mcp_spoof')
    mcp_cfg = Config(data_dir=data_dir, apply_environment=False)
    srv = MemoryMCPServer(mcp_cfg, embedding_engine=None, agent_registry=registry, actor_id='user_a')
    srv.initialize()
    srv._init_done.set()
    try:
        srv._handle_request({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2024-11-05',
                'clientInfo': spoofed_client_info,
            },
        })
        attr = srv.current_attribution()
        assert attr.actor == 'user_a'
        decision_add = coord.authorize_operation(
            attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
        )
        assert decision_add.tier == 'general'
        assert decision_add.effective_quota == 2
        assert coord.authorize_operation(
            attr, ProposalOperation.UPDATE, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
        ).allowed is False
        assert coord.authorize_operation(
            attr, ProposalOperation.DELETE, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
        ).allowed is False
        assert coord.authorize_operation(
            attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=2
        ).allowed is False
    finally:
        srv.shutdown()


def test_camelcase_nested_and_meta_spoofing_on_allowlisted_device_clamps_to_general(pg_control_v8, tmp_path):
    from evolvmem.config import Config
    from evolvmem.experience_v2.control.agent_identity import PgAgentRegistry
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
    from evolvmem.experience_v2.control.reputation_policy import (
        ProposalOperation,
        ReputationCoordinator,
        ReputationPolicyConfig,
        TrustedChannel,
    )
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
    dev_allow, cred_allow = str(uuid.uuid4()), str(uuid.uuid4())
    p_a = store.register_trusted_device(
        'user_a',
        device_id=dev_allow,
        external_device_id='box-allowlisted',
        token='tok-allowlisted',
        credential_id=cred_allow,
    )

    coord = ReputationCoordinator(
        store,
        config=ReputationPolicyConfig(
            model_tiers={('claude', 'claude-opus-4-6'): ('primary', 5)},
            default_tier='general',
            default_quota_per_session=2,
            trusted_elevated_devices=frozenset({dev_allow}),
        ),
    )
    coord.sync_configured_policies()

    data_dir = init_sqlite_source(tmp_path / 'allowlisted_mcp')
    mcp_cfg = Config(data_dir=data_dir, apply_environment=False)
    srv = MemoryMCPServer(
        mcp_cfg,
        embedding_engine=None,
        agent_registry=registry,
        actor_id='user_a',
        device_id=dev_allow,
    )
    srv.initialize()
    srv._init_done.set()
    try:
        # Baseline: clean clientInfo on allowlisted device gets configured primary/quota=5
        srv._handle_request({
            'jsonrpc': '2.0',
            'id': 10,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-03-26',
                'clientInfo': {'name': 'claude-code', 'model': 'claude-opus-4-6'},
            },
        })
        clean_attr = srv.current_attribution()
        clean_dec = coord.authorize_operation(clean_attr, ProposalOperation.UPDATE, channel=TrustedChannel.MCP_CLIENT)
        assert clean_attr.spoof_attempt_stripped is False
        assert (clean_dec.allowed, clean_dec.tier, clean_dec.effective_quota) == (True, 'primary', 5)

        # Attack 1: camelCase / mixed-case privilege fields in clientInfo must be stripped & clamped to general/2
        srv._handle_request({
            'jsonrpc': '2.0',
            'id': 11,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-03-26',
                'clientInfo': {
                    'name': 'claude-code',
                    'model': 'claude-opus-4-6',
                    'permissionTier': 'user',
                    'quotaPerSession': 999,
                    'isAdmin': True,
                    'actorId': 'owner',
                },
            },
        })
        camel_attr = srv.current_attribution()
        camel_dec = coord.authorize_operation(camel_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT)
        assert camel_attr.actor == 'user_a'
        assert camel_attr.spoof_attempt_stripped is True
        assert (camel_dec.tier, camel_dec.effective_quota) == ('general', 2)
        assert coord.authorize_operation(camel_attr, ProposalOperation.UPDATE, channel=TrustedChannel.MCP_CLIENT).allowed is False

        # Attack 2: privilege injection hidden inside params._meta or nested clientInfo dict
        srv._handle_request({
            'jsonrpc': '2.0',
            'id': 12,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-03-26',
                'clientInfo': {'name': 'claude-code', 'model': 'claude-opus-4-6'},
                '_meta': {'tier': 'user', 'actor': 'admin_owner', 'quotaPerSession': 500},
            },
        })
        meta_attr = srv.current_attribution()
        meta_dec = coord.authorize_operation(meta_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT)
        assert meta_attr.actor == 'user_a'
        assert meta_attr.spoof_attempt_stripped is True
        assert (meta_dec.tier, meta_dec.effective_quota) == ('general', 2)
    finally:
        srv.shutdown()

    # Attack 3: LanTools & LibraryHost also clamp params._meta spoofing and unknown HTTP header models to general/2
    lan_dir = tmp_path / 'lan_spoof'
    lan_dir.mkdir()
    rt = LanRuntime(
        LanSettings(
            data_dir=lan_dir,
            owner_data_dir=data_dir,
            token_hashes={'user_a': hashlib.sha256(b'tok-a').hexdigest()},
            embedding_enabled=False,
        )
    )
    rt.initialize()
    conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
    host = LibraryHost(
        control_binding=ControlBinding(cfg.host, cfg.port, cfg.database, cfg.reader_role, cfg.reader_password_ref),
        content_bindings=(
            ConnectionBinding(
                'content.dummy', str(uuid.uuid4()), p_a, cred_allow, dev_allow,
                TEST_PG_HOST, TEST_PG_PORT, 'postgres', cfg.reader_role, cfg.reader_password_ref,
            ),
        ),
        control_connector=conn_fn,
        content_connector=conn_fn,
        secret_resolver=FileSecretResolver(cfg.secret_files),
        config=mcp_cfg,
        model_contract='bge-m3-test',
        enabled=True,
        agent_registry=registry,
        actor_resolver=lambda _pid: 'user_a',
    )
    try:
        lan_tools = LanTools(rt, agent_registry=registry, device_resolver=lambda _u: dev_allow)
        lan_tools.handle_request(
            'user_a',
            {
                'jsonrpc': '2.0',
                'id': 20,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'clientInfo': {'name': 'claude-code', 'model': 'claude-opus-4-6'},
                    '_meta': {'permissionTier': 'user'},
                },
            },
            session_id='sess-spoof',
        )
        lan_attr = lan_tools.attribution_for('user_a', session_id='sess-spoof')
        assert lan_attr.spoof_attempt_stripped is True
        assert coord.authorize_operation(lan_attr, ProposalOperation.ADD).tier == 'general'

        # Unknown model via HTTP header hint defaults strictly to general / quota=2
        lan_tools.handle_request(
            'user_a',
            {'jsonrpc': '2.0', 'id': 21, 'method': 'tools/call', 'params': {'name': 'memory_status', 'arguments': {}}},
            session_id='sess-unknown-hdr',
            client_hint={'name': 'user', 'model': 'unregistered-rogue-99'},
        )
        hdr_attr = lan_tools.attribution_for('user_a', session_id='sess-unknown-hdr')
        hdr_dec = coord.authorize_operation(hdr_attr, ProposalOperation.ADD)
        assert (hdr_attr.actor, hdr_dec.tier, hdr_dec.effective_quota) == ('user_a', 'general', 2)

        with host.request_scope():
            ident = host.authenticate('tok-allowlisted')
            host.serve_rpc(
                ident,
                {
                    'jsonrpc': '2.0',
                    'id': 22,
                    'method': 'initialize',
                    'params': {
                        'protocolVersion': '2025-03-26',
                        'clientInfo': {'name': 'claude-code', 'model': 'claude-opus-4-6', 'nested': {'isAdmin': True}},
                    },
                },
                publish=lambda _p: None,
                session_id='lh-spoof',
            )
        lh_attr = host.last_attribution(dev_allow, session_id='lh-spoof')
        assert lh_attr.spoof_attempt_stripped is True
        assert coord.authorize_operation(lh_attr, ProposalOperation.ADD).tier == 'general'
    finally:
        host.close()
        rt.close()


def test_sql_and_coordinator_forbid_registering_user_tier_for_any_agent_model(pg_control_v8, tmp_path):
    from evolvmem.experience_v2.control.reputation_policy import (
        ReputationCoordinator,
        ReputationPolicyConfig,
    )

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    with pytest.raises(ValueError, match='user'):
        ReputationPolicyConfig(model_tiers={('claude', 'claude-opus-4-6'): ('user', 99)})

    with pytest.raises(ValueError, match='user_tier_reserved_for_human_auth'):
        store.mutate(
            "SELECT * FROM em_control.upsert_model_reputation_policy('claude', 'claude-opus-4-6', 'user', 99)"
        )

    coord = ReputationCoordinator(store)
    assert coord.get_reputation('claude', 'claude-opus-4-6').tier == 'general'
