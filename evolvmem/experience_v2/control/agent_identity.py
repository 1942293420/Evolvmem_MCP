"""Agent/model attribution normalization, CLI parser, active context, and PostgreSQL em_control.agents registry."""
from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
import re
from typing import Sequence
import uuid

_CLIENT_ALIASES = {
    'claude': 'claude',
    'claude-code': 'claude',
    'claude_code': 'claude',
    'claude-desktop': 'claude',
    'anthropic-claude': 'claude',
    'codex': 'codex',
    'codex-cli': 'codex',
    'openai-codex': 'codex',
    'kimi': 'kimi',
    'kimi-cli': 'kimi',
    'kimi-code': 'kimi',
    'kimi_code': 'kimi',
    'dsh': 'dsh',
    'dsh-cli': 'dsh',
    'cursor': 'cursor',
}
_SAFE_CLIENT_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
_UNSAFE_MODEL_RE = re.compile(r'[\x00-\x1f\x7f]')
# Fields inside clientInfo that must never be trusted to set actor, tier, or quota.
SPOOFABLE_CLIENT_INFO_KEYS = frozenset({
    'actor', 'actor_id', 'user', 'user_id', 'owner', 'owner_id',
    'principal', 'principal_id', 'is_admin', 'role', 'tier',
    'permission_tier', 'quota', 'quota_per_session',
})
_RESERVED_CLI_SUBCOMMANDS = frozenset({
    'session-start', 'session-end', 'heartbeat', 'stop',
    'inject', 'recall', 'extract', 'status', 'self-test',
    'worker', 'upload', 'snapshot', 'prompt-submit', 'discover-history',
})


@dataclass(frozen=True, slots=True)
class AgentAttribution:
    actor: str
    agent_id: str
    client: str
    model: str
    device_id: str | None = None
    client_version: str = ''
    spoof_attempt_stripped: bool = False


_ACTIVE_ATTRIBUTION: ContextVar[AgentAttribution | None] = ContextVar(
    'evolvmem_active_attribution', default=None
)


def get_active_attribution() -> AgentAttribution | None:
    """Return the AgentAttribution bound to the current MCP tools/call or CLI hook execution."""
    return _ACTIVE_ATTRIBUTION.get()


@contextmanager
def bind_active_attribution(attr: AgentAttribution | None):
    """Bind AgentAttribution into ContextVar for the duration of a tools/call or hook invocation."""
    token = _ACTIVE_ATTRIBUTION.set(attr)
    try:
        yield attr
    finally:
        _ACTIVE_ATTRIBUTION.reset(token)


def normalize_client_slug(raw_name: object, *, default: str = 'unknown') -> str:
    if not isinstance(raw_name, str) or not raw_name.strip():
        return default
    cleaned = raw_name.strip().lower()
    if cleaned in _CLIENT_ALIASES:
        return _CLIENT_ALIASES[cleaned]
    for prefix, canonical in (('claude', 'claude'), ('codex', 'codex'), ('kimi', 'kimi'), ('dsh', 'dsh')):
        if cleaned.startswith(prefix + '-') or cleaned.startswith(prefix + '_'):
            return canonical
    slug = re.sub(r'[^a-z0-9._-]+', '-', cleaned).strip('-._')[:64]
    if not slug or not _SAFE_CLIENT_RE.match(slug):
        return default
    return slug


def normalize_model_name(raw_model: object, *, default: str = 'unknown') -> str:
    if not isinstance(raw_model, str) or not raw_model.strip():
        return default
    cleaned = _UNSAFE_MODEL_RE.sub('', raw_model.strip())[:128]
    return cleaned if cleaned else default


def compute_agent_id(*, actor: str, device_id: str | None, client: str, model: str) -> str:
    scope = device_id if device_id else f'actor:{actor}'
    digest = hashlib.sha256(f'{scope}|{client}|{model}'.encode('utf-8')).hexdigest()[:24]
    return f'ag-{client[:16]}-{digest}'


