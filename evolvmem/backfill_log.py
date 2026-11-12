"""Bounded 0600 log rotation and shell-redirection fd rebinding for continuity_backfill."""
from __future__ import annotations

import os
from pathlib import Path
import sys

ACTIONABLE_ACTIONS = frozenset({"create", "update"})
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3


def rotate_log_if_needed(
    log_path: Path,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backups: int = DEFAULT_LOG_BACKUPS,
) -> bool:
    """Rotate ``log_path`` when ``>= max_bytes`` and rebind redirected stdout/stderr fds."""
    if max_bytes <= 0 or backups < 1 or not log_path.exists():
        return False
    try:
        st = log_path.stat()
    except OSError:
        return False
    if st.st_size < max_bytes:
        return False

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass

    redirected_fds: list[int] = []
    for fd in (1, 2):
        try:
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino):
                redirected_fds.append(fd)
        except OSError:
            pass

    oldest = Path(f"{log_path}.{backups}")
    oldest.unlink(missing_ok=True)
    for idx in range(backups - 1, 0, -1):
        src = Path(f"{log_path}.{idx}")
        if src.exists():
            os.replace(src, Path(f"{log_path}.{idx + 1}"))
    os.replace(log_path, Path(f"{log_path}.1"))

    new_fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(new_fd, 0o600)
        for target_fd in redirected_fds:
            if target_fd != new_fd:
                os.dup2(new_fd, target_fd)
    finally:
        if new_fd not in redirected_fds:
            os.close(new_fd)
    return True


def emit_cli_line(
    line: str,
    *,
    log_file: Path | None,
    log_target: Path,
    max_bytes: int,
    backups: int,
    is_error: bool = False,
) -> None:
    """Emit one JSON line with 0600 permissions and per-line rotation checks."""
    rotate_log_if_needed(log_target, max_bytes, backups)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        if is_error:
            print(line, file=sys.stderr, flush=True)
        return
    print(line, file=sys.stderr if is_error else sys.stdout, flush=True)
