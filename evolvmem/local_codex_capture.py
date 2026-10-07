"""Incremental capture of local Codex JSONL rollouts for the auto organizer.

Linux Codex has no capture hook, so a real session keeps writing its rollout
JSONL without ever entering ``session_archives``. This module is the missing
local collector: no service, no model, no dependency.

* Capture needs ``${data_dir}/local_codex_capture.json`` (valid, enabled) and the
  existing ``auto_new`` arrival switch, so pausing arrival stops collection.
* Only new complete LF records are consumed through a per-file cursor (physical
  identity, byte offset, line number). A half-written line waits, and every round
  is capped by byte, line, file and time budgets; a whole file is never read.
* ``since`` is the mandatory activation line, checked against each record's own
  timestamp (root first, then payload). A record with no timestamp inherits the
  newest one seen in the file, and with no valid lower bound yet it is filtered.
* An untouched file whose mtime predates ``since`` is never opened, so activation
  does not stream the old backlog; appends after activation are still found.
* Batches are immutable and keyed by session, source range and content digest, so
  retries and restarts never archive twice. The cursor advances only after the
  archive commits, and a truncated or rewritten file stops with an explicit
  reason instead of being silently tailed.
* Payloads keep the session id, per-line locations, event ids, timestamps and the
  raw batch lines, so every source stays traceable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from evolvmem.codex_transcript import dialogue_messages
from evolvmem.conversation import clean_messages
from evolvmem.session_archive import SessionArchiver

CONFIG_NAME = 'local_codex_capture.json'
ADAPTER = 'codex'

# Defaults for every round budget; each is a hard ceiling, not a target.
DEFAULT_BATCH_BYTES = 2 * 1024 * 1024
DEFAULT_BATCH_LINES = 400
DEFAULT_MAX_FILES_PER_SCAN = 4
DEFAULT_MAX_LINES_PER_SCAN = 2000
DEFAULT_MAX_SECONDS_PER_SCAN = 5.0
DEFAULT_MAX_LINE_BYTES = 32 * 1024 * 1024
DEFAULT_POLL_SECONDS = 2.0
ANCHOR_BYTES = 512

SESSION_ID_PATTERN = (r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
                      r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
ROLLOUT_PREFIX = 'rollout-'
ROLLOUT_SUFFIX = '.jsonl'

STATE_ADVANCING = 'advancing'
STATE_REWRITTEN = 'rewritten'
STATE_BLOCKED = 'blocked'

BLOCK_LINE_TOO_LONG = 'line_too_long'
BLOCK_CONTENT_CHANGED = 'content_changed_needs_review'
BLOCK_FILE_TRUNCATED = 'file_truncated_needs_review'
CONTENT_BLOCK_REASONS = (BLOCK_CONTENT_CHANGED, BLOCK_FILE_TRUNCATED)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS local_capture_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        path TEXT NOT NULL,
        device INTEGER NOT NULL DEFAULT 0,
        inode INTEGER NOT NULL DEFAULT 0,
        scanned_offset_bytes INTEGER NOT NULL DEFAULT 0,
        scanned_line INTEGER NOT NULL DEFAULT 0,
        pending_bytes INTEGER NOT NULL DEFAULT 0,
        since_floor REAL NOT NULL DEFAULT 0,
        file_size INTEGER NOT NULL DEFAULT 0,
        file_mtime REAL NOT NULL DEFAULT 0,
        anchor_offset INTEGER NOT NULL DEFAULT 0,
        anchor_bytes INTEGER NOT NULL DEFAULT 0,
        anchor_sha256 TEXT NOT NULL DEFAULT '',
        file_state TEXT NOT NULL DEFAULT 'advancing',
        blocked_cap_bytes INTEGER NOT NULL DEFAULT 0,
        archived_offset_bytes INTEGER NOT NULL DEFAULT 0,
        archived_line INTEGER NOT NULL DEFAULT 0,
        archived_batches INTEGER NOT NULL DEFAULT 0,
        archived_events INTEGER NOT NULL DEFAULT 0,
        filtered_events INTEGER NOT NULL DEFAULT 0,
        failed_batches INTEGER NOT NULL DEFAULT 0,
        rewrite_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        last_success_at TEXT NOT NULL DEFAULT '',
        last_error_at TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        UNIQUE(device, inode)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_capture_scans (
        id INTEGER PRIMARY KEY CHECK (id=1),
        last_scan_at TEXT NOT NULL DEFAULT '',
        last_success_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        scans INTEGER NOT NULL DEFAULT 0,
        scanned_bytes INTEGER NOT NULL DEFAULT 0,
        archived_batches INTEGER NOT NULL DEFAULT 0,
        archived_events INTEGER NOT NULL DEFAULT 0,
        filtered_events INTEGER NOT NULL DEFAULT 0
    )
    """,
)

