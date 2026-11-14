"""Idempotent SQLite -> PG runtime importer and local 768-dim BGE backfill."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any
import psycopg

from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.migrator_control_history import (
    _f,
    _i,
    _s,
    bulk_exec,
    import_control,
    import_organized_history,
    ro_sqlite,
)


def _vec_lit(vec: list[float] | None) -> str | None:
    return None if vec is None else "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def _has_tbl(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None


def _import_context_and_continuity(conn: sqlite3.Connection, pg_conn: psycopg.Connection, counts: dict[str, int]) -> None:
    if _has_tbl(conn, "local_capture_files"):
        counts["local_capture_files"] = bulk_exec(
            pg_conn,
            "INSERT INTO em_runtime.local_capture_files(id,session_id,path,device,inode,scanned_offset_bytes,scanned_line,pending_bytes,since_floor,file_size,file_mtime,anchor_offset,anchor_bytes,anchor_sha256,file_state,blocked_cap_bytes,archived_offset_bytes,archived_line,archived_batches,archived_events,filtered_events,failed_batches,rewrite_count,last_error,last_success_at,last_error_at,first_seen_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            ((_i(r[0]),_s(r[1]),_s(r[2]),_i(r[3]),_i(r[4]),_i(r[5]),_i(r[6]),_i(r[7]),_f(r[8]),_i(r[9]),_f(r[10]),_i(r[11]),_i(r[12]),_s(r[13]),_s(r[14],"advancing"),_i(r[15]),_i(r[16]),_i(r[17]),_i(r[18]),_i(r[19]),_i(r[20]),_i(r[21]),_i(r[22]),_s(r[23]),_s(r[24]),_s(r[25]),_s(r[26]),_s(r[27]))
             for r in conn.execute("SELECT id,session_id,path,device,inode,scanned_offset_bytes,scanned_line,pending_bytes,since_floor,file_size,file_mtime,anchor_offset,anchor_bytes,anchor_sha256,file_state,blocked_cap_bytes,archived_offset_bytes,archived_line,archived_batches,archived_events,filtered_events,failed_batches,rewrite_count,last_error,last_success_at,last_error_at,first_seen_at,updated_at FROM local_capture_files")),
        )
    if _has_tbl(conn, "local_capture_scans"):
        counts["local_capture_scans"] = bulk_exec(
            pg_conn,
            "INSERT INTO em_runtime.local_capture_scans(id,last_scan_at,last_success_at,last_error,scans,scanned_bytes,archived_batches,archived_events,filtered_events) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            ((_s(r[0]),_s(r[1]),_s(r[2]),_i(r[3]),_i(r[4]),_i(r[5]),_i(r[6]),_i(r[7])) for r in conn.execute("SELECT last_scan_at,last_success_at,last_error,scans,scanned_bytes,archived_batches,archived_events,filtered_events FROM local_capture_scans WHERE id=1")),
        )
    if _has_tbl(conn, "context_project_registry"):
        counts["context_project_registry"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_project_registry(project,status,revision,created_at,updated_at,display_name) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1],"active"),_i(r[2],1),_s(r[3]),_s(r[4]),_s(r[5])) for r in conn.execute("SELECT project,status,revision,created_at,updated_at,display_name FROM context_project_registry")))
    if _has_tbl(conn, "context_project_aliases"):
        counts["context_project_aliases"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_project_aliases(alias,project,revision,created_at,updated_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_i(r[2],1),_s(r[3]),_s(r[4])) for r in conn.execute("SELECT alias,project,revision,created_at,updated_at FROM context_project_aliases")))
    if _has_tbl(conn, "context_project_workspace_bindings"):
        counts["context_project_workspace_bindings"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_project_workspace_bindings(workspace_fingerprint,project,state,is_default,method,revision,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_s(r[2],"candidate"),_i(r[3]),_s(r[4]),_i(r[5],1),_s(r[6]),_s(r[7])) for r in conn.execute("SELECT workspace_fingerprint,project,state,is_default,method,revision,created_at,updated_at FROM context_project_workspace_bindings")))
    if _has_tbl(conn, "context_items"):
        counts["context_items"] = bulk_exec(
            pg_conn,
            "INSERT INTO em_runtime.context_items(id,identity_key,content_type,project,scope,status,tier,tags,importance,confidence,source_state,source_count,success_count,failure_count,access_count,last_accessed,last_verified_at,expires_at,supersedes,superseded_by,created_at,updated_at,experience_payload,effective_from,effective_until,occurred_at,mentioned_at,projects,personal_general) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            ((_i(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4],"project"),_s(r[5],"candidate"),_s(r[6],"normal"),_s(r[7]),_f(r[8],5.0),_f(r[9],0.5),_s(r[10],"none"),_i(r[11]),_i(r[12]),_i(r[13]),_i(r[14]),r[15],r[16],r[17],r[18],r[19],_s(r[20]),_s(r[21]),_s(r[22]),r[23],r[24],r[25],r[26],_s(r[27],"[]"),_i(r[28]))
             for r in conn.execute("SELECT id,identity_key,content_type,project,scope,status,tier,tags,importance,confidence,source_state,source_count,success_count,failure_count,access_count,last_accessed,last_verified_at,expires_at,supersedes,superseded_by,created_at,updated_at,experience_payload,effective_from,effective_until,occurred_at,mentioned_at,projects,personal_general FROM context_items")),
        )
    if _has_tbl(conn, "context_layers"):
        counts["context_layers"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_layers(id,item_id,layer,content,content_hash,generator,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", ((_i(r[0]),_i(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_s(r[5]),_s(r[6]),_s(r[7])) for r in conn.execute("SELECT id,item_id,layer,content,content_hash,generator,created_at,updated_at FROM context_layers")))
    if _has_tbl(conn, "context_project_resolutions"):
        counts["context_project_resolutions"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_project_resolutions(item_id,resolution_state,decision_source,review_state,proposed_project,resolved_project,confidence,method,evidence_json,resolver_version,revision,reviewed_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING", ((_i(r[0]),_s(r[1]),_s(r[2],"none"),_s(r[3],"not_required"),_s(r[4]),_s(r[5]),_s(r[6],"none"),_s(r[7]),_s(r[8],"[]"),_s(r[9]),_i(r[10],1),r[11],_s(r[12]),_s(r[13])) for r in conn.execute("SELECT item_id,resolution_state,decision_source,review_state,proposed_project,resolved_project,confidence,method,evidence_json,resolver_version,revision,reviewed_at,created_at,updated_at FROM context_project_resolutions")))
    if _has_tbl(conn, "context_sources"):
        counts["context_sources"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.context_sources(id,item_id,archive_id,source_kind,source_ref,extraction_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", ((_i(r[0]),_i(r[1]),r[2],_s(r[3]),_s(r[4]),_s(r[5]),_s(r[6])) for r in conn.execute("SELECT id,item_id,archive_id,source_kind,source_ref,extraction_version,created_at FROM context_sources")))
    if _has_tbl(conn, "continuity_workstreams"):
        counts["continuity_workstreams"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.continuity_workstreams(id,project,workspace_fingerprint,parent_id,current_context_id,checkpoint_revision,state_version,status,repo_kind,repo_branch,repo_root_commit,repo_head_commit,created_at,updated_at,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET checkpoint_revision=GREATEST(em_runtime.continuity_workstreams.checkpoint_revision, EXCLUDED.checkpoint_revision)", ((_s(r[0]),_s(r[1]),_s(r[2]),r[3],_i(r[4]),_i(r[5]),_i(r[6]),_s(r[7]),_s(r[8],"non_git"),_s(r[9]),_s(r[10]),_s(r[11]),_s(r[12]),_s(r[13]),r[14]) for r in conn.execute("SELECT id,project,workspace_fingerprint,parent_id,current_context_id,checkpoint_revision,state_version,status,repo_kind,repo_branch,repo_root_commit,repo_head_commit,created_at,updated_at,completed_at FROM continuity_workstreams")))
    if _has_tbl(conn, "continuity_focus"):
        counts["continuity_focus"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.continuity_focus(project,workspace_fingerprint,workstream_id,revision,updated_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project,workspace_fingerprint) DO UPDATE SET workstream_id=EXCLUDED.workstream_id, revision=GREATEST(em_runtime.continuity_focus.revision, EXCLUDED.revision)", ((_s(r[0]),_s(r[1]),r[2],_i(r[3]),_s(r[4])) for r in conn.execute("SELECT project,workspace_fingerprint,workstream_id,revision,updated_at FROM continuity_focus")))
    if _has_tbl(conn, "continuity_events"):
        counts["continuity_events"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.continuity_events(id,workstream_id,event_type,before_revision,after_revision,before_state_version,after_state_version,error_code,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", ((_i(r[0]),r[1],_s(r[2]),r[3],r[4],r[5],r[6],_s(r[7]),_s(r[8])) for r in conn.execute("SELECT id,workstream_id,event_type,before_revision,after_revision,before_state_version,after_state_version,error_code,created_at FROM continuity_events")))


def import_runtime_db(sqlite_path: str | Path, pg_conn: psycopg.Connection, *, dev_filter: str | None = None) -> dict[str, int]:
    conn = ro_sqlite(sqlite_path)
    counts: dict[str, int] = {}
    try:
        dev_clause = " WHERE device_id = ?" if dev_filter is not None else ""
        dev_args: tuple[Any, ...] = (dev_filter,) if dev_filter is not None else ()
        allowed_ids: set[int] | None = None
        if dev_filter is not None and _has_tbl(conn, "lan_session_uploads"):
            allowed_ids = {
                int(r[0]) for r in conn.execute("SELECT DISTINCT archive_id FROM lan_session_uploads WHERE device_id = ? AND archive_id IS NOT NULL", (dev_filter,))
            }

        if _has_tbl(conn, "session_archives"):
            sa_rows = conn.execute("SELECT id,project,adapter,external_session_id,payload_path,payload_sha256,state,expires_at,purged_at,created_at FROM session_archives").fetchall()
            if allowed_ids is not None:
                sa_rows = [r for r in sa_rows if int(r[0]) in allowed_ids]
            counts["session_archives"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.session_archives(id,project,adapter,external_session_id,payload_path,payload_sha256,state,expires_at,purged_at,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                ((_i(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), _s(r[4]), _s(r[5]), _s(r[6], "available"), _s(r[7]), r[8], _s(r[9])) for r in sa_rows),
            )
        if _has_tbl(conn, "session_archive_storage"):
            sas_rows = conn.execute("SELECT archive_id,manifest_sha256,content_sha256,logical_bytes FROM session_archive_storage").fetchall()
            if allowed_ids is not None:
                sas_rows = [r for r in sas_rows if int(r[0]) in allowed_ids]
            counts["session_archive_storage"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.session_archive_storage(archive_id,manifest_sha256,content_sha256,logical_bytes) VALUES (%s,%s,%s,%s) ON CONFLICT (archive_id) DO NOTHING",
                ((_i(r[0]), _s(r[1]), _s(r[2]), _i(r[3])) for r in sas_rows),
            )
        if _has_tbl(conn, "lan_session_uploads"):
            counts["lan_session_uploads"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.lan_session_uploads(device_id,session_id,sha256,project,declared_project,attribution_reason,received_at,backfill_status,backfill_result,total_bytes,received_bytes,archive_id,extraction_status,extraction_result,error,source_order,extracted_at,source_format) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (device_id,session_id,sha256) DO UPDATE SET received_bytes=EXCLUDED.received_bytes, extraction_status=EXCLUDED.extraction_status",
                ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_s(r[5]),_f(r[6]),_s(r[7],"pending"),_s(r[8],"{}"),_i(r[9]),_i(r[10]),r[11],_s(r[12],"not_requested"),_s(r[13],"{}"),_s(r[14]),r[15],r[16],_s(r[17],"codex-jsonl"))
                 for r in conn.execute(f"SELECT device_id,session_id,sha256,project,declared_project,attribution_reason,received_at,backfill_status,backfill_result,total_bytes,received_bytes,archive_id,extraction_status,extraction_result,error,source_order,extracted_at,source_format FROM lan_session_uploads{dev_clause}", dev_args)),
            )
        if _has_tbl(conn, "lan_session_heads"):
            counts["lan_session_heads"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.lan_session_heads(device_id,session_id,sha256,source_order) VALUES (%s,%s,%s,%s) ON CONFLICT (device_id,session_id) DO UPDATE SET sha256=EXCLUDED.sha256, source_order=EXCLUDED.source_order",
                ((_s(r[0]), _s(r[1]), r[2], r[3]) for r in conn.execute(f"SELECT device_id,session_id,sha256,source_order FROM lan_session_heads{dev_clause}", dev_args)),
            )
        if _has_tbl(conn, "lan_session_backfills"):
            counts["lan_session_backfills"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.lan_session_backfills(device_id,session_id,applied_revision,workstream_id) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                ((_s(r[0]), _s(r[1]), _i(r[2]), _s(r[3])) for r in conn.execute(f"SELECT device_id,session_id,applied_revision,workstream_id FROM lan_session_backfills{dev_clause}", dev_args)),
            )
        if _has_tbl(conn, "lan_memory_revision"):
            counts["lan_memory_revision"] = bulk_exec(
                pg_conn,
                "INSERT INTO em_runtime.lan_memory_revision(id, revision) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET revision = GREATEST(em_runtime.lan_memory_revision.revision, EXCLUDED.revision)",
                ((_i(r[0], 0),) for r in conn.execute("SELECT revision FROM lan_memory_revision WHERE id=1")),
            )

        if dev_filter is None:
            for tbl, col_map in (
                ("session_archive_holds", ("archive_id", "source_context_id", "reason", "created_at")),
                ("archive_source_exclusions", ("archive_id", "payload_sha256", "reason", "verified_at")),
                ("conversation_history", ("archive_id", "messages", "body", "content_hash", "cleaned_at")),
            ):
                if _has_tbl(conn, tbl):
                    ph = ",".join(["%s"] * len(col_map))
                    cols_sql = ",".join(col_map)
                    counts[tbl] = bulk_exec(pg_conn, f"INSERT INTO em_runtime.{tbl}({cols_sql}) VALUES ({ph}) ON CONFLICT DO NOTHING", (tuple(r) for r in conn.execute(f"SELECT {cols_sql} FROM {tbl}")))
            if _has_tbl(conn, "lan_write_requests"):
                counts["lan_write_requests"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.lan_write_requests(user_id,tool,request_id,payload_hash,result) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]), _s(r[1]), _s(r[2]), _s(r[3]), r[4]) for r in conn.execute('SELECT "user",tool,request_id,payload_hash,result FROM lan_write_requests')))
            if _has_tbl(conn, "lan_workspace_bindings"):
                counts["lan_workspace_bindings"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.lan_workspace_bindings(remote_key,fingerprint,workstream_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]), _s(r[1]), _s(r[2])) for r in conn.execute("SELECT remote_key,fingerprint,workstream_id FROM lan_workspace_bindings")))
            _import_context_and_continuity(conn, pg_conn, counts)
            if _has_tbl(conn, "task_history_grants"):
                counts["task_history_grants"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.task_history_grants(request_id,identity_hash,archive_id,source_sha256,source_revision,project,source_format,task_id,state,result,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (tuple(r) for r in conn.execute("SELECT request_id,identity_hash,archive_id,source_sha256,source_revision,project,source_format,task_id,state,result,created_at FROM task_history_grants")))
            if _has_tbl(conn, "lan_publications"):
                counts["lan_publications"] = bulk_exec(pg_conn, "INSERT INTO em_runtime.lan_publications(context_id,author,active,title,summary,project) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_i(r[0]), _s(r[1]), _i(r[2], 1), _s(r[3]), _s(r[4]), _s(r[5])) for r in conn.execute("SELECT context_id,author,active,title,summary,project FROM lan_publications")))

        for seq_tbl in ("session_archives", "local_capture_files", "context_items", "context_layers", "context_sources", "continuity_events"):
            pg_conn.execute(f"SELECT setval(pg_get_serial_sequence('em_runtime.{seq_tbl}', 'id'), COALESCE((SELECT MAX(id) FROM em_runtime.{seq_tbl}), 1), true)")
        pg_conn.commit()
        return counts
    finally:
        conn.close()


def backfill_message_embeddings(cfg: PgHistoryConfig, dbname: str, embedder: Any, *, limit: int = 256) -> int:
    if limit <= 0:
        return 0
    with psycopg.connect(cfg.admin_dsn(dbname)) as conn:
        rows = conn.execute(
            "SELECT block_id, ordinal, content FROM em_history.messages WHERE embedding IS NULL AND length(btrim(content)) > 0 "
            "ORDER BY CASE WHEN role = 'user' THEN 0 ELSE 1 END, block_id, ordinal LIMIT %s",
            (int(limit),),
        ).fetchall()
        if not rows:
            return 0
        vecs = embedder.encode_documents([str(c) for _, _, c in rows])
        updated = bulk_exec(conn, "UPDATE em_history.messages SET embedding = %s::vector WHERE block_id = %s AND ordinal = %s", ((_vec_lit(v), bid, ord_) for (bid, ord_, _), v in zip(rows, vecs) if v is not None))
        conn.commit()
        return updated


def embed_projects_and_blocks(cfg: PgHistoryConfig, dbname: str, embedder: Any, *, message_embed_limit: int = 256) -> dict[str, int]:
    with psycopg.connect(cfg.admin_dsn(dbname)) as conn:
        proj_rows = conn.execute("SELECT project, display_name, summary FROM em_history.projects WHERE embedding IS NULL ORDER BY project").fetchall()
        p_upd = 0
        if proj_rows:
            p_vecs = embedder.encode_documents([f"{p} {d} {s}".strip() for p, d, s in proj_rows])
            p_upd = bulk_exec(conn, "UPDATE em_history.projects SET embedding = %s::vector WHERE project = %s", ((_vec_lit(v), p) for (p, _, _), v in zip(proj_rows, p_vecs) if v is not None))

        blk_rows = conn.execute(
            "SELECT b.block_id, b.title, b.projects, COALESCE((SELECT string_agg(m.content, ' ') FROM (SELECT content FROM em_history.messages m WHERE m.block_id = b.block_id ORDER BY ordinal LIMIT 3) m), '') "
            "FROM em_history.blocks b WHERE b.embedding IS NULL ORDER BY b.block_id"
        ).fetchall()
        b_upd = 0
        if blk_rows:
            b_vecs = embedder.encode_documents([f"{t} {pr} {ex[:300]}".strip() for _, t, pr, ex in blk_rows])
            b_upd = bulk_exec(conn, "UPDATE em_history.blocks SET embedding = %s::vector WHERE block_id = %s", ((_vec_lit(v), bid) for (bid, _, _, _), v in zip(blk_rows, b_vecs) if v is not None))
        conn.commit()
    m_upd = backfill_message_embeddings(cfg, dbname, embedder, limit=message_embed_limit) if message_embed_limit > 0 else 0
    return {"projects_embedded": p_upd, "blocks_embedded": b_upd, "messages_embedded": m_upd}


def import_all_snapshots(cfg: PgHistoryConfig, snapshots: dict[str, dict[str, Any]], *, embedder: Any | None = None, message_embed_limit: int = 256) -> dict[str, Any]:
    cfg.validate_non_prod_target()
    with psycopg.connect(cfg.admin_dsn(cfg.history_migration_db)) as pg_hist:
        ctrl_counts = import_control(cfg, snapshots, pg_hist)
        hist_counts = import_organized_history(snapshots, pg_hist)
        rt_owner_counts = import_runtime_db(snapshots["memory_owner"]["snapshot_path"], pg_hist)

    emb_counts = embed_projects_and_blocks(cfg, cfg.history_migration_db, embedder, message_embed_limit=message_embed_limit) if embedder is not None else {}

    dev_counts: dict[str, Any] = {}
    for dev_id, dev_db in sorted(cfg.device_dbs.items()):
        with psycopg.connect(cfg.admin_dsn(dev_db)) as pg_dev:
            dev_counts[dev_db] = import_runtime_db(snapshots["memory_owner"]["snapshot_path"], pg_dev, dev_filter=dev_id)

    with psycopg.connect(cfg.admin_dsn(cfg.shared_public_db)) as pg_pub:
        pub_counts = import_runtime_db(snapshots["lan_public"]["snapshot_path"], pg_pub)

    kane_db = cfg.resolve_user_history_db("kane")
    with psycopg.connect(cfg.admin_dsn(kane_db)) as pg_kane:
        kane_counts = import_runtime_db(snapshots["lan_kane"]["snapshot_path"], pg_kane)

    return {
        "ok": True, "control": ctrl_counts, "organized_history": hist_counts, "runtime_owner": rt_owner_counts,
        "embeddings": emb_counts, "device_dbs": len(dev_counts), "shared_public": pub_counts, "kane": kane_counts,
        "llm_usage": {"calls": 0, "tokens": 0},
    }
