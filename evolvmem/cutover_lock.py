"""Shared/exclusive file lock serializing Context Core cutover writes.

Every ContextService mutation holds the lock shared; the formal cutover
holds it exclusively. The lock file lives inside the configured data
directory, is opened without truncation, and is never deleted — a released
lock must remain available to the next process.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import time
from typing import ContextManager

from evolvmem.config import Config


_LOCK_FILE_NAME = "context-core-cutover.lock"
_RETRY_INTERVAL_SECONDS = 0.05


class CutoverLockTimeout(TimeoutError):
    """The cutover lock stayed contended past the bounded wait."""


class CutoverLock:
    """fcntl.flock writer lock at ``data_dir/context-core-cutover.lock``."""

    def __init__(self, config: Config) -> None:
        if not isinstance(config, Config):
            raise TypeError("config must be a Config instance")
        self.config = config

    def shared(self, *, timeout_seconds: float = 30.0) -> "ContextManager[None]":
        """Hold the lock alongside other shared holders; block an exclusive one."""
        return self._acquire(fcntl.LOCK_SH, timeout_seconds=timeout_seconds)

    def exclusive(self, *, timeout_seconds: float = 30.0) -> "ContextManager[None]":
        """Hold the lock alone, blocking shared and exclusive holders alike."""
        return self._acquire(fcntl.LOCK_EX, timeout_seconds=timeout_seconds)

    @contextmanager
    def _acquire(self, mode: int, *, timeout_seconds: float) -> Iterator[None]:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        path = self._lock_path()
        # O_CREAT without O_TRUNC: a pre-existing lock file keeps its content.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._flock(fd, mode, timeout_seconds)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _lock_path(self) -> Path:
        data_dir = self.config.data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        resolved = (data_dir / _LOCK_FILE_NAME).resolve()
        if resolved == data_dir or not resolved.is_relative_to(data_dir):
            raise ValueError("cutover lock path escapes the data directory")
        return resolved

    @staticmethod
    def _flock(fd: int, mode: int, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise CutoverLockTimeout(
                        "cutover lock stayed contended past the bounded wait"
                    ) from None
                time.sleep(_RETRY_INTERVAL_SECONDS)
