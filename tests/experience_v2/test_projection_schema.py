"""Actual isolated PG checks for additive experience retrieval projections."""
from pathlib import Path
import uuid
import hashlib

import psycopg
import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.pg_test_guard import assert_safe_test_pg_target
from tests.stage5.conftest import ensure_stage5_pg_running, STAGE5_PW_FILE


@pytest.fixture
def projection_db():
    assert_safe_test_pg_target(port=55434, container_name='evolvmem-pg-test-stage5')
    ensure_stage5_pg_running()
    class Masked(str):
        def __repr__(self): return '<isolated-pg-dsn>'
    dsn = Masked('host=127.0.0.1 port=55434 dbname=postgres user=postgres password=' + STAGE5_PW_FILE.read_text().strip())
    schema = 'projection_' + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector')
        admin.execute(f'CREATE SCHEMA {schema}')
    try:
        with psycopg.connect(dsn, options=f'-c search_path={schema},public') as conn:
            apply_experience_migrations(conn)
            yield conn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {schema} CASCADE')


def seed(conn, note_id='actual-note', **projection):
    values = {'id': note_id, 'owner_id': 'owner', 'category': '系统环境经验',
              'problem': 'problem', 'solution': 'solution', 'reusable_experience': 'rule', **projection}
    columns = list(values)
    conn.execute('INSERT INTO experience_notes (' + ','.join(columns) + ') VALUES (' + ','.join(['%s'] * len(columns)) + ')', [values[x] for x in columns])


def activate(conn):
    conn.execute("INSERT INTO experience_embedding_policy(singleton,required,retrieval_contract_id,legacy_contract_id) VALUES (true,true,'contract-1024','contract-768')")


def vector(dim): return '[' + ','.join(['1'] + ['0'] * (dim - 1)) + ']'


def test_projection_is_additive_1024_and_old_vector_stays_768(projection_db):
    conn, schema = projection_db
    types = dict(conn.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname='experience_notes' AND a.attname IN ('embedding','retrieval_embedding')", (schema,)).fetchall())
    assert types['embedding'] == 'vector(768)'
    assert types['retrieval_embedding'] == 'vector(1024)'
    index = conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND indexname='idx_experience_notes_retrieval_hnsw'", (schema,)).fetchone()
    assert index and 'hnsw' in index[0] and 'vector_cosine_ops' in index[0]
    assert apply_experience_migrations(conn) == []


def test_policy_is_off_until_activated_then_empty_active_writes_fail(projection_db):
    conn, _ = projection_db
    seed(conn)
    activate(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction(): seed(conn, 'must-not-save')
    assert conn.execute("SELECT count(*) FROM experience_notes WHERE id='must-not-save'").fetchone()[0] == 0
    conn.execute("UPDATE experience_notes SET status='retired' WHERE id='actual-note'")


def test_policy_binds_current_text_and_accepts_atomic_real_projection(projection_db):
    conn, _ = projection_db
    activate(conn)
    text = '系统环境经验\nproblem\nsolution\nrule'
    seed(conn, embedding=vector(768), retrieval_embedding=vector(1024), retrieval_embedding_contract='contract-1024', retrieval_embedding_content_sha256=hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction(): conn.execute("UPDATE experience_notes SET solution='changed but stale vector' WHERE id='actual-note'")
    assert conn.execute("SELECT solution FROM experience_notes WHERE id='actual-note'").fetchone()[0] == 'solution'
    new_text = '系统环境经验\nproblem\ncorrectly updated\nrule'
    conn.execute("UPDATE experience_notes SET solution=%s,retrieval_embedding_content_sha256=%s,retrieval_embedding=%s::vector WHERE id='actual-note'", ('correctly updated', hashlib.sha256(new_text.encode()).hexdigest(), vector(1024)))


def test_wrong_contract_and_zero_vectors_are_not_active_eligible(projection_db):
    conn, _ = projection_db
    activate(conn)
    sha = hashlib.sha256('系统环境经验\nproblem\nsolution\nrule'.encode()).hexdigest()
    for contract, embedding in [('wrong-model', vector(1024)), ('contract-1024', '[' + ','.join(['0'] * 1024) + ']')]:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction(): seed(conn, retrieval_embedding=embedding, embedding=vector(768), retrieval_embedding_contract=contract, retrieval_embedding_content_sha256=sha)
