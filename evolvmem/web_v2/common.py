"""Shared request parsing, ACL resolution, and PG helpers for Web Console v2."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from typing import Any

from evolvmem.experience_v2.pg_experience_store import (
    PgExperienceUnavailableError,
    load_pg_experience_settings,
    open_experience_conn,
)


class V2ApiError(ValueError):
    """Structured API error carrying HTTP status code and machine error slug."""

    def __init__(self, code: str, status: int = 400, detail: str = ''):
        super().__init__(code)
        self.code = code
        self.status = int(status)
        self.detail = detail


def iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    return str(val)


def jval(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def parse_iso_dt(raw: Any) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    norm = text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError as exc:
        raise V2ApiError('invalid_timestamp', 400, str(exc)) from None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def parse_bool_flag(raw: Any, default: bool = False) -> bool:
    if raw is None or raw == '':
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        val = int(raw) if raw not in (None, '') else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, val))


def resolve_shadow_mode(policy: Any) -> bool:
    if hasattr(policy, 'shadow_mode'):
        return bool(policy.shadow_mode)
    mode = getattr(policy, 'experience_gate_mode', 'review')
    if isinstance(mode, dict):
        return not (bool(mode) and all(v == 'auto' for v in mode.values()))
    return mode != 'auto'


def resolve_actor_and_library(
    console: Any,
    session: Any,
    target_owner_id: str | None = None,
    *,
    need_experience: bool = False,
    need_history: bool = False,
    require_write: bool = False,
    inspect: bool = False,
) -> tuple[dict[str, Any], str, bool, dict[str, Any], dict[str, Any]]:
    """Verify user session against ConsoleService and fail closed on cross-library ACL."""
    dispatch_params = {'inspect': '1'} if inspect else {}
    status, _, me = console.dispatch('GET', '/me', dispatch_params, session)
    if status != 200 or not isinstance(me, dict) or not me.get('current_user'):
        code = str(me.get('error') or 'login_required') if isinstance(me, dict) else 'login_required'
        raise V2ApiError(code, status if status >= 400 else 401)

    actor = dict(me['current_user'])
    actor_id = str(actor.get('id') or '').strip()
    if not actor_id:
        raise V2ApiError('login_required', 401)

    owner_id = str(target_owner_id or actor_id).strip()
    libs = [row for row in (me.get('libraries') or []) if isinstance(row, dict)]
    lib = next((row for row in libs if str(row.get('id') or '') == owner_id), None)
    if lib is None:
        raise V2ApiError('library_access_denied', 403)

    is_owner = (actor_id == owner_id) and (not inspect)
    if require_write and not is_owner:
        raise V2ApiError('read_only_cross_user', 403)
    if need_experience and not (is_owner or bool(lib.get('can_read_experience'))):
        raise V2ApiError('experience_access_denied', 403)
    if need_history and not (is_owner or bool(lib.get('can_read_history'))):
        raise V2ApiError('history_access_denied', 403)
    return actor, owner_id, is_owner, lib, me


@contextmanager
def open_exp_pg(*, role: str = 'coordinator'):
    """Load PG experience settings and yield (settings, conn) with structured 503 errors."""
    try:
        settings = load_pg_experience_settings()
        with open_experience_conn(settings, role=role) as conn:
            yield settings, conn
    except PgExperienceUnavailableError as exc:
        raise V2ApiError(exc.code, getattr(exc, 'status', 503), getattr(exc, 'detail', '')) from None
