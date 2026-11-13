"""Behavioral tests for Task 2.1: Experience v2 PG schema, HNSW index, and NOTIFY trigger."""
from __future__ import annotations

import json
import psycopg
import pytest

from evolvmem.experience_v2.schema import (
    ExperienceMigrationError,
    apply_experience_migrations,
    list_migration_files,
)


def test_apply_migrations_creates_all_tables_and_tracks_checksums(pg_exp_schema, tmp_path):
    conn, schema, _ = pg_exp_schema
    applied = apply_experience_migrations(conn)
    assert applied, 'expected at least 001_experience_v2_schema.sql'
    assert '001_experience_v2_schema.sql' in applied[0]

    # Idempotent second run applies nothing new
    second = apply_experience_migrations(conn)
    assert second == []

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s",
            (schema,),
        ).fetchall()
    }
    expected = {
        'experience_schema_migrations',
        'experience_notes',
        'experience_requests',
        'experience_events',
        'experience_proposals',
        'proposal_decisions',
        'review_decisions',
        'experience_relations',
        'retrieval_impressions',
    }
    assert expected <= tables

    # Tampered migration hash fails closed
    bad_dir = tmp_path / 'migs'
    bad_dir.mkdir()
    orig = list_migration_files()[0]
    (bad_dir / orig.name).write_text('-- tampered\nSELECT 1;\n', encoding='utf-8')
    with pytest.raises(ExperienceMigrationError, match='checksum_mismatch'):
        apply_experience_migrations(conn, migrations_dir=bad_dir)


def test_experience_notes_constraints_and_hnsw_cosine_query(pg_exp_schema):
    conn, schema, _ = pg_exp_schema
    apply_experience_migrations(conn)

    # Check HNSW index exists using vector_cosine_ops
    idx_row = conn.execute(
        """
        SELECT am.amname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_am am ON am.oid = c.relam
        WHERE n.nspname = %s AND c.relname = 'idx_experience_notes_embedding_hnsw'
        """,
        (schema,),
    ).fetchone()
    assert idx_row is not None
    assert idx_row[0] == 'hnsw'

    # Legacy length compatibility up to 1000/8000 while rejecting empty or bad category
    vec_a = '[' + ','.join(['1.0'] + ['0.0'] * 767) + ']'
    vec_b = '[' + ','.join(['0.0', '1.0'] + ['0.0'] * 766) + ']'
    conn.execute(
        """
        INSERT INTO experience_notes(
            id, owner_id, category, problem, solution, reusable_experience,
            origin, status, embedding, revision
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, 1)
        """,
        ('manual-' + 'a' * 32, 'user-1', '前端经验', 'p' * 65, 's' * 950, 'r' * 400, 'manual', 'active', vec_a),
    )
    conn.execute(
        """
        INSERT INTO experience_notes(
            id, owner_id, category, problem, solution, reusable_experience,
            origin, status, embedding, revision
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, 1)
        """,
        ('manual-' + 'b' * 32, 'user-1', '业务经验', 'other problem', 'other solution', 'other rule', 'manual', 'active', vec_b),
    )
    nearest = conn.execute(
        "SELECT id FROM experience_notes ORDER BY embedding <=> %s::vector LIMIT 1",
        (vec_a,),
    ).fetchone()
    assert nearest[0] == 'manual-' + 'a' * 32

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO experience_notes(
                    id, owner_id, category, problem, solution, reusable_experience,
                    origin, status, revision
                ) VALUES ('manual-' || repeat('c', 32), 'user-1', '无效分类', 'p', 's', 'r', 'manual', 'active', 1)
                """
            )


def test_experience_events_insert_fires_pg_notify(pg_exp_schema):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    with psycopg.connect(dsn, autocommit=True, options=f'-c search_path={schema},public') as listener:
        listener.execute('LISTEN experience_changes')
        with psycopg.connect(dsn, autocommit=True, options=f'-c search_path={schema},public') as writer:
            writer.execute(
                """
                INSERT INTO experience_events(
                    event_id, owner_id, actor_id, request_id, action, note_id, before_json, after_json
                ) VALUES ('evt-1', 'user-1', 'user-1', 'req-123456', 'create', 'manual-1', NULL, '{}'::jsonb)
                """
            )
        gen = listener.notifies(timeout=2.0)
        note = next(gen)
        gen.close()
        assert note.channel == 'experience_changes'
        payload = json.loads(note.payload)
        assert payload['owner_id'] == 'user-1'
        assert payload['note_id'] == 'manual-1'
        assert payload['action'] == 'create'
        assert int(payload['seq']) >= 1
