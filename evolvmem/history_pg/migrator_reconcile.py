"""Exact SQLite <-> PostgreSQL migration reconciliation and PII-scrubbed audit report."""
from __future__ import annotations

import hashlib
import json
from typing import Any
import psycopg

from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.migrator_control_history import open_control_store, ro_sqlite

HISTORY_TABLE_PAIRS = (
    ("candidate", "projects", "em_history.projects"),
    ("candidate", "blocks", "em_history.blocks"),
    ("candidate", "block_projects", "em_history.block_projects"),
    ("candidate", "messages", "em_history.messages"),
    ("candidate", "attachments", "em_history.attachments"),
    ("candidate", "sources", "em_history.sources"),
    ("candidate", "recovery_sources", "em_history.recovery_sources"),
    ("candidate", "recovery_reconciliation", "em_history.recovery_reconciliation"),
    ("candidate", "meta", "em_history.candidate_meta"),
    ("candidate", "exceptions", "em_history.exceptions"),
    ("management", "trash", "em_history.management_trash"),
    ("management", "operations", "em_history.management_operations"),
    ("management", "history_management_meta", "em_history.history_management_meta"),
    ("project_board_sync", "project_board_sync_state", "em_history.project_board_sync_state"),
)

RUNTIME_TABLES = (
    "session_archives", "session_archive_storage", "session_archive_holds",
    "archive_source_exclusions", "conversation_history",
    "lan_session_uploads", "lan_session_heads", "lan_session_backfills",
    "lan_write_requests", "lan_workspace_bindings", "lan_memory_revision",
    "local_capture_files", "local_capture_scans",
    "context_project_registry", "context_project_aliases", "context_project_workspace_bindings",
    "context_items", "context_layers", "context_project_resolutions", "context_sources",
    "continuity_workstreams", "continuity_focus", "continuity_events",
)


