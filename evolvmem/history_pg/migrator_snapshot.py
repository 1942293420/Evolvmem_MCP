"""Online read-only SQLite snapshotting and 100% table classification manifest."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any

CONTROL_TABLES = frozenset({
    "console_users", "console_libraries", "console_grants",
    "console_pending", "console_audit", "console_meta",
})
EPHEMERAL_SESSION_TABLES = frozenset({"web_sessions"})

ORGANIZED_HISTORY_CANDIDATE_TABLES = frozenset({
    "projects", "blocks", "block_projects", "messages", "attachments",
    "sources", "recovery_sources", "recovery_reconciliation", "meta", "exceptions",
})
ORGANIZED_HISTORY_MGMT_TABLES = frozenset({"history_management_meta", "operations", "trash"})
ORGANIZED_HISTORY_BOARD_TABLES = frozenset({"project_board_sync_state"})

RUNTIME_PG_TABLES = frozenset({
    "session_archives", "session_archive_storage", "session_archive_holds",
    "archive_source_exclusions", "conversation_history",
    "lan_session_uploads", "lan_session_heads", "lan_session_backfills",
    "lan_write_requests", "lan_workspace_bindings", "lan_memory_revision",
    "local_capture_files", "local_capture_scans",
    "context_project_registry", "context_project_aliases", "context_project_workspace_bindings",
    "context_items", "context_layers", "context_project_resolutions", "context_sources",
    "lan_publications", "continuity_workstreams", "continuity_focus", "continuity_events",
    "task_history_grants",
})

STAGE4_EXPERIENCE_OR_GOVERNANCE_TABLES = frozenset({
    "memories", "legacy_memory_migrations", "classification_results", "extraction_receipts",
    "context_evidence", "context_health_generation", "context_project_rollups",
    "corrections", "round_metrics", "skill_proposals", "typed_ownership_repairs",
    "capability_assessments", "capability_categories", "capability_category_suggestions",
    "capability_intake", "capability_memories", "capability_provenance_repairs",
    "capability_sources", "existing_capability_assessment_runs",
    "governance_migration_audit", "governance_module_backfill_runs", "governance_module_seed_runs",
    "governance_source_enrollment_audit", "governance_source_enrollments",
    "private_governance_empty_terminals", "private_governance_empty_terminals_v2",
    "private_governance_source_upgrades",
    "history_experience_card_reviews", "history_experience_compact_previews",
    "history_experience_ingest_lineage_parents", "history_experience_ingest_materials",
    "history_experience_ingest_window_cards", "history_experience_ingest_windows",
    "history_experience_review_resets",
    "knowledge_cleaning_reviews", "knowledge_decisions", "knowledge_equivalences",
    "knowledge_fact_actions", "knowledge_fact_choices", "knowledge_fact_conditions",
    "knowledge_fact_groups", "knowledge_fact_issues", "knowledge_fact_relations",
    "knowledge_fact_separations", "knowledge_fact_sources", "knowledge_fact_versions",
    "knowledge_metadata", "knowledge_objects", "knowledge_qa",
    "knowledge_restructure_map", "knowledge_restructure_state", "knowledge_source_bindings",
    "learning_memories", "learning_project_types", "learning_rules",
    "learning_runs", "learning_settings", "learning_versions",
    "manual_experience_events", "manual_experience_notes", "manual_experience_requests",
    "organization_context_basis", "organization_conversations", "organization_guidance",
    "organization_review_feedback", "organization_review_jobs", "organization_review_vectors",
    "organization_scope_exclusions", "organization_settings", "organization_task_upgrades",
    "organization_tasks", "organization_units",
    "project_archives", "project_event_sources", "project_events",
    "project_history_date_repairs", "project_memory_documents",
    "project_module_audit", "project_module_bindings", "project_module_names",
    "project_modules", "unit_derivations",
})

FTS_BASES = ("capability_memories_fts", "context_layers_fts", "memories_fts")


def default_live_sources() -> dict[str, Path]:
    return {
        "users": Path("/home/jiangli/.config/evolvmem-console/users.db"),
        "candidate": Path("/home/jiangli/evolvmem-governance-backups/20261107-session-block-history/candidate-categorized-20261108T1300/candidate.db"),
        "management": Path("/home/jiangli/.config/evolvmem-history/management.db"),
        "memory_owner": Path("/home/jiangli/.claude/evolvmem/memory.db"),
        "project_board_sync": Path("/home/jiangli/.claude/evolvmem/project_board_sync.db"),
        "lan_public": Path("/home/jiangli/.local/share/evolvmem-lan/public/memory.db"),
        "lan_kane": Path("/home/jiangli/.local/share/evolvmem-lan/users/kane/memory.db"),
    }


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _inspect_snapshot(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}
    finally:
        conn.close()


def take_online_snapshots(sources: dict[str, Path], snapshot_dir: Path) -> dict[str, dict[str, Any]]:
    """Create online read-only SQLite backups (0600) with SHA256 and exact table row counts."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.chmod(0o700)
    result: dict[str, dict[str, Any]] = {}
    for key, src_path in sources.items():
        if not src_path.is_file():
            raise FileNotFoundError(f"missing_source_sqlite:{key}:{src_path}")
        dest = snapshot_dir / f"{key}.db"
        tmp = snapshot_dir / f".{key}.db.{os.getpid()}.tmp"
        if tmp.exists():
            tmp.unlink()
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(tfd)
        src_conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30.0)
        try:
            dst_conn = sqlite3.connect(str(tmp))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        tmp.chmod(0o600)
        os.replace(tmp, dest)
        counts = _inspect_snapshot(dest)
        result[key] = {
            "source_key": key,
            "snapshot_path": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": _file_sha256(dest),
            "sql_mode": "ro",
            "table_counts": counts,
        }
    return result


