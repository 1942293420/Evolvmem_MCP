"""Stage 2 Task 2.11 tests: Safe dry-run cutover script & maintenance-approval guard."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / 'scripts' / 'linux' / 'cutover_stage2_manual_to_pg.py'


def _create_synthetic_sqlite(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE manual_experience_notes (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                category TEXT NOT NULL,
                problem TEXT NOT NULL,
                solution TEXT NOT NULL,
                reusable_experience TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE manual_experience_events (
                event_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL,
                note_id TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE manual_experience_requests (
                owner_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                response_json TEXT NOT NULL,
                http_status INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, request_id)
            )
            """
        )
        after = json.dumps({
            'id': 'manual-11111111111111111111111111111111',
            'category': '系统环境经验',
            'problem': 'Synthetic problem marker DO_NOT_LEAK_TO_RECEIPT',
            'solution': 'Synthetic solution details',
            'reusable_experience': 'Synthetic reusable checklist',
            'revision': 1,
            'deleted_at': None,
            'created_at': '2026-11-12T00:00:00Z',
            'updated_at': '2026-11-12T00:00:00Z',
        }, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO manual_experience_notes(id, owner_id, category, problem, solution, reusable_experience, revision, deleted_at, created_at, updated_at)
            VALUES (?, 'synthetic_owner', '系统环境经验', 'Synthetic problem marker DO_NOT_LEAK_TO_RECEIPT', 'Synthetic solution details', 'Synthetic reusable checklist', 1, NULL, '2026-11-12T00:00:00Z', '2026-11-12T00:00:00Z')
            """,
            ('manual-11111111111111111111111111111111',),
        )
        conn.execute(
            """
            INSERT INTO manual_experience_events(event_id, owner_id, actor_id, request_id, action, note_id, before_json, after_json, created_at)
            VALUES ('ev-1', 'synthetic_owner', 'synthetic_owner', 'req-1', 'create', 'manual-11111111111111111111111111111111', NULL, ?, '2026-11-12T00:00:00Z')
            """,
            (after,),
        )
        conn.execute(
            """
            INSERT INTO manual_experience_requests(owner_id, request_id, actor_id, payload_sha256, response_json, http_status, created_at)
            VALUES ('synthetic_owner', 'req-1', 'synthetic_owner', 'abc', '{}', 201, '2026-11-12T00:00:00Z')
            """
        )
        conn.commit()


def test_cutover_refuses_execute_without_approval_and_dry_run_verifies_parity(pg_exp_schema, tmp_path: Path):
    conn, schema, _dsn = pg_exp_schema
    conn.close()

    sqlite_db = tmp_path / 'owner_memory.db'
    _create_synthetic_sqlite(sqlite_db)
    policy_path = tmp_path / 'product-policy.json'
    orig_policy = json.dumps({
        'console_enabled': True,
        'legacy_web_enabled': False,
        'ai_processing_enabled': False,
        'experience_v2_enabled': False,
    })
    policy_path.write_text(orig_policy, encoding='utf-8')
    receipt_path = tmp_path / 'precutover.json'

    assert SCRIPT.is_file(), f'Missing {SCRIPT}'

    # 1. Refuses --execute without --i-have-maintenance-approval
    deny = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            '--execute',
            '--sqlite-db', str(sqlite_db),
            '--owner-id', 'synthetic_owner',
            '--pg-port', '55433',
            '--pg-password-file', '/tmp/evolvmem-pg-test-s2.pw',
            '--schema', schema,
            '--policy-file', str(policy_path),
            '--receipt-out', str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert deny.returncode != 0
    assert 'maintenance_approval_required' in (deny.stdout + deny.stderr)
    assert policy_path.read_text(encoding='utf-8') == orig_policy

    # 2. Default --dry-run verifies parity in isolated schema without modifying policy or sqlite
    sqlite_mtime_before = sqlite_db.stat().st_mtime_ns
    dry = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            '--dry-run',
            '--cleanup-dry-run-schema',
            '--sqlite-db', str(sqlite_db),
            '--owner-id', 'synthetic_owner',
            '--pg-port', '55433',
            '--pg-password-file', '/tmp/evolvmem-pg-test-s2.pw',
            '--policy-file', str(policy_path),
            '--receipt-out', str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert sqlite_db.stat().st_mtime_ns == sqlite_mtime_before
    assert policy_path.read_text(encoding='utf-8') == orig_policy

    raw_receipt = receipt_path.read_text(encoding='utf-8')
    assert 'DO_NOT_LEAK_TO_RECEIPT' not in raw_receipt
    pw_val = Path('/tmp/evolvmem-pg-test-s2.pw').read_text(encoding='utf-8').strip()
    assert pw_val not in raw_receipt

    data = json.loads(raw_receipt)
    assert data['mode'] == 'dry_run'
    assert data['production_touched'] is False
    assert data['migration_verification']['notes_verified_count'] == 1
    assert data['migration_verification']['unexplained_diff_count'] == 0
    assert data['instant_rollback']['experience_v2_enabled'] is False


def test_task211_ast_and_file_line_limit():
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []
    if SCRIPT.exists():
        assert len(SCRIPT.read_text(encoding='utf-8').splitlines()) <= 600