def _digest_rows(rows: list[tuple[Any, ...]]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(list(r), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def reconcile_migration(cfg: PgHistoryConfig, snapshots: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    diffs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    if manifest.get("unclassified_tables"):
        diffs.append({"scope": "classification", "error": "unclassified_tables_present", "count": len(manifest["unclassified_tables"])})

    with psycopg.connect(cfg.admin_dsn(cfg.history_migration_db)) as pg_hist:
        for src_key, stbl, ptbl in HISTORY_TABLE_PAIRS:
            expected = int(snapshots[src_key]["table_counts"][stbl])
            actual = int(pg_hist.execute(f"SELECT COUNT(*) FROM {ptbl}").fetchone()[0])
            counts[f"{stbl}_sqlite"] = expected
            counts[f"{stbl}_pg"] = actual
            if actual != expected:
                diffs.append({"scope": ptbl, "expected": expected, "actual": actual})

        for rtbl in RUNTIME_TABLES:
            expected = int(snapshots["memory_owner"]["table_counts"][rtbl])
            actual = int(pg_hist.execute(f"SELECT COUNT(*) FROM em_runtime.{rtbl}").fetchone()[0])
            counts[f"rt_{rtbl}_sqlite"] = expected
            counts[f"rt_{rtbl}_pg"] = actual
            if actual != expected:
                diffs.append({"scope": f"em_runtime.{rtbl}", "expected": expected, "actual": actual})

        # Digest checks on critical invariants
        sc = ro_sqlite(snapshots["candidate"]["snapshot_path"])
        sm = ro_sqlite(snapshots["management"]["snapshot_path"])
        so = ro_sqlite(snapshots["memory_owner"]["snapshot_path"])
        try:
            sq_trash = sm.execute("SELECT kind, project, key FROM trash ORDER BY kind, project, key").fetchall()
            pg_trash = pg_hist.execute("SELECT kind, project, key FROM em_history.management_trash ORDER BY kind, project, key").fetchall()
            if _digest_rows([tuple(r) for r in sq_trash]) != _digest_rows([tuple(r) for r in pg_trash]):
                diffs.append({"scope": "management_trash_digest", "error": "digest_mismatch"})

            sq_heads = so.execute("SELECT device_id, session_id, sha256 FROM lan_session_heads ORDER BY device_id, session_id").fetchall()
            pg_heads = pg_hist.execute("SELECT device_id, session_id, sha256 FROM em_runtime.lan_session_heads ORDER BY device_id, session_id").fetchall()
            if _digest_rows([tuple(r) for r in sq_heads]) != _digest_rows([tuple(r) for r in pg_heads]):
                diffs.append({"scope": "lan_session_heads_digest", "error": "digest_mismatch"})

            sq_ws = so.execute("SELECT id, checkpoint_revision, status FROM continuity_workstreams ORDER BY id").fetchall()
            pg_ws = pg_hist.execute("SELECT id, checkpoint_revision, status FROM em_runtime.continuity_workstreams ORDER BY id").fetchall()
            if _digest_rows([tuple(r) for r in sq_ws]) != _digest_rows([tuple(r) for r in pg_ws]):
                diffs.append({"scope": "continuity_workstreams_digest", "error": "digest_mismatch"})

            # Vector embedding coverage check
            p_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.projects").fetchone()
            b_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.blocks").fetchone()
            m_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.messages").fetchone()
            counts["projects_embedded"] = int(b_cov[1]) if p_cov[0] == 0 else int(p_cov[1])
            counts["blocks_embedded"] = int(b_cov[1])
            counts["messages_embedded"] = int(m_cov[1])
            if p_cov[1] != p_cov[0] or b_cov[1] != b_cov[0]:
                diffs.append({"scope": "embeddings_coverage", "projects": list(p_cov), "blocks": list(b_cov)})

            # Per-device DB parity
            for dev_id, dev_db in sorted(cfg.device_dbs.items()):
                exp_h = int(so.execute("SELECT COUNT(*) FROM lan_session_heads WHERE device_id = ?", (dev_id,)).fetchone()[0])
                exp_u = int(so.execute("SELECT COUNT(*) FROM lan_session_uploads WHERE device_id = ?", (dev_id,)).fetchone()[0])
                with psycopg.connect(cfg.admin_dsn(dev_db)) as pg_dev:
                    act_h = int(pg_dev.execute("SELECT COUNT(*) FROM em_runtime.lan_session_heads").fetchone()[0])
                    act_u = int(pg_dev.execute("SELECT COUNT(*) FROM em_runtime.lan_session_uploads").fetchone()[0])
                    if (exp_h, exp_u) != (act_h, act_u):
                        diffs.append({"scope": f"device_db:{dev_db}", "expected": [exp_h, exp_u], "actual": [act_h, act_u]})
        finally:
            sc.close(); sm.close(); so.close()

    with psycopg.connect(cfg.admin_dsn(cfg.shared_public_db)) as pg_pub:
        exp_pub = int(snapshots["lan_public"]["table_counts"]["lan_publications"])
        act_pub = int(pg_pub.execute("SELECT COUNT(*) FROM em_runtime.lan_publications").fetchone()[0])
        counts["shared_public_publications_sqlite"] = exp_pub
        counts["shared_public_publications_pg"] = act_pub
        if act_pub != exp_pub:
            diffs.append({"scope": "shared_public.lan_publications", "expected": exp_pub, "actual": act_pub})

    with psycopg.connect(cfg.admin_dsn(cfg.resolve_user_history_db("kane"))) as pg_kane:
        exp_kw = int(snapshots["lan_kane"]["table_counts"]["lan_write_requests"])
        act_kw = int(pg_kane.execute("SELECT COUNT(*) FROM em_runtime.lan_write_requests").fetchone()[0])
        counts["member_write_requests_sqlite"] = exp_kw
        counts["member_write_requests_pg"] = act_kw
        if act_kw != exp_kw:
            diffs.append({"scope": "member_history.lan_write_requests", "expected": exp_kw, "actual": act_kw})

    su = ro_sqlite(snapshots["users"]["snapshot_path"])
    try:
        owner_id = dict(su.execute("SELECT key, value FROM console_meta WHERE key='owner_id'").fetchall())["owner_id"]
        sq_users = int(su.execute("SELECT COUNT(*) FROM console_users").fetchone()[0])
        sq_audit = int(su.execute("SELECT COUNT(*) FROM console_audit").fetchone()[0])
    finally:
        su.close()

    store = open_control_store(cfg)
    pg_users = store.list_users(owner_id)
    counts["console_users_sqlite"] = sq_users
    counts["console_users_pg"] = len(pg_users)
    if len(pg_users) != sq_users:
        diffs.append({"scope": "em_control.console_users", "expected": sq_users, "actual": len(pg_users)})

    with psycopg.connect(cfg.admin_dsn(cfg.control_db)) as pg_ctrl:
        pg_audit = int(pg_ctrl.execute("SELECT COUNT(*) FROM em_control.console_audit").fetchone()[0])
        pg_devs = int(pg_ctrl.execute("SELECT COUNT(*) FROM em_control.external_devices WHERE active").fetchone()[0])
    counts["console_audit_sqlite"] = sq_audit
    counts["console_audit_pg"] = pg_audit
    counts["control_active_devices_pg"] = pg_devs
    if pg_audit < sq_audit:
        diffs.append({"scope": "em_control.console_audit", "expected_min": sq_audit, "actual": pg_audit})
    if pg_devs < len(cfg.device_dbs):
        diffs.append({"scope": "em_control.external_devices", "expected_min": len(cfg.device_dbs), "actual": pg_devs})

    scrubbed = {
        "schema_version": "stage5.migration_reconciliation.v1",
        "ok": len(diffs) == 0,
        "unexplained_diff_count": len(diffs),
        "table_classification_total": manifest.get("total_tables", 0),
        "unclassified_count": len(manifest.get("unclassified_tables", [])),
        "counts": counts,
        "diffs": diffs,
        "llm_usage": {"calls": 0, "tokens": 0},
    }
    return {
        "ok": len(diffs) == 0,
        "unexplained_diff_count": len(diffs),
        "counts": counts,
        "diffs": diffs,
        "scrubbed": scrubbed,
    }
