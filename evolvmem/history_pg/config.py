"""Multi-database configuration, deterministic device/user routing, and bounded pooling."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Iterator

import psycopg

from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
from evolvmem.library_startup import FileSecretResolver
from evolvmem.pg_test_guard import assert_safe_test_pg_target

ENV_PG_HISTORY_CONFIG = "EVOLVMEM_HISTORY_PG_CONFIG"
DEFAULT_PG_HISTORY_CONFIG = Path("~/.config/evolvmem-pg/history.json")
_SAFE_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


class HistoryPgBusyError(RuntimeError):
    """Raised when bounded connection slots are exhausted."""
    code = "pg_connection_pool_busy"
    status = 503


@dataclass(frozen=True, slots=True)
class PoolLimits:
    max_connections: int = 16
    acquire_timeout_s: float = 0.25
    statement_timeout_ms: int = 3000
    lock_timeout_ms: int = 500


@dataclass(frozen=True, slots=True)
class DatabaseRoleDsn:
    dbname: str
    user: str
    password: str


def _safe_ident(value: str, *, label: str = "identifier") -> str:
    if not _SAFE_IDENT_RE.match(value):
        raise ValueError(f"unsafe_{label}:{value!r}")
    return value


def device_db_name_for_id(device_id: str) -> str:
    """Derive deterministic PostgreSQL database identifier for a confirmed device_id."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", device_id.strip().lower()).strip("_")
    digest = hashlib.sha256(device_id.strip().encode("utf-8")).hexdigest()[:8]
    prefix = f"evolvmem_dev_{cleaned[:24]}_{digest}" if cleaned else f"evolvmem_dev_{digest}"
    return _safe_ident(prefix[:60].rstrip("_"), label="device_db")


