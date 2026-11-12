"""PostgreSQL control-plane store for Knowledge Console, SSO accounts, and Device Project ACL."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
import uuid

from evolvmem.library_catalog import ControlBinding, ControlProvider
from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_project_acl import project_uuid
from evolvmem.library_startup import FileSecretResolver
from .migrations import loopback_psycopg_connector

CONSOLE_NS = uuid.UUID('4b23f9f6-7d3e-46b9-b9a1-8e6f3d0b5107')


class PgControlUnavailableError(RuntimeError):
    """Raised when PG control DB is configured but unreachable or misconfigured."""

    code = 'pg_control_unavailable'
    status = 503
    human_message = 'PG 不可用'

    def __init__(self, detail: str = 'pg_control_unavailable'):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PgControlConfig:
    host: str
    port: int
    database: str
    coordinator_role: str
    coordinator_password_ref: str
    reader_role: str
    reader_password_ref: str
    secret_files: MappingProxyType = field(repr=False)
    allow_loopback_disable_ssl: bool = False
    lock_timeout_ms: int = 500
    statement_timeout_ms: int = 2000

    @classmethod
    def from_dict(cls, raw: dict) -> PgControlConfig:
        if not isinstance(raw, dict):
            raise PgControlUnavailableError('invalid_pg_control_config')
        try:
            files = raw.get('secret_files')
            if not isinstance(files, dict) or not files:
                raise ValueError('missing secret_files')
            return cls(
                host=str(raw['host']),
                port=int(raw['port']),
                database=str(raw['database']),
                coordinator_role=str(raw['coordinator_role']),
                coordinator_password_ref=str(raw['coordinator_password_ref']),
                reader_role=str(raw.get('reader_role') or raw['coordinator_role']),
                reader_password_ref=str(raw.get('reader_password_ref') or raw['coordinator_password_ref']),
                secret_files=MappingProxyType(dict(files)),
                allow_loopback_disable_ssl=bool(raw.get('allow_loopback_disable_ssl', False)),
                lock_timeout_ms=int(raw.get('lock_timeout_ms', 500)),
                statement_timeout_ms=int(raw.get('statement_timeout_ms', 2000)),
            )
        except PgControlUnavailableError:
            raise
        except Exception:
            raise PgControlUnavailableError('invalid_pg_control_config') from None

    @classmethod
    def from_file(cls, path: str | Path) -> PgControlConfig:
        try:
            text = Path(path).expanduser().read_text(encoding='utf-8')
            return cls.from_dict(json.loads(text))
        except PgControlUnavailableError:
            raise
        except Exception:
            raise PgControlUnavailableError('pg_control_config_unreadable') from None


def _stable_uuid(kind: str, user_id: str) -> str:
    return str(uuid.uuid5(CONSOLE_NS, f'{kind}:{user_id}'))


def _json_str(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _map_pg_exception(exc: Exception):
    """Map PostgreSQL / ControlProvider exceptions to ConsoleError codes or PgControlUnavailableError."""
    from evolvmem.knowledge_console import ConsoleError

    msg = str(getattr(exc, 'diag', None) and exc.diag.message_primary or exc)
    for code, status in (
        ('console_not_initialized', 503),
        ('invalid_console_registry', 503),
        ('account_exists', 409),
        ('account_not_found', 404),
        ('unknown_user', 404),
        ('revision_conflict', 409),
        ('configured_owner_immutable', 400),
        ('login_already_bound', 409),
        ('login_not_observed', 400),
        ('self_access_immutable', 400),
        ('account_disabled', 403),
        ('session_identity_rejected', 403),
        ('session_token_conflict', 409),
    ):
        if code in msg:
            raise ConsoleError(code, status) from None
    for invalid_code in (
        'configured_owner_rebind_forbidden',
        'user_tier_reserved_for_human_auth',
        'invalid_reputation_policy',
        'invalid_agent_registration',
        'invalid_reputation_delta',
    ):
        if invalid_code in msg:
            raise ValueError(
                'configured owner identity cannot be rebound'
                if invalid_code == 'configured_owner_rebind_forbidden'
                else invalid_code
            ) from None
    raise PgControlUnavailableError() from None


class PgConsoleStore:
    """Thin PG control-plane store backed by ControlProvider (non-superuser + RLS + DISCARD ALL)."""

    def __init__(self, config: PgControlConfig, *, connector=None, secret_resolver=None):
        if not isinstance(config, PgControlConfig):
            raise PgControlUnavailableError('invalid_pg_control_config')
        self.config = config
        conn_fn = connector or loopback_psycopg_connector(
            allow_loopback_disable_ssl=config.allow_loopback_disable_ssl
        )
        resolver = secret_resolver or FileSecretResolver(config.secret_files)
        try:
            self.read_provider = ControlProvider(
                ControlBinding(
                    config.host, config.port, config.database,
                    config.reader_role, config.reader_password_ref,
                ),
                conn_fn,
                resolver,
                lock_timeout_ms=config.lock_timeout_ms,
                statement_timeout_ms=config.statement_timeout_ms,
            )
            self.coord_provider = ControlProvider(
                ControlBinding(
                    config.host, config.port, config.database,
                    config.coordinator_role, config.coordinator_password_ref,
                ),
                conn_fn,
                resolver,
                lock_timeout_ms=config.lock_timeout_ms,
                statement_timeout_ms=config.statement_timeout_ms,
            )
        except Exception:
            raise PgControlUnavailableError('invalid_pg_control_binding') from None

    def query(self, sql: str, params: tuple = ()):
        try:
            with self.read_provider.open(read_only=True) as session:
                try:
                    with session.raw.cursor() as cur:
                        cur.execute(sql, params)
                        if cur.description is None:
                            return ()
                        cols = [c.name for c in cur.description]
                        return tuple(dict(zip(cols, row)) for row in cur.fetchall())
                except Exception as inner:
                    session.broken = True
                    _map_pg_exception(inner)
        except FoundationError:
            raise PgControlUnavailableError() from None

    def mutate(self, sql: str, params: tuple = ()):
        try:
            with self.coord_provider.open(read_only=False) as session:
                try:
                    with session.raw.cursor() as cur:
                        cur.execute(sql, params)
                        rows = ()
                        if cur.description is not None:
                            cols = [c.name for c in cur.description]
                            rows = tuple(dict(zip(cols, row)) for row in cur.fetchall())
                    session.commit()
                    return rows
                except Exception as inner:
                    session.broken = True
                    _map_pg_exception(inner)
        except FoundationError:
            raise PgControlUnavailableError() from None

    def bootstrap(
        self,
        owner_id: str,
        owner_open_id: str,
        owner_name: str,
        owner_data_dir: str | None,
        history_environ: dict,
    ) -> dict:
        self.mutate(
            'SELECT em_control.console_bootstrap(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',
            (
                owner_id,
                _stable_uuid('principal', owner_id),
                owner_open_id,
                owner_name,
                _stable_uuid('exp-lib', owner_id),
                _stable_uuid('hist-lib', owner_id),
                owner_data_dir,
                _json_str(history_environ),
            ),
        )
        return {'ok': True}

    def registry_ready(self, expected_owner_id: str) -> bool:
        rows = self.query('SELECT * FROM em_control.console_status(%s)', (expected_owner_id,))
        return bool(rows and rows[0]['ready'])

    def observe_login(self, expected_owner_id: str, open_id: str, name: str) -> dict:
        rows = self.mutate(
            'SELECT em_control.console_observe_login(%s,%s,%s) AS pending',
            (expected_owner_id, open_id, name),
        )
        return {'ok': True, 'pending': bool(rows[0]['pending'])}

    def login_allowed(self, expected_owner_id: str, open_id: str) -> bool:
        rows = self.query(
            'SELECT em_control.console_login_allowed(%s,%s) AS allowed',
            (expected_owner_id, open_id),
        )
        return bool(rows and rows[0]['allowed'])

    def user_enabled(self, expected_owner_id: str, user_id: str) -> bool:
        rows = self.query(
            'SELECT em_control.console_user_enabled(%s,%s) AS enabled',
            (expected_owner_id, user_id),
        )
        return bool(rows and rows[0]['enabled'])

    @staticmethod
    def _normalize_user_row(row: dict) -> dict:
        env = row.get('history_environ')
        if not isinstance(env, str):
            env = _json_str(env or {})
        return {
            'id': row['user_id'],
            'user_id': row['user_id'],
            'principal_id': str(row['principal_id']),
            'display_name': row['display_name'],
            'enabled': bool(row['enabled']),
            'is_admin': bool(row['is_admin']),
            'open_id': row['open_id'],
            'lan_enabled': bool(row['lan_enabled']),
            'default_write_library_id': str(row['default_write_library_id']) if row['default_write_library_id'] else None,
            'revision': int(row['revision']),
            'experience_library_id': str(row['experience_library_id']) if row['experience_library_id'] else None,
            'history_library_id': str(row['history_library_id']) if row['history_library_id'] else None,
            'data_dir': row['data_dir'],
            'history_environ': env,
            'backend': row['backend'],
            'authority_state': row['authority_state'],
            'write_backend': row['write_backend'],
        }

    def list_users(self, expected_owner_id: str) -> list[dict]:
        rows = self.query('SELECT * FROM em_control.console_get_users(%s)', (expected_owner_id,))
        return [self._normalize_user_row(r) for r in rows]

    def get_user_source(self, expected_owner_id: str, user_id: str) -> dict | None:
        for row in self.list_users(expected_owner_id):
            if row['id'] == user_id:
                return row
        return None

    def actor_by_open_id(self, expected_owner_id: str, open_id: str) -> dict | None:
        for row in self.list_users(expected_owner_id):
            if row['open_id'] == open_id:
                return row
        return None

    def list_pending(self, expected_owner_id: str) -> list[dict]:
        return [dict(r) for r in self.query('SELECT * FROM em_control.console_get_pending(%s)', (expected_owner_id,))]

    def list_grants(self, expected_owner_id: str) -> list[dict]:
        rows = self.query('SELECT * FROM em_control.console_get_grants(%s)', (expected_owner_id,))
        return [
            {
                'viewer_id': r['viewer_id'],
                'owner_id': r['owner_id'],
                'history_read': bool(r['history_read']),
                'experience_read': bool(r['experience_read']),
                'revision': int(r['revision']),
            }
            for r in rows
        ]

    def upsert_user(
        self,
        expected_owner_id: str,
        actor_id: str,
        target_id: str,
        *,
        display_name: str,
        open_id: str | None,
        enabled: bool,
        revision: int | None,
        create: bool,
    ) -> dict:
        rows = self.mutate(
            'SELECT * FROM em_control.console_upsert_user(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (
                expected_owner_id,
                actor_id,
                target_id,
                _stable_uuid('principal', target_id),
                display_name,
                open_id,
                enabled,
                revision,
                create,
            ),
        )
        r = rows[0]
        return {
            'id': r['user_id'],
            'display_name': r['display_name'],
            'enabled': bool(r['enabled']),
            'is_admin': bool(r['is_admin']),
            'open_id': r['open_id'],
            'lan_enabled': bool(r['lan_enabled']),
            'revision': int(r['revision']),
        }

    def configure_library(
        self,
        expected_owner_id: str,
        actor_id: str,
        user_id: str,
        *,
        data_dir: str | None,
        history_environ: dict,
    ) -> None:
        self.mutate(
            'SELECT em_control.console_configure_library(%s,%s,%s,%s,%s,%s,%s::jsonb)',
            (
                expected_owner_id,
                actor_id,
                user_id,
                _stable_uuid('exp-lib', user_id),
                _stable_uuid('hist-lib', user_id),
                data_dir,
                _json_str(history_environ),
            ),
        )

    def set_grant(
        self,
        expected_owner_id: str,
        actor_id: str,
        *,
        viewer_id: str,
        owner_id: str,
        history_read: bool,
        experience_read: bool,
        revision: int,
    ) -> dict:
        rows = self.mutate(
            'SELECT em_control.console_set_grant(%s,%s,%s,%s,%s,%s,%s) AS next_rev',
            (expected_owner_id, actor_id, viewer_id, owner_id, history_read, experience_read, revision),
        )
        after = {
            'viewer_id': viewer_id,
            'owner_id': owner_id,
            'history_read': history_read,
            'experience_read': experience_read,
            'revision': int(rows[0]['next_rev']),
        }
        return {'ok': True, 'grant': after, 'row': after}

    def record_audit(self, actor_id: str, action: str, target_id: str, *, before=None, after=None, outcome='allowed') -> None:
        self.mutate(
            'SELECT em_control.console_record_audit(%s,%s,%s,%s::jsonb,%s::jsonb,%s)',
            (actor_id, action, target_id, _json_str(before), _json_str(after), outcome),
        )

    def register_trusted_device(
        self,
        user_id: str,
        *,
        device_id: str,
        external_device_id: str,
        token: str,
        credential_id: str | None = None,
        expires_at: float | None = None,
        unrestricted_own_projects: bool = False,
    ) -> str:
        cred_id = credential_id or str(uuid.uuid4())
        digest = hashlib.sha256(token.encode('utf-8')).digest()
        exp_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at is not None else None
        rows = self.mutate(
            'SELECT em_control.register_trusted_device(%s,%s,%s,%s,%s,%s,%s) AS principal_id',
            (user_id, device_id, external_device_id, cred_id, digest, exp_dt, unrestricted_own_projects),
        )
        return str(rows[0]['principal_id'])

    def register_pg_active_library(
        self,
        *,
        owner_user_id: str,
        library_id: str,
        connection_ref: str,
        projects: tuple[str, ...] = (),
    ) -> None:
        proj_payload = [
            {'project_id': project_uuid(key), 'project_key': key}
            for key in projects
        ]
        self.mutate(
            'SELECT em_control.register_pg_library_with_projects(%s,%s,%s,%s::jsonb)',
            (owner_user_id, library_id, connection_ref, json.dumps(proj_payload)),
        )

    def grant_device_project(
        self,
        device_id: str,
        library_id: str,
        project_key: str,
        *,
        permission: str = 'read',
        active: bool = True,
    ) -> None:
        self.mutate(
            'SELECT em_control.grant_device_project(%s,%s,%s,%s,%s,%s)',
            (device_id, library_id, project_uuid(project_key), project_key, permission, active),
        )

    def resolve_device_project_access(
        self,
        identity: RequestIdentity,
        library_id: str,
        project_key: str,
        action: str,
    ) -> bool:
        rows = self.query(
            'SELECT em_control.resolve_device_project_access(%s,%s,%s,%s,%s,%s,%s,%s,%s) AS allowed',
            (
                identity.credential_id,
                identity.principal_id,
                identity.device_id,
                identity.credential_epoch,
                identity.principal_epoch,
                identity.device_epoch,
                library_id,
                project_key,
                action,
            ),
        )
        return bool(rows and rows[0]['allowed'])