# The schema marker is bounded to one connection: a keyed cache would keep every
# closed connection alive. Re-running CREATE IF NOT EXISTS for a new connection
# is cheap, so a single-entry cache is both correct and leak-free.
_SCHEMA_CONNECTION = None


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())


def ensure_schema(store):
    """Create this module's own tables once per live connection."""
    global _SCHEMA_CONNECTION
    connection = store._connection()
    if connection is _SCHEMA_CONNECTION:
        return
    with store.transaction():
        for statement in _SCHEMA:
            connection.execute(statement)
    _SCHEMA_CONNECTION = connection


# ------------------------------------------------------------------ timestamps

def parse_time(value):
    """Epoch seconds for an ISO-8601 or numeric timestamp; None when unreadable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    normalised = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        moment = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _event_time(row):
    """One record's own time: the root ``timestamp`` first, then the payload.

    Real Codex JSONL carries ``timestamp`` on the record root, including
    ``response_item``. ``session_meta`` also has one inside its payload, but
    there it means the whole session's start, so the root value is authoritative
    whenever it is present.
    """
    moment = parse_time(row.get('timestamp'))
    if moment is not None:
        return moment
    payload = row.get('payload')
    if not isinstance(payload, dict):
        return None
    for key in ('timestamp', 'started_at', 'created_at'):
        moment = parse_time(payload.get(key))
        if moment is not None:
            return moment
    milliseconds = payload.get('started_at_ms')
    if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool):
        return float(milliseconds) / 1000.0
    return None


def _event_id(row):
    payload = row.get('payload')
    if not isinstance(payload, dict):
        return ''
    for key in ('id', 'call_id', 'turn_id'):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ''


def _record_session_id(row, fallback):
    if row.get('type') != 'session_meta':
        return ''
    payload = row.get('payload')
    if isinstance(payload, dict) and isinstance(payload.get('id'), str):
        return payload['id']
    return fallback


# --------------------------------------------------------------- configuration

class CaptureConfig:
    """Parsed ``local_codex_capture.json``; invalid input never collects.

    Every budget is named exactly like its JSON key, so a documented key can
    never be silently ignored.
    """

    def __init__(self, *, enabled=False, roots=(), since=0.0,
                 poll_seconds=DEFAULT_POLL_SECONDS,
                 batch_bytes=DEFAULT_BATCH_BYTES, batch_lines=DEFAULT_BATCH_LINES,
                 max_files_per_scan=DEFAULT_MAX_FILES_PER_SCAN,
                 max_lines_per_scan=DEFAULT_MAX_LINES_PER_SCAN,
                 max_seconds_per_scan=DEFAULT_MAX_SECONDS_PER_SCAN,
                 max_line_bytes=DEFAULT_MAX_LINE_BYTES,
                 restart_changed_files=False, problems=(), exists=False, error=''):
        self.enabled = bool(enabled)
        self.roots = tuple(roots)
        self.since = float(since)
        self.poll_seconds = float(poll_seconds)
        self.batch_bytes = int(batch_bytes)
        self.batch_lines = int(batch_lines)
        self.max_files_per_scan = int(max_files_per_scan)
        self.max_lines_per_scan = int(max_lines_per_scan)
        self.max_seconds_per_scan = float(max_seconds_per_scan)
        self.max_line_bytes = int(max_line_bytes)
        self.restart_changed_files = bool(restart_changed_files)
        self.problems = tuple(problems)
        self.exists = bool(exists)
        self.error = str(error)

    @property
    def ready(self):
        return bool(self.enabled and self.roots and not self.error)

    def view(self):
        # The read-only view never exposes the configured absolute roots: only
        # how many are set, so a wrong config is still visible.
        return {'enabled': self.enabled, 'ready': self.ready, 'root_count': len(self.roots),
                'since_epoch': self.since, 'poll_seconds': self.poll_seconds,
                'batch_bytes': self.batch_bytes, 'batch_lines': self.batch_lines,
                'max_files_per_scan': self.max_files_per_scan,
                'max_lines_per_scan': self.max_lines_per_scan,
                'max_seconds_per_scan': self.max_seconds_per_scan,
                'max_line_bytes': self.max_line_bytes,
                'restart_changed_files': self.restart_changed_files,
                'problems': list(self.problems)}


def _number(body, key, default, minimum):
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        return default, '%s_invalid' % key
    return value, ''


def load_config(data_dir):
    """Read the operator-written config; absent or invalid means no collection."""
    path = data_dir / CONFIG_NAME
    if not path.exists():
        return CaptureConfig(problems=('config_missing',))
    try:
        body = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return CaptureConfig(problems=('config_unreadable',), exists=True, error='config_error')
    if not isinstance(body, dict):
        return CaptureConfig(problems=('config_not_object',), exists=True, error='config_error')
    problems, roots = [], []
    enabled = body.get('enabled') is True
    raw_roots = body.get('sessions_roots')
    if not isinstance(raw_roots, list):
        problems.append('sessions_roots_invalid')
    else:
        for entry in raw_roots:
            if not isinstance(entry, str) or not entry or not os.path.isabs(entry):
                problems.append('sessions_root_not_absolute')
                continue
            roots.append(os.path.normpath(entry))
    if not roots and 'sessions_roots_invalid' not in problems:
        problems.append('sessions_roots_empty')
    # ``since`` is the activation line and stays mandatory: without it the gate
    # would import the whole historical backlog on first contact.
    raw_since = body.get('since')
    since = None if raw_since in (None, '') else parse_time(raw_since)
    if since is None:
        problems.append('since_missing' if raw_since in (None, '') else 'since_invalid')
    values = {}
    for key, default, minimum in (('poll_seconds', DEFAULT_POLL_SECONDS, 0.5),
                                  ('batch_bytes', DEFAULT_BATCH_BYTES, 4096),
                                  ('batch_lines', DEFAULT_BATCH_LINES, 1),
                                  ('max_files_per_scan', DEFAULT_MAX_FILES_PER_SCAN, 1),
                                  ('max_lines_per_scan', DEFAULT_MAX_LINES_PER_SCAN, 1),
                                  ('max_seconds_per_scan', DEFAULT_MAX_SECONDS_PER_SCAN, 0.2),
                                  ('max_line_bytes', DEFAULT_MAX_LINE_BYTES, 4096)):
        values[key], problem = _number(body, key, default, minimum)
        if problem:
            problems.append(problem)
    config = CaptureConfig(enabled=enabled, roots=roots, since=since or 0.0,
                           restart_changed_files=body.get('restart_changed_files') is True,
                           problems=problems, exists=True, **values)
    if problems:
        config.error = 'config_error'
    return config


# ---------------------------------------------------------------- file walking

def _walk_meta(root):
    """Yield ``(path, mtime)`` for every ``.jsonl`` file under ``root``.

    Enumeration is complete on purpose: a fixed first-N cap would hide later date
    directories forever. Only metadata is touched here; bodies are opened later
    and always inside the per-round budget.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and entry.name.endswith('.jsonl'):
                            yield entry.path, entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
        except OSError:
            continue