@dataclass(frozen=True, slots=True)
class PgHistoryConfig:
    host: str = "127.0.0.1"
    port: int = 55434
    admin_user: str = "postgres"
    admin_password: str = ""
    control_db: str = "evolvmem_control"
    history_migration_db: str = "evolvmem_history_migration"
    shared_public_db: str = "evolvmem_shared_public"
    user_history_dbs: dict[str, str] = field(default_factory=lambda: {"jiangli": "evolvmem_history_migration"})
    device_dbs: dict[str, str] = field(default_factory=dict)
    cas_root: Path = Path("~/.local/share/evolvmem-pg-cas")
    pool_limits: PoolLimits = field(default_factory=PoolLimits)
    allow_loopback_disable_ssl: bool = True
    control_owner_role: str = "evolvmem_control_owner"
    control_coordinator_role: str = "evolvmem_control_coordinator"
    control_coordinator_password: str = ""
    control_reader_role: str = "evolvmem_control_reader"
    control_reader_password: str = ""
    container_name: str = ""
    allow_prod: bool = False
    secret_files: dict[str, str] = field(default_factory=dict)

    def validate_non_prod_target(self) -> None:
        if self.allow_prod:
            return
        assert_safe_test_pg_target(
            port=self.port,
            dsn=f"postgresql://{self.host}:{self.port}/{self.control_db}",
            container_name=self.container_name or None,
        )

    def all_business_dbs(self) -> tuple[str, ...]:
        names = [self.history_migration_db, self.shared_public_db]
        names.extend(self.user_history_dbs.values())
        names.extend(self.device_dbs.values())
        seen: dict[str, None] = {}
        for name in names:
            if name:
                seen[_safe_ident(name, label="dbname")] = None
        return tuple(seen.keys())

    def resolve_user_history_db(self, user_id: str) -> str:
        uid = user_id.strip()
        if uid in self.user_history_dbs:
            return _safe_ident(self.user_history_dbs[uid], label="user_db")
        if uid == "public":
            return _safe_ident(self.shared_public_db, label="shared_db")
        return _safe_ident(self.history_migration_db, label="migration_db")

    def resolve_device_db(self, device_id: str) -> str:
        did = device_id.strip()
        if did in self.device_dbs:
            return _safe_ident(self.device_dbs[did], label="device_db")
        return device_db_name_for_id(did)

    def admin_dsn(self, dbname: str = "postgres") -> str:
        _safe_ident(dbname, label="dbname")
        ssl = "disable" if self.allow_loopback_disable_ssl and self.host in ("127.0.0.1", "::1", "localhost") else "require"
        return (
            f"host={self.host} port={self.port} dbname={dbname} "
            f"user={self.admin_user} password={self.admin_password} sslmode={ssl}"
        )

    def connect(
        self,
        dbname: str,
        *,
        user: str | None = None,
        password: str | None = None,
        autocommit: bool = False,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> psycopg.Connection:
        self.validate_non_prod_target()
        _safe_ident(dbname, label="dbname")
        connector = loopback_psycopg_connector(allow_loopback_disable_ssl=self.allow_loopback_disable_ssl)
        conn = connector(
            host=self.host,
            port=self.port,
            dbname=dbname,
            user=user or self.admin_user,
            password=self.admin_password if password is None else password,
            autocommit=autocommit,
        )
        stmt_ms = self.pool_limits.statement_timeout_ms if statement_timeout_ms is None else int(statement_timeout_ms)
        lock_ms = self.pool_limits.lock_timeout_ms if lock_timeout_ms is None else int(lock_timeout_ms)
        conn.execute(f"SET statement_timeout = {max(0, stmt_ms)}")
        conn.execute(f"SET lock_timeout = {max(0, lock_ms)}")
        return conn


class PgHistoryPool:
    """Bounded per-config connection gate enforcing concurrency + deadlines."""

    def __init__(self, config: PgHistoryConfig) -> None:
        self.config = config
        self._sem = threading.BoundedSemaphore(max(1, config.pool_limits.max_connections))

    @contextmanager
    def connection(
        self,
        dbname: str,
        *,
        user: str | None = None,
        password: str | None = None,
        autocommit: bool = False,
        statement_timeout_ms: int | None = None,
    ) -> Iterator[psycopg.Connection]:
        if not self._sem.acquire(timeout=self.config.pool_limits.acquire_timeout_s):
            raise HistoryPgBusyError("history_pg_pool_exhausted")
        conn: psycopg.Connection | None = None
        try:
            conn = self.config.connect(
                dbname, user=user, password=password, autocommit=autocommit,
                statement_timeout_ms=statement_timeout_ms,
            )
            yield conn
            if not autocommit and not conn.closed:
                conn.commit()
        except Exception:
            if conn is not None and not autocommit and not conn.closed:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            try:
                if conn is not None and not conn.closed:
                    conn.close()
            finally:
                self._sem.release()


def load_pg_history_config(path: Path | str | None = None, *, allow_prod: bool = False) -> PgHistoryConfig:
    cfg_path = Path(path or os.environ.get(ENV_PG_HISTORY_CONFIG) or DEFAULT_PG_HISTORY_CONFIG).expanduser()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    secrets_map = dict(raw.get("secret_files") or {})
    resolver = FileSecretResolver(secrets_map) if secrets_map else None

    def _pw(ref_key: str, direct_key: str, default: str = "") -> str:
        ref = raw.get(ref_key)
        if ref and resolver is not None:
            return resolver.resolve(str(ref))
        return str(raw.get(direct_key, default))

    limits_raw = dict(raw.get("pool_limits") or {})
    limits = PoolLimits(
        max_connections=int(limits_raw.get("max_connections", 16)),
        acquire_timeout_s=float(limits_raw.get("acquire_timeout_s", 0.25)),
        statement_timeout_ms=int(limits_raw.get("statement_timeout_ms", 3000)),
        lock_timeout_ms=int(limits_raw.get("lock_timeout_ms", 500)),
    )
    cfg = PgHistoryConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 55434)),
        admin_user=str(raw.get("admin_user", "postgres")),
        admin_password=_pw("admin_password_ref", "admin_password", ""),
        control_db=str(raw.get("control_db", "evolvmem_control")),
        history_migration_db=str(raw.get("history_migration_db", "evolvmem_history_migration")),
        shared_public_db=str(raw.get("shared_public_db", "evolvmem_shared_public")),
        user_history_dbs=dict(raw.get("user_history_dbs") or {"jiangli": "evolvmem_history_migration"}),
        device_dbs=dict(raw.get("device_dbs") or {}),
        cas_root=Path(raw.get("cas_root", "~/.local/share/evolvmem-pg-cas")).expanduser(),
        pool_limits=limits,
        allow_loopback_disable_ssl=bool(raw.get("allow_loopback_disable_ssl", True)),
        control_owner_role=str(raw.get("control_owner_role", "evolvmem_control_owner")),
        control_coordinator_role=str(raw.get("control_coordinator_role", "evolvmem_control_coordinator")),
        control_coordinator_password=_pw("coordinator_password_ref", "coordinator_password", ""),
        control_reader_role=str(raw.get("control_reader_role", "evolvmem_control_reader")),
        control_reader_password=_pw("reader_password_ref", "reader_password", ""),
        container_name=str(raw.get("container_name", "")),
        allow_prod=bool(allow_prod or raw.get("allow_prod", False)),
        secret_files=secrets_map,
    )
    cfg.validate_non_prod_target()
    return cfg
