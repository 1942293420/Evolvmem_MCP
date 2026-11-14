"""Deterministic (0 LLM) PG continuity backfill from 0600 CAS archives."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from psycopg.rows import dict_row

from evolvmem.history_pg.config import PgHistoryConfig, PgHistoryPool, load_pg_history_config
from evolvmem.transcript_formats import CODEX_FORMAT, parse_transcript, workspace_details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_from_details(declared: str, cwd: str) -> str:
    if declared and declared.strip():
        return declared.strip()[:128]
    if cwd and cwd.strip():
        tail = Path(cwd.replace("\\", "/")).name.strip()
        if tail:
            return tail[:128]
    return "general"


def run_continuity_backfill(
    cfg: PgHistoryConfig,
    user_id: str = "jiangli",
    *,
    limit: int = 50,
    pool: PgHistoryPool | None = None,
) -> dict[str, Any]:
    """Process pending PG uploads using 0600 CAS files with zero LLM calls."""
    pg_pool = pool or PgHistoryPool(cfg)
    dbname = cfg.resolve_user_history_db(user_id)
    processed = 0
    skipped = 0
    errors: list[str] = []

    with pg_pool.connection(dbname) as conn:
        conn.row_factory = dict_row
        pending = conn.execute(
            "SELECT u.device_id, u.session_id, u.sha256, u.project, u.declared_project, u.source_format, "
            "u.archive_id, a.payload_path "
            "FROM em_runtime.lan_session_uploads u "
            "JOIN em_runtime.session_archives a ON a.id = u.archive_id "
            "WHERE u.backfill_status = 'pending' AND u.archive_id IS NOT NULL "
            "ORDER BY u.received_at ASC LIMIT %s",
            (int(limit),),
        ).fetchall()

        for row in pending:
            dev_id = row["device_id"]
            sess_id = row["session_id"]
            sha = row["sha256"]
            cas_path = Path(row["payload_path"])
            if not cas_path.exists():
                skipped += 1
                errors.append("missing_cas_payload")
                continue
            raw = cas_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                skipped += 1
                errors.append("cas_sha256_mismatch")
                continue

            fmt = row["source_format"] or CODEX_FORMAT
            rows, dialogue = parse_transcript(raw, sess_id, fmt)
            det = workspace_details(rows, fmt)
            cwd = str(det.get("cwd") or "")
            proj = row["project"] or _project_from_details(row["declared_project"] or "", cwd)
            user_msgs = [m.get("content", "") for m in dialogue if m.get("role") == "user" and str(m.get("content", "")).strip()]
            first_q = str(user_msgs[0])[:160] if user_msgs else "Session captured"
            last_q = str(user_msgs[-1])[:160] if user_msgs else first_q

            now_utc = _utc_now()
            ws_fp = hashlib.sha256(f"{dev_id}::{cwd or proj}".encode("utf-8")).hexdigest()[:32]
            ident_key = f"backfill:{dev_id}:{sess_id}:{sha[:16]}"
            l0 = f"[{proj}] {first_q}"[:200]
            l1 = f"Session {sess_id} ({len(dialogue)} turns)\nInitial: {first_q}\nLatest: {last_q}"[:800]
            l2 = json.dumps(
                {"session_id": sess_id, "device_id": dev_id, "turns": len(dialogue), "source_format": fmt, "archive_id": int(row["archive_id"])},
                ensure_ascii=False,
            )

            conn.execute(
                "INSERT INTO em_runtime.context_project_registry (project, status, revision, created_at, updated_at, display_name) "
                "VALUES (%s,'active',1,%s,%s,%s) ON CONFLICT (project) DO NOTHING",
                (proj, now_utc, now_utc, proj),
            )
            existing_ci = conn.execute("SELECT id FROM em_runtime.context_items WHERE identity_key=%s", (ident_key,)).fetchone()
            if existing_ci is None:
                cid = int(
                    conn.execute(
                        "INSERT INTO em_runtime.context_items "
                        "(identity_key, content_type, project, scope, status, tier, tags, importance, confidence, "
                        "source_state, source_count, created_at, updated_at, experience_payload, projects, personal_general) "
                        "VALUES (%s,'session_backfill',%s,'project','active','normal','[]',6.0,0.85,'verified',1,%s,%s,'','[]',0) RETURNING id",
                        (ident_key, proj, now_utc, now_utc),
                    ).fetchone()["id"]
                )
            else:
                cid = int(existing_ci["id"])

            for layer_name, text in (("l0", l0), ("l1", l1), ("l2", l2)):
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO em_runtime.context_layers (item_id, layer, content, content_hash, generator, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,'pg_backfill_rule_v1',%s,%s)",
                    (cid, layer_name, text, h, now_utc, now_utc),
                )

            ws = conn.execute(
                "SELECT id, checkpoint_revision FROM em_runtime.continuity_workstreams WHERE project=%s AND workspace_fingerprint=%s AND status='open' LIMIT 1",
                (proj, ws_fp),
            ).fetchone()
            if ws is None:
                ws_id = f"ws-{uuid.uuid5(uuid.NAMESPACE_URL, f'{proj}:{ws_fp}:{sess_id}').hex[:16]}"
                after_rev = 1
                conn.execute(
                    "INSERT INTO em_runtime.continuity_workstreams "
                    "(id, project, workspace_fingerprint, current_context_id, checkpoint_revision, state_version, status, repo_kind, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,1,1,'open','non_git',%s,%s) ON CONFLICT (id) DO NOTHING",
                    (ws_id, proj, ws_fp, cid, now_utc, now_utc),
                )
            else:
                ws_id = ws["id"]
                after_rev = int(ws["checkpoint_revision"]) + 1
                conn.execute(
                    "UPDATE em_runtime.continuity_workstreams SET current_context_id=%s, checkpoint_revision=%s, updated_at=%s WHERE id=%s",
                    (cid, after_rev, now_utc, ws_id),
                )

            conn.execute(
                "INSERT INTO em_runtime.continuity_focus (project, workspace_fingerprint, workstream_id, revision, updated_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project, workspace_fingerprint) DO UPDATE SET workstream_id=EXCLUDED.workstream_id, revision=EXCLUDED.revision, updated_at=EXCLUDED.updated_at",
                (proj, ws_fp, ws_id, after_rev, now_utc),
            )
            conn.execute(
                "INSERT INTO em_runtime.lan_session_backfills (device_id, session_id, applied_revision, workstream_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (device_id, session_id) DO UPDATE SET applied_revision=EXCLUDED.applied_revision, workstream_id=EXCLUDED.workstream_id",
                (dev_id, sess_id, after_rev, ws_id),
            )
            bf_res = json.dumps({"workstream_id": ws_id, "context_id": cid, "turns": len(dialogue), "llm_calls": 0}, ensure_ascii=False)
            conn.execute(
                "UPDATE em_runtime.lan_session_uploads SET backfill_status='completed', backfill_result=%s, project=COALESCE(NULLIF(project,''), %s) "
                "WHERE device_id=%s AND session_id=%s AND sha256=%s",
                (bf_res, proj, dev_id, sess_id, sha),
            )
            conn.execute(
                "INSERT INTO em_runtime.lan_memory_revision (id, revision) VALUES (1, 1) "
                "ON CONFLICT (id) DO UPDATE SET revision = em_runtime.lan_memory_revision.revision + 1"
            )
            conn.execute(
                "INSERT INTO em_runtime.cutover_write_journal (library_key, request_id, actor_id, op_type, target_table, target_key, payload) "
                "VALUES (%s,%s,%s,'continuity_backfill','em_runtime.lan_session_backfills',%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (
                    user_id,
                    f"backfill-{sha[:16]}",
                    user_id,
                    f"{dev_id}:{sess_id}",
                    json.dumps(
                        {
                            "device_id": dev_id, "session_id": sess_id, "sha256": sha,
                            "workstream_id": ws_id, "context_id": cid, "applied_revision": after_rev, "project": proj,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            processed += 1

    return {"ok": True, "processed": processed, "skipped": skipped, "errors": errors, "llm_usage": {"calls": 0, "tokens": 0}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-config", default="")
    ap.add_argument("--user", default="jiangli")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    cfg = load_pg_history_config(args.pg_config or None)
    out = run_continuity_backfill(cfg, args.user, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
