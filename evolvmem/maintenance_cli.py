"""Operator CLI for the one-shot historical backfill (plan/apply/verify).

    python -m evolvmem.maintenance_cli [--data-dir DIR] plan [--json]
    python -m evolvmem.maintenance_cli [--data-dir DIR] apply --plan-digest <hex> --yes [--json]
    python -m evolvmem.maintenance_cli [--data-dir DIR] verify [--json]

``plan`` is read-only and deterministic; its digest fingerprints the
database. ``apply`` holds the exclusive cutover lock, recomputes the plan
under the lock (a digest mismatch is a usage error, exit 2), makes a
verified cutover backup, migrates every legacy row and backfills project
resolutions in one transaction, then rolls per-project summaries, sweeps
retention, and rebuilds the disposable vector cache. Any step failure
reports the backup directory name (never an absolute path) plus a stable
error code and exits 1. ``verify`` checks the post-apply invariants and
exits 0 only when every one passes.

Output carries counts, typed id references, and stable codes only — never
memory text, absolute paths, key material, or tracebacks. The process
environment's ``EVOLVMEM_*`` overrides are stripped for the duration of the
command so the explicit ``--data-dir`` always wins.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from evolvmem.codex_config import CodexMcpSnapshot
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator, LegacyMigrationReport
from evolvmem.context_models import ContextValidationError
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.context_vector_sync import ContextVectorSynchronizer
from evolvmem.cutover_backup import CutoverBackupError, create_cutover_backup
from evolvmem.cutover_cli import _load_embedding_engine, _scrubbed_environment
from evolvmem.cutover_lock import CutoverLock, CutoverLockTimeout
from evolvmem.maintenance import (
    MaintenanceError,
    _assess_rows,
    _build_plan_with_store,
    _is_sha256,
    _project_store,
    _request_for_row,
    build_plan,
    verify_invariants,
)
from evolvmem.project_resolver import ProjectResolver
from evolvmem.project_rollup import ProjectRollupGenerator, ProjectRollupReport
from evolvmem.project_store import ProjectStoreError
from evolvmem.summary_retention import SummaryRetention, SummaryRetentionReport
from evolvmem.vector_index import VectorIndex


def _load_rollup_llm():
    """Reuse the configured extraction provider for maintenance rollups."""
    from evolvmem.kimi_hooks import _load_llm_callable

    return _load_llm_callable(log_errors=False)


# ---- apply report ----


@dataclass(frozen=True, slots=True)
class MaintenanceApplyReport:
    """One apply run's outcome: counts, statuses, and the backup name only."""

    plan_digest: str
    planned_actions: int
    migration: LegacyMigrationReport
    resolutions_written: int
    projects_updated: int
    rollups: tuple[ProjectRollupReport, ...]
    retention: SummaryRetentionReport
    vector_status: str
    vector_documents: int
    backup_directory: str

    def public_dict(self) -> dict:
        return {
            "schema": "evolvmem.maintenance_apply",
            "version": 1,
            "ok": True,
            "plan_digest": self.plan_digest,
            "planned_actions": self.planned_actions,
            "migration": {
                "legacy_table_found": self.migration.legacy_table_found,
                "scanned": self.migration.scanned,
                "created": self.migration.created,
                "already_migrated": self.migration.already_migrated,
                "duplicate_active_count": self.migration.duplicate_active_count,
            },
            "resolutions_written": self.resolutions_written,
            "projects_updated": self.projects_updated,
            "rollups": [
                {
                    "project": report.project,
                    "status": report.status,
                    "reason": report.reason,
                    "context_id": report.context_id,
                }
                for report in self.rollups
            ],
            "retention": {
                "archived": len(self.retention.archived_ids),
                "held": len(self.retention.held_ids),
                "pending_projects": list(self.retention.pending_projects),
            },
            "vector": {
                "status": self.vector_status,
                "documents": self.vector_documents,
            },
            "backup_directory": self.backup_directory,
        }


# ---- apply orchestration ----