def session_id_for(path):
    """The rollout filename's session id; empty when the name is not canonical.

    Codex names the file ``rollout-<timestamp>-<session id>.jsonl``, so the id is
    the trailing UUID.
    """
    name = os.path.basename(path)
    if not name.startswith(ROLLOUT_PREFIX) or not name.endswith(ROLLOUT_SUFFIX):
        return ''
    candidate = name[len(ROLLOUT_PREFIX):-len(ROLLOUT_SUFFIX)]
    tail = candidate[-36:]
    return tail if SESSION_ID_RE.fullmatch(tail) else ''


def _peek_session_id(path, limit=262144):
    """First complete line's session id, for files whose name is not canonical."""
    try:
        with open(path, 'rb') as handle:
            raw = handle.readline(limit)
    except OSError:
        return ''
    if not raw.endswith(b'\n'):
        return ''
    try:
        row = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return ''
    payload = row.get('payload') if isinstance(row, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get('id'), str) \
            and SESSION_ID_RE.fullmatch(payload['id']):
        return payload['id']
    return ''


def discover_files(roots, *, since=0.0, known=()):
    """Newest-first ``(path, mtime)`` for every rollout file worth opening.

    An untouched file whose mtime is older than ``since`` cannot hold anything
    from after the activation line, so it stays unread; a file already tracked by
    this capture is always returned so its next append is noticed. Returns the
    list and the total number of files enumerated.
    """
    newest = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path, mtime in _walk_meta(root):
            if path not in newest or mtime > newest[path]:
                newest[path] = mtime
    files = [(path, mtime) for path, mtime in newest.items()
             if path in known or mtime >= since]
    files.sort(key=lambda item: (-item[1], item[0]))
    return files, len(newest)


# ------------------------------------------------------------------- reading

def _anchor_for(handle, end):
    """Fingerprint of the last ``ANCHOR_BYTES`` already-read bytes before ``end``.

    Anchoring to the end of the read range keeps appends invisible: they only add
    bytes after it. A same-length in-place rewrite of read data changes it.
    """
    start = max(0, int(end) - ANCHOR_BYTES)
    handle.seek(start)
    raw = handle.read(int(end) - start)
    return start, hashlib.sha256(raw).hexdigest(), len(raw)


