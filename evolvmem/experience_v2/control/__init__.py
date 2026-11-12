"""PostgreSQL control-plane adapters for Console, Web sessions, Agents, and Reputation."""
from .agent_identity import (
    AgentAttribution,
    PgAgentRegistry,
    compute_agent_id,
    normalize_client_slug,
    normalize_model_name,
    parse_cli_agent_model,
    resolve_mcp_client_info,
)
from .migrations import apply_control_migrations, loopback_psycopg_connector
from .pg_console_store import PgConsoleStore, PgControlConfig, PgControlUnavailableError
from .pg_web_sessions import PgPersistentSessions
from .reputation_policy import (
    VALID_AGENT_TIERS,
    ModelReputationSnapshot,
    OperationDecision,
    ProposalOperation,
    ReputationCoordinator,
    ReputationPolicyConfig,
    TrustedChannel,
)

__all__ = [
    'AgentAttribution',
    'ModelReputationSnapshot',
    'OperationDecision',
    'PgAgentRegistry',
    'PgConsoleStore',
    'PgControlConfig',
    'PgControlUnavailableError',
    'PgPersistentSessions',
    'ProposalOperation',
    'ReputationCoordinator',
    'ReputationPolicyConfig',
    'TrustedChannel',
    'VALID_AGENT_TIERS',
    'apply_control_migrations',
    'compute_agent_id',
    'loopback_psycopg_connector',
    'normalize_client_slug',
    'normalize_model_name',
    'parse_cli_agent_model',
    'resolve_mcp_client_info',
]
