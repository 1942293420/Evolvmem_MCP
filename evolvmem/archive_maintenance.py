"""Lossless archive compaction and reference-based garbage collection.

Automatic maintenance never expires unique original evidence. It replaces
full ciphertext copies only after exact reconstruction, and reclaims files
that no available archive references. Logical archive IDs remain intact.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time

from evolvmem.archive_storage import MAGIC

logger = logging.getLogger(__name__)


def protected_archive_ids(store):
    conn = store._connection()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    protected = set()
    for table, column in (('context_sources', 'archive_id'),
                          ('session_archive_holds', 'archive_id'),
                          ('organization_context_basis', 'prior_archive_id')):
        if table in tables:
            protected.update(r[0] for r in conn.execute(
                f'SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL'))
    fields = {'source_key', 'prior_source_key', 'source_ref', 'members',
              'context_basis', 'experience_payload', 'source_ranges', 'task_record'}
    for table in tables:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', table):
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for column in columns & fields:
            for row in conn.execute(f'SELECT DISTINCT "{column}" FROM "{table}"'):
                text = str(row[0] or '')
                ids = re.findall(r'archive:(\d+)', text)
                ids += re.findall(r'"(?:prior_)?archive_id"\s*:\s*(\d+)', text)
                protected.update(map(int, ids))
    if {'lan_session_uploads', 'lan_session_heads'} <= tables:
        protected.update(r[0] for r in conn.execute(
            'SELECT u.archive_id FROM lan_session_uploads u JOIN lan_session_heads h '
            'ON u.device_id=h.device_id AND u.session_id=h.session_id AND u.sha256=h.sha256'))
        protected.update(r[0] for r in conn.execute(
            "SELECT archive_id FROM lan_session_uploads WHERE extraction_status IN ('pending','processing') "
            "OR backfill_status IN ('pending','processing')"))
    return protected - {None}


def compact(archiver, *, limit=20, max_bytes=32 * 1024 * 1024):
    """Convert a bounded batch without changing content, IDs or source revisions."""
    store, root = archiver.store, archiver.config.data_dir
    conn = store._connection()
    rows = conn.execute("SELECT a.id FROM session_archives a LEFT JOIN session_archive_storage s "
                        "ON a.id=s.archive_id WHERE a.state='available' AND s.archive_id IS NULL "
                        'ORDER BY a.id LIMIT ?', (limit,)).fetchall()
    result = {'compacted': 0, 'errors': 0, 'processed_bytes': 0}
    for (aid,) in rows:
        written = None
        try:
            row = store.get_session_archive(aid)
            if not row or row['state'] != 'available':
                continue
            original_path = root / row['payload_path']
            result['processed_bytes'] += original_path.stat().st_size
            original = archiver.read_payload(aid)
            if original is None:
                raise ValueError('archive_unreadable')
            # Compression and exact reconstruction can be expensive for old
            # full snapshots. Keep them outside the SQLite writer lock.
            storage = archiver._storage()
            blob, manifest = storage.pack(original)
            if storage.unpack(blob) != original:
                raise ValueError('archive_reconstruction_mismatch')
            with store.transaction():
                current = store.get_session_archive(aid)
                if (not current or current['state'] != 'available'
                        or current['payload_path'] != row['payload_path']
                        or conn.execute('SELECT 1 FROM session_archive_storage WHERE archive_id=?', (aid,)).fetchone()):
                    continue
                # GC may run while preparing. It cannot remove referenced
                # blocks; check unreferenced reused blocks again under its lock.
                if any(not storage._path(part).is_file() for part in manifest['parts'] if isinstance(part, str)):
                    raise ValueError('archive_block_unavailable')
                relative = 'session_archives/' + hashlib.sha256(blob).hexdigest() + '.bin'
                target = root / relative
                archiver._write_payload_file(target, blob)
                written = target
                # payload_sha256 is the immutable evidence revision. Physical
                # manifest integrity has its own digest in the storage table.
                conn.execute('UPDATE session_archives SET payload_path=? WHERE id=?', (relative, aid))
                archiver._register_storage(aid, blob, manifest)
                store.defer_until_commit(lambda row=row, relative=relative:
                                          archiver._remove_superseded(row, relative))
            result['compacted'] += 1
        except Exception:
            if written is not None:
                written.unlink(missing_ok=True)
            result['errors'] += 1
            logger.warning('session archive %d compaction deferred', aid)
        if result['processed_bytes'] >= max_bytes:
            break
    result['remaining'] = conn.execute("SELECT count(*) FROM session_archives a LEFT JOIN "
        "session_archive_storage s ON a.id=s.archive_id WHERE a.state='available' AND s.archive_id IS NULL").fetchone()[0]
    return result


def collect(archiver, *, grace_seconds=3600, now=None):
    """Mark from actual live manifests before sweeping; fail closed on damage."""
    root, store = archiver.config.data_dir, archiver.store
    result = {'reclaimed_bytes': 0, 'removed_files': 0, 'errors': 0}
    cutoff = (time.time() if now is None else now) - grace_seconds
    with store.transaction():
        storage = archiver._storage()
        live_files, live_blocks = set(), set()
        rows = store._connection().execute(
            'SELECT a.payload_path,s.archive_id FROM session_archives a '
            "LEFT JOIN session_archive_storage s ON a.id=s.archive_id WHERE a.state='available'").fetchall()
        try:
            for relative, encoded in rows:
                path = root / relative
                if path.parent != root / 'session_archives' or path.is_symlink():
                    raise ValueError('invalid_archive_path')
                live_files.add(path.name)
                with path.open('rb') as stream:
                    prefix = stream.read(len(MAGIC))
                    if prefix != MAGIC:
                        if encoded is not None:
                            raise ValueError('archive_manifest_unreadable')
                        continue  # Legacy ciphertext cannot name shared blocks.
                    manifest = storage.manifest(prefix + stream.read())
                for part in manifest['parts']:
                    if isinstance(part, str):
                        if not storage._path(part).is_file():
                            raise ValueError('archive_block_unavailable')
                        live_blocks.add(part + '.bin')
        except Exception:
            result['errors'] += 1
            return result
        for directory, live in ((root / 'session_archives', live_files),
                                (root / 'session_chunks', live_blocks)):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.name in live or path.is_symlink() or not path.is_file():
                    continue
                if not (re.fullmatch(r'[0-9a-f]{64}\.bin', path.name)
                        or (path.name.startswith('.block-') and path.suffix == '.tmp')):
                    continue
                try:
                    stat = path.stat()
                    if stat.st_mtime > cutoff:
                        continue
                    path.unlink()
                    result['reclaimed_bytes'] += stat.st_size
                    result['removed_files'] += 1
                except OSError:
                    result['errors'] += 1
    return result


def prune_history_cache(archiver, *, limit=100, max_bytes=32 * 1024 * 1024):
    """Only old LAN snapshots with a verified readable original are disposable."""
    store, conn = archiver.store, archiver.store._connection()
    result = {'pruned_history_caches': 0, 'history_has_more': False}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lan_session_heads'").fetchone():
        return result
    with store.transaction():
        protected = protected_archive_ids(store)
        rows = conn.execute('SELECT DISTINCT u.archive_id,a.payload_path FROM lan_session_uploads u '
            'JOIN lan_session_heads h ON u.device_id=h.device_id AND u.session_id=h.session_id '
            'JOIN session_archives a ON a.id=u.archive_id '
            'JOIN session_archive_storage s ON s.archive_id=a.id '
            'JOIN conversation_history ch ON ch.archive_id=a.id '
            "WHERE u.sha256!=h.sha256 AND a.state='available' AND s.logical_bytes<=? ORDER BY a.id", (max_bytes,)).fetchall()
        candidates = [(aid, path) for aid, path in rows if aid not in protected]
        used = 0
        examined = 0
        for aid, _ in candidates[:limit]:
            original = archiver.read_payload(aid)
            examined += 1
            if original is not None:
                used += len(original.encode())
                conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (aid,))
                result['pruned_history_caches'] += 1
            if used >= max_bytes:
                break
        result['history_has_more'] = examined < len(candidates)
    return result


def maintain(archiver, *, limit=20, max_bytes=32 * 1024 * 1024):
    result = compact(archiver, limit=limit, max_bytes=max_bytes)
    result.update(prune_history_cache(archiver, limit=100, max_bytes=max_bytes))
    garbage = collect(archiver)
    result['errors'] += garbage.pop('errors')
    result.update(garbage)
    return result


def save_report(config, report, *, checked_at=None):
    """Overwrite one small status file; never append bodies or create snapshots."""
    fd, temporary = tempfile.mkstemp(prefix='.archive-maintenance-', dir=config.data_dir)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'checked_at': time.time() if checked_at is None else checked_at,
                       'report': report}, stream)
        os.replace(temporary, config.data_dir / 'archive_maintenance.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LanArchiveMaintenance:
    """A bounded local task in the existing worker, without model calls."""
    def __init__(self, adapter, *, now=time.time):
        self.adapter = adapter
        self.now = now
        self.next_check = 0
        self.last_report = {}

    def check(self):
        now = self.now()
        if now < self.next_check:
            return False
        # Also back off after failures; never retry maintenance every five seconds.
        self.next_check = now + 3600
        from evolvmem.session_archive import SessionArchiver
        reports = {}
        from evolvmem.context_store import ContextStore
        with self.adapter.lock:
            namespaces = [(user + ':' + space, self.adapter.runtime.server_for(user, space).config)
                for user, space in (('jiangli', 'personal'), ('kane', 'personal'), ('jiangli', 'public'))]
        # An independent connection lets uploads proceed while old large
        # snapshots are being compressed. Only final pointer writes serialize.
        for name, config in namespaces:
            try:
                with ContextStore(config) as store:
                    report = maintain(SessionArchiver(config, store))
            except Exception:
                report = {'errors': 1, 'reason': 'maintenance_deferred'}
            reports[name] = report
            try:
                save_report(config, report, checked_at=now)
            except OSError:
                report['errors'] += 1
        if not any(r['errors'] for r in reports.values()) and any(
                r['remaining'] or r['history_has_more'] for r in reports.values()):
            self.next_check = now + 60
        self.last_report = reports
        return True