def _store_anchor(store, row, handle, end):
    offset, digest, length = _anchor_for(handle, end)
    with store.transaction():
        store._connection().execute(
            'UPDATE local_capture_files SET anchor_offset=?,anchor_sha256=?,anchor_bytes=? WHERE id=?',
            (offset, digest, length, row['id']))


def content_moved(row, size):
    """Whether the bytes this capture already read are no longer on disk.

    Truncation is proved by size; an in-place rewrite of the read range by its
    trailing window. Both checks are bounded reads shared by the scan and the
    read-only status view, so the two can never disagree.
    """
    if size < row['scanned_offset_bytes']:
        return True
    if not row['anchor_bytes']:
        return False
    try:
        with open(row['path'], 'rb') as handle:
            handle.seek(row['anchor_offset'])
            raw = handle.read(row['anchor_bytes'])
    except OSError:
        return True
    if len(raw) != row['anchor_bytes']:
        return True
    return hashlib.sha256(raw).hexdigest() != row['anchor_sha256']


def change_reason(row, size):
    """Which conservative reason applies to a file whose content moved."""
    return (BLOCK_FILE_TRUNCATED if size < row['scanned_offset_bytes']
            else BLOCK_CONTENT_CHANGED)


def live_state(row):
    """The row's state as the file looks right now, before the next scan.

    mtime is the cheap change signal: an untouched file needs no anchor read at
    all, which keeps the read-only view and the scan selection affordable for a
    library of hundreds of sessions.
    """
    if row['file_state'] == STATE_BLOCKED:
        return row['file_state']
    try:
        stat = os.stat(row['path'])
    except OSError:
        return row['file_state']
    if stat.st_size == row['file_size'] and stat.st_mtime == row['file_mtime']:
        return row['file_state']
    if not content_moved(row, stat.st_size):
        return row['file_state']
    return STATE_BLOCKED


def _load_row(store, stat):
    return store._connection().execute(
        'SELECT * FROM local_capture_files WHERE device=? AND inode=?',
        (stat.st_dev, stat.st_ino)).fetchone()


def _create_row(store, path, session_id, stat):
    now = _now()
    with store.transaction():
        store._connection().execute(
            'INSERT INTO local_capture_files (session_id,path,device,inode,file_size,file_mtime,'
            'first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(device,inode) DO NOTHING',
            (session_id, path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime, now, now))
    return _load_row(store, stat)


def _advance(store, row, position, line, pending, stat, state, floor=None, filtered=0):
    with store.transaction():
        store._connection().execute(
            'UPDATE local_capture_files SET scanned_offset_bytes=?,scanned_line=?,pending_bytes=?,file_size=?,'
            'file_mtime=?,since_floor=?,file_state=?,filtered_events=filtered_events+?,updated_at=? WHERE id=?',
            (position, line, pending, stat.st_size, stat.st_mtime,
             max(float(row['since_floor']), float(floor or 0.0)), state, filtered, _now(), row['id']))


def _freeze(store, row, reason, *, cap=0, stat=None):
    """Stop on one file with an explicit reason instead of tailing it blindly."""
    size = stat.st_size if stat is not None else row['file_size']
    mtime = stat.st_mtime if stat is not None else row['file_mtime']
    with store.transaction():
        store._connection().execute(
            'UPDATE local_capture_files SET file_state=?,blocked_cap_bytes=?,file_size=?,file_mtime=?,'
            'last_error=?,last_error_at=?,updated_at=? WHERE id=?',
            (STATE_BLOCKED, int(cap), size, mtime, reason, _now(), _now(), row['id']))


def _clear_block(store, row, state):
    with store.transaction():
        store._connection().execute(
            "UPDATE local_capture_files SET file_state=?,last_error='',blocked_cap_bytes=0,updated_at=? "
            'WHERE id=?', (state, _now(), row['id']))
    return _load_row(store, os.stat(row['path']))


def release_block(store, row, config):
    """Retry a blocked file only when the operator widened the gate."""
    if row['file_state'] != STATE_BLOCKED:
        return row
    reason = row['last_error']
    if reason == BLOCK_LINE_TOO_LONG and config.max_line_bytes > row['blocked_cap_bytes']:
        return _clear_block(store, row, STATE_ADVANCING)
    if reason in CONTENT_BLOCK_REASONS and config.restart_changed_files:
        return _clear_block(store, row, STATE_ADVANCING)
    return None


