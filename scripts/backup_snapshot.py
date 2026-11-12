"""Create a private, self-contained snapshot of available encrypted archives.

Uses SQLite's online backup API, copies only snapshot-referenced immutable files,
and fails closed if concurrent maintenance removes a referenced file. No source
mutation, application initialization, automatic retry, cleanup, or restore occurs.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from types import SimpleNamespace

from evolvmem.archive_storage import ArchiveStorage


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(piece)
    return digest.hexdigest()


def _safe_source(root, relative):
    path = root / relative
    resolved = path.resolve(strict=True)
    if Path(relative).is_absolute() or not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError('unsafe_archive_path')
    return path


def create_snapshot(source, destination, *, sample_count=3):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists():
        raise FileExistsError(destination)
    if destination.resolve().is_relative_to(source):
        raise ValueError('backup_must_be_outside_data_directory')
    destination.mkdir(mode=0o700, parents=True)
    os.chmod(destination, 0o700)
    started = datetime.now(timezone.utc).isoformat()
    files = {}

    def copy(relative):
        if relative in files:
            return
        original = _safe_source(source, relative)
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        os.chmod(target, 0o600)
        with target.open('rb') as copied:
            os.fsync(copied.fileno())
        files[relative] = {'bytes': target.stat().st_size, 'sha256': _sha(target)}

    db_path = destination / 'memory.db'
    db_path.touch(mode=0o600, exist_ok=False)
    with sqlite3.connect((source / 'memory.db').as_uri() + '?mode=ro', uri=True, timeout=5) as origin:
        # Pin the WAL read snapshot. Without this, every writer may restart the
        # paged backup, preventing a busy multi-gigabyte database from finishing.
        origin.execute('BEGIN')
        origin.execute('SELECT count(*) FROM sqlite_master').fetchone()
        deadline = time.monotonic() + 180
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('backup_database_snapshot_deadline')
        with sqlite3.connect(db_path) as backup:
            origin.backup(backup, pages=512, sleep=0.02, progress=progress)
    os.chmod(db_path, 0o600)
    for name in ('archive.key', 'workspace.key', 'config.json', 'web_auth.json'):
        if (source / name).exists():
            copy(name)
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('backup_database_check_failed')
        rows = [dict(row) for row in conn.execute(
            "SELECT a.id,a.payload_path,a.payload_sha256,s.manifest_sha256,"
            "s.content_sha256,s.logical_bytes FROM session_archives a "
            "LEFT JOIN session_archive_storage s ON s.archive_id=a.id "
            "WHERE a.state='available' ORDER BY a.id")]
    storage = None
    if rows:
        key = (destination / 'archive.key').read_bytes()
        storage = ArchiveStorage(SimpleNamespace(data_dir=destination), key)
    chunks = set()
    for row in rows:
        relative = row['payload_path']
        # Enforce the archive namespace before even resolving the candidate.
        if Path(relative).is_absolute() or '..' in Path(relative).parts or not relative.startswith('session_archives/'):
            raise ValueError('unsafe_archive_path')
        copy(relative)
        blob = (destination / relative).read_bytes()
        expected = row['manifest_sha256'] or row['payload_sha256']
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError('backup_archive_manifest_changed')
        manifest = storage.manifest(blob)
        if manifest is not None:
            if manifest['sha256'] != row['content_sha256'] or manifest['size'] != row['logical_bytes']:
                raise ValueError('backup_archive_metadata_mismatch')
            for part in manifest['parts']:
                if isinstance(part, str):
                    if part not in chunks:
                        copy('session_chunks/' + part + '.bin')
                        storage._read_block(part)  # authenticated decrypt, size and digest
                        chunks.add(part)
                else:
                    base64.b64decode(part['inline'], validate=True)
    candidates = [row for row in rows if row['logical_bytes'] is None or row['logical_bytes'] <= 1024 * 1024]
    count = max(0, int(sample_count))
    sampled = candidates[:count // 2] + candidates[-(count - count // 2):] if count else []
    verified_ids = []
    for row in sampled:
        if row['id'] not in verified_ids:
            storage.unpack((destination / row['payload_path']).read_bytes())
            verified_ids.append(row['id'])
    files['memory.db'] = {'bytes': db_path.stat().st_size, 'sha256': _sha(db_path)}
    with db_path.open('rb') as database_file:
        os.fsync(database_file.fileno())
    if os.name != 'nt':
        for directory in sorted({(destination / name).parent for name in files}, key=str):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    result = {
        'complete': True, 'started_utc': started,
        'finished_utc': datetime.now(timezone.utc).isoformat(),
        'available_archives': len(rows), 'authenticated_unique_chunks': len(chunks),
        'sample_payloads_verified': len(verified_ids), 'sample_archive_ids': verified_ids,
        'database_quick_check': 'ok', 'files': files,
        'scope': 'Snapshot database, available archive manifests, all referenced encrypted chunks and private local keys/config. Excludes purged payloads, unrelated users, vectors and unreferenced files. Same-host snapshot is not off-host disaster recovery.',
    }
    manifest_path = destination / 'snapshot-manifest.json'
    with manifest_path.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(manifest_path, 0o600)
    if os.name != 'nt':
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {key: value for key, value in result.items() if key != 'files'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(create_snapshot(args.data_dir, args.output, sample_count=args.samples), indent=2))


if __name__ == '__main__':
    main()
