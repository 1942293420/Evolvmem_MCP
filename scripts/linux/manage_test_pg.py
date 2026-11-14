"""Manage an isolated disposable PostgreSQL+pgvector container for tests only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from evolvmem.pg_test_guard import PINNED_PGVECTOR_IMAGE, assert_safe_test_pg_target


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage isolated EvolvMem test PostgreSQL container")
    sub = p.add_subparsers(dest="action", required=True)

    st = sub.add_parser("start")
    st.add_argument("--name", default="evolvmem-pg-test-stage1")
    st.add_argument("--port", type=int, default=55433)
    st.add_argument("--volume", default="")
    st.add_argument("--wal-archive-dir", type=Path, default=None)
    st.add_argument("--data-dir", type=Path, default=None)
    st.add_argument("--password-file", type=Path, default=Path("/tmp/evolvmem-pg-test.pw"))

    sp = sub.add_parser("stop")
    sp.add_argument("--name", default="evolvmem-pg-test-stage1")
    sp.add_argument("--clean-data-dir", type=Path, default=None)

    rc = sub.add_parser("status")
    rc.add_argument("--name", default="evolvmem-pg-test-stage1")
    return p


def _ensure_password(pw_file: Path) -> str:
    pw_file.parent.mkdir(parents=True, exist_ok=True)
    if pw_file.exists():
        pw = pw_file.read_text(encoding="utf-8").strip()
        if pw:
            pw_file.chmod(0o600)
            return pw
    pw = secrets.token_urlsafe(24)
    pw_file.write_text(pw + "\n", encoding="utf-8")
    pw_file.chmod(0o600)
    return pw


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(raw_argv)

    vols: list[str] = []
    if getattr(args, "volume", ""):
        vols.append(args.volume)
    if getattr(args, "wal_archive_dir", None):
        vols.append(str(args.wal_archive_dir))
    if getattr(args, "data_dir", None):
        vols.append(str(args.data_dir))

    assert_safe_test_pg_target(
        port=getattr(args, "port", None),
        container_name=args.name,
        volumes=vols,
        argv=raw_argv,
    )

    if args.action == "stop":
        subprocess.run(["docker", "rm", "-f", args.name], capture_output=True, check=False)
        if args.clean_data_dir and args.clean_data_dir.exists():
            shutil.rmtree(args.clean_data_dir, ignore_errors=True)
        print(json.dumps({"status": "stopped", "container": args.name}))
        return 0

    if args.action == "status":
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", args.name],
            capture_output=True, text=True, check=False,
        )
        print(json.dumps({"container": args.name, "status": r.stdout.strip() or "absent"}))
        return 0 if r.returncode == 0 else 1

    pw_file: Path = args.password_file
    pw = _ensure_password(pw_file)
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", args.name],
        capture_output=True, text=True, check=False,
    )
    if running.returncode == 0 and running.stdout.strip() == "running":
        print(json.dumps({"status": "already_running", "container": args.name, "port": args.port}))
        return 0
    if running.returncode == 0:
        subprocess.run(["docker", "rm", "-f", args.name], capture_output=True, check=False)

    cmd = [
        "docker", "run", "-d", "--name", args.name,
        "-p", f"127.0.0.1:{args.port}:5432",
        "-e", f"POSTGRES_PASSWORD={pw}",
        "-e", "POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C.UTF-8 --data-checksums",
        "--label", "evolvmem.role=isolated-test-pg",
    ]
    pg_flags = ["-c", "shared_preload_libraries=pg_stat_statements"]

    wal_dir: Path | None = args.wal_archive_dir
    if wal_dir is not None:
        wal_dir = wal_dir.expanduser().resolve()
        wal_dir.mkdir(parents=True, exist_ok=True)
        wal_dir.chmod(0o700)
        pgdata_dir = (args.data_dir or (wal_dir.parent / f".{args.name}_pgdata")).expanduser().resolve()
        if pgdata_dir.exists():
            shutil.rmtree(pgdata_dir, ignore_errors=True)
        pgdata_dir.mkdir(parents=True, exist_ok=True)
        pgdata_dir.chmod(0o700)
        cmd.extend([
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", "/etc/passwd:/etc/passwd:ro",
            "-v", "/etc/group:/etc/group:ro",
            "-v", f"{pgdata_dir}:/pgdata",
            "-e", "PGDATA=/pgdata/data",
            "-v", f"{wal_dir}:/wal_archive",
        ])
        archive_cmd = (
            "test ! -f /wal_archive/%f && "
            "cp %p /wal_archive/.%f.tmp && "
            "chmod 600 /wal_archive/.%f.tmp && "
            "mv -n /wal_archive/.%f.tmp /wal_archive/%f"
        )
        pg_flags.extend([
            "-c", "wal_level=replica",
            "-c", "archive_mode=on",
            "-c", "archive_timeout=60",
            "-c", f"archive_command={archive_cmd}",
        ])
    elif args.volume:
        cmd.extend(["-v", f"{args.volume}:/var/lib/postgresql/data"])

    cmd.extend([PINNED_PGVECTOR_IMAGE, "postgres", *pg_flags])
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start test PG container: {res.stderr.strip()}")

    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "exec", args.name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
            capture_output=True, check=False,
        )
        if r.returncode == 0:
            print(json.dumps({
                "status": "ready", "container": args.name,
                "host": "127.0.0.1", "port": args.port, "password_file": str(pw_file),
            }))
            return 0
        time.sleep(0.5)
    raise RuntimeError(f"Test PG container {args.name} did not become ready within 25s")


if __name__ == "__main__":
    sys.exit(main())
