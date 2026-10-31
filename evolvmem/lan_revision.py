"""Revision reads isolated from business stores and mutable request identity."""
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import threading
import time
from evolvmem.revision_observation import RevisionObservation
from evolvmem.request_trace import revision_observation

_REVISION_SQL = ('SELECT p.revision, s.revision FROM main.lan_memory_revision p '
                 'CROSS JOIN public.lan_memory_revision s WHERE p.id=1 AND s.id=1 LIMIT 2')
_SQL_SECONDS = 0.25
_LEASE_SECONDS = 0.05
_WARM_SECONDS = 1.0
_SLOTS_PER_USER = 2


class RevisionReader:
    """Pin the three trusted namespace files after runtime initialization.

    Replacement requires reinitializing the adapter, as resident stores would
    otherwise keep serving the old inode while polling reported another file.
    """

    def __init__(self, settings):
        self._paths = {
            'jiangli': Path(settings.owner_data_dir) / 'memory.db',
            'kane': Path(settings.data_dir) / 'users' / 'kane' / 'memory.db',
            'public': Path(settings.data_dir) / 'public' / 'memory.db',
        }
        self._identities = {key: self._identity(path) for key, path in self._paths.items()}
        if len({identity[1:] for identity in self._identities.values()}) != 3:
            raise RuntimeError('LAN database directories must be distinct')

    @staticmethod
    def _identity(path):
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('revision database unavailable')
        return (path.resolve(), info.st_dev, info.st_ino)

    def _check(self):
        for key, path in self._paths.items():
            if self._identity(path) != self._identities[key]:
                raise RuntimeError('revision database unavailable')

    def read(self, user):
        observation = RevisionObservation(clock=time.monotonic)
        try:
            with observation.phase('identity_precheck'):
                self._check()
            deadline = time.monotonic() + 0.25
            observation.arm(deadline)
            uri = lambda path: path.absolute().as_uri() + '?mode=ro'
            with observation.phase('open_personal'):
                conn = sqlite3.connect(uri(self._paths[user]), uri=True, timeout=0.1)
            # SQLite's context manager does not close connections. closing does,
            # including attach, malformed schema, timeout and validation failures.
            with closing(conn):
                conn.set_progress_handler(observation.progress, 100)
                with observation.phase('trusted_schema'):
                    conn.execute('PRAGMA trusted_schema=OFF')
                with observation.phase('attach_public'):
                    conn.execute('ATTACH DATABASE ? AS public', (uri(self._paths['public']),))
                with observation.phase('query_only'):
                    conn.execute('PRAGMA query_only=ON')
                with observation.phase('begin_snapshot'):
                    conn.execute('BEGIN')
                with observation.phase('read_revision'):
                    rows = conn.execute(
                        'SELECT p.revision, s.revision FROM main.lan_memory_revision p '
                        'CROSS JOIN public.lan_memory_revision s WHERE p.id=1 AND s.id=1 LIMIT 2').fetchall()
                with observation.phase('identity_postcheck'):
                    self._check()
                with observation.phase('validate_result'):
                    if len(rows) != 1 or any(type(v) is not int or v < 0 for v in rows[0]):
                        raise RuntimeError('revision database unavailable')
                    return '.'.join(str(v) for v in rows[0])
        finally:
            # Diagnostics must not mask an original result/error or close failure.
            try:
                revision_observation(observation)
            except Exception:
                pass


