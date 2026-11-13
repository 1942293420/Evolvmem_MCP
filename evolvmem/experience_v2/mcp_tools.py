"""Task 2.5/2.10: MCP specs and handlers for experience_v2 tools."""
from __future__ import annotations

from typing import Any

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import (
    ReputationCoordinator,
    ReputationPolicyConfig,
    TrustedChannel,
)
from evolvmem.experience_v2.gate.propose import (
    CATEGORIES,
    ProposalPolicyError,
    ProposalService,
    VALID_KINDS,
    VALID_OPS,
)
from evolvmem.experience_v2.pg_experience_store import (
    PgExperienceUnavailableError,
    open_experience_conn,
)
from evolvmem.manual_experience_mcp import ManualMcpError
from evolvmem.product_policy import load_policy

EXPERIENCE_V2_MCP_SPECS: dict[str, dict[str, Any]] = {
    'experience_propose': {
        'name': 'experience_propose',
        'description': (
            'Submit a reusable engineering experience proposal into the gated PG inbox '
            'under model tier and session quota enforcement. Returns proposal uid and state.'
        ),
        'annotations': {'readOnlyHint': False, 'openWorldHint': False},
        'inputSchema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'op': {'type': 'string', 'enum': sorted(VALID_OPS)},
                'target_uid': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                'category': {'type': 'string', 'enum': list(CATEGORIES)},
                'kind': {'type': 'string', 'enum': sorted(VALID_KINDS)},
                'problem': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                'solution': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                'reusable_experience': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                'misbelief': {'type': 'string', 'maxLength': 400},
                'scope': {'type': 'object'},
                'evidence': {'type': 'array', 'items': {'type': 'object'}},
                'importance': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                'pinned': {'type': 'boolean'},
                'session_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                'request_id': {'type': 'string', 'minLength': 8, 'maxLength': 128},
                'source_track': {'type': 'string', 'enum': ['live', 'backfill']},
            },
            'required': ['category', 'problem', 'solution', 'reusable_experience', 'request_id'],
        },
    },
}


def call_experience_v2_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    owner_id: str,
    actor_id: str,
    attribution: AgentAttribution,
    channel: TrustedChannel = TrustedChannel.MCP_CLIENT,
    reputation_config: ReputationPolicyConfig | None = None,
    gate_hook=None,
) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ManualMcpError('invalid_arguments')
    policy = load_policy()
    if not policy.valid or not policy.experience_v2_enabled:
        raise ManualMcpError('experience_v2_disabled')
    if tool_name not in EXPERIENCE_V2_MCP_SPECS:
        raise ManualMcpError('unknown_tool')

    spec = EXPERIENCE_V2_MCP_SPECS[tool_name]['inputSchema']
    if set(args) - set(spec['properties']) or set(spec.get('required', [])) - set(args):
        raise ManualMcpError('invalid_arguments')

    from evolvmem.experience_v2.gate.propose import _ConfigOnlyReputationStore
    engine = (
        ReputationCoordinator(_ConfigOnlyReputationStore(), config=reputation_config)
        if reputation_config is not None else None
    )
    svc = ProposalService(reputation_engine=engine)
    try:
        with open_experience_conn(role='coordinator') as conn:
            return svc.propose(
                conn,
                owner_id=owner_id,
                actor_id=actor_id,
                channel=channel,
                attribution=attribution,
                body=args,
                source_kind='agent',
                gate_hook=gate_hook,
            )
    except PgExperienceUnavailableError:
        raise ManualMcpError('pg_experience_unavailable') from None
    except ProposalPolicyError as exc:
        raise ManualMcpError(exc.code) from None
