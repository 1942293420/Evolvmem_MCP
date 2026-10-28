"""Backup, dry-run, resumable migration and verification commands."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.knowledge_restructure import (
    backup_database, dry_run, migrate_batch, set_structure_mode, verify_migration,
)


def _service(data_dir: Path):
    path = Path(data_dir)
    is_copy = path.is_file()
    config = Config.from_file(data_dir=path.parent if is_copy else path)
    if is_copy:
        config.database_path = path.resolve()
    store = ContextStore(config)
    store.initialize()
    return SimpleNamespace(config=config, store=store)


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evolvmem.knowledge_restructure_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "dry-run", "migrate", "verify"):
        command = sub.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--data-dir", type=Path)
        source.add_argument("--copy", type=Path)
        if name == "backup":
            command.add_argument("--target", type=Path)
        if name == "dry-run":
            command.add_argument("--report", type=Path)
        if name == "migrate":
            command.add_argument("--batch", type=int, default=100)
            command.add_argument("--resume", action="store_true")
    mode_parser = sub.add_parser("set-mode")
    mode_parser.add_argument("--data-dir", type=Path, required=True)
    mode_parser.add_argument("mode", choices=("legacy", "dual", "primary"))
    args = parser.parse_args(argv)
    data_dir = args.data_dir or getattr(args, "copy", None)
    if args.command == "backup":
        target = args.target or data_dir / "backups" / (
            "knowledge-restructure-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".db"
        )
        path = backup_database(Config(data_dir=data_dir), target)
        _emit({"backup": str(path), "integrity_check": "ok"})
        return 0
    if args.command == "dry-run":
        report = dry_run(data_dir)
        if args.report:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _emit(report)
        return 0
    service = _service(data_dir)
    try:
        if args.command == "set-mode":
            _emit(set_structure_mode(service, args.mode))
            return 0
        if args.command == "verify":
            report = verify_migration(service)
            _emit(report)
            return 0 if report["ok"] else 1
        totals = {"scanned": 0, "capabilities": 0, "events": 0, "pending": 0, "skipped": 0}
        while True:
            report = migrate_batch(service, batch_size=args.batch)
            for key in totals:
                totals[key] += report[key]
            if report["scanned"] == 0:
                break
        totals["last_item_id"] = report["last_item_id"]
        _emit(totals)
        return 0
    finally:
        service.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
