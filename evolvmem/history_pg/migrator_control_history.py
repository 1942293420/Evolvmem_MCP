"""Idempotent migrator helpers for em_control and em_history schemas."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Iterable
import uuid
import psycopg

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.history_pg.config import PgHistoryConfig

_DEV_NS = uuid.UUID("c1d9e44b-1b7c-4f21-8b3d-82e7b11a5a09")


def _s(v: Any, default: str = "") -> str:
    return default if v is None else str(v)


def _i(v: Any, default: int = 0) -> int:
    return default if v is None else int(v)


def _f(v: Any, default: float = 0.0) -> float:
    return default if v is None else float(v)


def ro_sqlite(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def bulk_exec(pg_conn: psycopg.Connection, sql: str, rows: Iterable[tuple[Any, ...]], chunk_size: int = 1000) -> int:
    total = 0
    batch: list[tuple[Any, ...]] = []
    with pg_conn.cursor() as cur:
        for row in rows:
            batch.append(row)
            if len(batch) >= chunk_size:
                cur.executemany(sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            cur.executemany(sql, batch)
            total += len(batch)
    return total


def _checkpoint_done(pg_conn: psycopg.Connection, src_db: str, tbl: str, target_db: str, sha: str) -> bool:
    row = pg_conn.execute(
        "SELECT snapshot_sha256, completed FROM em_runtime.migration_checkpoints "
        "WHERE source_db = %s AND table_name = %s AND target_db = %s",
        (src_db, tbl, target_db),
    ).fetchone()
    return bool(row and row[0] == sha and row[1])


def _mark_checkpoint(pg_conn: psycopg.Connection, src_db: str, tbl: str, target_db: str, sha: str, rows_cnt: int) -> None:
    pg_conn.execute(
        "INSERT INTO em_runtime.migration_checkpoints(source_db, table_name, snapshot_sha256, target_db, rows_imported, completed) "
        "VALUES (%s, %s, %s, %s, %s, true) "
        "ON CONFLICT (source_db, table_name, target_db) DO UPDATE "
        "SET snapshot_sha256 = EXCLUDED.snapshot_sha256, rows_imported = EXCLUDED.rows_imported, completed = true, updated_at = clock_timestamp()",
        (src_db, tbl, sha, target_db, rows_cnt),
    )


def _write_secret_0600(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def open_control_store(cfg: PgHistoryConfig) -> PgConsoleStore:
    pw = cfg.control_coordinator_password or cfg.admin_password
    sec_dir = cfg.cas_root / ".secrets"
    sec_file = sec_dir / f"coord_{cfg.port}.pw"
    _write_secret_0600(sec_file, pw)
    ctrl_cfg = PgControlConfig.from_dict({
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.control_db,
        "coordinator_role": cfg.control_coordinator_role,
        "coordinator_password_ref": "coord",
        "allow_loopback_disable_ssl": cfg.allow_loopback_disable_ssl,
        "secret_files": {"coord": str(sec_file)},
    })
    return PgConsoleStore(ctrl_cfg)


def import_control(cfg: PgHistoryConfig, snapshots: dict[str, Any], pg_hist: psycopg.Connection) -> dict[str, int]:
    snap = snapshots["users"]
    sha = snap["sha256"]
    sconn = ro_sqlite(snap["snapshot_path"])
    store = open_control_store(cfg)
    try:
        meta = {r["key"]: r["value"] for r in sconn.execute("SELECT key, value FROM console_meta").fetchall()}
        owner_id = meta.get("owner_id", "jiangli")
        users = [dict(r) for r in sconn.execute("SELECT * FROM console_users ORDER BY id").fetchall()]
        libs = {r["user_id"]: dict(r) for r in sconn.execute("SELECT * FROM console_libraries").fetchall()}
        owner_row = next(u for u in users if u["id"] == owner_id)
        owner_oid = owner_row["open_id"] or f"migrated-owner-{owner_id}"

        def _pg_env(uid: str) -> dict[str, str]:
            raw = json.loads((libs.get(uid) or {}).get("history_environ") or "{}")
            if uid == "public":
                raw["EVOLVMEM_HISTORY_PG_DB"] = cfg.shared_public_db
            elif uid in cfg.user_history_dbs:
                raw["EVOLVMEM_HISTORY_PG_DB"] = cfg.resolve_user_history_db(uid)
            else:
                raise KeyError(f"unmapped_history_user_db:{uid}")
            raw["EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE"] = "personal" if uid == owner_id else uid
            return {k: str(v) for k, v in raw.items() if isinstance(v, str)}

        if not store.registry_ready(owner_id):
            store.bootstrap(
                owner_id, owner_oid, _s(owner_row["display_name"], owner_id),
                _s((libs.get(owner_id) or {}).get("data_dir")), _pg_env(owner_id),
            )
        existing_users = {u["id"]: u for u in store.list_users(owner_id)}
        for u in users:
            uid = u["id"]
            if uid != owner_id and uid not in existing_users:
                oid = u["open_id"]
                if oid:
                    store.observe_login(owner_id, oid, _s(u["display_name"], uid))
                store.upsert_user(
                    owner_id, owner_id, uid,
                    display_name=_s(u["display_name"], uid), open_id=oid,
                    enabled=bool(u["enabled"]),
                    revision=None, create=True,
                )
            store.configure_library(
                owner_id, owner_id, uid,
                data_dir=_s((libs.get(uid) or {}).get("data_dir")),
                history_environ=_pg_env(uid),
            )
        for p in sconn.execute("SELECT open_id, name FROM console_pending").fetchall():
            store.observe_login(owner_id, p["open_id"], p["name"])
        for g in sconn.execute("SELECT * FROM console_grants").fetchall():
            cur_g = next((x for x in store.list_grants(owner_id) if x["viewer_id"] == g["viewer_id"] and x["owner_id"] == g["owner_id"]), None)
            store.set_grant(
                owner_id, owner_id, viewer_id=g["viewer_id"], owner_id=g["owner_id"],
                history_read=bool(g["history_read"]), experience_read=bool(g["experience_read"]),
                revision=cur_g["revision"] if cur_g else None,
            )

        audit_cnt = 0
        if not _checkpoint_done(pg_hist, "users", "console_audit", cfg.control_db, sha):
            for r in sconn.execute("SELECT actor_id, action, target_id, before_json, after_json, outcome FROM console_audit ORDER BY id"):
                store.record_audit(
                    _s(r["actor_id"], owner_id), _s(r["action"], "migrated"), _s(r["target_id"], ""),
                    before=json.loads(r["before_json"] or "{}"),
                    after=json.loads(r["after_json"] or "{}"),
                    outcome=_s(r["outcome"], "allowed"),
                )
                audit_cnt += 1
            _mark_checkpoint(pg_hist, "users", "console_audit", cfg.control_db, sha, audit_cnt)
        else:
            audit_cnt = int(sconn.execute("SELECT COUNT(*) FROM console_audit").fetchone()[0])

        if not _checkpoint_done(pg_hist, "users", "confirmed_devices", cfg.control_db, sha):
            dev_sec_dir = cfg.cas_root / ".secrets" / "devices"
            dev_sec_dir.mkdir(parents=True, exist_ok=True)
            dev_sec_dir.chmod(0o700)
            for dev_id in sorted(cfg.device_dbs):
                dev_uuid = str(uuid.uuid5(_DEV_NS, dev_id))
                tok_file = dev_sec_dir / f"{hashlib.sha256(dev_id.encode('utf-8')).hexdigest()[:24]}.tok"
                seed_tok = tok_file.read_text(encoding="utf-8").strip() if tok_file.is_file() else ""
                if not seed_tok:
                    seed_tok = secrets.token_urlsafe(32)
                    _write_secret_0600(tok_file, seed_tok)
                store.register_trusted_device(
                    owner_id, device_id=dev_uuid, external_device_id=dev_id,
                    token=seed_tok, unrestricted_own_projects=True,
                )
            _mark_checkpoint(pg_hist, "users", "confirmed_devices", cfg.control_db, sha, len(cfg.device_dbs))

        pg_hist.commit()
        return {"users": len(users), "audit_imported": audit_cnt, "devices": len(cfg.device_dbs)}
    finally:
        sconn.close()


def import_organized_history(snapshots: dict[str, Any], pg_conn: psycopg.Connection) -> dict[str, int]:
    sc = ro_sqlite(snapshots["candidate"]["snapshot_path"])
    sm = ro_sqlite(snapshots["management"]["snapshot_path"])
    sb = ro_sqlite(snapshots["project_board_sync"]["snapshot_path"])
    counts: dict[str, int] = {}
    try:
        counts["projects"] = bulk_exec(
            pg_conn,
            "INSERT INTO em_history.projects(project,display_name,status,revision,created_at,updated_at,block_count,message_count,turn_count,attachment_count,first_at,last_at,summary,summary_purpose,summary_source,environments,evidence_status,intro_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project) DO UPDATE SET display_name=EXCLUDED.display_name,block_count=EXCLUDED.block_count,message_count=EXCLUDED.message_count,summary=EXCLUDED.summary,last_at=EXCLUDED.last_at",
            ((_s(r[0]),_s(r[1]),_s(r[2],"active"),_i(r[3],1),_s(r[4]),_s(r[5]),_i(r[6]),_i(r[7]),_i(r[8]),_i(r[9]),_s(r[10]),_s(r[11]),_s(r[12]),_s(r[13]),_s(r[14]),_s(r[15],"[]"),_s(r[16]),_s(r[17],"{}"))
             for r in sc.execute("SELECT project,display_name,status,revision,created_at,updated_at,block_count,message_count,turn_count,attachment_count,first_at,last_at,summary,summary_purpose,summary_source,environments,evidence_status,intro_json FROM projects")),
        )
        counts["blocks"] = bulk_exec(
            pg_conn,
            "INSERT INTO em_history.blocks(block_id,adapter,kind,family_key,native_session_id,native_device_id,native_id_evidence,title,message_count,turn_count,attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256,device_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (block_id) DO UPDATE SET title=EXCLUDED.title,message_count=EXCLUDED.message_count,projects=EXCLUDED.projects",
            ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_s(r[5]),_s(r[6]),_s(r[7]),_i(r[8]),_i(r[9]),_i(r[10]),_i(r[11]),_s(r[12]),_s(r[13]),_s(r[14]),_s(r[15]),_s(r[16],"{}"),_s(r[17],"[]"),_s(r[18],"[]"),_s(r[19],"[]"),_s(r[20],"{}"),_s(r[21]),_s(r[5]))
             for r in sc.execute("SELECT block_id,adapter,kind,family_key,native_session_id,native_device_id,native_id_evidence,title,message_count,turn_count,attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256 FROM blocks")),
        )
        counts["block_projects"] = bulk_exec(pg_conn, "INSERT INTO em_history.block_projects(block_id,project,evidence) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_s(r[2])) for r in sc.execute("SELECT block_id,project,evidence FROM block_projects")))
        counts["messages"] = bulk_exec(pg_conn, "INSERT INTO em_history.messages(block_id,ordinal,role,content,recorded_at,archive_id,message_index,attachments) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (block_id,ordinal) DO UPDATE SET content=EXCLUDED.content", ((_s(r[0]),_i(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),r[5],r[6],_s(r[7],"[]")) for r in sc.execute("SELECT block_id,ordinal,role,content,recorded_at,archive_id,message_index,attachments FROM messages")))
        counts["attachments"] = bulk_exec(pg_conn, "INSERT INTO em_history.attachments(sha256,mime_type,bytes,path,format_ok,width,height,occurrences) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (sha256) DO UPDATE SET path=EXCLUDED.path", ((_s(r[0]),_s(r[1]),_i(r[2]),_s(r[3]),_i(r[4]),r[5],r[6],_i(r[7])) for r in sc.execute("SELECT sha256,mime_type,bytes,path,format_ok,width,height,occurrences FROM attachments")))
        counts["sources"] = bulk_exec(pg_conn, "INSERT INTO em_history.sources(archive_id,family_id,block_id,adapter,kind,external_session_id,native_session_id,native_device_id,selection_status,state,status,detail,logical_bytes,manifest_bytes,manifest_sha256_verified,content_sha256_verified,native_hash_verified,messages,attachments_recovered,attachments_missing,retained_range,elapsed_seconds) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (archive_id) DO NOTHING", ((_i(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_s(r[5]),_s(r[6]),_s(r[7]),_s(r[8]),_s(r[9]),_s(r[10]),_s(r[11]),_i(r[12]),_i(r[13]),_i(r[14]),_i(r[15]),_i(r[16]),_i(r[17]),_i(r[18]),_i(r[19]),_s(r[20]),_f(r[21])) for r in sc.execute("SELECT archive_id,family_id,block_id,adapter,kind,external_session_id,native_session_id,native_device_id,selection_status,state,status,detail,logical_bytes,manifest_bytes,manifest_sha256_verified,content_sha256_verified,native_hash_verified,messages,attachments_recovered,attachments_missing,retained_range,elapsed_seconds FROM sources")))
        counts["recovery_sources"] = bulk_exec(pg_conn, "INSERT INTO em_history.recovery_sources(block_id,project,adapter,kind,session_id,cwd,source_path,source_sha256,line_start,line_end,archive_ids,message_count,user_messages,assistant_messages,completeness,note,extension_of,source_message_range) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_s(r[5]),_s(r[6]),_s(r[7]),r[8],r[9],_s(r[10],"[]"),_i(r[11]),_i(r[12]),_i(r[13]),_s(r[14]),_s(r[15]),_s(r[16]),_s(r[17])) for r in sc.execute("SELECT block_id,project,adapter,kind,session_id,cwd,source_path,source_sha256,line_start,line_end,archive_ids,message_count,user_messages,assistant_messages,completeness,note,extension_of,source_message_range FROM recovery_sources")))
        pg_conn.execute("DELETE FROM em_history.recovery_reconciliation")
        counts["recovery_reconciliation"] = bulk_exec(pg_conn, "INSERT INTO em_history.recovery_reconciliation(project,candidate,action,base_block_id,new_block_id,covered_messages,note) VALUES (%s,%s,%s,%s,%s,%s,%s)", ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4]),_i(r[5]),_s(r[6])) for r in sc.execute("SELECT project,candidate,action,base_block_id,new_block_id,covered_messages,note FROM recovery_reconciliation")))
        counts["candidate_meta"] = bulk_exec(pg_conn, "INSERT INTO em_history.candidate_meta(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", ((_s(r[0]),_s(r[1])) for r in sc.execute("SELECT key,value FROM meta")))
        counts["exceptions"] = bulk_exec(pg_conn, "INSERT INTO em_history.exceptions(id,archive_id,family_id,classification,detail) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", ((_i(r[0]),r[1],_s(r[2]),_s(r[3]),_s(r[4])) for r in sc.execute("SELECT id,archive_id,family_id,classification,detail FROM exceptions")))
        counts["management_trash"] = bulk_exec(pg_conn, "INSERT INTO em_history.management_trash(kind,project,key,title,deleted_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3]),_s(r[4])) for r in sm.execute("SELECT kind,project,key,title,deleted_at FROM trash")))
        counts["management_operations"] = bulk_exec(pg_conn, "INSERT INTO em_history.management_operations(request_id,fingerprint,result,created_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", ((_s(r[0]),_s(r[1]),_s(r[2]),_s(r[3])) for r in sm.execute("SELECT request_id,fingerprint,result,created_at FROM operations")))
        counts["history_management_meta"] = bulk_exec(pg_conn, "INSERT INTO em_history.history_management_meta(id,version,scope) VALUES (1,%s,%s) ON CONFLICT (id) DO UPDATE SET version=EXCLUDED.version, scope=EXCLUDED.scope", ((_i(r[0],1),_s(r[1],"personal")) for r in sm.execute("SELECT version,scope FROM history_management_meta")))
        counts["project_board_sync_state"] = bulk_exec(pg_conn, "INSERT INTO em_history.project_board_sync_state(project,workspace_fingerprint,workstream_id,checkpoint_revision,state_version,payload_json,content_hash,pending,target_record_id,binding_id,sent_hash,last_status,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project,workspace_fingerprint,workstream_id) DO UPDATE SET payload_json=EXCLUDED.payload_json", ((_s(r[0]),_s(r[1]),_s(r[2]),_i(r[3]),_i(r[4]),_s(r[5],"{}"),_s(r[6]),_i(r[7],1),_s(r[8]),_s(r[9]),_s(r[10]),_s(r[11],"pending"),_s(r[12])) for r in sb.execute("SELECT project,workspace_fingerprint,workstream_id,checkpoint_revision,state_version,payload_json,content_hash,pending,target_record_id,binding_id,sent_hash,last_status,updated_at FROM project_board_sync_state")))
        gap_file = Path("/home/jiangli/.config/evolvmem-history/gap-audit.json")
        if gap_file.is_file():
            pg_conn.execute("INSERT INTO em_history.coverage_reports(report_key, payload) VALUES ('gap_audit', %s::jsonb) ON CONFLICT (report_key) DO UPDATE SET payload = EXCLUDED.payload", (gap_file.read_text(encoding="utf-8"),))
        pg_conn.commit()
        return counts
    finally:
        sc.close(); sm.close(); sb.close()
