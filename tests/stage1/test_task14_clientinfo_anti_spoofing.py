"""Task 1.4: Adversarial TDD tests proving clientInfo spoofing cannot escalate permission tier."""
from __future__ import annotations

import pytest

from tests.stage1.pg_stage1_harness import (
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
        # 1. Server-authenticated actor is NEVER overridden by spoofed clientInfo
        assert attr.actor == 'user_a'
        # 2. Tier resolved over MCP_CLIENT channel is strictly 'general' with quota=2 (never 'user' or 'primary')
        decision_add = coord.authorize_operation(
            attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
        )
        assert decision_add.tier == 'general'
        assert decision_add.effective_quota == 2

        # 3. Privileged operations (UPDATE / RETIRE / DELETE / exceed quota 2) are all blocked
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


def test_sql_and_coordinator_forbid_registering_user_tier_for_any_agent_model(pg_control_v8, tmp_path):
    from evolvmem.experience_v2.control.reputation_policy import (
        ReputationCoordinator,
        ReputationPolicyConfig,
    )

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    with pytest.raises(ValueError, match='user'):
        ReputationPolicyConfig(model_tiers={('claude', 'claude-opus-4-6'): ('user', 99)})

    # Direct SQL RPC attempt to register tier='user' must also be rejected by PostgreSQL
    with pytest.raises(ValueError, match='user_tier_reserved_for_human_auth'):
        store.mutate(
            "SELECT * FROM em_control.upsert_model_reputation_policy('claude', 'claude-opus-4-6', 'user', 99)"
        )

    coord = ReputationCoordinator(store)
    assert coord.get_reputation('claude', 'claude-opus-4-6').tier == 'general'
