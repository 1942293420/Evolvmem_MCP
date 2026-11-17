"""Isolated PostgreSQL 17 test database/role harness for Stage 1 TDD tests.

Connects ONLY to the disposable test container on 127.0.0.1:55433 (guarded by
evolvmem.pg_test_guard so production :55432 is unconditionally blocked).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import uuid

import psycopg

from evolvmem.pg_test_guard import assert_safe_test_pg_target

ROOT = Path(__file__).resolve().parents[2]
TEST_PG_CONTAINER = os.environ.get('EVOLVMEM_TEST_PG_CONTAINER', 'evolvmem-pg-test-stage1')
TEST_PG_HOST = os.environ.get('EVOLVMEM_TEST_PG_HOST', '127.0.0.1')
TEST_PG_PORT = int(os.environ.get('EVOLVMEM_TEST_PG_PORT', '55433'))
TEST_PG_PW_FILE = Path(os.environ.get('EVOLVMEM_TEST_PG_PW_FILE', '/tmp/evolvmem-pg-test.pw'))


def superuser_password() -> str:
    return TEST_PG_PW_FILE.read_text(encoding='utf-8').strip()


def admin_exec(sql_statements: list[str], *, dbname: str = 'postgres') -> None:
    assert_safe_test_pg_target(
        port=TEST_PG_PORT,
        dsn=f'postgresql://{TEST_PG_HOST}:{TEST_PG_PORT}/{dbname}',
        container_name=TEST_PG_CONTAINER,
    )
    with psycopg.connect(
        host=TEST_PG_HOST,
        port=TEST_PG_PORT,
        dbname=dbname,
        user='postgres',
        password=superuser_password(),
        autocommit=True,
        sslmode='disable',
    ) as conn:
        with conn.cursor() as cur:
            for stmt in sql_statements:
                cur.execute(stmt)


def run_psql_as_owner(database_name: str, owner_role: str, sql_path: Path, **variables: str) -> str:
    """Execute a migration/provisioning SQL file via psql after SET ROLE owner_role."""
    assert_safe_test_pg_target(
        port=TEST_PG_PORT,
        dsn=f'postgresql://{TEST_PG_HOST}:{TEST_PG_PORT}/{database_name}',
        container_name=TEST_PG_CONTAINER,
    )
    cmd = [
        'docker', 'exec', '-i', TEST_PG_CONTAINER,
        'psql', '-X', '-q', '-U', 'postgres', '-d', database_name,
        '-v', 'ON_ERROR_STOP=1',
        '-v', f'database_name={database_name}',
        '-v', f'owner_role={owner_role}',
    ]
    for key, value in variables.items():
        cmd.extend(['-v', f'{key}={value}'])
    payload = f'SET ROLE "{owner_role}";\n' + sql_path.read_text(encoding='utf-8')
    completed = subprocess.run(cmd, input=payload, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f'psql failed ({completed.returncode}) on {sql_path.name}:\n'
            f'STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}'
        )
    return completed.stdout


@dataclass(frozen=True)
class IsolatedControlEnv:
    prefix: str
    database: str
    owner_role: str
    coordinator_role: str
    reader_role: str
    coordinator_pw: str
    reader_pw: str
    secret_files: dict[str, str]
    config_file: Path


def create_isolated_control_db(tmp_path: Path, *, up_to_version: int = 7) -> IsolatedControlEnv:
    """Create a fresh control DB + non-superuser roles and apply migrations."""
    from evolvmem.experience_v2.control.migrations import apply_control_migrations

    tag = uuid.uuid4().hex[:8]
    db = f'em_ctrl_{tag}'
    owner = f'em_ctrl_own_{tag}'
    coord = f'em_ctrl_crd_{tag}'
    reader = f'em_ctrl_rdr_{tag}'
    coord_pw = f'pw-coord-{uuid.uuid4().hex}'
    reader_pw = f'pw-read-{uuid.uuid4().hex}'

    admin_exec([
        f'CREATE ROLE "{owner}" NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION',
        f'CREATE ROLE "{coord}" LOGIN PASSWORD \'{coord_pw}\' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION',
        f'CREATE ROLE "{reader}" LOGIN PASSWORD \'{reader_pw}\' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION',
        f'CREATE DATABASE "{db}" OWNER "{owner}"',
        f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC',
        f'GRANT CONNECT ON DATABASE "{db}" TO "{coord}", "{reader}"',
    ])

    try:
        apply_control_migrations(
            run_psql_as_owner,
            database_name=db,
            owner_role=owner,
            coordinator_role=coord,
            reader_role=reader,
            up_to_version=up_to_version,
        )
    except Exception:
        drop_isolated_db(db, (owner, coord, reader))
        raise

    secrets_dir = tmp_path / f'secrets_{tag}'
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    coord_pw_path = secrets_dir / 'coord.pw'
    reader_pw_path = secrets_dir / 'reader.pw'
    coord_pw_path.write_text(coord_pw + '\n', encoding='utf-8')
    reader_pw_path.write_text(reader_pw + '\n', encoding='utf-8')
    os.chmod(coord_pw_path, 0o600)
    os.chmod(reader_pw_path, 0o600)

    secret_files = {
        'control.coordinator': str(coord_pw_path),
        'control.reader': str(reader_pw_path),
    }
    cfg_path = secrets_dir / 'pg_control.json'
    cfg_path.write_text(
        json.dumps({
            'host': TEST_PG_HOST,
            'port': TEST_PG_PORT,
            'database': db,
            'coordinator_role': coord,
            'coordinator_password_ref': 'control.coordinator',
            'reader_role': reader,
            'reader_password_ref': 'control.reader',
            'secret_files': secret_files,
            'allow_loopback_disable_ssl': True,
        }),
        encoding='utf-8',
    )
    os.chmod(cfg_path, 0o600)
    return IsolatedControlEnv(
        prefix=tag,
        database=db,
        owner_role=owner,
        coordinator_role=coord,
        reader_role=reader,
        coordinator_pw=coord_pw,
        reader_pw=reader_pw,
        secret_files=secret_files,
        config_file=cfg_path,
    )


def drop_isolated_db(database: str, roles: tuple[str, ...]) -> None:
    stmts = [f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)']
    for role in roles:
        stmts.append(f'DROP ROLE IF EXISTS "{role}"')
    admin_exec(stmts)


@dataclass(frozen=True)
class IsolatedContentEnv:
    database: str
    owner_role: str
    projector_role: str
    projector_pw: str
    runtime_roles: dict[str, tuple[str, str]]


def create_isolated_content_db(
    *,
    library_id: str,
    connection_ref: str,
    owner_principal_id: str,
    runtime_principals: list[tuple[str, str, str, str, int]],
) -> IsolatedContentEnv:
    """Create a real PG content DB with 001..004 + projector + runtime roles."""
    tag = uuid.uuid4().hex[:8]
    db = f'em_cnt_{tag}'
    owner = f'em_cnt_own_{tag}'
    proj = f'em_cnt_prj_{tag}'
    proj_pw = f'pw-proj-{uuid.uuid4().hex}'
    stmts = [
        f'CREATE ROLE "{owner}" NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION',
        f'CREATE ROLE "{proj}" LOGIN PASSWORD \'{proj_pw}\' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION',
    ]
    runtime_roles: dict[str, tuple[str, str]] = {}
    for label, *_ in runtime_principals:
        r_name = f'em_cnt_rt_{label}_{tag}'
        r_pw = f'pw-rt-{label}-{uuid.uuid4().hex}'
        runtime_roles[label] = (r_name, r_pw)
        stmts.append(
            f'CREATE ROLE "{r_name}" LOGIN PASSWORD \'{r_pw}\' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION'
        )
    stmts.extend([
        f'CREATE DATABASE "{db}" OWNER "{owner}"',
        f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC',
        f'GRANT CONNECT ON DATABASE "{db}" TO "{proj}"',
    ])
    for r_name, _ in runtime_roles.values():
        stmts.append(f'GRANT CONNECT ON DATABASE "{db}" TO "{r_name}"')
    admin_exec(stmts)

    run_psql_as_owner(db, owner, ROOT / 'evolvmem/library_foundation/sql/001_content.sql')
    run_psql_as_owner(db, owner, ROOT / 'evolvmem/library_foundation/sql/002_content_acl_projector.sql')
    admin_exec(
        [
            f'SET ROLE "{owner}"',
            f"INSERT INTO em_security.library_state(singleton, library_id, kind, device_id, project_id, "
            f"policy_epoch, route_epoch, authority_state, acl_state) "
            f"VALUES (true, '{library_id}', 'legacy', NULL, NULL, 1, 1, 'pg_active', 'ready')",
            'RESET ROLE',
        ],
        dbname=db,
    )
    run_psql_as_owner(
        db, owner, ROOT / 'evolvmem/library_foundation/sql/provision_content_projector.sql',
        projector_role=proj, library_id=library_id, connection_ref=connection_ref,
    )
    run_psql_as_owner(
        db, owner, ROOT / 'evolvmem/library_project_acl/sql/003_content_project_acl.sql',
        library_owner_principal_id=owner_principal_id,
    )
    run_psql_as_owner(db, owner, ROOT / 'evolvmem/library_project_acl/sql/004_content_project_scope.sql')
    for label, principal_id, credential_id, device_id, epoch in runtime_principals:
        r_name, _ = runtime_roles[label]
        run_psql_as_owner(
            db, owner, ROOT / 'evolvmem/library_foundation/sql/provision_runtime.sql',
            runtime_role=r_name,
            principal_id=principal_id,
            credential_id=credential_id,
            authenticated_device_id=device_id,
            credential_epoch=str(epoch),
        )
        run_psql_as_owner(
            db, owner, ROOT / 'evolvmem/library_project_acl/sql/provision_project_scope.sql',
            runtime_role=r_name,
        )
    return IsolatedContentEnv(
        database=db,
        owner_role=owner,
        projector_role=proj,
        projector_pw=proj_pw,
        runtime_roles=runtime_roles,
    )
