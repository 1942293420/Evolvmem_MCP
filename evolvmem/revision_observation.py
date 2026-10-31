"""Bounded, content-free observations for the owned revision-read connection."""
from contextlib import contextmanager
import math
import sqlite3
import time

PHASES = frozenset({'identity_precheck', 'open_personal', 'trusted_schema',
                    'attach_public', 'query_only', 'begin_snapshot',
                    'read_revision', 'identity_postcheck', 'validate_result', 'unknown'})
SQLITE_CLASSES = {
    sqlite3.SQLITE_BUSY: 'sqlite_busy',
    sqlite3.SQLITE_LOCKED: 'sqlite_locked',
    sqlite3.SQLITE_INTERRUPT: 'sqlite_interrupted',
    sqlite3.SQLITE_CANTOPEN: 'sqlite_cannot_open',
    sqlite3.SQLITE_READONLY: 'sqlite_readonly',
    sqlite3.SQLITE_IOERR: 'sqlite_io_error',
    sqlite3.SQLITE_CORRUPT: 'sqlite_corrupt',
    sqlite3.SQLITE_NOTADB: 'sqlite_not_database',
    sqlite3.SQLITE_AUTH: 'sqlite_authorization',
    sqlite3.SQLITE_PERM: 'sqlite_permission',
    sqlite3.SQLITE_SCHEMA: 'sqlite_schema_changed',
    sqlite3.SQLITE_ERROR: 'sqlite_sql_or_schema_error',
}


def classified(exc, deadline_requested):
    """Use numeric SQLite codes only; never read str(exc) or sqlite_errorname."""
    result = {'classification': 'unexpected_error', 'sqlite_code': None,
              'sqlite_primary_code': None}
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', None)
        if type(code) is int and 0 <= code <= 65535:
            primary = code & 255
            result.update(sqlite_code=code, sqlite_primary_code=primary,
                          classification=SQLITE_CLASSES.get(primary, 'sqlite_other'))
            if primary == sqlite3.SQLITE_INTERRUPT and deadline_requested:
                result['classification'] = 'revision_deadline'
        else:
            result['classification'] = 'sqlite_code_unavailable'
    elif isinstance(exc, FileNotFoundError):
        result['classification'] = 'filesystem_missing'
    elif isinstance(exc, PermissionError):
        result['classification'] = 'filesystem_permission'
    elif isinstance(exc, OSError):
        result['classification'] = 'filesystem_error'
    elif isinstance(exc, RuntimeError):
        result['classification'] = 'revision_invariant'
    return result


class RevisionObservation:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.current = 'unknown'
        self.deadline = None
        self.deadline_requested = False
        self.durations = {}
        self.error = None
        self.failed_phase = None

    def arm(self, deadline):
        # Caller supplies its original deadline after the identity precheck.
        self.deadline = deadline

    def progress(self):
        requested = self.deadline is not None and self.clock() >= self.deadline
        self.deadline_requested |= requested
        return requested

    @contextmanager
    def phase(self, name):
        name = name if isinstance(name, str) and name in PHASES else 'unknown'
        self.current = name
        started = self.clock()
        try:
            yield
        except Exception as exc:
            # Preserve the original exception object and type, even for caught
            # SQLite failures. Never attach exception text to public errors.
            if self.error is None:
                self.failed_phase = name
                self.error = classified(exc, self.deadline_requested)
            raise
        finally:
            elapsed = max(0, (self.clock() - started) * 1000)
            if math.isfinite(elapsed):
                self.durations[name] = round(self.durations.get(name, 0) + elapsed, 3)

    def snapshot(self):
        elapsed = max(0, (self.clock() - self.started) * 1000)
        return {'phase': self.failed_phase or self.current,
                'elapsed_ms': round(elapsed, 3) if math.isfinite(elapsed) else None,
                'phases_ms': dict(self.durations), 'deadline_requested': self.deadline_requested,
                'failure': None if self.error is None else dict(self.error)}
