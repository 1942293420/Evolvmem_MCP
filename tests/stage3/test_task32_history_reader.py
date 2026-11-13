"""TDD tests for Stage 3 Task 3.2: HistoryReader Protocol & strictly read-only SQLite adapter."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest


def _seed_sqlite_memory_db(base_dir: Path) -> None:
    db_path = base_dir / 'memory.db'
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            adapter TEXT NOT NULL,
            external_session_id TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'available',
            expires_at TEXT NOT NULL,
            purged_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE conversation_history (
            archive_id INTEGER PRIMARY KEY,
            messages TEXT NOT NULL,
            body TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            cleaned_at TEXT NOT NULL
        );
        CREATE TABLE lan_session_uploads (
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            archive_id INTEGER,
            source_format TEXT NOT NULL DEFAULT 'codex-jsonl'
        );
        """
    )
    from evolvmem.archive_storage import ArchiveStorage
    from evolvmem.config import Config

    key = os.urandom(32)
    (base_dir / 'archive.key').write_bytes(key)
    storage = ArchiveStorage(Config(data_dir=base_dir, apply_environment=False), key)
    raw_codex = '\n'.join([
        json.dumps({'type': 'session_meta', 'payload': {'id': 'sess-c1'}}),
        json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'check port 9378'}}),
        json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'exit code 0\nok'}}),
    ]) + '\n'
    blob, _ = storage.pack(raw_codex)
    (base_dir / 'session_archives').mkdir(parents=True, exist_ok=True)
    rel_path = 'session_archives/test1.bin'
    (base_dir / rel_path).write_bytes(blob)

    msgs = [
        {'role': 'user', 'content': 'check port 9378'},
        {'role': 'assistant', 'content': 'verified active'},
    ]
    conn.execute(
        'INSERT INTO session_archives VALUES (1, ?, ?, ?, ?, ?, ?, ?, NULL, ?)',
        ('proj-a', 'codex', 'sess-c1', rel_path, 'sha1', 'available', '2099-01-01 00:00:00', '2026-11-13 01:00:00'),
    )
    conn.execute(
        'INSERT INTO conversation_history VALUES (1, ?, ?, ?, ?)',
        (json.dumps(msgs), 'text', 'h1', '2026-11-13 01:00:01'),
    )
    conn.execute(
        'INSERT INTO lan_session_uploads VALUES (?, ?, ?, 1, ?)',
        ('dev-1', 'sess-c1', 'sha1', 'codex-jsonl'),
    )
    # Archive 2 without binary payload (conversation_history fallback)
    conn.execute(
        'INSERT INTO session_archives VALUES (2, ?, ?, ?, ?, ?, ?, ?, NULL, ?)',
        ('proj-b', 'kimi', 'sess-k2', 'session_archives/missing.bin', 'sha2', 'available', '2099-01-01 00:00:00', '2026-11-13 02:00:00'),
    )
    conn.execute(
        'INSERT INTO conversation_history VALUES (2, ?, ?, ?, ?)',
        (json.dumps([{'role': 'user', 'content': 'msg1'}, {'role': 'assistant', 'content': 'msg2'}, {'role': 'user', 'content': 'msg3'}]), 't2', 'h2', '2026-11-13 02:00:01'),
    )
    conn.commit()
    conn.close()


def _seed_candidate_db(cand_path: Path) -> None:
    conn = sqlite3.connect(cand_path)
    conn.executescript(
        """
        CREATE TABLE blocks(
            block_id TEXT PRIMARY KEY, adapter TEXT, kind TEXT, family_key TEXT,
            native_session_id TEXT, native_device_id TEXT, native_id_evidence TEXT,
            title TEXT, message_count INTEGER, turn_count INTEGER,
            attachment_recovered INTEGER, attachment_missing INTEGER,
            observed_first_at TEXT, observed_last_at TEXT, ended_at TEXT,
            coverage_state TEXT, coverage_json TEXT, source_archive_ids TEXT,
            failed_archive_ids TEXT, projects TEXT, block_json TEXT, block_sha256 TEXT
        );
        CREATE TABLE messages(
            block_id TEXT, ordinal INTEGER, role TEXT, content TEXT, recorded_at TEXT,
            archive_id INTEGER, message_index INTEGER, attachments TEXT,
            PRIMARY KEY(block_id, ordinal)
        );
        """
    )
    conn.execute(
        'INSERT INTO blocks(block_id, adapter, kind, native_session_id, observed_first_at, projects, message_count) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        ('blk-42', 'dsh', 'native_session_recovery', 'dsh-42', '2026-11-12 10:00:00', json.dumps(['proj-c']), 2),
    )
    conn.execute(
        'INSERT INTO messages VALUES (?, 1, ?, ?, ?, 42, 1, ?)',
        ('blk-42', 'user', 'run command', '2026-11-12 10:00:01', '[]'),
    )
    conn.execute(
        'INSERT INTO messages VALUES (?, 2, ?, ?, ?, 42, 2, ?)',
        ('blk-42', 'assistant', 'command succeeded', '2026-11-12 10:00:02', '[]'),
    )
    conn.commit()
    conn.close()


