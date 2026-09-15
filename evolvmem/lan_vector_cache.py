"""Opt-in coordination of this host's resident vector caches.

SQLite stays authoritative. Only disk load/merge/replace holds flock, never
model encoding or an HTTP call. Old non-participating writers must exit first.
"""
from contextlib import contextmanager
import fcntl
import os
import tempfile
from usearch.index import Index


class SharedVectorCache:
    def __init__(self, owner):
        self.owner = owner
        self.pending = {}
        self.stamp = None
        # Pair the loaded image and its stamp under the same local save lock.
        with self._locked():
            self._reload()

    def _stamp(self):
        try:
            info = self.owner.path.stat()
            return (info.st_ino, info.st_size, info.st_mtime_ns)
        except FileNotFoundError:
            return None

    @contextmanager
    def _locked(self):
        path = self.owner.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + '.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _reload(self):
        if self.owner.path.exists():
            index = Index.restore(str(self.owner.path), view=False)
            for ident, vector in self.pending.items():
                if ident in index:
                    index.remove(ident)
                if vector is not None:
                    index.add(ident, vector)
            self.owner._index = index
        self.stamp = self._stamp()

    def refresh(self):
        if self.stamp != self._stamp():
            with self._locked():
                self._reload()

    def save(self):
        with self._locked():
            self._reload()
            if not self.pending and self.owner.path.exists():
                return
            fd, temporary = tempfile.mkstemp(prefix='.lan-vector-', dir=self.owner.path.parent)
            os.close(fd)
            try:
                self.owner._index.save(temporary)
                os.replace(temporary, self.owner.path)
                self.pending.clear()
                self.stamp = self._stamp()
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