def _maintenance_snapshot() -> CodexMcpSnapshot:
    """Synthetic empty stanza: maintenance touches no Codex configuration."""
    stanza: dict = {}
    blob = json.dumps(stanza, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return CodexMcpSnapshot(
        stanza=stanza,
        stanza_sha256=hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        source_file_sha256=hashlib.sha256(b"").hexdigest(),
    )


def _backfill_projects(config: Config, store: ContextStore) -> tuple[int, int]:
    """Re-resolve every mapped legacy row inside the caller's transaction.

    Records the resolver's decision (conflict/unresolved stay pending with an
    empty project) and aligns the item's project with the resolved value.
    Human-final resolution rows (accepted/rejected) are never rewritten.
    Already-converged rows are skipped, so an empty plan applies as a no-op.
    Returns ``(resolutions_written, projects_updated)``.
    """
    project_store = _project_store(config, store)
    snapshot = project_store.snapshot()
    resolver = ProjectResolver()
    conn = store._connection()
    resolutions_written = 0
    projects_updated = 0
    now = _now_iso()
    for assessment in _assess_rows(store, resolver, snapshot):
        if assessment.item_id is None:
            continue  # migrate() maps every row first; this is defensive only
        item = conn.execute(
            "SELECT project FROM context_items WHERE id=?",
            (assessment.item_id,),
        ).fetchone()
        if item is None:
            continue
        existing = conn.execute(
            "SELECT resolution_state, resolved_project, review_state "
            "FROM context_project_resolutions WHERE item_id=?",
            (assessment.item_id,),
        ).fetchone()
        if existing is not None and str(existing["review_state"]) in (
            "accepted",
            "rejected",
        ):
            continue  # human decisions are final
        decision = assessment.decision
        target = decision.resolved_project
        needs_resolution = (
            existing is None
            or str(existing["resolution_state"]) != decision.state.value
            or str(existing["resolved_project"]) != target
        )
        if needs_resolution:
            project_store.record_resolution(assessment.item_id, decision)
            resolutions_written += 1
        if str(item["project"]) != target:
            conn.execute(
                "UPDATE context_items SET project=?, updated_at=? WHERE id=?",
                (target, now, assessment.item_id),
            )
            projects_updated += 1
    return resolutions_written, projects_updated


def apply_plan(
    config: Config,
    *,
    plan_digest: str,
    timestamp: datetime | None = None,
    embedding_engine=None,
    llm=None,
) -> MaintenanceApplyReport:
    """Execute an approved plan under the exclusive cutover lock.

    The plan is recomputed under the lock and must match ``plan_digest``
    exactly — a stale approval is a usage error, not an apply failure. Every
    later step failure raises ``MaintenanceError`` carrying the backup
    directory name (basename only) and a stable code.
    """
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if not _is_sha256(plan_digest):
        raise MaintenanceError("invalid_plan_digest")
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    backup_directory = ""
    try:
        with CutoverLock(config).exclusive():
            with ContextStore(config) as store:
                plan = _build_plan_with_store(config, store)
                if plan.digest != plan_digest:
                    raise MaintenanceError("plan_digest_mismatch")
                try:
                    manifest = create_cutover_backup(
                        config,
                        codex_snapshot=_maintenance_snapshot(),
                        preflight_digest=plan.digest,
                        timestamp=timestamp,
                    )
                except CutoverBackupError as exc:
                    raise MaintenanceError("backup_failed") from exc
                backup_directory = manifest.directory_name
                try:
                    with store.transaction():
                        migrator = LegacyMemoryMigrator(
                            store,
                            config,
                            project_decider=_row_project_decider(config, store),
                        )
                        migration = migrator.migrate()
                        resolutions, projects = _backfill_projects(config, store)
                except (
                    sqlite3.Error,
                    ContextValidationError,
                    ProjectStoreError,
                    ValueError,
                ) as exc:
                    raise MaintenanceError(
                        "migration_failed", backup_directory=backup_directory
                    ) from exc
                try:
                    rollups = ProjectRollupGenerator(
                        config, store, llm=llm
                    ).rollup_all()
                except Exception as exc:
                    raise MaintenanceError(
                        "rollup_failed", backup_directory=backup_directory
                    ) from exc
                try:
                    retention = SummaryRetention(config, store).sweep(_now_iso())
                except Exception as exc:
                    raise MaintenanceError(
                        "retention_failed", backup_directory=backup_directory
                    ) from exc
                synchronizer = ContextVectorSynchronizer(
                    config,
                    store,
                    VectorIndex(config, path=config.context_vector_path),
                    embedding_engine,
                )
                try:
                    vector = synchronizer.rebuild_active_l0()
                except Exception as exc:
                    raise MaintenanceError(
                        "vector_rebuild_failed", backup_directory=backup_directory
                    ) from exc
                if vector.status == "failed":
                    raise MaintenanceError(
                        "vector_rebuild_failed", backup_directory=backup_directory
                    )
    except CutoverLockTimeout as exc:
        raise MaintenanceError("lock_timeout") from exc
    return MaintenanceApplyReport(
        plan_digest=plan.digest,
        planned_actions=len(plan.planned_actions),
        migration=migration,
        resolutions_written=resolutions,
        projects_updated=projects,
        rollups=tuple(rollups),
        retention=retention,
        vector_status=vector.status,
        vector_documents=vector.document_count,
        backup_directory=backup_directory,
    )


def _row_project_decider(config: Config, store: ContextStore):
    """Row-signal resolver for the batch migrator: resolved project or ""."""
    snapshot = _project_store(config, store).snapshot()
    resolver = ProjectResolver()
    return lambda row: resolver.resolve(_request_for_row(row), snapshot).resolved_project


# ---- CLI ----


def _plan_digest_arg(value: str) -> str:
    if not _is_sha256(value):
        raise argparse.ArgumentTypeError("plan digest must be a lowercase SHA-256 hex")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolvmem.maintenance_cli",
        description="One-shot historical backfill: plan, apply, verify",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Config().data_dir,
        help="evolvmem data directory (default: the configured one)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="read-only deterministic backfill preview")
    plan.add_argument("--json", action="store_true")

    apply_cmd = sub.add_parser("apply", help="locked, verified-backup backfill")
    apply_cmd.add_argument("--plan-digest", required=True, type=_plan_digest_arg)
    apply_cmd.add_argument("--yes", action="store_true")
    apply_cmd.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="post-apply invariant check")
    verify.add_argument("--json", action="store_true")
    return parser


