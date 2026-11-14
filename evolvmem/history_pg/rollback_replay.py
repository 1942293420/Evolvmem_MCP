"""Section 11.2 single-library PG -> SQLite write-preserving journal replay."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from psycopg.rows import dict_row

from evolvmem.history_pg.config import PgHistoryConfig


def _has_sq_table(sq_conn: sqlite3.Connection, name: str) -> bool:
    return sq_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _replay_archive(pg_conn, sq_conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    aid = int(row["target_key"])
    arch = pg_conn.execute(
        "SELECT id, project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, purged_at, created_at "
        "FROM em_runtime.session_archives WHERE id = %s",
        (aid,),
    ).fetchone()
    if arch is None:
        return False
    sq_conn.execute(
        "INSERT INTO session_archives(id, project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, purged_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET project=excluded.project, state=excluded.state, payload_path=excluded.payload_path",
        (
            int(arch["id"]), arch["project"], arch["adapter"], arch["external_session_id"],
            arch["payload_path"], arch["payload_sha256"], arch["state"], arch["expires_at"], arch["purged_at"], arch["created_at"],
        ),
    )
    st = pg_conn.execute(
        "SELECT archive_id, manifest_sha256, content_sha256, logical_bytes FROM em_runtime.session_archive_storage WHERE archive_id = %s",
        (aid,),
    ).fetchone()
    if st is not None:
        sq_conn.execute(
            "INSERT INTO session_archive_storage(archive_id, manifest_sha256, content_sha256, logical_bytes) VALUES (?,?,?,?) "
            "ON CONFLICT(archive_id) DO NOTHING",
            (int(st["archive_id"]), st["manifest_sha256"], st["content_sha256"], int(st["logical_bytes"])),
        )
    ups = pg_conn.execute(
        "SELECT device_id, session_id, sha256, project, declared_project, attribution_reason, received_at, backfill_status, "
        "backfill_result, total_bytes, received_bytes, archive_id, extraction_status, extraction_result, error, source_order, extracted_at, source_format "
        "FROM em_runtime.lan_session_uploads WHERE archive_id = %s",
        (aid,),
    ).fetchall()
    for u in ups:
        sq_conn.execute(
            "INSERT INTO lan_session_uploads(device_id, session_id, sha256, project, declared_project, attribution_reason, received_at, "
            "backfill_status, backfill_result, total_bytes, received_bytes, archive_id, extraction_status, extraction_result, error, source_order, extracted_at, source_format) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(device_id, session_id, sha256) DO UPDATE SET received_bytes=excluded.received_bytes, archive_id=excluded.archive_id, "
            "project=excluded.project, backfill_status=excluded.backfill_status, extraction_status=excluded.extraction_status",
            (
                u["device_id"], u["session_id"], u["sha256"], u["project"], u["declared_project"], u["attribution_reason"],
                float(u["received_at"]), u["backfill_status"], u["backfill_result"], int(u["total_bytes"]), int(u["received_bytes"]),
                int(u["archive_id"]), u["extraction_status"], u["extraction_result"], u["error"], u["source_order"], u["extracted_at"], u["source_format"],
            ),
        )
        hd = pg_conn.execute(
            "SELECT device_id, session_id, sha256, source_order FROM em_runtime.lan_session_heads WHERE device_id=%s AND session_id=%s",
            (u["device_id"], u["session_id"]),
        ).fetchone()
        if hd is not None:
            sq_conn.execute(
                "INSERT INTO lan_session_heads(device_id, session_id, sha256, source_order) VALUES (?,?,?,?) "
                "ON CONFLICT(device_id, session_id) DO UPDATE SET sha256=excluded.sha256, source_order=excluded.source_order",
                (hd["device_id"], hd["session_id"], hd["sha256"], hd["source_order"]),
            )
    return sq_conn.execute("SELECT 1 FROM session_archives WHERE id = ?", (aid,)).fetchone() is not None


def _replay_assign(pg_conn, sq_conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    dev_id = str(p.get("device_id") or "")
    sess_id = str(p.get("session_id") or "")
    sha = str(p.get("sha256") or "")
    proj = str(p.get("project") or "")
    if not (dev_id and sess_id and sha and proj):
        return False
    sq_conn.execute(
        "UPDATE lan_session_uploads SET project=? WHERE device_id=? AND session_id=? AND sha256=?",
        (proj, dev_id, sess_id, sha),
    )
    aid = p.get("archive_id")
    if aid is None:
        up_sq = sq_conn.execute(
            "SELECT archive_id FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?",
            (dev_id, sess_id, sha),
        ).fetchone()
        if up_sq is not None and up_sq[0] is not None:
            aid = int(up_sq[0])
        else:
            up_pg = pg_conn.execute(
                "SELECT archive_id FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s AND sha256=%s",
                (dev_id, sess_id, sha),
            ).fetchone()
            if up_pg is not None and up_pg.get("archive_id") is not None:
                aid = int(up_pg["archive_id"])
    if aid is not None:
        sq_conn.execute("UPDATE session_archives SET project=? WHERE id=?", (proj, int(aid)))
    up_ok = sq_conn.execute(
        "SELECT 1 FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=? AND project=?",
        (dev_id, sess_id, sha, proj),
    ).fetchone() is not None
    if not up_ok:
        return False
    if aid is not None:
        has_sa = sq_conn.execute("SELECT 1 FROM session_archives WHERE id=?", (int(aid),)).fetchone() is not None
        if has_sa and sq_conn.execute("SELECT 1 FROM session_archives WHERE id=? AND project=?", (int(aid), proj)).fetchone() is None:
            return False
    return True


def _replay_continuity(pg_conn, sq_conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    ws_id = str(payload.get("workstream_id") or "")
    proj = str(payload.get("project") or "")
    if proj:
        pr = pg_conn.execute("SELECT project, status, revision, created_at, updated_at, display_name FROM em_runtime.context_project_registry WHERE project=%s", (proj,)).fetchone()
        if pr is not None:
            sq_conn.execute(
                "INSERT INTO context_project_registry(project, status, revision, created_at, updated_at, display_name) VALUES (?,?,?,?,?,?) ON CONFLICT(project) DO NOTHING",
                (pr["project"], pr["status"], int(pr["revision"]), pr["created_at"], pr["updated_at"], pr["display_name"]),
            )
    ws = pg_conn.execute("SELECT * FROM em_runtime.continuity_workstreams WHERE id=%s", (ws_id,)).fetchone() if ws_id else None
    if ws is None:
        return True
    cid = int(ws["current_context_id"])
    ci = pg_conn.execute("SELECT * FROM em_runtime.context_items WHERE id=%s", (cid,)).fetchone()
    if ci is not None:
        sq_conn.execute(
            "INSERT INTO context_items(id, identity_key, content_type, project, scope, status, tier, tags, importance, confidence, "
            "source_state, source_count, success_count, failure_count, access_count, last_accessed, last_verified_at, expires_at, "
            "supersedes, superseded_by, created_at, updated_at, experience_payload, effective_from, effective_until, occurred_at, mentioned_at, projects, personal_general) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, project=excluded.project, updated_at=excluded.updated_at",
            (
                int(ci["id"]), ci["identity_key"], ci["content_type"], ci["project"], ci["scope"], ci["status"], ci["tier"], ci["tags"],
                float(ci["importance"]), float(ci["confidence"]), ci["source_state"], int(ci["source_count"]), int(ci["success_count"]),
                int(ci["failure_count"]), int(ci["access_count"]), ci["last_accessed"], ci["last_verified_at"], ci["expires_at"],
                ci["supersedes"], ci["superseded_by"], ci["created_at"], ci["updated_at"], ci["experience_payload"], ci["effective_from"],
                ci["effective_until"], ci["occurred_at"], ci["mentioned_at"], ci["projects"], int(ci["personal_general"]),
            ),
        )
        for ly in pg_conn.execute("SELECT id, item_id, layer, content, content_hash, generator, created_at, updated_at FROM em_runtime.context_layers WHERE item_id=%s", (cid,)).fetchall():
            sq_conn.execute(
                "INSERT INTO context_layers(id, item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id, layer) DO UPDATE SET content=excluded.content, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                (int(ly["id"]), int(ly["item_id"]), ly["layer"], ly["content"], ly["content_hash"], ly["generator"], ly["created_at"], ly["updated_at"]),
            )
    sq_conn.execute(
        "INSERT INTO continuity_workstreams(id, project, workspace_fingerprint, parent_id, current_context_id, checkpoint_revision, state_version, status, "
        "repo_kind, repo_branch, repo_root_commit, repo_head_commit, created_at, updated_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET current_context_id=excluded.current_context_id, checkpoint_revision=excluded.checkpoint_revision, "
        "state_version=excluded.state_version, updated_at=excluded.updated_at",
        (
            ws["id"], ws["project"], ws["workspace_fingerprint"], ws["parent_id"], int(ws["current_context_id"]),
            int(ws["checkpoint_revision"]), int(ws["state_version"]), ws["status"], ws["repo_kind"], ws["repo_branch"],
            ws["repo_root_commit"], ws["repo_head_commit"], ws["created_at"], ws["updated_at"], ws["completed_at"],
        ),
    )
    fo = pg_conn.execute(
        "SELECT project, workspace_fingerprint, workstream_id, revision, updated_at FROM em_runtime.continuity_focus WHERE project=%s AND workspace_fingerprint=%s",
        (ws["project"], ws["workspace_fingerprint"]),
    ).fetchone()
    if fo is not None:
        sq_conn.execute(
            "INSERT INTO continuity_focus(project, workspace_fingerprint, workstream_id, revision, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(project, workspace_fingerprint) DO UPDATE SET workstream_id=excluded.workstream_id, revision=excluded.revision, updated_at=excluded.updated_at",
            (fo["project"], fo["workspace_fingerprint"], fo["workstream_id"], int(fo["revision"]), fo["updated_at"]),
        )
    if _has_sq_table(sq_conn, "continuity_events"):
        for ev in pg_conn.execute(
            "SELECT id, workstream_id, event_type, before_revision, after_revision, before_state_version, after_state_version, error_code, created_at "
            "FROM em_runtime.continuity_events WHERE workstream_id=%s ORDER BY id ASC",
            (ws_id,),
        ).fetchall():
            sq_conn.execute(
                "INSERT INTO continuity_events(id, workstream_id, event_type, before_revision, after_revision, before_state_version, after_state_version, error_code, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (
                    int(ev["id"]), ev["workstream_id"], ev["event_type"],
                    ev["before_revision"], ev["after_revision"], ev["before_state_version"],
                    ev["after_state_version"], ev["error_code"], ev["created_at"],
                ),
            )
    if row["op_type"] == "continuity_backfill" and payload.get("device_id") and payload.get("session_id"):
        bf = pg_conn.execute(
            "SELECT device_id, session_id, applied_revision, workstream_id FROM em_runtime.lan_session_backfills WHERE device_id=%s AND session_id=%s",
            (payload["device_id"], payload["session_id"]),
        ).fetchone()
        if bf is not None:
            sq_conn.execute(
                "INSERT INTO lan_session_backfills(device_id, session_id, applied_revision, workstream_id) VALUES (?,?,?,?) "
                "ON CONFLICT(device_id, session_id) DO UPDATE SET applied_revision=excluded.applied_revision, workstream_id=excluded.workstream_id",
                (bf["device_id"], bf["session_id"], int(bf["applied_revision"]), bf["workstream_id"]),
            )
    return sq_conn.execute("SELECT 1 FROM continuity_workstreams WHERE id = ?", (ws_id,)).fetchone() is not None


def _sync_runtime_aux(pg_conn, sq_conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    req_id = str(row.get("request_id") or "")
    if req_id and _has_sq_table(sq_conn, "lan_write_requests"):
        for w in pg_conn.execute(
            "SELECT user_id, tool, request_id, payload_hash, result FROM em_runtime.lan_write_requests WHERE request_id = %s",
            (req_id,),
        ).fetchall():
            sq_conn.execute(
                'INSERT INTO lan_write_requests("user", tool, request_id, payload_hash, result) '
                'VALUES (?,?,?,?,?) ON CONFLICT("user", tool, request_id) DO NOTHING',
                (w["user_id"], w["tool"], w["request_id"], w["payload_hash"], w["result"]),
            )
    if _has_sq_table(sq_conn, "lan_memory_revision"):
        rev_row = pg_conn.execute("SELECT revision FROM em_runtime.lan_memory_revision WHERE id = 1").fetchone()
        if rev_row is not None:
            sq_conn.execute(
                "INSERT INTO lan_memory_revision(id, revision) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET revision = MAX(lan_memory_revision.revision, excluded.revision)",
                (int(rev_row["revision"]),),
            )


def _replay_management(pg_conn, mgmt_conn: sqlite3.Connection | None, row: dict[str, Any]) -> bool:
    if mgmt_conn is None:
        return False
    p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    kind, proj, key = str(p.get("kind") or ""), str(p.get("project") or ""), str(p.get("key") or "")
    op_type = str(row["op_type"])
    if op_type == "history_manage_delete":
        mgmt_conn.execute(
            "INSERT INTO trash(kind, project, key, title, deleted_at) VALUES (?,?,?,?,?) ON CONFLICT(kind, project, key) DO NOTHING",
            (kind, proj, key, str(p.get("title") or ""), str(p.get("deleted_at") or "")),
        )
    elif op_type == "history_manage_restore":
        mgmt_conn.execute("DELETE FROM trash WHERE kind=? AND project=? AND key=?", (kind, proj, key))
    else:
        return False
    op = pg_conn.execute("SELECT request_id, fingerprint, result, created_at FROM em_history.management_operations WHERE request_id=%s", (row["request_id"],)).fetchone()
    if op is not None:
        mgmt_conn.execute(
            "INSERT INTO operations(request_id, fingerprint, result, created_at) VALUES (?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
            (op["request_id"], op["fingerprint"], op["result"], op["created_at"]),
        )
    has_op = mgmt_conn.execute("SELECT 1 FROM operations WHERE request_id=?", (row["request_id"],)).fetchone() is not None
    has_trash = mgmt_conn.execute("SELECT 1 FROM trash WHERE kind=? AND project=? AND key=?", (kind, proj, key)).fetchone() is not None
    return bool(has_op and (has_trash if op_type == "history_manage_delete" else not has_trash))


def replay_library_journal_to_sqlite(
    cfg: PgHistoryConfig,
    dbname: str,
    library_key: str,
    *,
    runtime_sqlite_path: Path,
    management_sqlite_path: Path | None = None,
) -> dict[str, Any]:
    """Replay un-replayed PG writes for one library back to its disposable SQLite copy."""
    replayed_seqs: list[int] = []
    lost_writes = 0
    by_op: dict[str, int] = {}

    sq_conn = sqlite3.connect(str(runtime_sqlite_path), timeout=10.0)
    mgmt_conn = sqlite3.connect(str(management_sqlite_path), timeout=10.0) if management_sqlite_path is not None else None
    try:
        with cfg.connect(dbname, role_kind="writer", statement_timeout_ms=60000, lock_timeout_ms=10000) as pg_conn:
            pg_conn.row_factory = dict_row
            rows = pg_conn.execute(
                "SELECT seq, library_key, request_id, op_type, target_table, target_key, payload "
                "FROM em_runtime.cutover_write_journal "
                "WHERE library_key = ANY(%s) AND NOT replayed_to_sqlite ORDER BY seq ASC",
                ([library_key, dbname],),
            ).fetchall()
            for r in rows:
                op = str(r["op_type"])
                ok = False
                is_rt = False
                if op == "session_archive_upload":
                    ok, is_rt = _replay_archive(pg_conn, sq_conn, r), True
                elif op == "session_archive_assign":
                    ok, is_rt = _replay_assign(pg_conn, sq_conn, r), True
                elif op in ("continuity_begin", "continuity_checkpoint", "continuity_backfill"):
                    ok, is_rt = _replay_continuity(pg_conn, sq_conn, r), True
                elif op in ("history_manage_delete", "history_manage_restore"):
                    ok = _replay_management(pg_conn, mgmt_conn, r)
                if ok and is_rt:
                    _sync_runtime_aux(pg_conn, sq_conn, r)
                if not ok:
                    lost_writes += 1
                else:
                    replayed_seqs.append(int(r["seq"]))
                    by_op[op] = by_op.get(op, 0) + 1
            sq_conn.commit()
            if mgmt_conn is not None:
                mgmt_conn.commit()
            if replayed_seqs:
                pg_conn.execute("UPDATE em_runtime.cutover_write_journal SET replayed_to_sqlite = true WHERE seq = ANY(%s)", (replayed_seqs,))
                pg_conn.commit()
    finally:
        sq_conn.close()
        if mgmt_conn is not None:
            mgmt_conn.close()

    return {"ok": lost_writes == 0, "replayed_count": len(replayed_seqs), "lost_writes": lost_writes, "by_op": by_op}
