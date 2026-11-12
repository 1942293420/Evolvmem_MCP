#!/usr/bin/env python3
"""INT-managed isolated test PostgreSQL lifecycle helper with hard production guard."""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evolvmem.pg_test_guard import (  # noqa: E402
    PINNED_PGVECTOR_IMAGE,
    assert_safe_test_pg_target,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage isolated EvolvMem test PostgreSQL container")
    sub = p.add_subparsers(dest="action", required=True)

    st = sub.add_parser("start")
    st.add_argument("--name", default="evolvmem-pg-test-stage1")
    st.add_argument("--port", type=int, default=55433)
    st.add_argument("--volume", default="")
    st.add_argument("--password-file", type=Path, default=Path("/tmp/evolvmem-pg-test.pw"))

    sp = sub.add_parser("stop")
    sp.add_argument("--name", default="evolvmem-pg-test-stage1")
    sp.add_argument("--volume", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(raw_argv)
    vols = [args.volume] if getattr(args, "volume", "") else []
    assert_safe_test_pg_target(
        port=getattr(args, "port", None),
        volumes=vols,
        container_name=args.name,
        argv=raw_argv,
    )

    if args.action == "stop":
        subprocess.run(["docker", "rm", "-f", args.name], check=False, capture_output=True)
        if args.volume:
            subprocess.run(["docker", "volume", "rm", "-f", args.volume], check=False, capture_output=True)
        print(json.dumps({"status": "stopped", "container": args.name}))
        return 0

    pw_file: Path = args.password_file
    pw_file.parent.mkdir(parents=True, exist_ok=True)
    if not pw_file.exists():
        pw_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        pw_file.chmod(0o600)
    pw = pw_file.read_text(encoding="utf-8").strip()

    subprocess.run(["docker", "rm", "-f", args.name], check=False, capture_output=True)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        args.name,
        "--shm-size=512m",
        "-p",
        f"127.0.0.1:{args.port}:5432",
        "-e",
        f"POSTGRES_PASSWORD={pw}",
        "-e",
        "POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C.UTF-8 --data-checksums",
        "--label",
        "evolvmem.role=isolated-test-pg",
    ]
    if args.volume:
        cmd.extend(["-v", f"{args.volume}:/var/lib/postgresql/data"])
    cmd.extend([PINNED_PGVECTOR_IMAGE, "postgres", "-c", "shared_preload_libraries=pg_stat_statements"])
    assert_safe_test_pg_target(port=args.port, volumes=vols, container_name=args.name, argv=cmd)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    for _ in range(40):
        r = subprocess.run(
            ["docker", "exec", args.name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "container": args.name,
                        "host": "127.0.0.1",
                        "port": args.port,
                        "password_file": str(pw_file),
                    }
                )
            )
            return 0
        time.sleep(0.5)
    raise RuntimeError(f"Test PG container {args.name} did not become ready within 20s")


if __name__ == "__main__":
    sys.exit(main())
