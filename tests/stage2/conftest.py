"""Shared isolated PostgreSQL schema fixtures for Stage 2 tests (port 55433 only)."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

import psycopg
import pytest


class _MaskedDsn(str):
    """String DSN whose repr masks credentials in pytest failure tracebacks."""

    def __repr__(self) -> str:
        return "'<stage2-test-pg-dsn>'"


def _test_pg_password() -> str:
    if os.environ.get('EVOLVMEM_TEST_PG_PASSWORD'):
        return os.environ['EVOLVMEM_TEST_PG_PASSWORD']
    pw_file = Path('/tmp/evolvmem-pg-test-s2.pw')
    if pw_file.is_file():
        return pw_file.read_text(encoding='utf-8').strip()
    raise RuntimeError('Missing Stage 2 test PG password (/tmp/evolvmem-pg-test-s2.pw)')


def stage2_pg_dsn() -> _MaskedDsn:
    dsn = os.environ.get('EVOLVMEM_TEST_PG_DSN')
    if dsn:
        return _MaskedDsn(dsn)
    pw = _test_pg_password()
    return _MaskedDsn(f'host=127.0.0.1 port=55433 dbname=postgres user=postgres password={pw}')


@pytest.fixture
def pg_exp_schema():
    """Yield (conn, schema_name, base_dsn) with an isolated PG schema on 55433."""
    dsn = stage2_pg_dsn()
    schema = f's2_{uuid.uuid4().hex[:12]}'
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, options=f'-c search_path={schema},public') as conn:
            yield conn, schema, dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
