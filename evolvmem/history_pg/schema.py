"""Database provisioning and SQL schema application for Stage 5 PG history + control."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
from typing import Any

from psycopg import sql

from evolvmem.experience_v2.control.migrations import apply_control_migrations
from evolvmem.history_pg.config import PgHistoryConfig

SQL_DIR = Path(__file__).resolve().parent / "sql"
HISTORY_SQL_FILES = (
    "001_organized_history_schema.sql",
    "002_runtime_capture_and_continuity.sql",
)


def _quote_sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_databases_exist(config: PgHistoryConfig, *, include_control: bool = False) -> list[str]:
    """Create missing target databases cleanly with UTF-8 encoding."""
    config.validate_non_prod_target()
    targets: list[str] = list(config.all_business_dbs())
    if include_control and config.control_db not in targets:
        targets.insert(0, config.control_db)
    created_or_present: list[str] = []
    with config.connect("postgres", autocommit=True) as conn:
        existing = {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}
        for db_name in targets:
            if db_name not in existing:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
            created_or_present.append(db_name)
    return created_or_present


def _ensure_control_roles(config: PgHistoryConfig) -> None:
    """Create owner (NOLOGIN non-superuser) and service login roles for em_control."""
    coord_pw = config.control_coordinator_password or secrets.token_urlsafe(18)
    reader_pw = config.control_reader_password or secrets.token_urlsafe(18)
    with config.connect("postgres", autocommit=True) as conn:
        for role_sql in (
            sql.SQL(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {owner_lit}) THEN "
                "CREATE ROLE {owner_id} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
                "END IF; END $$;"
            ).format(owner_lit=sql.Literal(config.control_owner_role), owner_id=sql.Identifier(config.control_owner_role)),
            sql.SQL(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {coord_lit}) THEN "
                "CREATE ROLE {coord_id} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {coord_pw}; "
                "END IF; END $$;"
            ).format(
                coord_lit=sql.Literal(config.control_coordinator_role),
                coord_id=sql.Identifier(config.control_coordinator_role),
                coord_pw=sql.Literal(coord_pw),
            ),
            sql.SQL(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {read_lit}) THEN "
                "CREATE ROLE {read_id} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {read_pw}; "
                "END IF; END $$;"
            ).format(
                read_lit=sql.Literal(config.control_reader_role),
                read_id=sql.Identifier(config.control_reader_role),
                read_pw=sql.Literal(reader_pw),
            ),
            sql.SQL("GRANT {owner_id} TO {admin_id};").format(
                owner_id=sql.Identifier(config.control_owner_role), admin_id=sql.Identifier(config.admin_user),
            ),
            sql.SQL("ALTER DATABASE {db_id} OWNER TO {owner_id};").format(
                db_id=sql.Identifier(config.control_db), owner_id=sql.Identifier(config.control_owner_role),
            ),
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {db_id} TO {coord_id}, {read_id};").format(
                db_id=sql.Identifier(config.control_db),
                coord_id=sql.Identifier(config.control_coordinator_role),
                read_id=sql.Identifier(config.control_reader_role),
            ),
        ):
            conn.execute(role_sql)


def _run_control_psql(config: PgHistoryConfig, database_name: str, owner_role: str, sql_path: Path, **vars_map: str) -> str:
    env = dict(os.environ)
    env["PGPASSWORD"] = config.admin_password
    psql_args = [
        "psql", "-X", "-q", "-U", config.admin_user, "-d", database_name,
        "-v", "ON_ERROR_STOP=1",
        "-v", f"database_name={database_name}",
        "-v", f"owner_role={owner_role}",
    ]
    for key, value in vars_map.items():
        psql_args.extend(["-v", f"{key}={value}"])

    host_psql = shutil.which("psql")
    if host_psql:
        cmd = [host_psql, *psql_args[1:], "-h", config.host, "-p", str(config.port)]
    elif config.container_name:
        cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={config.admin_password}", config.container_name, *psql_args]
    else:
        raise RuntimeError("psql_unavailable_and_container_name_unset")

    payload = f"SET ROLE {_quote_sql_ident(owner_role)};\n" + sql_path.read_text(encoding="utf-8")
    res = subprocess.run(cmd, input=payload, text=True, capture_output=True, env=env, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"control_psql_failed({sql_path.name}): {res.stderr.strip()}")
    return res.stdout


def apply_history_schemas(config: PgHistoryConfig, *, include_control: bool = False) -> dict[str, Any]:
    """Idempotently initialize em_control (when requested) and em_history/em_runtime schemas."""
    config.validate_non_prod_target()
    ensure_databases_exist(config, include_control=include_control)
    applied: dict[str, Any] = {}

    if include_control:
        _ensure_control_roles(config)
        with config.connect(config.control_db) as conn:
            has_ctrl = bool(conn.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'em_control' AND table_name = 'schema_migrations'"
                ")"
            ).fetchone()[0])
            ver = 0
            if has_ctrl:
                row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM em_control.schema_migrations").fetchone()
                ver = int(row[0] or 0) if row else 0
        if ver < 7:
            ctrl_steps = apply_control_migrations(
                lambda db, owner, path, **kw: _run_control_psql(config, db, owner, path, **kw),
                database_name=config.control_db,
                owner_role=config.control_owner_role,
                coordinator_role=config.control_coordinator_role,
                reader_role=config.control_reader_role,
                up_to_version=7,
            )
            applied[config.control_db] = ctrl_steps
        else:
            applied[config.control_db] = [f"already_at_v{ver}"]

    for db_name in config.all_business_dbs():
        steps: list[str] = []
        with config.connect(db_name, autocommit=True) as conn:
            for fname in HISTORY_SQL_FILES:
                sql_text = (SQL_DIR / fname).read_text(encoding="utf-8")
                conn.execute(sql_text)
                steps.append(fname)
        applied[db_name] = steps
    return applied