def _restart_after_change(store, row, state):
    """Restart a changed file from zero instead of reusing a stale offset.

    Reusing the archived offset for content that is no longer there tears a line
    and can skip data forever. Restarting at zero is safe because the time gate
    still applies and a range that is byte-identical to an archived batch is
    skipped by its stable batch identity. Only an explicit operator opt-in takes
    this path; the default is to stop and ask for a review.
    """
    with store.transaction():
        store._connection().execute(
            "UPDATE local_capture_files SET scanned_offset_bytes=0,scanned_line=0,pending_bytes=0,"
            "since_floor=0,anchor_offset=0,anchor_bytes=0,anchor_sha256='',file_state=?,"
            "rewrite_count=rewrite_count+1,last_error='',blocked_cap_bytes=0,updated_at=? WHERE id=?",
            (state, _now(), row['id']))
    return _load_row(store, os.stat(row['path']))


def _record_failure(store, row, error):
    with store.transaction():
        store._connection().execute(
            'UPDATE local_capture_files SET failed_batches=failed_batches+1,last_error=?,last_error_at=?,'
            'updated_at=? WHERE id=?', (error[:200], _now(), _now(), row['id']))


def _read_records(handle, offset, line_number, *, budget_bytes, budget_lines, max_line_bytes):
    """One bounded round of complete LF records starting at ``offset``.

    A line longer than ``max_line_bytes`` is reported as ``too_long`` instead of
    being mistaken for a half-written line that never completes. A line that fits
    the cap is always consumed whole, even when it exceeds the byte budget.

    Each record keeps its original bytes exactly as they were written: compact
    JSON, escaped unicode, stray spaces and CRLF must survive, because the
    evidence path re-measures every located line against the real file.
    """
    handle.seek(offset)
    records, read_bytes, position, line, partial, too_long = [], 0, offset, line_number, 0, False
    while len(records) < budget_lines and read_bytes < budget_bytes:
        start = position
        raw = handle.readline(max_line_bytes)
        if not raw:
            break
        if not raw.endswith(b'\n'):
            if len(raw) >= max_line_bytes:
                too_long = True
            else:
                partial = len(raw)
            break
        read_bytes += len(raw)
        position += len(raw)
        line += 1
        try:
            row = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            row = None
        if row is not None and not isinstance(row, dict):
            row = None
        records.append({'line': line, 'start': start, 'end': position, 'row': row,
                        'raw': raw,  # the exact original line, LF included
                        'event_time': _event_time(row) if row else None,
                        'event_id': _event_id(row) if row else ''})
    return position, line, partial, records, too_long


# ------------------------------------------------------------------ archiving

def batch_external_id(session_id, start_line, end_line, digest):
    """Stable external identity: session plus the exact source range."""
    stable = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return '%s:%d-%d:%s' % (stable, start_line, end_line, digest[:16])


def batch_digest(records):
    """Content digest over the original bytes of the batch lines.

    Never a re-serialization: the batch identity must match the bytes the
    evidence resolver measures in the source file.
    """
    return hashlib.sha256(b''.join(record['raw'] for record in records)).hexdigest()


def batch_messages(records):
    """Visible user/assistant material for one batch; empty for pure noise."""
    return clean_messages(dialogue_messages(
        [record['row'] for record in records if record['row'] is not None]))


def build_batch(config, store, row, records):
    """Archive one immutable batch; None when there is nothing to archive.

    The transcript is the exact original bytes of these lines and the digest is
    taken over them, so a real Codex file's compact JSON, escaped unicode, stray
    spaces and CRLF stay byte-identical. The source range is derived from the
    same records the ``line_locations`` describe, so a batch that dropped
    pre-activation lines at the head of the round never claims those bytes. The
    scan watermark lives on the cursor row, not here.
    """
    cleaned = batch_messages(records)
    if not cleaned:
        return None
    locations = []
    for record in records:
        parsed = record['row']
        if parsed is None:
            locations.append({'line': record['line'], 'start_offset': record['start'],
                              'end_offset': record['end'], 'type': '', 'event_id': '',
                              'event_time': None, 'session_id': '', 'malformed': True})
            continue
        locations.append({'line': record['line'], 'start_offset': record['start'],
                          'end_offset': record['end'], 'type': str(parsed.get('type', '')),
                          'event_id': record['event_id'], 'event_time': record['event_time'],
                          'session_id': _record_session_id(parsed, row['session_id']),
                          'malformed': False})
    transcript = b''.join(record['raw'] for record in records).decode('utf-8')
    digest = batch_digest(records)
    # The identity and the digest must describe the same bytes.
    if hashlib.sha256(transcript.encode('utf-8')).hexdigest() != digest:
        raise RuntimeError('batch_bytes_changed')
    event_ids = [location['event_id'] for location in locations if location['event_id']]
    payload = json.dumps({
        'conversation': cleaned,
        'transcript': transcript,
        'project': '',
        'source_sha256': digest,
        'source': {'kind': 'local_codex_jsonl', 'adapter': ADAPTER, 'session_id': row['session_id'],
                   'file': row['path'], 'start_offset': records[0]['start'],
                   'end_offset': records[-1]['end'],
                   'start_line': records[0]['line'], 'end_line': records[-1]['line'],
                   'event_ids': event_ids},
        'line_locations': locations,
    }, ensure_ascii=False)
    archive = SessionArchiver(config, store).archive_session(
        '', ADAPTER, batch_external_id(row['session_id'], records[0]['line'], records[-1]['line'], digest),
        payload)
    if archive is None:
        # Encryption unavailable: a real failure, never a silent consumption.
        raise RuntimeError('archive_unavailable')
    return archive, len(cleaned)


