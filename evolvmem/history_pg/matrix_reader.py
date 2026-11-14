"""PG-backed read_matrix for Knowledge Console (/matrix) with legacy SQLite fallback when PG disabled."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from evolvmem.experience_v2.pg_experience_store import load_pg_experience_settings, open_experience_conn
from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config
from evolvmem.product_policy import load_policy


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def read_matrix(
    actor: dict[str, Any],
    source_row: dict[str, Any] | None,
    owner_data_dir: Path,
    lan_data_dir: Path,
    lan_peers: list[dict[str, Any]],
    *,
    pg_config: PgHistoryConfig | None = None,
) -> dict[str, Any]:
    hist_env: dict[str, Any] = {}
    if source_row and isinstance(source_row.get("history_environ"), str):
        try:
            hist_env = json.loads(source_row["history_environ"] or "{}")
        except ValueError:
            hist_env = {}
    elif source_row and isinstance(source_row.get("history_environ"), dict):
        hist_env = dict(source_row["history_environ"])

    policy = load_policy()
    if pg_config is None and not getattr(policy, "history_pg_enabled", False) and not hist_env.get("EVOLVMEM_HISTORY_PG_DB"):
        from evolvmem.neural_matrix import read_matrix as sqlite_read_matrix

        return sqlite_read_matrix(actor, source_row, owner_data_dir, lan_data_dir, lan_peers)

    cfg = pg_config if pg_config is not None else load_pg_history_config()
    actor_id = str(actor.get("id") or "")
    target_id = str((source_row or {}).get("id") or actor_id or "").strip()
    explicit_db = str(hist_env.get("EVOLVMEM_HISTORY_PG_DB") or "").strip()
    if explicit_db:
        dbname = explicit_db
    elif target_id:
        dbname = cfg.resolve_user_history_db(target_id)
    else:
        raise ValueError("missing_target_user_id")

    is_admin = bool(actor.get("is_admin"))
    catalog_rows: list[dict[str, Any]] = []
    catalog_categories: list[dict[str, Any]] = []
    catalog_sync: dict[str, Any] = {}
    if is_admin:
        try:
            from evolvmem.skill_library import catalog_root, read_catalog

            root = catalog_root(owner_data_dir)
            if root.is_dir():
                cat = read_catalog(root, {})
                catalog_rows = list(cat.get("rows") or [])
                catalog_categories = list(cat.get("categories") or [])
                catalog_sync = dict(cat.get("sync") or {})
        except Exception:
            catalog_rows = []

    counts: dict[str, int] = {
        "session_archives": 0, "lan_uploads": 0, "lan_heads": 0, "lan_backfills": 0,
        "lan_writes": 0, "local_capture_files": 0, "context_items": 0, "memories": 0,
        "capability_memories": 0, "manual_active": 0, "history_cards": 0,
        "continuity_focus": 0, "continuity_workstreams": 0,
        "rules_active": 0, "rules_candidate": 0, "fact_conflicts": 0,
        "skill_proposals": 0, "category_suggestions": 0,
    }
    agents: list[dict[str, Any]] = []
    handoffs: list[dict[str, Any]] = []
    write_receipts: list[dict[str, Any]] = []

    with cfg.connect(dbname, role_kind="reader") as conn:
        for key, tbl in (
            ("session_archives", "em_runtime.session_archives"),
            ("lan_uploads", "em_runtime.lan_session_uploads"),
            ("lan_heads", "em_runtime.lan_session_heads"),
            ("lan_backfills", "em_runtime.lan_session_backfills"),
            ("lan_writes", "em_runtime.lan_write_requests"),
            ("local_capture_files", "em_runtime.local_capture_files"),
            ("context_items", "em_runtime.context_items"),
            ("continuity_focus", "em_runtime.continuity_focus"),
            ("continuity_workstreams", "em_runtime.continuity_workstreams"),
        ):
            counts[key] = int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] or 0)
        counts["memories"] = int(conn.execute(
            "SELECT COUNT(*) FROM em_history.blocks b "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM em_history.management_trash t "
            "  WHERE t.kind = 'block' AND t.key = b.block_id"
            ") AND NOT ("
            "  EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM em_history.block_projects bp "
            "    WHERE bp.block_id = b.block_id AND NOT EXISTS ("
            "      SELECT 1 FROM em_history.management_trash tp "
            "      WHERE tp.kind = 'project' AND COALESCE(NULLIF(tp.project, ''), tp.key) = bp.project"
            "    )"
            "  )"
            ")"
        ).fetchone()[0] or 0)

        for did, cnt, latest, sample_sha in conn.execute(
            "SELECT device_id, COUNT(*), MAX(source_order), MAX(sha256) "
            "FROM em_runtime.lan_session_heads GROUP BY device_id ORDER BY COUNT(*) DESC LIMIT 8"
        ).fetchall():
            agents.append({
                "id": str(did), "name": f"Device {str(did)[:12]}", "role": "LAN PG Client",
                "status": "online", "sessionsSynced": int(cnt or 0), "backfillsApplied": 0,
                "lastPulse": str(latest or ""), "headShaPrefix": str(sample_sha or "")[:16],
                "transport": "PostgreSQL + LAN MCP CAS",
            })

        for proj, ws_id, rev, upd in conn.execute(
            "SELECT project, workstream_id, revision, updated_at FROM em_runtime.continuity_focus ORDER BY updated_at DESC LIMIT 12"
        ).fetchall():
            handoffs.append({
                "project": str(proj or ""), "workstreamId": str(ws_id or ""),
                "revision": int(rev or 0), "updatedAt": str(upd or ""), "verified": True,
            })

        for tool, req_id, ph in conn.execute(
            "SELECT tool, request_id, payload_hash FROM em_runtime.lan_write_requests ORDER BY created_at DESC LIMIT 10"
        ).fetchall():
            write_receipts.append({
                "tool": str(tool), "requestId": str(req_id),
                "payloadHash": str(ph or "")[:16], "meta": "PostgreSQL idempotent",
            })

    exp_status = "ok"
    exp_error = ""
    try:
        exp_settings = load_pg_experience_settings()
        with open_experience_conn(exp_settings, role="reader") as econn:
            row = econn.execute(
                "SELECT COUNT(*) FROM experience_notes WHERE owner_id = %s AND deleted_at IS NULL",
                (target_id,),
            ).fetchone()
            counts["manual_active"] = int(row[0] or 0) if row else 0
    except Exception as exc:
        exp_status = "degraded"
        exp_error = exc.__class__.__name__

    funnel = [
        {"stage": "L0 · Multi-device Archives (PG)", "count": max(counts["session_archives"], counts["lan_uploads"]), "desc": f"Uploads {counts['lan_uploads']} · Archives {counts['session_archives']}"},
        {"stage": "L1 · Context & Blocks (PG)", "count": max(counts["context_items"], counts["memories"]), "desc": f"Context {counts['context_items']} · Blocks {counts['memories']}"},
        {"stage": "L2 · Verified Experience (PG)", "count": counts["manual_active"], "desc": f"Manual Notes {counts['manual_active']}"},
        {"stage": "L3 · Active Continuity", "count": counts["continuity_workstreams"], "desc": f"Workstreams {counts['continuity_workstreams']}"},
        {"stage": "L4 · Skill Catalog", "count": len(catalog_rows), "desc": f"Skills {len(catalog_rows)}"},
    ]

    return {
        "ok": True,
        "generatedAt": _now_iso(),
        "pgBacked": True,
        "stats": {
            "onlineAgents": len(agents),
            "totalAgents": len(agents),
            "lanSessionHeads": counts["lan_heads"],
            "lanUploads": counts["lan_uploads"],
            "lanWrites": counts["lan_writes"],
            "continuityFocusCount": counts["continuity_focus"],
            "workstreamsCount": counts["continuity_workstreams"],
            "manualExperienceCount": counts["manual_active"],
            "historyExperienceCount": counts["history_cards"],
            "capabilityMemoryCount": counts["capability_memories"],
            "coreMemoryCount": counts["memories"],
            "pendingEvolutionCount": 0,
            "candidateRulesCount": 0,
            "factConflictsCount": 0,
            "skillCatalogCount": len(catalog_rows),
            "activeRulesCount": 0,
        },
        "agents": agents,
        "handoffs": handoffs,
        "writeReceipts": write_receipts,
        "funnel": funnel,
        "evolutionQueue": [],
        "nebula": {"memories": [], "skills": [], "nebulaLinks": [], "evolutionQueue": []},
        "experienceNotesStatus": exp_status,
        "experienceNotesError": exp_error,
        "skillCategories": catalog_categories,
        "skillSync": catalog_sync,
    }