def test_sqlite_history_reader_session_archives_and_encrypted_payload(tmp_path):
    from evolvmem.experience_v2.pipeline.history_reader import HistoryReader, SqliteHistoryReader

    mem_dir = tmp_path / 'mem'
    mem_dir.mkdir()
    _seed_sqlite_memory_db(mem_dir)
    mtime_before = (mem_dir / 'memory.db').stat().st_mtime_ns

    reader = SqliteHistoryReader(memory_db=mem_dir / 'memory.db', data_dir=mem_dir)
    assert isinstance(reader, HistoryReader)

    sessions = reader.list_sessions()
    assert len(sessions) == 2
    assert reader.list_sessions(project='proj-a')[0].source_ref == 'archive:1'
    assert reader.list_sessions(adapter='kimi')[0].source_ref == 'archive:2'

    s1 = reader.get_session('archive:1')
    assert s1 is not None
    assert s1.summary.source_format == 'codex-jsonl'
    assert s1.raw_bytes is not None
    assert b'"session_meta"' in s1.raw_bytes
    assert len(s1.records) >= 2

    s2 = reader.get_session('archive:2')
    assert s2 is not None
    assert len(s2.records) == 3
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'archive:2', 'event': 2}) is True
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'archive:2', 'event': 99}) is False
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'archive:999', 'event': 1}) is False

    # Read-only guarantee: DB file untouched and RO connection rejects writes
    assert (mem_dir / 'memory.db').stat().st_mtime_ns == mtime_before
    with reader._connect(mem_dir / 'memory.db') as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute('CREATE TABLE should_fail (x INT)')


def test_sqlite_history_reader_candidate_db_blocks(tmp_path):
    from evolvmem.experience_v2.pipeline.history_reader import SqliteHistoryReader

    cand_path = tmp_path / 'candidate.db'
    _seed_candidate_db(cand_path)

    reader = SqliteHistoryReader(candidate_db=cand_path)
    listed = reader.list_sessions(project='proj-c')
    assert len(listed) == 1
    assert listed[0].source_ref == 'block:blk-42'

    blk = reader.get_session('block:blk-42')
    assert blk is not None
    assert [r.event_index for r in blk.records] == [1, 2]
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'block:blk-42', 'event': 2}) is True
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'archive:42', 'event': 2}) is True
    assert reader.verify_evidence_pointer({'library': 'personal', 'source': 'block:blk-42', 'event': 3}) is False


def test_pipeline_consumer_swaps_sqlite_and_mock_pg_reader_unchanged(tmp_path):
    from evolvmem.experience_v2.pipeline.history_reader import (
        HistoryRawRecord,
        HistoryReader,
        HistorySession,
        HistorySessionSummary,
        InMemoryHistoryReader,
        SqliteHistoryReader,
    )

    def summarize_sessions(reader: HistoryReader) -> list[tuple[str, int]]:
        return [
            (s.source_ref, len(reader.get_session(s.source_ref).records))
            for s in reader.list_sessions()
        ]

    cand_path = tmp_path / 'candidate.db'
    _seed_candidate_db(cand_path)
    sqlite_reader = SqliteHistoryReader(candidate_db=cand_path)

    pg_sim_session = HistorySession(
        summary=HistorySessionSummary(
            session_id='block:blk-42',
            adapter='dsh',
            source_format='messages',
            project='proj-c',
            source_ref='block:blk-42',
            library='personal',
            external_session_id='dsh-42',
            created_at='2026-11-12 10:00:00',
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=1, role='user', content='run command'),
            HistoryRawRecord(event_index=2, role='assistant', content='command succeeded'),
        ),
    )
    pg_sim_reader = InMemoryHistoryReader([pg_sim_session])
    assert summarize_sessions(sqlite_reader) == summarize_sessions(pg_sim_reader)


def test_no_direct_sqlite_outside_history_reader_in_pipeline():
    from evolvmem.experience_v2.pipeline.history_reader import scan_direct_sqlite_in_pipeline

    pipe_dir = Path(__file__).resolve().parents[2] / 'evolvmem' / 'experience_v2' / 'pipeline'
    assert scan_direct_sqlite_in_pipeline(pipe_dir) == []


def test_stage0_readonly_snapshot_smoke():
    from evolvmem.experience_v2.pipeline.history_reader import SqliteHistoryReader

    snap_mem = Path('/home/jiangli/evolvmem-stage0-20261112/backups/sqlite-20261112/owner_memory.db')
    snap_cand = Path('/home/jiangli/evolvmem-stage0-20261112/backups/sqlite-20261112/history_candidate.db')
    if not snap_mem.is_file() or not snap_cand.is_file():
        pytest.skip('Stage 0 read-only snapshot not present in this environment')

    reader = SqliteHistoryReader(memory_db=snap_mem, candidate_db=snap_cand)
    listed = reader.list_sessions(limit=5)
    assert len(listed) == 5
    first = reader.get_session(listed[0].source_ref)
    assert first is not None and len(first.records) >= 1
