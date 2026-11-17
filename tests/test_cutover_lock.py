"""Behavioral contracts for the shared/exclusive cutover writer lock."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from evolvmem.cutover_lock import CutoverLock, CutoverLockTimeout


_HOLD_SCRIPT = """
import sys, time
from pathlib import Path
from evolvmem.config import Config
from evolvmem.cutover_lock import CutoverLock

data_dir, mode, ready_path, release_path = sys.argv[1:5]
lock = CutoverLock(Config(data_dir=Path(data_dir)))
manager = lock.shared() if mode == "shared" else lock.exclusive()
with manager:
    Path(ready_path).write_text("held", encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while not Path(release_path).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
"""

_RAISE_SCRIPT = """
import sys
from pathlib import Path
from evolvmem.config import Config
from evolvmem.cutover_lock import CutoverLock

data_dir, ready_path = sys.argv[1:3]
lock = CutoverLock(Config(data_dir=Path(data_dir)))
with lock.exclusive():
    Path(ready_path).write_text("held", encoding="utf-8")
    raise RuntimeError("boom")
"""


def _subprocess_env():
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn_holder(temp_dir: Path, mode: str):
    """Start a subprocess that holds the lock until the release file appears."""
    ready = temp_dir / f"{mode}.ready"
    release = temp_dir / f"{mode}.release"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD_SCRIPT,
            str(temp_dir),
            mode,
            str(ready),
            str(release),
        ],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 15.0
    while not ready.exists():
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            raise AssertionError(
                f"lock holder exited early: {stderr.decode(errors='replace')}"
            )
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            raise AssertionError("lock holder never acquired the lock")
        time.sleep(0.02)
    return proc, release


def _stop_holder(proc, release: Path) -> None:
    release.write_text("go", encoding="utf-8")
    proc.communicate(timeout=15)


def test_shared_lock_holders_do_not_block_each_other(test_config, temp_dir):
    proc, release = _spawn_holder(temp_dir, "shared")
    try:
        with CutoverLock(test_config).shared(timeout_seconds=2.0):
            pass  # a second shared holder joins while the subprocess holds it
    finally:
        _stop_holder(proc, release)
    assert proc.returncode == 0


def test_exclusive_lock_waits_bounded_while_shared_is_held(test_config, temp_dir):
    proc, release = _spawn_holder(temp_dir, "shared")
    lock = CutoverLock(test_config)
    try:
        started = time.monotonic()
        with pytest.raises(CutoverLockTimeout) as excinfo:
            with lock.exclusive(timeout_seconds=0.4):
                pass
        waited = time.monotonic() - started
        assert isinstance(excinfo.value, TimeoutError)
        assert 0.3 <= waited < 10.0  # bounded wait, never an infinite block
    finally:
        _stop_holder(proc, release)
    with lock.exclusive(timeout_seconds=5.0):
        pass  # once released, the exclusive acquisition succeeds
    assert proc.returncode == 0


def test_shared_lock_waits_bounded_while_exclusive_is_held(test_config, temp_dir):
    proc, release = _spawn_holder(temp_dir, "exclusive")
    lock = CutoverLock(test_config)
    try:
        with pytest.raises(CutoverLockTimeout):
            with lock.shared(timeout_seconds=0.4):
                pass
    finally:
        _stop_holder(proc, release)
    assert proc.returncode == 0


def test_lock_is_released_when_the_holder_process_raises(test_config, temp_dir):
    ready = temp_dir / "raise.ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _RAISE_SCRIPT, str(temp_dir), str(ready)],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 15.0
    while not ready.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    proc.communicate(timeout=15)
    assert proc.returncode != 0  # the exception escaped the holder

    with CutoverLock(test_config).exclusive(timeout_seconds=2.0):
        pass  # the crashed holder's lock was released in its finally path


def test_lock_file_is_never_truncated_or_deleted(test_config, temp_dir):
    lock_path = temp_dir / "context-core-cutover.lock"
    lock_path.write_text("sentinel", encoding="utf-8")
    lock = CutoverLock(test_config)

    with lock.shared():
        pass
    with lock.exclusive():
        pass

    assert lock_path.read_text(encoding="utf-8") == "sentinel"


def test_lock_path_escaping_the_data_directory_is_rejected(test_config, temp_dir):
    outside = temp_dir.parent / f"escaped-{temp_dir.name}.lock"
    (temp_dir / "context-core-cutover.lock").symlink_to(outside)
    with pytest.raises(ValueError):
        with CutoverLock(test_config).shared(timeout_seconds=0.1):
            pass
    assert not outside.exists()  # the escaped target is never created


def test_lock_rejects_a_negative_timeout_and_a_foreign_config(test_config):
    lock = CutoverLock(test_config)
    with pytest.raises(ValueError):
        with lock.shared(timeout_seconds=-1.0):
            pass
    with pytest.raises(TypeError):
        CutoverLock("not-a-config")
