"""Verify 100% SQLite table classification, zero-diff reconciliation, and candidate acceptance receipts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def run_verification(
    *,
    manifest_path: Path,
    reconciliation_path: Path,
    mode: str = "migrated",
    acceptance_dir: Path | None = None,
) -> dict[str, Any]:
    if mode not in ("migrated", "candidate"):
        raise ValueError(f"invalid_mode:{mode}")
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    r = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if m.get("unclassified_tables"):
        errors.append(f"unclassified_tables:{len(m['unclassified_tables'])}")
    if int(m.get("classified_tables", -1)) != int(m.get("total_tables", -2)):
        errors.append("classification_incomplete")
    if int(r.get("unexplained_diff_count", -1)) != 0:
        errors.append(f"unexplained_diffs:{r.get('unexplained_diff_count')}")
    if r.get("ok") is not True:
        errors.append("reconciliation_not_ok")
    if mode == "candidate":
        counts = r.get("counts") or {}
        if int(counts.get("blocks_embedded", 0)) < int(counts.get("blocks_pg", 1)):
            errors.append("blocks_embeddings_incomplete")
        adir = acceptance_dir or manifest_path.parent
        for fname in (
            "web-mcp-feature-acceptance.json",
            "desktop-capture-continuity-acceptance.json",
            "load-acceptance.json",
            "rollback-drill.json",
            "disk-peak-budget.json",
        ):
            fpath = adir / fname
            if not fpath.exists():
                errors.append(f"missing_acceptance:{fname}")
                continue
            doc = json.loads(fpath.read_text(encoding="utf-8"))
            if doc.get("ok") is not True:
                errors.append(f"acceptance_not_ok:{fname}")
            if "sqlite_fd_count" in doc and int(doc["sqlite_fd_count"]) != 0:
                errors.append(f"sqlite_fds_nonzero:{fname}")
            if "lost_writes" in doc and int(doc["lost_writes"]) != 0:
                errors.append(f"rollback_lost_writes:{fname}")
            if "free_gib" in doc and float(doc["free_gib"]) < 50.0:
                errors.append(f"disk_free_low:{fname}")
            if (doc.get("llm_usage") or {"calls": 0}) != {"calls": 0, "tokens": 0}:
                errors.append(f"llm_usage_nonzero:{fname}")
    return {
        "schema_version": "stage5.candidate_manifest_verification.v1",
        "ok": len(errors) == 0,
        "mode": mode,
        "total_tables": int(m.get("total_tables", 0)),
        "classified_tables": int(m.get("classified_tables", 0)),
        "unexplained_diff_count": int(r.get("unexplained_diff_count", -1)),
        "errors": errors,
        "llm_usage": {"calls": 0, "tokens": 0},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify Stage 5 PG cutover manifest & reconciliation")
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--reconciliation", type=Path, required=True)
    ap.add_argument("--mode", choices=["migrated", "candidate"], default="migrated")
    ap.add_argument("--acceptance-dir", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args(argv)
    res = run_verification(
        manifest_path=args.manifest,
        reconciliation_path=args.reconciliation,
        mode=args.mode,
        acceptance_dir=args.acceptance_dir,
    )
    if args.output is not None:
        args.output.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
