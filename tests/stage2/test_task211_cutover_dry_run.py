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

def test_experience_pg_config_isolated_from_control_and_l1_migration_embeddings(pg_exp_schema, tmp_path: Path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    from evolvmem import console_access
    from evolvmem.experience_v2.pg_experience_store import load_pg_experience_settings, open_experience_conn
    from evolvmem.experience_v2.schema import apply_experience_migrations
    from evolvmem.experience_v2.manual_migrator import migrate_manual_experiences

    apply_experience_migrations(conn)
    conn.commit()
    conn.close()

    import os
    pw_file = tmp_path / 'exp_pw'
    pw_file.write_text(Path('/tmp/evolvmem-pg-test-s2.pw').read_text(encoding='utf-8').strip(), encoding='utf-8')
    os.chmod(pw_file, 0o600)
    ticket_file = tmp_path / 'ticket_pw'
    ticket_file.write_text('ticket-secret-xyz', encoding='utf-8')
    os.chmod(ticket_file, 0o600)
    cfg_file = tmp_path / 'experience-v2.json'
    cfg_file.write_text(json.dumps({
        'host': '127.0.0.1',
        'port': 55433,
        'database': 'postgres',
        'coordinator_role': 'postgres',
        'coordinator_password_ref': 'coord',
        'reader_role': 'postgres',
        'reader_password_ref': 'coord',
        'allow_loopback_disable_ssl': True,
        'lock_timeout_ms': 600,
        'statement_timeout_ms': 2500,
        'secret_files': {'coord': str(pw_file), 'experience_ticket': str(ticket_file)},
    }), encoding='utf-8')

    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_PG_DSN', raising=False)
    monkeypatch.delenv('EVOLVMEM_CONSOLE_PG_CONFIG', raising=False)
    monkeypatch.delenv('EVOLVMEM_CONTROL_BACKEND', raising=False)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_CONFIG', str(cfg_file))

    assert console_access.is_pg_control_configured() is False
    settings = load_pg_experience_settings()
    assert settings.control_config is not None
    assert settings.ticket_secret == b'ticket-secret-xyz'
    with open_experience_conn(settings, role='reader') as rconn:
        assert rconn.execute('SELECT 1').fetchone()[0] == 1

    import psycopg
    sqlite_db = tmp_path / 'mig.db'
    _create_synthetic_sqlite(sqlite_db)
    vec = [0.5] + [0.0] * 767
    with psycopg.connect(dsn, options=f'-c search_path={schema},public') as mconn:
        apply_experience_migrations(mconn)
        res = migrate_manual_experiences(
            sqlite_db, mconn, secret=b's3cr3t', owner_filter='synthetic_owner',
            embeddings_by_id={'manual-11111111111111111111111111111111': vec},
        )
        mconn.commit()
        assert res['notes_verified_count'] == 1
        row = mconn.execute('SELECT embedding IS NOT NULL FROM experience_notes WHERE id = %s', ('manual-11111111111111111111111111111111',)).fetchone()
        assert row and row[0] is True


def test_live_gate_wiring_and_multi_db_evidence_verifier(pg_exp_schema, tmp_path: Path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    from evolvmem.experience_v2.control.agent_identity import AgentAttribution
    from evolvmem.experience_v2.gate.rules import SqliteHistoryEvidenceVerifier
    from evolvmem.experience_v2.mcp_tools import call_experience_v2_tool
    from evolvmem.experience_v2.schema import apply_experience_migrations

    apply_experience_migrations(conn)
    conn.commit()
    conn.close()

    hist_db = tmp_path / 'hist.db'
    with sqlite3.connect(hist_db) as sconn:
        sconn.execute('CREATE TABLE session_archives (id INTEGER PRIMARY KEY, external_session_id TEXT)')
        sconn.execute('INSERT INTO session_archives (id, external_session_id) VALUES (101, "sess-101")')
        sconn.commit()

    verifier = SqliteHistoryEvidenceVerifier([tmp_path / 'missing.db', hist_db])
    assert verifier.verify_all([{'library': 'personal', 'source': 'archive:101', 'event': 1}])[0] is True

    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(json.dumps({'console_enabled': True, 'experience_v2_enabled': True}), encoding='utf-8')
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)

    attr = AgentAttribution(actor='u1', agent_id='a1', client='codex', model='unknown-m')
    class _FakeDocEmbedder:
        is_loaded = True
        def encode_document(self, text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

    bad = call_experience_v2_tool(
        'experience_propose',
        {
            'op': 'add', 'category': '系统环境经验', 'kind': 'pitfall',
            'problem': '用户要求检查容器配置',
            'solution': '查看了配置并已修复',
            'reusable_experience': '按要求排查配置',
            'evidence': [{'library': 'personal', 'source': 'archive:101', 'event': 1}],
            'request_id': 'req-bad-l0-001',
        },
        owner_id='u1', actor_id='u1', attribution=attr,
        history_db_paths=[tmp_path / 'missing.db', hist_db], embedder=_FakeDocEmbedder(),
    )
    assert bad['ok'] is True
    assert bad['state'] == 'rejected'

    good = call_experience_v2_tool(
        'experience_propose',
        {
            'op': 'add', 'category': '系统环境经验', 'kind': 'pitfall',
            'problem': '容器 DNS 超时导致内网不通',
            'solution': '固定 daemon resolv 后重启',
            'reusable_experience': '容器超时先核对 DNS 解析链',
            'evidence': [{'library': 'personal', 'source': 'archive:101', 'event': 1}],
            'request_id': 'req-good-l0-002',
        },
        owner_id='u1', actor_id='u1', attribution=attr,
        history_db_paths=[hist_db], embedder=_FakeDocEmbedder(),
    )
    assert good['ok'] is True
    assert good['state'] == 'review'


def test_pg_experience_store_accepts_sqlite3_row_source(monkeypatch):
    import contextlib
    import sqlite3
    import pytest
    from types import SimpleNamespace
    from evolvmem.experience_v2 import pg_experience_store as mod
    from evolvmem.experience_v2.writer import ExperienceWriteError

    sconn = sqlite3.connect(':memory:')
    sconn.row_factory = sqlite3.Row
    row = sconn.execute("SELECT 'owner_u' AS user_id, '/tmp' AS data_dir").fetchone()

    class _FakeCursor:
        def __init__(self, sql: str):
            self._sql = sql
        def fetchone(self):
            if 'SUM(' in self._sql:
                return (0, 0)
            if 'count(*)' in self._sql:
                return (0,)
            return None
        def fetchall(self):
            return []

    class _FakeConn:
        def execute(self, sql, params=()):
            return _FakeCursor(sql)

    @contextlib.contextmanager
    def _fake_open_conn(_settings=None, *, role='reader'):
        yield _FakeConn()

    monkeypatch.setattr(mod, 'open_experience_conn', _fake_open_conn)
    store = mod.PgExperienceStore(SimpleNamespace())
    res = store.read(row, {}, category='', query='', page=1, page_size=10, deleted=0)
    assert res['connected'] is True
    assert res['total'] == 0

    with pytest.raises(ExperienceWriteError) as exc_info:
        store.write(row, 'wrong_actor', {'action': 'create'}, {})
    assert exc_info.value.status == 403


def test_lan_tools_experience_v2_attribution_lookup(monkeypatch):
    import threading
    from pathlib import Path
    from types import SimpleNamespace
    from evolvmem.lan_tools import LanTools
    from evolvmem.experience_v2 import mcp_tools as exp_v2_mcp

    monkeypatch.setattr(
        exp_v2_mcp,
        'call_experience_v2_tool',
        lambda *a, **kw: {'ok': True, 'next_cursor': 38, 'events': []},
    )
    lt = object.__new__(LanTools)
    lt._attribution_lock = threading.RLock()
    lt._attributions = {}
    lt._latest_user_attribution = {}
    lt.runtime = SimpleNamespace(
        _namespace_for=lambda u, s: Path('/tmp'),
        _shared_engine=None,
        settings=SimpleNamespace(authenticate=lambda x: None),
    )
    out = lt._experience_v2('u1', 'experience_changes_since', {'cursor': 0})
    assert out['next_cursor'] == 38
