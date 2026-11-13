"""Behavioral tests for Task 2.3: SQLite manual_experience_* -> PG fidelity migrator."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest

from evolvmem import manual_experiences
from evolvmem.experience_v2.manual_migrator import (
    ManualMigrationError,
    migrate_manual_experiences,
    verify_manual_migration,
)
from evolvmem.experience_v2.schema import apply_experience_migrations

SECRET = b'test-stage2-hmac-secret-key-0001'


def _seed_sqlite_fixture(db_path: Path, *, owner: str = 'user-alpha') -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.close()
    src = {'user_id': owner, 'data_dir': str(db_path.parent)}
    # 1. Active note at revision 2 (created then updated)
    _, r1 = manual_experiences.write(
        src,
        owner,
        {
            'action': 'create',
            'request_id': 'seed-req-create-1',
            'category': '前端经验',
            'problem': '表格窄屏溢出被截断',
            'solution': '外层容器加 overflow-x: auto 并固定 min-width',
            'reusable_experience': '窄屏数据表格需显式横向滚动包装',
        },
    )
    n1 = r1['experience']
    _, r1u = manual_experiences.write(
        src,
        owner,
        {
            'action': 'update',
            'id': n1['id'],
            'revision': n1['revision'],
            'request_id': 'seed-req-update-1',
            'category': '业务经验',
            'problem': '表格窄屏溢出且列宽异常',
            'solution': '外层容器加 overflow-x: auto 并设 table-layout: fixed',
            'reusable_experience': '窄屏表格固定列宽并保留滚动条',
        },
    )
    n1 = r1u['experience']

    # 2. Deleted note at revision 2 (created then deleted)
    _, r2 = manual_experiences.write(
        src,
        owner,
        {
            'action': 'create',
            'request_id': 'seed-req-create-2',
            'category': '系统环境经验',
            'problem': 'WSL 代理变量干扰本地服务',
            'solution': '请求本地环回前清理 HTTP_PROXY 变量',
            'reusable_experience': '本机调用前先清理代理环境变量',
        },
    )
    n2 = r2['experience']
    _, r2d = manual_experiences.write(
        src,
        owner,
        {
            'action': 'delete',
            'id': n2['id'],
            'revision': n2['revision'],
            'request_id': 'seed-req-delete-2',
        },
    )
    n2 = r2d['experience']
    return [n1, n2]


def test_migrate_manual_experiences_preserves_id_revision_deleted_events_and_no_raw_text_in_receipt(
    pg_exp_schema, tmp_path
):
    conn, _, _ = pg_exp_schema
    apply_experience_migrations(conn)
    db_path = tmp_path / 'memory.db'
    notes = _seed_sqlite_fixture(db_path)

    receipt = migrate_manual_experiences(db_path, conn, secret=SECRET)
    assert receipt['notes_source_count'] == 2
    assert receipt['notes_verified_count'] == 2
    assert receipt['requests_source_count'] == 4
    assert receipt['requests_verified_count'] == 4
    assert receipt['events_source_count'] == 4
    assert receipt['events_verified_count'] == 4
    assert receipt['unexplained_diff_count'] == 0

    # Verify raw note text and plaintext owner_id never leak into receipt JSON
    receipt_json = json.dumps(receipt, ensure_ascii=False)
    assert 'user-alpha' not in receipt_json
    assert 'owner_sha256_prefix' in receipt['note_digests'][0]
    for n in notes:
        assert n['problem'] not in receipt_json
        assert n['solution'] not in receipt_json
        assert n['reusable_experience'] not in receipt_json

    # Read back from PG and check exact IDs, revisions, deleted_at, and events
    pg_notes = {
        r[0]: {'revision': r[1], 'deleted_at': r[2], 'origin': r[3]}
        for r in conn.execute('SELECT id, revision, deleted_at, origin FROM experience_notes').fetchall()
    }
    assert set(pg_notes) == {n['id'] for n in notes}
    for n in notes:
        assert pg_notes[n['id']]['revision'] == n['revision']
        assert pg_notes[n['id']]['origin'] == 'manual'
        if n['deleted_at'] is None:
            assert pg_notes[n['id']]['deleted_at'] is None
        else:
            src_dt = datetime.fromisoformat(n['deleted_at'])
            assert pg_notes[n['id']]['deleted_at'] == src_dt

    assert conn.execute('SELECT count(*) FROM experience_events').fetchone()[0] == 4
    assert conn.execute('SELECT count(*) FROM experience_requests').fetchone()[0] == 4

    # Idempotent re-run produces zero duplicates and zero diffs
    second_receipt = migrate_manual_experiences(db_path, conn, secret=SECRET)
    assert second_receipt['unexplained_diff_count'] == 0
    assert second_receipt['notes_migrated_this_run'] == 0
    assert conn.execute('SELECT count(*) FROM experience_notes').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM experience_events').fetchone()[0] == 4

    check = verify_manual_migration(db_path, conn)
    assert check['unexplained_diff_count'] == 0


def test_verify_manual_migration_detects_missing_or_drifted_pg_row(pg_exp_schema, tmp_path):
    conn, _, _ = pg_exp_schema
    apply_experience_migrations(conn)
    db_path = tmp_path / 'memory.db'
    _seed_sqlite_fixture(db_path)

    migrate_manual_experiences(db_path, conn, secret=SECRET)

    # Inject synthetic PG mismatch via SQLite modification to prove verifier is not a no-op
    with sqlite3.connect(db_path) as sconn:
        sconn.execute("UPDATE manual_experience_notes SET revision = revision + 1")
    with pytest.raises(ManualMigrationError, match='verification_failed'):
        verify_manual_migration(db_path, conn)
