"""PostgreSQL-backed persistent Web sessions in em_control.web_sessions.

Stores ONLY SHA-256 cookie token digests, Feishu open_id, display name, CSRF
token, and fixed 8-hour absolute expiry. Never stores browser cookies or OAuth
access/refresh tokens, never renews expiry on get(), and never falls back to
SQLite or in-memory sessions when PostgreSQL is unavailable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from evolvmem.web_sessions import (
    SessionIdentityRejected,
    SessionStoreError,
    _identity,
    _record,
    _timestamp,
    _token_hash,
)
from .pg_console_store import PgConsoleStore, PgControlConfig, PgControlUnavailableError


def _unavailable() -> SessionStoreError:
    return SessionStoreError('pg_control_unavailable', message='PG 不可用')


class PgPersistentSessions:
    """Drop-in PostgreSQL session store matching PersistentSessions contract."""

    def __init__(
        self,
        store_or_config: PgConsoleStore | PgControlConfig,
        auth_scope: str,
        *,
        expected_owner_id: str | None = None,
        console_mode: bool = False,
    ):
        if (
            not isinstance(auth_scope, str)
            or not 1 <= len(auth_scope) <= 256
            or any(ord(c) < 32 for c in auth_scope)
        ):
            raise SessionStoreError('invalid_auth_scope')
        try:
            self.store = (
                store_or_config
                if isinstance(store_or_config, PgConsoleStore)
                else PgConsoleStore(store_or_config)
            )
        except PgControlUnavailableError:
            raise _unavailable() from None
        self.auth_scope = auth_scope
        self.expected_owner_id = expected_owner_id
        self.console_mode = bool(console_mode)

    def initialize(self) -> None:
        """Verify PG control schema v7 session RPC readiness without implicit DDL."""
        try:
            if self.console_mode:
                if not self.store.registry_ready(self.expected_owner_id):
                    raise SessionStoreError('pg_control_unavailable', message='PG 不可用')
            else:
                self.store.query('SELECT * FROM em_control.web_session_get(%s,%s)', ('0' * 64, self.auth_scope))
        except SessionStoreError:
            raise
        except Exception:
            raise _unavailable() from None

    def put(self, token: str, session: dict, *, validate=None) -> None:
        digest = _token_hash(token)
        if digest is None:
            raise SessionStoreError('invalid_session_token')
        record = _record(session)
        if validate is not None and not callable(validate):
            raise SessionStoreError('invalid_session_record')
        exp_dt = datetime.fromtimestamp(record['expires'], tz=timezone.utc)
        from evolvmem.knowledge_console import ConsoleError

        try:
            self.store.mutate(
                'SELECT em_control.web_session_put(%s,%s,%s,%s,%s,%s,%s,%s)',
                (
                    self.expected_owner_id,
                    self.console_mode,
                    digest,
                    self.auth_scope,
                    record['open_id'],
                    record['name'],
                    record['csrf'],
                    exp_dt,
                ),
            )
        except ConsoleError as exc:
            if exc.code in ('session_identity_rejected', 'account_disabled'):
                raise SessionIdentityRejected() from None
            if exc.code == 'session_token_conflict':
                raise SessionStoreError('session_token_conflict') from None
            raise _unavailable() from None
        except PgControlUnavailableError:
            raise _unavailable() from None

    def get(self, token: str) -> dict | None:
        """Read-only lookup; never extends expires_at."""
        digest = _token_hash(token)
        if digest is None:
            return None
        try:
            rows = self.store.query(
                'SELECT * FROM em_control.web_session_get(%s,%s)',
                (digest, self.auth_scope),
            )
        except Exception:
            raise _unavailable() from None
        if not rows:
            return None
        r = rows[0]
        rec = _record({
            'open_id': r['open_id'],
            'name': r['name'],
            'csrf': r['csrf'],
            'expires': float(r['expires_epoch']),
        })
        return rec if rec['expires'] > time.time() else None

    def pop(self, token: str) -> dict | None:
        digest = _token_hash(token)
        if digest is None:
            return None
        now = time.time()
        try:
            rows = self.store.mutate(
                'SELECT * FROM em_control.web_session_pop(%s,%s)',
                (digest, self.auth_scope),
            )
        except Exception:
            raise _unavailable() from None
        if not rows:
            return None
        r = rows[0]
        rec = _record({
            'open_id': r['open_id'],
            'name': r['name'],
            'csrf': r['csrf'],
            'expires': float(r['expires_epoch']),
        })
        return rec if rec['expires'] > now else None

    def prune(self, now: float | None = None) -> int:
        boundary = _timestamp(time.time() if now is None else now)
        dt = datetime.fromtimestamp(boundary, tz=timezone.utc)
        try:
            rows = self.store.mutate('SELECT em_control.web_session_prune(%s) AS n', (dt,))
            return int(rows[0]['n'])
        except Exception:
            raise _unavailable() from None

    def revoke_open_id(self, open_id: str) -> int:
        open_id = _identity(open_id)
        try:
            rows = self.store.mutate('SELECT em_control.web_session_revoke_open_id(%s) AS n', (open_id,))
            return int(rows[0]['n'])
        except Exception:
            raise _unavailable() from None