def read_payload(config, store, archive_id):
    """Decrypt one locally captured batch for verification; None when absent."""
    return SessionArchiver(config, store).read_payload(archive_id)


def batch_recorded(store, session_id, start_line, end_line, digest):
    """Whether this exact range and content already have an archive row."""
    return store.get_session_archive_by_external(
        ADAPTER, batch_external_id(session_id, start_line, end_line, digest)) is not None


# ------------------------------------------------------------------- one file

class _Round:
    def __init__(self):
        self.bytes_read = 0
        self.archives = 0
        self.archived_events = 0
        self.filtered_events = 0
        self.files_seen = 0
        self.files_read = 0
        self.files_skipped_old = 0
        self.files_blocked = 0
        self.lines_read = 0
        self.errors = []


def _visible(records, since, floor):
    """Content accepted by the formal activation line.

    A record without its own timestamp inherits the newest timestamp already seen
    in this file. With no valid lower bound observed yet, it is filtered instead
    of being imported as unknown time: the activation line has to hold.
    """
    kept, filtered, position = [], 0, floor
    for record in records:
        if record['row'] is None:
            filtered += 1
            continue
        moment = record['event_time']
        if moment is not None and moment > position:
            position = moment
        effective = moment if moment is not None else position
        if not effective or effective < since:
            filtered += 1
            continue
        kept.append(record)
    return kept, filtered, position


def _scan_file(capture, config, row, round_, deadline):
    """Consume one bounded range of one file; returns True when it archived."""
    store = capture.store
    try:
        stat = os.stat(row['path'])
    except OSError:
        return False
    state = row['file_state']
    changed = (row['device'] != stat.st_dev or row['inode'] != stat.st_ino
               or content_moved(row, stat.st_size))
    if changed:
        reason = change_reason(row, stat.st_size)
        if config.restart_changed_files:
            row = _restart_after_change(store, row, STATE_REWRITTEN)
            state = STATE_REWRITTEN
        else:
            _freeze(store, row, reason, stat=stat)
            round_.files_blocked += 1
            round_.errors.append(reason)
            return False
    with open(row['path'], 'rb') as handle:
        offset, line = row['scanned_offset_bytes'], row['scanned_line']
        if offset >= stat.st_size and not row['pending_bytes']:
            return False
        if time.monotonic() >= deadline:
            return False
        position, line, partial, records, too_long = _read_records(
            handle, offset, line, budget_bytes=config.batch_bytes,
            budget_lines=config.batch_lines, max_line_bytes=config.max_line_bytes)
        round_.files_read += 1
        round_.bytes_read += position - offset
        round_.lines_read += len(records)
        if too_long:
            # Explicit block: a permanently over-long line must never be reported
            # as idle, and the cursor stays before it so widening the cap resumes.
            _freeze(store, row, BLOCK_LINE_TOO_LONG, cap=config.max_line_bytes, stat=stat)
            round_.files_blocked += 1
            round_.errors.append(BLOCK_LINE_TOO_LONG)
            return False
        if not records:
            _advance(store, row, position, line, partial or row['pending_bytes'], stat, state)
            if not partial:
                _store_anchor(store, row, handle, position)
            return False
        kept, filtered, floor = _visible(records, config.since, row['since_floor'])
        round_.filtered_events += filtered
        final_state = STATE_REWRITTEN if state == STATE_REWRITTEN else STATE_ADVANCING

        def consume(filtered_count=0, *, archived=False):
            _advance(store, row, position, line, partial, stat, final_state, floor, filtered_count)
            _store_anchor(store, row, handle, position)
            if archived:
                with store.transaction():
                    store._connection().execute(
                        'UPDATE local_capture_files SET archived_offset_bytes=?,archived_line=?,'
                        'last_success_at=?,last_error=?,updated_at=? WHERE id=?',
                        (position, line, _now(), '', _now(), row['id']))

        if not kept:
            # Pre-activation history or malformed noise: consume, never archive.
            consume(filtered)
            return False
        digest = batch_digest(kept)
        if batch_recorded(store, row['session_id'], kept[0]['line'], kept[-1]['line'], digest):
            # This exact range is already archived; only bookkeeping lagged.
            consume(filtered, archived=True)
            return False
        try:
            archived = build_batch(capture.config, store, row, kept)
        except Exception as error:  # bounded failure surface; the cursor stays put
            _record_failure(store, row, 'archive_failed:' + type(error).__name__)
            round_.errors.append(type(error).__name__)
            return False
        if archived is None:
            # A pure tool/analysis batch: consume the range, archive nothing.
            consume(filtered)
            return False
        _, events = archived
        consume(filtered, archived=True)
        with store.transaction():
            store._connection().execute(
                'UPDATE local_capture_files SET archived_batches=archived_batches+1,'
                'archived_events=archived_events+?,updated_at=? WHERE id=?',
                (events, _now(), row['id']))
        round_.archives += 1
        round_.archived_events += events
        return True