class RevisionPool(RevisionReader):
    """Four prewarmed readonly connections, exclusively leased per request.

    Construction is a startup operation. Reads never open/attach or refill a
    retired slot. Idle handles hold no transaction, cursor or request callback.
    The default RevisionReader remains a connection-free identity checker for
    other independent readers, with its original cold-read compatibility path.
    """

    def __init__(self, settings, *, closed=lambda: False):
        self._condition = threading.Condition()
        self._closed = False
        self._runtime_closed = closed
        self._idle = {'jiangli': [], 'kane': []}
        self._leased = set()
        super().__init__(settings)
        self._permissions = {key: self._permission(path) for key, path in self._paths.items()}
        try:
            for user in self._idle:
                for _ in range(_SLOTS_PER_USER):
                    self._idle[user].append(self._warm(user))
            self._check()
            self._guard()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _permission(path):
        info = path.stat()
        return info.st_mode, info.st_uid, info.st_gid

    def _guard(self):
        if self._closed or self._runtime_closed():
            raise RuntimeError('revision reader unavailable')

    def _check(self):
        try:
            self._guard()
            super()._check()
            if any(self._permission(path) != self._permissions[key] for key, path in self._paths.items()):
                raise RuntimeError('revision database unavailable')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _execute(conn, sql, parameters=()):
        # No cursor may outlive a lease (including PRAGMA/BEGIN cursors).
        with closing(conn.execute(sql, parameters)) as cursor:
            return cursor.fetchall() if cursor.description is not None else []

    @staticmethod
    def _validate(rows):
        if len(rows) != 1 or any(type(value) is not int or value < 0 for value in rows[0]):
            raise RuntimeError('revision database unavailable')
        return '.'.join(str(value) for value in rows[0])

    def _clean(self, conn):
        # Remove an expired/closed callback BEFORE attempting ROLLBACK.
        conn.set_progress_handler(None, 0)
        conn.rollback()
        if (conn.in_transaction or conn.total_changes or
                self._execute(conn, 'PRAGMA query_only') != [(1,)] or
                self._execute(conn, 'PRAGMA trusted_schema') != [(0,)]):
            raise RuntimeError('revision connection unavailable')

    def _warm(self, user):
        self._check()
        deadline = time.monotonic() + _WARM_SECONDS
        uri = lambda path: path.absolute().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri(self._paths[user]), uri=True, timeout=0.1,
                               isolation_level=None, check_same_thread=False, cached_statements=16)
        try:
            conn.set_progress_handler(lambda: time.monotonic() >= deadline or
                                      self._closed or self._runtime_closed(), 100)
            self._execute(conn, 'PRAGMA trusted_schema=OFF')
            self._execute(conn, 'ATTACH DATABASE ? AS public', (uri(self._paths['public']),))
            self._execute(conn, 'PRAGMA query_only=ON')
            self._execute(conn, 'PRAGMA main.cache_size=-256')
            self._execute(conn, 'PRAGMA public.cache_size=-256')
            self._execute(conn, 'PRAGMA mmap_size=0')
            self._execute(conn, 'PRAGMA public.mmap_size=0')
            self._execute(conn, 'BEGIN')
            self._validate(self._execute(conn, _REVISION_SQL))
            self._clean(conn)
            self._check()
            if time.monotonic() >= deadline:
                raise RuntimeError('revision prewarm unavailable')
            return conn
        except BaseException:
            conn.close()
            raise

    def _acquire(self, user, deadline):
        until = min(deadline, time.monotonic() + _LEASE_SECONDS)
        with self._condition:
            while True:
                self._guard()
                if self._idle[user]:
                    conn = self._idle[user].pop()
                    self._leased.add(conn)
                    return conn
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('revision reader busy')
                self._condition.wait(remaining)

    def _release(self, user, conn, reusable):
        with self._condition:
            self._leased.remove(conn)
            keep = reusable and not self._closed and not self._runtime_closed()
            if keep:
                self._idle[user].append(conn)
            self._condition.notify_all()
        if not keep:
            conn.close()

    def _current(self, observation):
        self._guard()
        if observation.progress():
            error = sqlite3.OperationalError('revision deadline exceeded')
            error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise error

    def read(self, user):
        observation = RevisionObservation(clock=time.monotonic)
        try:
            if user not in self._idle:
                raise ValueError('unknown revision namespace')
            with observation.phase('identity_precheck'):
                self._check()
            deadline = time.monotonic() + _SQL_SECONDS
            observation.arm(deadline)
            with observation.phase('pool_acquire'):
                conn = self._acquire(user, deadline)
            failed, reusable = False, False
            try:
                with observation.phase('identity_precheck'):
                    self._check()
                    self._current(observation)
                if conn.in_transaction:
                    raise RuntimeError('revision connection unavailable')
                conn.set_progress_handler(lambda: observation.progress() or
                                          self._closed or self._runtime_closed(), 100)
                with observation.phase('begin_snapshot'):
                    self._execute(conn, 'BEGIN')
                with observation.phase('read_revision'):
                    rows = self._execute(conn, _REVISION_SQL)
                with observation.phase('identity_postcheck'):
                    self._check()
                with observation.phase('validate_result'):
                    result = self._validate(rows)
                    self._current(observation)
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    with observation.phase('pool_cleanup'):
                        self._clean(conn)
                        reusable = True
                except BaseException:
                    if not failed:
                        raise
                finally:
                    self._release(user, conn, reusable)
            with observation.phase('validate_result'):
                self._current(observation)
            return result
        finally:
            try:
                revision_observation(observation)
            except Exception:
                pass

    def close(self):
        # Only the lease owner closes an active connection. Waiting requests
        # wake immediately; callbacks and postchecks observe this state too.
        with self._condition:
            self._closed = True
            idle = [conn for values in self._idle.values() for conn in values]
            for values in self._idle.values():
                values.clear()
            self._condition.notify_all()
        for conn in idle:
            try:
                conn.close()
            except Exception:
                # Attempt every owned idle handle even if one close fails.
                pass
