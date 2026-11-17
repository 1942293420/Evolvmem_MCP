"""Exact SQLite <-> PostgreSQL migration reconciliation and PII-scrubbed audit report."""
from __future__ import annotations

import hashlib
import json
from typing import Any
import psycopg

from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.migrator_control_history import _i, _s, open_control_store, ro_sqlite

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
    "continuity_workstreams", "continuity_focus", "continuity_events", "task_history_grants",
)

MEMBER_PUBLIC_RT_TABLES = (
    "lan_publications", "context_items", "context_layers",
    "context_project_resolutions", "lan_memory_revision", "lan_write_requests",
)


def _stream_digest(cur, norm_fn=lambda r: tuple(r)) -> str:
    h = hashlib.sha256()
    while True:
        batch = cur.fetchmany(4096)
        if not batch:
            break
        for r in batch:
            h.update(json.dumps(list(norm_fn(r)), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def _check_digest(diffs: list[dict[str, Any]], digest_checks: dict[str, bool], name: str, sq_cur, pg_cur, norm_fn=lambda r: tuple(r)) -> None:
    ok = _stream_digest(sq_cur, norm_fn) == _stream_digest(pg_cur, norm_fn)
    digest_checks[name] = ok
    if not ok:
        diffs.append({"scope": f"{name}_digest", "error": "digest_mismatch"})


def _reconcile_sub_db(
    cfg: PgHistoryConfig, dbname: str, prefix: str, tbl_counts: dict[str, Any],
    counts: dict[str, int], diffs: list[dict[str, Any]], scope_prefix: str,
) -> None:
    with psycopg.connect(cfg.admin_dsn(dbname)) as conn:
        for tbl in MEMBER_PUBLIC_RT_TABLES:
            if tbl not in tbl_counts:
                continue
            exp = int(tbl_counts[tbl])
            act = int(conn.execute(f"SELECT COUNT(*) FROM em_runtime.{tbl}").fetchone()[0])
            counts[f"{prefix}_{tbl}_sqlite"] = exp
            counts[f"{prefix}_{tbl}_pg"] = act
            if act != exp:
                diffs.append({"scope": f"{scope_prefix}.{tbl}", "expected": exp, "actual": act})


def reconcile_migration(cfg: PgHistoryConfig, snapshots: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    diffs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    digest_checks: dict[str, bool] = {}

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

        sc = ro_sqlite(snapshots["candidate"]["snapshot_path"])
        sm = ro_sqlite(snapshots["management"]["snapshot_path"])
        so = ro_sqlite(snapshots["memory_owner"]["snapshot_path"])
        try:
            _check_digest(diffs, digest_checks, "management_trash",
                sm.execute("SELECT kind, project, key FROM trash ORDER BY kind, project, key"),
                pg_hist.execute('SELECT kind, project, key FROM em_history.management_trash ORDER BY kind COLLATE "C", project COLLATE "C", key COLLATE "C"'))
            _check_digest(diffs, digest_checks, "lan_session_heads",
                so.execute("SELECT device_id, session_id, sha256 FROM lan_session_heads ORDER BY device_id, session_id"),
                pg_hist.execute('SELECT device_id, session_id, sha256 FROM em_runtime.lan_session_heads ORDER BY device_id COLLATE "C", session_id COLLATE "C"'))
            _check_digest(diffs, digest_checks, "continuity_workstreams",
                so.execute("SELECT id, checkpoint_revision, status FROM continuity_workstreams ORDER BY id"),
                pg_hist.execute('SELECT id, checkpoint_revision, status FROM em_runtime.continuity_workstreams ORDER BY id COLLATE "C"'))
            _check_digest(diffs, digest_checks, "blocks",
                sc.execute("SELECT block_id, adapter, kind, title, message_count, block_sha256, projects FROM blocks ORDER BY block_id"),
                pg_hist.execute('SELECT block_id, adapter, kind, title, message_count, block_sha256, projects FROM em_history.blocks ORDER BY block_id COLLATE "C"'),
                lambda r: (_s(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _i(r[4]), _s(r[5]), _s(r[6], "[]")))
            _check_digest(diffs, digest_checks, "messages",
                sc.execute("SELECT block_id, ordinal, role, content, recorded_at, attachments FROM messages ORDER BY block_id, ordinal"),
                pg_hist.execute('SELECT block_id, ordinal, role, content, recorded_at, attachments FROM em_history.messages ORDER BY block_id COLLATE "C", ordinal'),
                lambda r: (_s(r[0]), _i(r[1]), _s(r[2]), _s(r[3]), _s(r[4]), _s(r[5], "[]")))
            _check_digest(diffs, digest_checks, "session_archives",
                so.execute("SELECT id, project, adapter, external_session_id, payload_sha256, state FROM session_archives ORDER BY id"),
                pg_hist.execute("SELECT id, project, adapter, external_session_id, payload_sha256, state FROM em_runtime.session_archives ORDER BY id"),
                lambda r: (_i(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _s(r[4]), _s(r[5], "available")))
            _check_digest(diffs, digest_checks, "context_items",
                so.execute("SELECT id, identity_key, content_type, project, scope, status FROM context_items ORDER BY id"),
                pg_hist.execute("SELECT id, identity_key, content_type, project, scope, status FROM em_runtime.context_items ORDER BY id"),
                lambda r: (_i(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _s(r[4], "project"), _s(r[5], "candidate")))
            _check_digest(diffs, digest_checks, "context_layers",
                so.execute("SELECT id, item_id, layer, content_hash FROM context_layers ORDER BY id"),
                pg_hist.execute("SELECT id, item_id, layer, content_hash FROM em_runtime.context_layers ORDER BY id"),
                lambda r: (_i(r[0]), _i(r[1]), _s(r[2]), _s(r[3])))

            p_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.projects").fetchone()
            b_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.blocks").fetchone()
            m_cov = pg_hist.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM em_history.messages").fetchone()
            counts["projects_embedded"] = int(p_cov[1] or 0)
            counts["blocks_embedded"] = int(b_cov[1] or 0)
            counts["messages_embedded"] = int(m_cov[1] or 0)
            if p_cov[1] != p_cov[0] or b_cov[1] != b_cov[0]:
                diffs.append({"scope": "embeddings_coverage", "projects": list(p_cov), "blocks": list(b_cov)})

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

    _reconcile_sub_db(cfg, cfg.shared_public_db, "shared_public", snapshots["lan_public"]["table_counts"], counts, diffs, "shared_public")
    counts["shared_public_publications_sqlite"] = counts.get("shared_public_lan_publications_sqlite", 0)
    counts["shared_public_publications_pg"] = counts.get("shared_public_lan_publications_pg", 0)
    _reconcile_sub_db(cfg, cfg.resolve_user_history_db("kane"), "member", snapshots["lan_kane"]["table_counts"], counts, diffs, "member_history")
    counts["member_write_requests_sqlite"] = counts.get("member_lan_write_requests_sqlite", 0)
    counts["member_write_requests_pg"] = counts.get("member_lan_write_requests_pg", 0)

    su = ro_sqlite(snapshots["users"]["snapshot_path"])
    try:
        owner_id = dict(su.execute("SELECT key, value FROM console_meta WHERE key='owner_id'").fetchall())["owner_id"]
        sq_users = int(su.execute("SELECT COUNT(*) FROM console_users").fetchone()[0])
        sq_audit = int(su.execute("SELECT COUNT(*) FROM console_audit").fetchone()[0])
        sq_grants = int(su.execute("SELECT COUNT(*) FROM console_grants").fetchone()[0])
        sq_libs = int(su.execute("SELECT COUNT(*) FROM console_libraries").fetchone()[0])
        sq_pending = int(su.execute("SELECT COUNT(*) FROM console_pending").fetchone()[0])
    finally:
        su.close()

    store = open_control_store(cfg)
    pg_users = store.list_users(owner_id)
    pg_grants = len(store.list_grants(owner_id))
    pg_pending = len(store.list_pending(owner_id))
    with psycopg.connect(cfg.admin_dsn(cfg.control_db)) as pg_ctrl:
        pg_audit = int(pg_ctrl.execute("SELECT COUNT(*) FROM em_control.console_audit").fetchone()[0])
        pg_libs = int(pg_ctrl.execute("SELECT COUNT(*) FROM em_control.console_library_sources").fetchone()[0])
        pg_devs = int(pg_ctrl.execute("SELECT COUNT(*) FROM em_control.external_devices WHERE active").fetchone()[0])

    counts.update({
        "console_users_sqlite": sq_users, "console_users_pg": len(pg_users),
        "console_audit_sqlite": sq_audit, "console_audit_pg": pg_audit,
        "console_grants_sqlite": sq_grants, "console_grants_pg": pg_grants,
        "console_libraries_sqlite": sq_libs, "console_libraries_pg": pg_libs,
        "console_pending_sqlite": sq_pending, "console_pending_pg": pg_pending,
        "control_active_devices_pg": pg_devs,
    })
    for scope_name, exp_v, act_v, min_only in (
        ("em_control.console_users", sq_users, len(pg_users), False),
        ("em_control.console_grants", sq_grants, pg_grants, False),
        ("em_control.console_library_sources", sq_libs, pg_libs, False),
        ("em_control.console_audit", sq_audit, pg_audit, True),
        ("em_control.console_pending", sq_pending, pg_pending, True),
        ("em_control.external_devices", len(cfg.device_dbs), pg_devs, True),
    ):
        if (act_v < exp_v) if min_only else (act_v != exp_v):
            diffs.append({"scope": scope_name, "expected_min" if min_only else "expected": exp_v, "actual": act_v})

    content_digests_match = bool(digest_checks) and all(digest_checks.values())
    scrubbed = {
        "schema_version": "stage5.migration_reconciliation.v1",
        "ok": len(diffs) == 0 and content_digests_match,
        "unexplained_diff_count": len(diffs),
        "content_digests_match": content_digests_match,
        "digest_checks": digest_checks,
        "table_classification_total": manifest.get("total_tables", 0),
        "unclassified_count": len(manifest.get("unclassified_tables", [])),
        "counts": counts,
        "diffs": diffs,
        "llm_usage": {"calls": 0, "tokens": 0},
    }
    return {
        "ok": scrubbed["ok"], "unexplained_diff_count": len(diffs),
        "content_digests_match": content_digests_match, "digest_checks": digest_checks,
        "counts": counts, "diffs": diffs, "scrubbed": scrubbed,
    }
