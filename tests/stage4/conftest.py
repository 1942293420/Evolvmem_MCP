"""Shared isolated PostgreSQL schema fixtures for Stage 4 & Backfill tests (port 55434 only)."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

import psycopg
import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations


class _MaskedDsn(str):
    """String DSN whose repr masks credentials in pytest failure tracebacks."""

    def __repr__(self) -> str:
        return "'<stage4-test-pg-dsn>'"


def _test_pg_password() -> str:
    if os.environ.get('EVOLVMEM_TEST_PG_PASSWORD'):
        return os.environ['EVOLVMEM_TEST_PG_PASSWORD']
    for fname in ('/tmp/evolvmem-pg-test-s4.pw', '/tmp/evolvmem-pg-test-s3.pw'):
        p = Path(fname)
        if p.is_file():
            return p.read_text(encoding='utf-8').strip()
    raise RuntimeError('Missing Stage 4 test PG password (/tmp/evolvmem-pg-test-s4.pw)')


def stage4_pg_dsn() -> _MaskedDsn:
    dsn = os.environ.get('EVOLVMEM_TEST_PG_DSN')
    if dsn:
        if ':55432' in dsn or 'port=55432' in dsn or 'evolvmem_pgdata' in dsn:
            raise RuntimeError('Refusing prod PG port 55432 or prod volume in tests')
        return _MaskedDsn(dsn)
    pw = _test_pg_password()
    return _MaskedDsn(f'host=127.0.0.1 port=55434 dbname=evolvmem_test_backfill_unit user=postgres password={pw}')


@pytest.fixture
def pg_s4_schema():
    """Yield (conn, schema_name, base_dsn) with an isolated PG schema on 55434."""
    dsn = stage4_pg_dsn()
    schema = f's4_{uuid.uuid4().hex[:12]}'
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, options=f'-c search_path={schema},public') as conn:
            yield conn, schema, dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def pg_s4_migrated(pg_s4_schema):
    """Yield (conn, schema_name, base_dsn) after applying all experience_v2 migrations (001..003)."""
    conn, schema, dsn = pg_s4_schema
    apply_experience_migrations(conn)
    return conn, schema, dsn
