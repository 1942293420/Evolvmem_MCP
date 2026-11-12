"""Task 1.3: Behavioral TDD tests for em_control.model_reputation, permission tiers, and rolling quota adjustment."""
from __future__ import annotations

from pathlib import Path
import pytest

from tests.stage1.pg_stage1_harness import (
    ROOT,
    create_isolated_control_db,
    drop_isolated_db,
)
from tests.stage1.test_task11_identity_acl import build_pg_console


@pytest.fixture()
def pg_control_v8(tmp_path):
    env = create_isolated_control_db(tmp_path, up_to_version=8)
    try:
        yield env
    finally:
        drop_isolated_db(env.database, (env.coordinator_role, env.reader_role, env.owner_role))


def test_no_hardcoded_usernames_in_reputation_policy():
    policy_py = ROOT / 'evolvmem/experience_v2/control/reputation_policy.py'
    assert policy_py.is_file(), 'reputation_policy.py missing'
    text = policy_py.read_text(encoding='utf-8').lower()
    assert 'jiangli' not in text
    assert 'kane' not in text


def test_tier_permissions_quotas_and_pinned_review_gate(pg_control_v8, tmp_path):
    from evolvmem.experience_v2.control.agent_identity import AgentAttribution
    from evolvmem.experience_v2.control.reputation_policy import (
        ProposalOperation,
        ReputationCoordinator,
        ReputationPolicyConfig,
        TrustedChannel,
    )

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    cfg = ReputationPolicyConfig(
        model_tiers={
            ('claude', 'claude-opus-4-6'): ('primary', 5),
            ('dsh', 'judge-verifier-v1'): ('judge', 0),
        },
        default_tier='general',
        default_quota_per_session=2,
        min_evaluated_samples=10,
    )
    coord = ReputationCoordinator(store, config=cfg)
    coord.sync_configured_policies()

    # 1. Human user via trusted Web UI -> tier='user', unlimited quota, all ops allowed
    human_attr = AgentAttribution(actor='user_a', agent_id='ag-web-human', client='web', model='human')
    d_user = coord.authorize_operation(
        human_attr,
        ProposalOperation.DELETE,
        channel=TrustedChannel.WEB_HUMAN,
        session_proposed_count=99,
        target_pinned=True,
    )
    assert d_user.allowed is True
    assert d_user.tier == 'user'
    assert d_user.effective_quota is None
    assert d_user.requires_user_review is False

    # 2. Primary model -> quota=5, add/update/retire allowed, delete forbidden, pinned requires user review
    prim_attr = AgentAttribution(actor='user_a', agent_id='ag-claude-1', client='claude', model='claude-opus-4-6')
    d_prim_ok = coord.authorize_operation(
        prim_attr,
        ProposalOperation.UPDATE,
        channel=TrustedChannel.MCP_CLIENT,
        session_proposed_count=4,
        target_pinned=True,
    )
    assert d_prim_ok.allowed is True
    assert d_prim_ok.tier == 'primary'
    assert d_prim_ok.effective_quota == 5
    assert d_prim_ok.requires_user_review is True

    d_prim_quota = coord.authorize_operation(
        prim_attr,
        ProposalOperation.ADD,
        channel=TrustedChannel.MCP_CLIENT,
        session_proposed_count=5,
    )
    assert d_prim_quota.allowed is False and d_prim_quota.reason == 'session_quota_exceeded'

    d_prim_del = coord.authorize_operation(
        prim_attr,
        ProposalOperation.DELETE,
        channel=TrustedChannel.MCP_CLIENT,
        session_proposed_count=0,
    )
    assert d_prim_del.allowed is False and d_prim_del.reason == 'operation_forbidden_for_tier'

    # 3. Unknown model -> defaults to general, quota=2, add-only
    gen_attr = AgentAttribution(actor='user_a', agent_id='ag-unk-1', client='kimi', model='kimi-experimental-mini')
    assert coord.authorize_operation(
        gen_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=1
    ).allowed is True
    assert coord.authorize_operation(
        gen_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=2
    ).allowed is False
    for forbidden_op in (ProposalOperation.UPDATE, ProposalOperation.RETIRE, ProposalOperation.DELETE):
        res = coord.authorize_operation(
            gen_attr, forbidden_op, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
        )
        assert res.allowed is False and res.reason == 'operation_forbidden_for_tier'
    assert coord.authorize_operation(
        gen_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0, target_pinned=True
    ).allowed is False

    # 4. Judge model -> quota=0, zero proposals allowed, verdict allowed
    judge_attr = AgentAttribution(actor='user_a', agent_id='ag-judge-1', client='dsh', model='judge-verifier-v1')
    assert coord.authorize_operation(
        judge_attr, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
    ).allowed is False
    assert coord.authorize_operation(
        judge_attr, ProposalOperation.VERDICT, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
    ).allowed is True


def test_rolling_acceptance_rate_suspends_below_30pct_and_boosts_above_80pct(pg_control_v8, tmp_path):
    from evolvmem.experience_v2.control.agent_identity import AgentAttribution
    from evolvmem.experience_v2.control.reputation_policy import (
        ProposalOperation,
        ReputationCoordinator,
        ReputationPolicyConfig,
        TrustedChannel,
    )

    _, store, _ = build_pg_console(pg_control_v8, tmp_path)
    cfg = ReputationPolicyConfig(
        model_tiers={('claude', 'claude-sonnet-4-6'): ('primary', 5)},
        default_tier='general',
        default_quota_per_session=2,
        min_evaluated_samples=10,
    )
    coord = ReputationCoordinator(store, config=cfg)
    coord.sync_configured_policies()

    noisy = AgentAttribution(actor='user_a', agent_id='ag-noisy', client='codex', model='noisy-mini-v1')
    # 10 evaluated: 2 accepted, 8 rejected -> 20% (< 30%) => quota drops to 0 + alert!
    snap_low = coord.record_outcomes(noisy.client, noisy.model, proposed=10, accepted=2, rejected=8)
    assert snap_low.acceptance_rate == pytest.approx(0.20)
    assert snap_low.effective_quota == 0
    assert snap_low.alert == 'low_acceptance_rate'

    denied_low = coord.authorize_operation(
        noisy, ProposalOperation.ADD, channel=TrustedChannel.MCP_CLIENT, session_proposed_count=0
    )
    assert denied_low.allowed is False
    assert denied_low.reason == 'reputation_suspended_low_acceptance'
    assert denied_low.alert == 'low_acceptance_rate'

    strong = AgentAttribution(actor='user_a', agent_id='ag-strong', client='claude', model='claude-sonnet-4-6')
    # 10 evaluated: 9 accepted, 1 rejected -> 90% (> 80%) => boost_eligible & effective_quota > 5!
    snap_high = coord.record_outcomes(strong.client, strong.model, proposed=10, accepted=9, rejected=1)
    assert snap_high.acceptance_rate == pytest.approx(0.90)
    assert snap_high.boost_eligible is True
    assert snap_high.effective_quota > 5
