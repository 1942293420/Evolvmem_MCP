"""CLI for WAL segment archiving, base backups, PITR drills, and prod config patch preview."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from evolvmem.history_pg.wal_archive import (
    archive_wal_segment,
    generate_prod_wal_config_patch,
    take_base_backup,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EvolvMem PG WAL archiving and PITR utility")
    sub = p.add_subparsers(dest="cmd", required=True)

    arc = sub.add_parser("archive-segment")
    arc.add_argument("--src", type=Path, required=True)
    arc.add_argument("--archive-dir", type=Path, required=True)
    arc.add_argument("--name", required=True)

    bak = sub.add_parser("basebackup")
    bak.add_argument("--container", required=True)
    bak.add_argument("--backup-dir", type=Path, required=True)
    bak.add_argument("--port", type=int, required=True)
    bak.add_argument("--password-file", type=Path, required=True)
    bak.add_argument("--allow-prod", action="store_true")

    sub.add_parser("prod-wal-patch")

    args = p.parse_args(argv)
    if args.cmd == "archive-segment":
        print(json.dumps(archive_wal_segment(args.src, args.archive_dir, args.name)))
        return 0
    if args.cmd == "basebackup":
        print(json.dumps(take_base_backup(
            args.container, args.backup_dir,
            port=args.port, password_file=args.password_file, allow_prod=args.allow_prod,
        )))
        return 0
    if args.cmd == "prod-wal-patch":
        print(json.dumps(generate_prod_wal_config_patch(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
