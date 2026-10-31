"""Bounded revision reads with no resident connection or mutable request identity."""
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import time


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
        self._check()
        deadline = time.monotonic() + 0.25
        uri = lambda path: path.absolute().as_uri() + '?mode=ro'
        # SQLite's context manager does not close connections. closing does,
        # including attach, malformed schema, timeout and validation failures.
        with closing(sqlite3.connect(uri(self._paths[user]), uri=True, timeout=0.1)) as conn:
            conn.set_progress_handler(lambda: time.monotonic() >= deadline, 100)
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('ATTACH DATABASE ? AS public', (uri(self._paths['public']),))
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            rows = conn.execute(
                'SELECT p.revision, s.revision FROM main.lan_memory_revision p '
                'CROSS JOIN public.lan_memory_revision s WHERE p.id=1 AND s.id=1 LIMIT 2').fetchall()
            self._check()
            if len(rows) != 1 or any(type(v) is not int or v < 0 for v in rows[0]):
                raise RuntimeError('revision database unavailable')
            return '.'.join(str(v) for v in rows[0])
