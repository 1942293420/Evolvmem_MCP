"""Task 2.5: Proposal Inbox, experience_propose MCP tool, tier/quota enforcement, and idempotency."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from evolvmem import lan_tools, mcp_server
from evolvmem.experience_v2.control.agent_identity import AgentAttribution, resolve_mcp_client_info
from evolvmem.experience_v2.control.reputation_policy import (
    ReputationPolicyConfig,
    ReputationCoordinator,
    TrustedChannel,
)
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sample_body(**overrides) -> dict:
    base = {
        'op': 'add',
        'category': '系统环境经验',
        'kind': 'pitfall',
        'problem': '容器 DNS 超时导致内网服务不可达',
        'solution': '固定 daemon resolv DNS 地址后重启服务',
        'reusable_experience': '容器网络超时先核对宿主机与容器 DNS 解析链',
        'misbelief': '误以为目标端口防火墙拦截',
        'scope': {'os': 'linux'},
        'evidence': [{'library': 'personal', 'source': 'archive:101', 'event': 3}],
        'importance': 5,
        'pinned': False,
        'session_id': 'sess-t25-001',
        'request_id': 'prop-req-' + uuid.uuid4().hex[:12],
    }
    base.update(overrides)
    return base


def test_proposal_service_tier_quota_and_idempotency(pg_exp_schema):
    from evolvmem.experience_v2.gate.propose import (
        ProposalPolicyError, ProposalService, _ConfigOnlyReputationStore,
    )

    conn, _schema, _dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    policy_cfg = ReputationPolicyConfig(
        model_tiers={
            ('claude', 'claude-sonnet-4'): ('primary', 5),
            ('kimi', 'judge-v1'): ('judge', 0),
        },
        default_tier='general',
        default_quota_per_session=2,
    )
    engine = ReputationCoordinator(_ConfigOnlyReputationStore(), config=policy_cfg)
    svc = ProposalService(reputation_engine=engine)

    # 1. Unknown model defaults to general with session quota = 2
    unknown_attr = resolve_mcp_client_info(
        {'name': 'codex', 'model': 'unknown-x'},
        actor='owner1',
        device_id='dev-1',
    )
    r1 = svc.propose(
        conn,
        owner_id='owner1',
        actor_id='owner1',
        channel=TrustedChannel.MCP_CLIENT,
        attribution=unknown_attr,
        body=_sample_body(request_id='req-gen-1-' + uuid.uuid4().hex[:8]),
    )
    assert r1['ok'] is True
    assert r1['uid'].startswith('prop-')
    assert r1['state'] == 'pending'
    assert r1['tier'] == 'general'
    assert r1['replayed'] is False

    # Same request_id replays once without consuming quota or inserting duplicate row
    req2 = 'req-gen-2-' + uuid.uuid4().hex[:8]
    body2 = _sample_body(request_id=req2)
    r2 = svc.propose(
        conn, owner_id='owner1', actor_id='owner1',
        channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr, body=body2,
    )
    r2_replay = svc.propose(
        conn, owner_id='owner1', actor_id='owner1',
        channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr, body=body2,
    )
    assert r2_replay['uid'] == r2['uid']
    assert r2_replay['replayed'] is True

    # Mutated payload on same request_id fails 409
    with pytest.raises(ProposalPolicyError) as conflict_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr,
            body=_sample_body(request_id=req2, problem='篡改问题文本'),
        )
    assert conflict_exc.value.status == 409
    assert conflict_exc.value.code == 'proposal_request_id_conflict'

    # 3rd proposal by general model in same session hits quota=2
    with pytest.raises(ProposalPolicyError) as quota_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr,
            body=_sample_body(request_id='req-gen-3-' + uuid.uuid4().hex[:8]),
        )
    assert quota_exc.value.code == 'session_quota_exceeded'

    # General tier cannot submit update/retire or high importance (>=9 / pinned)
    with pytest.raises(ProposalPolicyError) as op_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr,
            body=_sample_body(op='update', target_uid='exp-' + '0' * 26, session_id='sess-other'),
        )
    assert op_exc.value.code == 'operation_forbidden_for_tier'

    with pytest.raises(ProposalPolicyError) as hi_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=unknown_attr,
            body=_sample_body(importance=9, session_id='sess-other'),
        )
    assert hi_exc.value.code == 'pinned_or_high_importance_forbidden'

    # 2. Primary tier allows update/retire and spoofed clientInfo clamps back to general
    primary_attr = resolve_mcp_client_info(
        {'name': 'claude-code', 'model': 'claude-sonnet-4'}, actor='owner1', device_id='dev-1'
    )
    r_upd = svc.propose(
        conn, owner_id='owner1', actor_id='owner1',
        channel=TrustedChannel.MCP_CLIENT, attribution=primary_attr,
        body=_sample_body(op='update', target_uid='exp-' + 'a' * 26, session_id='sess-p1'),
    )
    assert r_upd['tier'] == 'primary'

    spoofed_attr = resolve_mcp_client_info(
        {'name': 'claude-code', 'model': 'claude-sonnet-4', 'tier': 'user', 'role': 'admin'},
        actor='owner1', device_id='dev-1',
    )
    assert spoofed_attr.spoof_attempt_stripped is True
    with pytest.raises(ProposalPolicyError) as spoof_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=spoofed_attr,
            body=_sample_body(op='update', target_uid='exp-' + 'b' * 26, session_id='sess-p1'),
        )
    assert spoof_exc.value.code == 'operation_forbidden_for_tier'

    # 3. Judge tier cannot propose
    judge_attr = resolve_mcp_client_info({'name': 'kimi', 'model': 'judge-v1'}, actor='owner1', device_id='dev-1')
    with pytest.raises(ProposalPolicyError) as judge_exc:
        svc.propose(
            conn, owner_id='owner1', actor_id='owner1',
            channel=TrustedChannel.MCP_CLIENT, attribution=judge_attr,
            body=_sample_body(session_id='sess-j1'),
        )
    assert judge_exc.value.code == 'operation_forbidden_for_tier'
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []


def test_experience_propose_mcp_wiring_via_stdio_and_policy(pg_exp_schema, tmp_path: Path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(
        json.dumps({'console_enabled': True, 'experience_v2_enabled': True}),
        encoding='utf-8',
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)

    from evolvmem.experience_v2.mcp_tools import EXPERIENCE_V2_MCP_SPECS, call_experience_v2_tool

    assert 'experience_propose' in EXPERIENCE_V2_MCP_SPECS
    attr = AgentAttribution(actor='owner1', agent_id='agent-1', client='codex', model='unknown-model')
    res = call_experience_v2_tool(
        'experience_propose',
        _sample_body(request_id='mcp-prop-' + uuid.uuid4().hex[:10]),
        owner_id='owner1',
        actor_id='owner1',
        attribution=attr,
        channel=TrustedChannel.MCP_CLIENT,
    )
    assert res['ok'] is True
    assert res['uid'].startswith('prop-')
    assert res['tier'] == 'general'
    # Verify mcp_server and lan_tools imported and registered specs hook
    assert hasattr(mcp_server, 'MemoryMCPServer')
    assert hasattr(lan_tools, 'LanTools')