def classify_table(db_key: str, table_name: str) -> str | None:
    if db_key == "users":
        if table_name in CONTROL_TABLES:
            return "migrated_control_pg"
        if table_name in EPHEMERAL_SESSION_TABLES:
            return "transient_ephemeral_session"
        return None
    if db_key == "candidate" and table_name in ORGANIZED_HISTORY_CANDIDATE_TABLES:
        return "migrated_organized_history_pg"
    if db_key == "management" and table_name in ORGANIZED_HISTORY_MGMT_TABLES:
        return "migrated_organized_history_pg"
    if db_key == "project_board_sync" and table_name in ORGANIZED_HISTORY_BOARD_TABLES:
        return "migrated_organized_history_pg"
    if db_key in ("memory_owner", "lan_public", "lan_kane"):
        if table_name in RUNTIME_PG_TABLES:
            return "migrated_runtime_pg"
        if table_name in STAGE4_EXPERIENCE_OR_GOVERNANCE_TABLES:
            return "migrated_stage4_experience_v2_or_superseded"
        if any(table_name == base or table_name.startswith(f"{base}_") for base in FTS_BASES):
            return "sqlite_fts_shadow_rebuilt_as_pg_index"
    return None


def classify_all_tables(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Classify 100% of non-internal SQLite tables across every source snapshot."""
    entries: list[dict[str, Any]] = []
    unclassified: list[dict[str, str]] = []
    category_counts: dict[str, int] = {}

    for db_key in sorted(snapshots):
        pub_key = "lan_member_1" if db_key == "lan_kane" else db_key
        t_counts = snapshots[db_key]["table_counts"]
        for tbl, row_cnt in sorted(t_counts.items()):
            cat = classify_table(db_key, tbl)
            if cat is None:
                unclassified.append({"source_key": pub_key, "table": tbl})
            else:
                category_counts[cat] = category_counts.get(cat, 0) + 1
                entries.append({
                    "source_key": pub_key,
                    "table": tbl,
                    "rows": int(row_cnt),
                    "classification": cat,
                })

    total = len(entries) + len(unclassified)
    scrubbed = {
        "schema_version": "stage5.table_classification.v1",
        "source_count": len(snapshots),
        "total_tables": total,
        "classified_tables": len(entries),
        "unclassified_tables": unclassified,
        "category_counts": category_counts,
        "snapshots": {
            ("lan_member_1" if k == "lan_kane" else k): {
                "bytes": v["bytes"],
                "sha256": v["sha256"],
                "table_count": len(v["table_counts"]),
            }
            for k, v in sorted(snapshots.items())
        },
        "tables": entries,
    }
    return {
        "total_tables": total,
        "classified_tables": len(entries),
        "unclassified_tables": unclassified,
        "category_counts": category_counts,
        "entries": entries,
        "scrubbed": scrubbed,
    }