# ------------------------------------------------------------------ public API

class LocalCodexCapture:
    """One bounded scan over the configured roots using the worker's own store."""

    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.settings = load_config(config.data_dir)

    @property
    def ready(self):
        return self.settings.ready

    def poll_seconds(self):
        return self.settings.poll_seconds

    def scan(self):
        ensure_schema(self.store)
        settings = self.settings
        if not settings.enabled:
            return {'skipped': 'disabled' if settings.exists else 'not_configured',
                    'bytes_read': 0, 'archives': 0}
        if not settings.ready:
            return {'skipped': 'not_configured', 'bytes_read': 0, 'archives': 0,
                    'error': settings.error or 'config_error'}
        round_ = _Round()
        deadline = time.monotonic() + settings.max_seconds_per_scan
        known = {row['path'] for row in
                 self.store._connection().execute('SELECT path FROM local_capture_files')}
        discovered, enumerated = discover_files(settings.roots, since=settings.since, known=known)
        round_.files_seen = enumerated
        pending = []
        for path, mtime in discovered:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            row = _load_row(self.store, stat)
            if row is None:
                session_id = session_id_for(path) or _peek_session_id(path)
                if not session_id:
                    continue
                row = _create_row(self.store, path, session_id, stat)
            elif row['file_state'] == STATE_BLOCKED:
                row = release_block(self.store, row, settings)
                if row is None:
                    round_.files_blocked += 1
                    continue
            size = stat.st_size
            moved = live_state(row) == STATE_BLOCKED
            if moved and not settings.restart_changed_files:
                # A change since the last scan is recorded here and left for a
                # human; only an explicit opt-in restarts such a file.
                if row['file_state'] != STATE_BLOCKED:
                    _freeze(self.store, row, change_reason(row, size), stat=stat)
                round_.files_blocked += 1
                continue
            caught_up = row['scanned_offset_bytes'] >= size and not row['pending_bytes']
            if caught_up and row['file_state'] != STATE_REWRITTEN and not moved \
                    and mtime < settings.since:
                # Nothing was written since activation: skip without opening it.
                round_.files_skipped_old += 1
                continue
            pending.append(row)
        for row in pending:
            if round_.files_read >= settings.max_files_per_scan \
                    or round_.lines_read >= settings.max_lines_per_scan \
                    or time.monotonic() >= deadline:
                break
            try:
                _scan_file(self, settings, row, round_, deadline)
            except Exception as error:
                # One unreadable file never stops the round: its cursor stays put
                # and the next poll retries it.
                round_.errors.append(type(error).__name__)
        self._record_scan(round_)
        return {'skipped': '', 'bytes_read': round_.bytes_read, 'archives': round_.archives,
                'archived_events': round_.archived_events, 'filtered_events': round_.filtered_events,
                'files_seen': round_.files_seen, 'files_read': round_.files_read,
                'files_skipped_old': round_.files_skipped_old,
                'files_blocked': round_.files_blocked, 'lines_read': round_.lines_read,
                'scanned_bytes': round_.bytes_read, 'errors': round_.errors}

    def _record_scan(self, round_):
        progressed = round_.bytes_read > 0
        with self.store.transaction():
            self.store._connection().execute(
                'INSERT INTO local_capture_scans (id,last_scan_at,last_success_at,last_error,scans,'
                'scanned_bytes,archived_batches,archived_events,filtered_events) VALUES (1,?,?,?,1,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET last_scan_at=excluded.last_scan_at,'
                'last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE local_capture_scans.last_success_at END,'
                "last_error=excluded.last_error,scans=scans+1,scanned_bytes=scanned_bytes+excluded.scanned_bytes,"
                'archived_batches=archived_batches+excluded.archived_batches,'
                'archived_events=archived_events+excluded.archived_events,'
                'filtered_events=filtered_events+excluded.filtered_events',
                (_now(), _now(), round_.errors[0][:200] if round_.errors else '', round_.bytes_read,
                 round_.archives, round_.archived_events, round_.filtered_events, 1 if progressed else 0))


