"""Append-only PG control-plane migration runner and loopback connector helper."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import psycopg

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})


def loopback_psycopg_connector(*, allow_loopback_disable_ssl: bool = False):
    """Return a psycopg.connect wrapper that allows sslmode=disable ONLY on loopback when configured."""
    def _connect(**kwargs):
        params = dict(kwargs)
        if allow_loopback_disable_ssl and params.get('host') in LOOPBACK_HOSTS:
            params['sslmode'] = 'disable'
        return psycopg.connect(**params)

    return _connect


def apply_control_migrations(
    psql_runner: Callable[..., str],
    *,
    database_name: str,
    owner_role: str,
    coordinator_role: str,
    reader_role: str | None = None,
    up_to_version: int = 7,
) -> list[str]:
    """Apply the append-only control SQL chain 001..up_to_version and provision service roles.

    Never modifies existing numbered SQL files (001..006).
    """
    if up_to_version not in (6, 7, 8):
        raise ValueError(f'unsupported target control schema version: {up_to_version}')

    applied: list[str] = []

    def _run(rel_path: str, **vars_map: str) -> None:
        sql_path = PACKAGE_ROOT / rel_path
        psql_runner(database_name, owner_role, sql_path, **vars_map)
        applied.append(rel_path)

    _run('library_foundation/sql/001_control.sql')
    _run('library_catalog/sql/002_catalog_api.sql')
    _run('library_catalog/sql/003_acl_api.sql')
    _run(
        'library_catalog/sql/provision_catalog_service.sql',
        service_role=coordinator_role,
        can_coordinate='true',
    )
    if reader_role and reader_role != coordinator_role:
        _run(
            'library_catalog/sql/provision_catalog_service.sql',
            service_role=reader_role,
            can_coordinate='false',
        )
    _run('library_catalog/sql/004_control_projection_plan.sql', coordinator_role=coordinator_role)
    _run('library_project_acl/sql/005_control_project_acl.sql', coordinator_role=coordinator_role)
    _run('library_project_acl/sql/005a_control_acl_identity_share.sql', coordinator_role=coordinator_role)
    _run('library_catalog/sql/006_authority_lifecycle.sql', coordinator_role=coordinator_role)

    if up_to_version >= 7:
        _run('library_catalog/sql/007_console_control_plane.sql', coordinator_role=coordinator_role)
        _run('library_catalog/sql/007a_device_project_acl.sql', coordinator_role=coordinator_role)
    if up_to_version >= 8:
        _run('library_catalog/sql/008_agents_and_model_reputation.sql', coordinator_role=coordinator_role)

    if up_to_version >= 7:
        _run(
            'library_catalog/sql/provision_console_service.sql',
            service_role=coordinator_role,
            can_coordinate='true',
        )
        if reader_role and reader_role != coordinator_role:
            _run(
                'library_catalog/sql/provision_console_service.sql',
                service_role=reader_role,
                can_coordinate='false',
            )
    return applied
