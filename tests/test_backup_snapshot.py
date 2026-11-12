import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest
from evolvmem.archive_storage import ArchiveStorage


def seed(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    key = os.urandom(32)
    (source / 'archive.key').write_bytes(key)
    (source / 'config.json').write_text('{"adapter":"codex"}')
    storage = ArchiveStorage(SimpleNamespace(data_dir=source, context_archive_min_free_mb=0), key)
    payload = json.dumps({'message': 'historical source ' * 800})
    blob, manifest = storage.pack(payload)
    relative = 'session_archives/sample.bin'
    (source / 'session_archives').mkdir()
    (source / relative).write_bytes(blob)
    c = sqlite3.connect(source / 'memory.db')
    c.execute('CREATE TABLE session_archives(id INTEGER PRIMARY KEY,payload_path TEXT,payload_sha256 TEXT,state TEXT)')
    c.execute('CREATE TABLE session_archive_storage(archive_id INTEGER PRIMARY KEY,manifest_sha256 TEXT,content_sha256 TEXT,logical_bytes INTEGER)')
    c.execute('INSERT INTO session_archives VALUES(1,?,?,?)', (relative, hashlib.sha256(blob).hexdigest(), 'available'))
    c.execute('INSERT INTO session_archive_storage VALUES(1,?,?,?)', (hashlib.sha256(blob).hexdigest(), manifest['sha256'], manifest['size']))
    c.commit()
    c.close()
    return source, payload, manifest


def test_snapshot_restores_referenced_encrypted_archive_without_touching_source(tmp_path):
    from scripts.backup_snapshot import create_snapshot
    source, payload, _ = seed(tmp_path)
    before = (source / 'memory.db').read_bytes()
    (source / 'session_chunks' / 'unreferenced.bin').write_bytes(b'unreferenced')
    dest = tmp_path / 'backup'
    result = create_snapshot(source, dest, sample_count=1)
    assert result['complete'] is True
    assert result['available_archives'] == 1
    assert result['sample_payloads_verified'] == 1
    assert (source / 'memory.db').read_bytes() == before
    storage = ArchiveStorage(SimpleNamespace(data_dir=dest), (dest / 'archive.key').read_bytes())
    assert storage.unpack((dest / 'session_archives/sample.bin').read_bytes()) == payload
    assert not (dest / 'session_chunks/unreferenced.bin').exists()
    assert json.loads((dest / 'snapshot-manifest.json').read_text())['complete'] is True
    assert os.stat(dest / 'archive.key').st_mode & 0o777 == 0o600


def test_snapshot_missing_chunk_does_not_claim_complete(tmp_path):
    from scripts.backup_snapshot import create_snapshot
    source, _, manifest = seed(tmp_path)
    digest = next(part for part in manifest['parts'] if isinstance(part, str))
    (source / 'session_chunks' / (digest + '.bin')).unlink()
    dest = tmp_path / 'backup'
    with pytest.raises(FileNotFoundError):
        create_snapshot(source, dest)
    assert not (dest / 'snapshot-manifest.json').exists()
    assert (source / 'memory.db').is_file()


def test_snapshot_refuses_path_escape_and_existing_destination(tmp_path):
    from scripts.backup_snapshot import create_snapshot
    source, _, _ = seed(tmp_path)
    conn = sqlite3.connect(source / 'memory.db')
    conn.execute("UPDATE session_archives SET payload_path='../outside.bin'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match='unsafe_archive_path'):
        create_snapshot(source, tmp_path / 'bad-backup')
    with pytest.raises(FileExistsError):
        create_snapshot(source, source)


def test_online_snapshot_does_not_restart_on_concurrent_writer(tmp_path, monkeypatch):
    from scripts.backup_snapshot import create_snapshot
    source, _, _ = seed(tmp_path)
    original_connect = sqlite3.connect
    conn = original_connect(source / 'memory.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE epoch(value INTEGER,padding BLOB)')
    conn.execute('INSERT INTO epoch VALUES(0,zeroblob(6000000))')
    conn.commit()
    conn.close()
    class ConcurrentWriteConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            existing = kwargs.pop('progress', None)
            changed = False
            def progress(status, remaining, total):
                nonlocal changed
                if remaining and not changed:
                    with original_connect(source / 'memory.db', timeout=1) as writer:
                        writer.execute('UPDATE epoch SET value=1')
                    changed = True
                if existing:
                    existing(status, remaining, total)
            return super().backup(target, progress=progress, **kwargs)
    def connect(path, *args, **kwargs):
        if str(path).startswith((source / 'memory.db').as_uri()):
            kwargs['factory'] = ConcurrentWriteConnection
        return original_connect(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    create_snapshot(source, tmp_path / 'snapshot')
    with original_connect(tmp_path / 'snapshot/memory.db') as saved:
        assert saved.execute('SELECT value FROM epoch').fetchone()[0] == 0
    with original_connect(source / 'memory.db') as current:
        assert current.execute('SELECT value FROM epoch').fetchone()[0] == 1
