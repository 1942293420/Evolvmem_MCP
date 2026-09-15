"""Resumable encrypted Codex snapshots, scoped to one personal SQLite store."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time

from evolvmem.codex_transcript import parse_transcript, workspace_details
from evolvmem.lan_sharing import LanError
from evolvmem.session_archive import SessionArchiver, AESGCM


CAPTURE_TOOLS = frozenset({'session_archive_upload', 'session_archive_status', 'session_archive_retry', 'session_archive_assign'})


def capture_specs():
    identity = {k: {'type': 'string', 'minLength': 1, 'maxLength': n}
                for k, n in (('device_id', 64), ('session_id', 128))}
    upload = {**identity, 'project': {'type': 'string', 'maxLength': 128},
              'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64},
              'total_bytes': {'type': 'integer', 'minimum': 1},
              'offset': {'type': 'integer', 'minimum': 0},
              'content_b64': {'type': 'string', 'maxLength': 349528},
              'extract': {'type': 'boolean'},
              'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}}
    retry = {**identity, 'request_id': upload['request_id']}
    assign = {**retry, 'project': {'type': 'string', 'minLength': 1, 'maxLength': 128}}
    return {name: {'name': name, 'description': description,
                   'annotations': {'readOnlyHint': readonly},
                   'inputSchema': {'type': 'object', 'properties': props,
                                   'required': list(props), 'additionalProperties': False}}
            for name, props, readonly, description in (
                ('session_archive_upload', upload, False,
                 'Upload a complete persisted Codex JSONL snapshot in sequential base64 chunks. '
                 'Idempotent by device/session/content/offset. Archived and extracted are separate states.'),
                ('session_archive_status', identity, True,
                 'Read this personal device/session archive coverage and extraction status; no raw text.'),
                ('session_archive_retry', retry, False,
                 'Explicitly retry the latest failed or unrequested extraction after inspecting its status.'),
                ('session_archive_assign', assign, False,
                 'Explicitly assign an unclassified archive to a registered project and queue extraction.'))}


class LanCapture:
    """Caller serializes access with the existing LAN runtime dispatch lock."""

    def __init__(self, server):
        self.server = server
        self.store = server.context_service.store
        self.archiver = SessionArchiver(server.config, self.store)
        self.directory = server.config.data_dir / 'session_uploads'
        with self.store.transaction():
            self.conn.execute('''CREATE TABLE IF NOT EXISTS lan_session_uploads (
                device_id TEXT NOT NULL, session_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                project TEXT NOT NULL, declared_project TEXT NOT NULL,
                attribution_reason TEXT NOT NULL DEFAULT '', received_at REAL NOT NULL,
                backfill_status TEXT NOT NULL DEFAULT 'pending', backfill_result TEXT NOT NULL DEFAULT '{}',
                total_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0,
                archive_id INTEGER REFERENCES session_archives(id),
                extraction_status TEXT NOT NULL DEFAULT 'not_requested',
                extraction_result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(device_id, session_id, sha256))''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS lan_session_backfills (
                device_id TEXT NOT NULL, session_id TEXT NOT NULL,
                applied_revision INTEGER NOT NULL DEFAULT 0, workstream_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(device_id, session_id))''')

    @property
    def conn(self):
        return self.store._connection()

    @staticmethod
    def _identity(args):
        for name, maximum in (('device_id', 64), ('session_id', 128)):
            if not isinstance(args.get(name), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,%d}' % maximum, args[name]):
                raise LanError('invalid_' + name)
        return args['device_id'], args['session_id']

    def _stage(self, key):
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return self.directory / (digest + '.bin')

    def _read_stage(self, path):
        try:
            blob = path.read_bytes()
            return AESGCM(self.archiver._load_or_create_key()).decrypt(blob[:12], blob[12:], None)
        except Exception:
            raise LanError('upload_staging_unavailable') from None

    def _write_stage(self, path, raw):
        if AESGCM is None:
            raise LanError('archive_encryption_unavailable')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self.archiver._load_or_create_key()).encrypt(nonce, raw, None)
        temporary = path.with_suffix('.tmp')
        try:
            with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _latest(self, identity):
        return self.conn.execute('''SELECT * FROM lan_session_uploads
            WHERE device_id=? AND session_id=? AND archive_id IS NOT NULL
            ORDER BY total_bytes DESC, rowid DESC LIMIT 1''', identity).fetchone()

    @staticmethod
    def _receipt(row, status=None):
        result = dict(status=status or ('archived' if row['archive_id'] else 'receiving'),
                      next_offset=row['received_bytes'], total_bytes=row['total_bytes'],
                      source_sha256=row['sha256'], extraction_status=row['extraction_status'],
                      project=row['project'], attribution_reason=row['attribution_reason'],
                      backfill_status=row['backfill_status'])
        if row['archive_id']:
            result['archive_id'] = row['archive_id']
        if row['error']:
            result['processing_error'] = row['error']
        if row['extraction_result'] != '{}':
            result['extraction'] = json.loads(row['extraction_result'])
        if row['backfill_result'] != '{}':
            result['backfill'] = json.loads(row['backfill_result'])
        return result

    @classmethod
    def _stale_receipt(cls, latest, *, submitted_sha256, submitted_total_bytes):
        result = cls._receipt(latest, 'stale')
        result['submitted_sha256'] = submitted_sha256
        result['submitted_total_bytes'] = submitted_total_bytes
        return result

    def status(self, args):
        identity = self._identity(args)
        row = self._latest(identity)
        if row is None:
            row = self.conn.execute('''SELECT * FROM lan_session_uploads WHERE device_id=?
                AND session_id=? ORDER BY rowid DESC LIMIT 1''', identity).fetchone()
        if row is None:
            return {'status': 'not_found'}
        result = self._receipt(row)
        if row['archive_id']:
            archive = self.store.get_session_archive(row['archive_id'])
            result['payload_state'] = archive['state'] if archive else 'unavailable'
        return result

    def claim_pending(self):
        row = self.conn.execute('''SELECT a.* FROM lan_session_uploads a
            WHERE a.extraction_status='pending' AND a.archive_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM lan_session_uploads b WHERE b.device_id=a.device_id
                AND b.session_id=a.session_id AND b.archive_id IS NOT NULL AND b.total_bytes>a.total_bytes)
            ORDER BY a.rowid LIMIT 1''').fetchone()
        if row is None:
            return None
        with self.store.transaction():
            self.conn.execute("UPDATE lan_session_uploads SET extraction_status='processing' WHERE device_id=? AND session_id=? AND sha256=?",
                              (row['device_id'], row['session_id'], row['sha256']))
        return dict(row)

    def retry(self, args):
        identity = self._identity(args)
        row = self._latest(identity)
        if row is None:
            raise LanError('archive_not_found')
        if not row['project']:
            raise LanError('archive_project_unassigned')
        with self.store.transaction():
            self.conn.execute("UPDATE lan_session_uploads SET backfill_status='pending' WHERE device_id=? AND session_id=? AND sha256=?",
                              (*identity, row['sha256']))
            if row['extraction_status'] not in ('extracted', 'processing', 'pending'):
                self.conn.execute("UPDATE lan_session_uploads SET extraction_status='pending',error='' WHERE device_id=? AND session_id=? AND sha256=?",
                                  (*identity, row['sha256']))
        return self.status(args)

    def assign(self, args):
        identity = self._identity(args)
        project = args['project']
        known = self.conn.execute("SELECT 1 FROM context_project_registry WHERE project=? AND status='active'", (project,)).fetchone()
        if known is None:
            raise LanError('project_not_registered')
        row = self._latest(identity)
        if row is None:
            raise LanError('archive_not_found')
        if row['project'] == project:
            return self.status(args)
        if row['project'] or row['extraction_status'] == 'processing':
            raise LanError('archive_project_conflict')
        with self.store.transaction():
            self.conn.execute("UPDATE lan_session_uploads SET project=?,extraction_status='pending',backfill_status='pending',error='' WHERE device_id=? AND session_id=? AND sha256=?",
                              (project, *identity, row['sha256']))
            self.conn.execute('UPDATE session_archives SET project=? WHERE id=?', (project, row['archive_id']))
        return self.status(args)

    def finish_extraction(self, row, *, result=None, error=''):
        with self.store.transaction():
            self.conn.execute('''UPDATE lan_session_uploads SET extraction_status=?, extraction_result=?, error=?
                WHERE device_id=? AND session_id=? AND sha256=?''',
                ('failed' if error else 'extracted', json.dumps(result or {}), error,
                 row['device_id'], row['session_id'], row['sha256']))

    def upload(self, args):
        identity = self._identity(args)
        digest = args['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise LanError('invalid_transcript_hash')
        key = (*identity, digest)
        total, offset = args['total_bytes'], args['offset']
        try:
            chunk = base64.b64decode(args['content_b64'], validate=True)
        except (ValueError, TypeError):
            raise LanError('invalid_chunk') from None
        if not chunk or len(chunk) > 262144 or offset + len(chunk) > total:
            raise LanError('invalid_chunk')
        row = self.conn.execute('SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone()
        if row is not None and (row['total_bytes'] != total or row['declared_project'] != args['project']):
            raise LanError('upload_metadata_conflict')
        path = self._stage(key)
        if row is not None and row['archive_id']:
            payload = self.archiver.read_payload(row['archive_id'])
            if payload is None:
                raise LanError('archive_payload_unavailable')
            raw = json.loads(payload)['transcript'].encode('utf-8')
        elif path.exists():
            # File goes before SQLite; an interrupted metadata commit can be resumed.
            raw = self._read_stage(path)
        else:
            raw = b''
        if offset > len(raw) or (offset < len(raw) and raw[offset:offset + len(chunk)] != chunk):
            raise LanError('invalid_chunk')
        if offset == len(raw):
            raw += chunk
        if len(raw) > total:
            raise LanError('invalid_chunk')
        complete = len(raw) == total
        archive_id = row['archive_id'] if row is not None else None
        project = row['project'] if row is not None else args['project']
        details = {}
        reason = row['attribution_reason'] if row is not None else ''
        if complete:
            if hashlib.sha256(raw).hexdigest() != digest:
                raise LanError('transcript_hash_mismatch')
            rows, _ = parse_transcript(raw, identity[1])
            details = workspace_details(rows)
            reason = details['attribution_reason']
            if reason and not archive_id:
                project = ''
            latest = self._latest(identity)
            if latest is not None and latest['sha256'] != digest:
                previous = self.archiver.read_payload(latest['archive_id'])
                if previous is None:
                    prior_length = int(latest['total_bytes'])
                    if len(raw) < prior_length:
                        raise LanError('previous_archive_unavailable')
                    if hashlib.sha256(raw[:prior_length]).hexdigest() != latest['sha256']:
                        raise LanError('transcript_fork')
                    prior = None
                else:
                    prior = json.loads(previous)['transcript'].encode('utf-8')
                if prior is not None and prior.startswith(raw):
                    path.unlink(missing_ok=True)
                    return self._stale_receipt(
                        latest,
                        submitted_sha256=digest,
                        submitted_total_bytes=total,
                    )
                if prior is not None and not raw.startswith(prior):
                    raise LanError('transcript_fork')
        # Retain encrypted staging until the archive pointer is committed.
        if not archive_id:
            self._write_stage(path, raw)
        if complete and not archive_id:
            payload = json.dumps({'source': 'client_reported', 'device_id': identity[0],
                                  'session_id': identity[1], 'source_sha256': digest,
                                  'parent_session_id': details.get('parent_session_id', ''),
                                  'transcript': raw.decode('utf-8')}, ensure_ascii=False)
            # Versions are immutable: old evidence references retain their payload.
            external = hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ':' + digest
            archive = self.archiver.archive_session(project, 'codex', external, payload)
            if archive is None:
                raise LanError('archive_unavailable')
            archive_id = archive.id
        extraction = row['extraction_status'] if row is not None else 'not_requested'
        if args['extract'] and extraction == 'not_requested':
            extraction = 'pending' if project else 'unassigned'
        if complete and not project and extraction == 'pending':
            extraction = 'unassigned'
        with self.store.transaction():
            self.conn.execute('''INSERT INTO lan_session_uploads
                (device_id,session_id,sha256,project,declared_project,attribution_reason,received_at,
                 total_bytes,received_bytes,archive_id,extraction_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,session_id,sha256) DO UPDATE SET
                received_bytes=excluded.received_bytes,archive_id=excluded.archive_id,
                project=excluded.project,attribution_reason=excluded.attribution_reason,
                received_at=CASE WHEN lan_session_uploads.archive_id IS NULL THEN excluded.received_at ELSE lan_session_uploads.received_at END,
                extraction_status=excluded.extraction_status''',
                (*key, project, args['project'], reason, time.time(), total, len(raw), archive_id, extraction))
            if complete:
                self.conn.execute("""UPDATE lan_session_uploads SET extraction_status='superseded'
                    WHERE device_id=? AND session_id=? AND total_bytes<? AND extraction_status='pending'""",
                    (*identity, total))
        if complete:
            path.unlink(missing_ok=True)
        return self._receipt(self.conn.execute('SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone())