def _emit(payload: dict, use_json: bool, fallback: str) -> None:
    if use_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(fallback)


def _cmd_plan(args) -> int:
    plan = build_plan(Config(data_dir=args.data_dir))
    _emit(
        plan.public_dict(),
        args.json,
        "digest={} legacy_total={} mapped={} unmapped={} resolved={} "
        "conflict={} unresolved={} global={} planned_actions={}".format(
            plan.digest[:16],
            plan.legacy_total,
            plan.mapped,
            plan.unmapped,
            plan.resolved,
            plan.conflict,
            plan.unresolved,
            plan.global_,
            len(plan.planned_actions),
        ),
    )
    return 0


def _cmd_apply(args) -> int:
    config = Config(data_dir=args.data_dir)
    engine = _load_embedding_engine(config)
    try:
        report = apply_plan(
            config,
            plan_digest=args.plan_digest,
            embedding_engine=engine,
            llm=_load_rollup_llm(),
        )
    finally:
        close = getattr(engine, "close", None)
        if callable(close):
            close()
    _emit(
        report.public_dict(),
        args.json,
        f"ok=True backup={report.backup_directory} "
        f"digest={report.plan_digest[:16]} vector={report.vector_status}",
    )
    return 0


def _cmd_verify(args) -> int:
    report = verify_invariants(Config(data_dir=args.data_dir))
    failed = [entry.name for entry in report.invariants if not entry.passed]
    _emit(
        report.public_dict(),
        args.json,
        f"ok={report.ok} digest={report.plan_digest[:16]} failed={len(failed)}",
    )
    return 0 if report.ok else 1


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "apply" and not args.yes:
        parser.error("apply requires an explicit --yes")
    handlers = {
        "plan": _cmd_plan,
        "apply": _cmd_apply,
        "verify": _cmd_verify,
    }
    with _scrubbed_environment():
        try:
            return handlers[args.command](args)
        except MaintenanceError as exc:
            payload: dict[str, object] = {"error": exc.code}
            if exc.backup_directory:
                payload["backup_directory"] = exc.backup_directory
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
            return 2 if exc.code in ("invalid_plan_digest", "plan_digest_mismatch") else 1
        except Exception:
            # never leak a traceback (absolute paths) across the CLI boundary
            print(json.dumps({"error": "internal"}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