def resolve_mcp_client_info(
    client_info: object,
    *,
    actor: str,
    device_id: str | None = None,
    default_client: str = 'unknown',
    default_model: str = 'unknown',
) -> AgentAttribution:
    """Resolve (actor, agent, model) from MCP initialize params.clientInfo.

    Actor comes strictly from server-side authentication, NEVER from clientInfo.
    Any privilege/tier/actor override keys inside clientInfo are stripped and flagged.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError('authenticated server actor is required')
    spoofed = False
    raw_name: object = None
    raw_model: object = None
    version = ''
    if isinstance(client_info, dict):
        if SPOOFABLE_CLIENT_INFO_KEYS.intersection(client_info):
            spoofed = True
        raw_name = client_info.get('name')
        raw_model = client_info.get('model')
        raw_ver = client_info.get('version')
        if isinstance(raw_ver, str):
            version = _UNSAFE_MODEL_RE.sub('', raw_ver.strip())[:64]
    client = normalize_client_slug(raw_name, default=default_client)
    model = normalize_model_name(raw_model, default=default_model)
    valid_device = _valid_uuid_or_none(device_id)
    agent_id = compute_agent_id(actor=actor.strip(), device_id=valid_device, client=client, model=model)
    return AgentAttribution(
        actor=actor.strip(),
        agent_id=agent_id,
        client=client,
        model=model,
        device_id=valid_device,
        client_version=version,
        spoof_attempt_stripped=spoofed,
    )


def _validate_cli_flag_value(flag: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.startswith('-') or cleaned.lower() in _RESERVED_CLI_SUBCOMMANDS:
        raise ValueError(f'invalid or missing value for {flag}: {value!r}')
    return cleaned


def parse_cli_agent_model(
    argv: Sequence[str],
    *,
    default_client: str = 'unknown',
    default_model: str = 'unknown',
    actor: str | None = None,
) -> tuple[AgentAttribution, list[str]]:
    """Extract --agent / --model flags from CLI argv without swallowing flags or subcommands."""
    resolved_actor = (
        actor.strip()
        if isinstance(actor, str) and actor.strip()
        else (os.environ.get('EVOLVMEM_AGENT_ACTOR') or os.environ.get('EVOLVMEM_CONSOLE_OWNER_ID') or 'local').strip() or 'local'
    )
    raw_agent: str | None = None
    raw_model: str | None = None
    remaining: list[str] = []
    idx = 0
    items = list(argv)
    while idx < len(items):
        token = items[idx]
        if token == '--':
            remaining.extend(items[idx + 1:])
            break
        if token in ('--agent', '--model'):
            if idx + 1 >= len(items):
                raise ValueError(f'{token} requires a value')
            val = _validate_cli_flag_value(token, items[idx + 1])
            if token == '--agent':
                raw_agent = val
            else:
                raw_model = val
            idx += 2
            continue
        if token.startswith('--agent='):
            raw_agent = _validate_cli_flag_value('--agent', token.split('=', 1)[1])
            idx += 1
            continue
        if token.startswith('--model='):
            raw_model = _validate_cli_flag_value('--model', token.split('=', 1)[1])
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    client = normalize_client_slug(raw_agent, default=default_client)
    model = normalize_model_name(raw_model, default=default_model)
    agent_id = compute_agent_id(actor=resolved_actor, device_id=None, client=client, model=model)
    return AgentAttribution(actor=resolved_actor, agent_id=agent_id, client=client, model=model), remaining


def activate_cli_attribution(
    attr: AgentAttribution,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> AgentAttribution:
    """Apply CLI --agent/--model attribution to process env, ContextVar, and optional PG em_control.agents."""
    env = os.environ if environ is None else environ
    env['EVOLVMEM_AGENT_CLIENT'] = attr.client
    env['EVOLVMEM_AGENT_MODEL'] = attr.model
    env['EVOLVMEM_AGENT_ID'] = attr.agent_id
    _ACTIVE_ATTRIBUTION.set(attr)
    pg_cfg_file = (env.get('EVOLVMEM_CONSOLE_PG_CONFIG') or '').strip()
    if pg_cfg_file:
        try:
            from .pg_console_store import PgConsoleStore, PgControlConfig

            store = PgConsoleStore(PgControlConfig.from_file(pg_cfg_file))
            PgAgentRegistry(store).record_attribution(attr)
        except Exception:
            # CLI hooks remain fail-open when PG control is transiently unavailable.
            pass
    return attr


def _valid_uuid_or_none(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class PgAgentRegistry:
    """PostgreSQL-backed registry for em_control.agents."""

    def __init__(self, store) -> None:
        self._store = store

    def record_attribution(self, attr: AgentAttribution) -> AgentAttribution:
        self._store.mutate(
            'SELECT * FROM em_control.upsert_agent_seen(%s,%s,%s,%s)',
            (attr.agent_id, attr.device_id, attr.client, attr.model),
        )
        return attr

    def record_seen(
        self,
        *,
        actor: str,
        client_info: object = None,
        device_id: str | None = None,
        default_client: str = 'unknown',
        default_model: str = 'unknown',
    ) -> AgentAttribution:
        attr = resolve_mcp_client_info(
            client_info,
            actor=actor,
            device_id=device_id,
            default_client=default_client,
            default_model=default_model,
        )
        return self.record_attribution(attr)

    def get_agent(self, agent_id: str) -> dict | None:
        rows = self._store.query('SELECT * FROM em_control.get_agent(%s)', (agent_id,))
        if not rows:
            return None
        r = rows[0]
        return {
            'agent_id': r['agent_id'],
            'device_id': str(r['device_id']) if r['device_id'] else None,
            'client': r['client'],
            'model': r['model'],
            'first_seen': r['first_seen'],
            'last_seen': r['last_seen'],
        }

    def list_for_device(self, device_id: str) -> list[dict]:
        rows = self._store.query('SELECT * FROM em_control.list_agents_for_device(%s)', (device_id,))
        return [
            {
                'agent_id': r['agent_id'],
                'device_id': str(r['device_id']) if r['device_id'] else None,
                'client': r['client'],
                'model': r['model'],
                'first_seen': r['first_seen'],
                'last_seen': r['last_seen'],
            }
            for r in rows
        ]
