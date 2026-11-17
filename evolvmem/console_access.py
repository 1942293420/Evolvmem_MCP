"""Trusted console construction, session store selection, and LAN enabled-account gate.

Supports PostgreSQL control plane (EVOLVMEM_CONTROL_BACKEND=postgres or
EVOLVMEM_CONSOLE_PG_CONFIG) with zero SQLite fallback when PG is unavailable,
while preserving legacy SQLite mode when PG control is not configured.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from evolvmem.experience_v2.control.pg_console_store import (
    PgConsoleStore,
    PgControlConfig,
    PgControlUnavailableError,
)
from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
from evolvmem.knowledge_console import ConsoleError, ConsoleService
from evolvmem.product_policy import load_policy
from evolvmem.web_sessions import PersistentSessions, SessionStoreError


def is_pg_control_configured(environ=None) -> bool:
    env = os.environ if environ is None else environ
    backend = (env.get('EVOLVMEM_CONTROL_BACKEND') or '').strip().lower()
    return backend in ('postgres', 'postgresql', 'pg') or bool((env.get('EVOLVMEM_CONSOLE_PG_CONFIG') or '').strip())


def load_pg_control_store(environ=None) -> PgConsoleStore:
    env = os.environ if environ is None else environ
    cfg_path = (env.get('EVOLVMEM_CONSOLE_PG_CONFIG') or '').strip()
    if not cfg_path:
        raise PgControlUnavailableError('missing_pg_control_config_path')
    return PgConsoleStore(PgControlConfig.from_file(cfg_path))


def configured_console(*, owner_data_dir=None, lan_data_dir=None, history_environ=None, experience_search=None):
    """Build from trusted server configuration; never falls back to SQLite in PG mode."""
    explicit_owner = (os.environ.get('EVOLVMEM_CONSOLE_OWNER_ID') or '').strip() or None
    lan = Path(os.environ.get('EVOLVMEM_LAN_DATA_DIR') or lan_data_dir or
               Path.home() / '.local/share/evolvmem-lan').expanduser()
    personal = Path(owner_data_dir or Path.home() / '.claude/evolvmem').expanduser()
    registry = Path(os.environ.get('EVOLVMEM_CONSOLE_REGISTRY') or
                    Path.home() / '.config/evolvmem-console/users.db').expanduser()
    if is_pg_control_configured():
        try:
            store = load_pg_control_store()
        except PgControlUnavailableError:
            raise ConsoleError('pg_control_unavailable', 503, message='PG 不可用') from None
        return ConsoleService(
            registry, explicit_owner, personal, lan, history_environ or {},
            experience_search=experience_search, pg_control=store,
        )
    legacy_owner = explicit_owner or getpass.getuser()
    return ConsoleService(
        registry, legacy_owner, personal, lan, history_environ or {},
        experience_search=experience_search,
    )


def configured_session_store(console: ConsoleService, auth_scope: str, *, console_sessions: bool, fallback_sqlite_path: Path):
    """Select PG or legacy SQLite persistent Web session store without fallback."""
    if console.pg_mode:
        return PgPersistentSessions(
            console._pg_store,
            auth_scope,
            expected_owner_id=console._expected_owner_id,
            console_mode=console_sessions,
        )
    session_path = console.registry_path if console_sessions else fallback_sqlite_path
    return PersistentSessions(session_path, auth_scope, require_existing=console_sessions)


def console_user_gate(user_id, settings):
    """None permits; otherwise return bounded HTTP status and error payload."""
    pg_mode = is_pg_control_configured()
    try:
        policy = load_policy()
        if not policy.valid:
            return 503, {'error': 'product_policy_unavailable'}
        if not policy.console_enabled:
            return None
        service = configured_console(owner_data_dir=settings.owner_data_dir, lan_data_dir=settings.data_dir)
        if not service.registry_ready():
            if pg_mode:
                return 503, {'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
            return 503, {'error': 'console_registry_unavailable'}
        if not service.user_enabled(user_id):
            return 403, {'error': 'account_disabled'}
        return None
    except (PgControlUnavailableError, SessionStoreError):
        return 503, {'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
    except ConsoleError as exc:
        if pg_mode or exc.code == 'pg_control_unavailable':
            return 503, {'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
        return 503, {'error': 'console_registry_unavailable'}
    except Exception:
        if pg_mode:
            return 503, {'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
        return 503, {'error': 'console_registry_unavailable'}
