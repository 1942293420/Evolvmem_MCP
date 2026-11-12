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

__all__ = [
    'AgentAttribution',
    'PgAgentRegistry',
    'PgConsoleStore',
    'PgControlConfig',
    'PgControlUnavailableError',
    'PgPersistentSessions',
    'apply_control_migrations',
    'compute_agent_id',
    'loopback_psycopg_connector',
    'normalize_client_slug',
    'normalize_model_name',
    'parse_cli_agent_model',
    'resolve_mcp_client_info',
]
