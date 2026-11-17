"""One nonblocking raw scan per physical namespace across host processes."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path


@contextmanager
def capture_lease(data_dir):
    # Linux server-side collector; import lazily so policy-only tools remain
    # usable on clients. Do not unlink a released lock and split its identity.
    import fcntl
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / 'local-codex-capture.lock', os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
