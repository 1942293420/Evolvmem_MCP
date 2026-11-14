from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import sys

import pytest

from evolvmem.pg_test_guard import assert_safe_test_pg_target

STAGE5_PG_CONTAINER = "evolvmem-pg-test-stage5"
STAGE5_PG_PORT = 55434
STAGE5_PITR_CONTAINER = "evolvmem-pg-pitr-drill-stage5"
STAGE5_PITR_PORT = 25436
STAGE5_WORK_DIR = Path("/home/jiangli/evolvmem-stage5-20261115")
STAGE5_PW_FILE = Path("/tmp/evolvmem-pg-stage5.pw")


def _ensure_secret_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if existing:
        path.chmod(0o600)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (secrets.token_urlsafe(24) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    path.chmod(0o600)


def ensure_stage5_pg_running() -> None:
    STAGE5_WORK_DIR.mkdir(parents=True, exist_ok=True)
    STAGE5_WORK_DIR.chmod(0o700)
    _ensure_secret_file(STAGE5_PW_FILE)

    st = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", STAGE5_PG_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if st.returncode == 0 and st.stdout.strip() == "running":
        ready = subprocess.run(
            ["docker", "exec", STAGE5_PG_CONTAINER, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
            capture_output=True, check=False,
        )
        if ready.returncode == 0:
            return
        subprocess.run(["docker", "rm", "-f", STAGE5_PG_CONTAINER], capture_output=True, check=False)

    repo_root = Path(__file__).resolve().parents[2]
    manage_py = repo_root / "scripts" / "linux" / "manage_test_pg.py"
    wal_dir = STAGE5_WORK_DIR / "wal_archive"
    res = subprocess.run(
        [
            sys.executable, str(manage_py), "start",
            "--name", STAGE5_PG_CONTAINER,
            "--port", str(STAGE5_PG_PORT),
            "--wal-archive-dir", str(wal_dir),
            "--password-file", str(STAGE5_PW_FILE),
        ],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start Stage 5 test PG: {res.stderr or res.stdout}")


@pytest.fixture(scope="session", autouse=True)
def _guard_and_start_stage5_pg() -> None:
    assert_safe_test_pg_target(
        port=STAGE5_PG_PORT,
        container_name=STAGE5_PG_CONTAINER,
        volumes=[STAGE5_WORK_DIR.as_posix()],
    )
    assert_safe_test_pg_target(
        port=STAGE5_PITR_PORT,
        container_name=STAGE5_PITR_CONTAINER,
    )
    if os.environ.get("PGPORT", "").strip() == "55432":
        raise RuntimeError("PGPORT=55432 forbidden in pytest")
    ensure_stage5_pg_running()