def _auto_new(store):
    """The existing arrival switch; collection pauses whenever it is off."""
    conn = store._connection()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='organization_settings'"
                    ).fetchone() is None:
        return False
    row = conn.execute('SELECT auto_new FROM organization_settings WHERE id=1').fetchone()
    return bool(row and row['auto_new'])


def status(config, store):
    """Content-free capture state for the read-only Web view."""
    ensure_schema(store)
    settings = load_config(config.data_dir)
    connection = store._connection()
    scan_row = connection.execute('SELECT * FROM local_capture_scans WHERE id=1').fetchone()
    auto_new = _auto_new(store)
    sessions = []
    for row in connection.execute('SELECT * FROM local_capture_files ORDER BY updated_at DESC, id DESC'):
        try:
            size = os.stat(row['path']).st_size
        except OSError:
            size = row['file_size']
        pending = max(0, size - row['scanned_offset_bytes'])
        state = live_state(row)
        blocked = state == STATE_BLOCKED
        if not blocked:
            reason = ''
        elif row['last_error']:
            reason = row['last_error']
        else:
            reason = change_reason(row, size)
        sessions.append({
            'session_id': row['session_id'],
            'file': os.path.basename(row['path']),
            'file_state': state,
            'blocked': blocked,
            'needs_review': blocked,
            'blocked_reason': reason,
            'scanned_offset_bytes': row['scanned_offset_bytes'],
            'scanned_line': row['scanned_line'],
            'archived_offset_bytes': row['archived_offset_bytes'],
            'pending_bytes': pending,
            'archived_batches': row['archived_batches'],
            'archived_events': row['archived_events'],
            'filtered_events': row['filtered_events'],
            'failed_batches': row['failed_batches'],
            'rewrite_count': row['rewrite_count'],
            'last_error': row['last_error'],
            'last_success_at': row['last_success_at'],
            'last_error_at': row['last_error_at'],
            'updated_at': row['updated_at'],
        })
    return {
        'ok': True,
        'enabled': bool(settings.ready),
        'configured': settings.exists,
        'auto_new': auto_new,
        'active': bool(settings.ready and auto_new),
        'error': settings.error,
        'config': settings.view(),
        'last_scan_at': scan_row['last_scan_at'] if scan_row else '',
        'last_success_at': scan_row['last_success_at'] if scan_row else '',
        'scans': scan_row['scans'] if scan_row else 0,
        'scanned_bytes': scan_row['scanned_bytes'] if scan_row else 0,
        'archived_batches': scan_row['archived_batches'] if scan_row else 0,
        'archived_events': scan_row['archived_events'] if scan_row else 0,
        'filtered_events': scan_row['filtered_events'] if scan_row else 0,
        'failed_batches': sum(session['failed_batches'] for session in sessions),
        'needs_review': sum(1 for session in sessions if session['needs_review']),
        'files': len(sessions),
        'last_error': scan_row['last_error'] if scan_row else '',
        'pending_bytes': sum(session['pending_bytes'] for session in sessions),
        'sessions': sessions,
    }


# ------------------------------------------------------------- worker adapter

def capture_once(service, state, *, now=None):
    """One throttled, arrival-gated capture round for the organization worker.

    ``state`` is a dict the worker owns. Returns the number of newly archived
    batches (0 when arrival is off, capture is unconfigured, throttled or idle).
    """
    from evolvmem.organization_arrival import settings as arrival_settings
    if not arrival_settings(service)['auto_new']:
        state['capture'] = None
        state['last_poll'] = 0.0
        return 0
    stamp = _config_stamp(service.config)
    instance = state.get('capture')
    if instance is None or state.get('config_stamp') != stamp:
        instance = LocalCodexCapture(service.config, service.store)
        state['capture'] = instance
        state['config_stamp'] = stamp
    if not instance.ready:
        return 0
    moment = time.monotonic() if now is None else now
    if moment - state.get('last_poll', 0.0) < instance.poll_seconds():
        return 0
    state['last_poll'] = moment
    return int(instance.scan().get('archives', 0))


def _config_stamp(config):
    try:
        stat = os.stat(config.data_dir / CONFIG_NAME)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)
