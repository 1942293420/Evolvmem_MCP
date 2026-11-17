"""Stage 5.5 Load/Deadline Benchmark, Mid-Load Revocation, Embed Drain, and s11.2 Rollback Drill."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

from psycopg import sql

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.capture_store import PgCaptureStore
from evolvmem.history_pg.config import PgHistoryConfig, PoolLimits
from evolvmem.history_pg.continuity_store import PgContinuityStore
from evolvmem.history_pg.local_embedder import LocalBgeEmbedder
from evolvmem.history_pg.migrator_import import backfill_message_embeddings, import_all_snapshots
from evolvmem.history_pg.migrator_reconcile import reconcile_migration
from evolvmem.history_pg.migrator_snapshot import (
    _file_sha256,
    _inspect_snapshot,
    classify_all_tables,
    default_live_sources,
    take_online_snapshots,
)
from evolvmem.history_pg.rollback_replay import replay_library_journal_to_sqlite
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.web_auth import AuthSettings, WebAuth
from scripts.verify_pg_cutover_manifest import run_verification


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mcp_post(port: int, token: str, expected_user: str, method: str, params: dict, timeout: float = 8.0) -> tuple[int, dict, float]:
    t0 = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}).encode("utf-8")
        conn.request("POST", "/mcp", body=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-11-25", "X-EvolvMem-Expected-User": expected_user,
        })
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {}), time.monotonic() - t0
    finally:
        conn.close()


def _web_get(port: int, path: str, session_cookie: str, timeout: float = 8.0) -> tuple[int, dict, float]:
    t0 = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Cookie": f"evolvmem_session={session_cookie}"})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {}), time.monotonic() - t0
    finally:
        conn.close()


def _sqlite_fds(pid: int) -> int:
    return sum(1 for p in Path(f"/proc/{pid}/fd").iterdir() if p.is_symlink() and os.readlink(p).endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm")))


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
    return round(s[idx] * 1000.0, 2)


def _ensure_kane_ready(store: PgConsoleStore, cfg: PgHistoryConfig, cas_dir: Path) -> tuple[dict, bool, str]:
    kane = store.get_user_source("jiangli", "kane")
    if kane is None:
        oid = f"oid-kane-{uuid.uuid4().hex[:8]}"
        store.observe_login("jiangli", oid, "kane")
        store.upsert_user("jiangli", "jiangli", "kane", display_name="kane", open_id=oid, enabled=True, revision=None, create=True)
        kane = store.get_user_source("jiangli", "kane")
    orig_enabled = bool(kane["enabled"])
    if not orig_enabled:
        store.upsert_user("jiangli", "jiangli", "kane", display_name=kane["display_name"], open_id=kane["open_id"], enabled=True, revision=kane["revision"], create=False)
        kane = store.get_user_source("jiangli", "kane")
    kane_dir = cas_dir / "kane"
    kane_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.configure_library(
        "jiangli", "jiangli", "kane",
        data_dir=str(kane_dir),
        history_environ={"EVOLVMEM_HISTORY_PG_DB": cfg.resolve_user_history_db("kane"), "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "kane"},
    )
    return kane, orig_enabled, str(kane["principal_id"])


def _ensure_baseline(cfg: PgHistoryConfig, wdir: Path) -> None:
    snap_dir = wdir / "snapshots"
    sources = default_live_sources()
    missing_snaps = any(not (snap_dir / f"{k}.db").is_file() for k in sources)
    manifest_path = wdir / "table-classification-manifest.json"
    rec_path = wdir / "migration-reconciliation.json"
    with cfg.connect(cfg.history_migration_db) as hconn:
        has_projects = int(hconn.execute("SELECT count(*) FROM em_history.projects").fetchone()[0]) > 0
    has_rec = rec_path.is_file() and "content_digests_match" in json.loads(rec_path.read_text(encoding="utf-8"))
    if not (missing_snaps or not manifest_path.is_file() or not has_rec or not has_projects):
        return
    if missing_snaps:
        snaps = take_online_snapshots(sources, snap_dir)
    else:
        snaps = {}
        for k in sources:
            dest = snap_dir / f"{k}.db"
            snaps[k] = {
                "source_key": k,
                "snapshot_path": str(dest),
                "bytes": dest.stat().st_size,
                "sha256": _file_sha256(dest),
                "sql_mode": "ro",
                "table_counts": _inspect_snapshot(dest),
            }
    manifest = classify_all_tables(snaps)
    manifest_path.write_text(json.dumps(manifest["scrubbed"], indent=2) + "\n", encoding="utf-8")
    if not has_projects:
        import_all_snapshots(cfg, snaps, embedder=LocalBgeEmbedder(), message_embed_limit=64)
    rec = reconcile_migration(cfg, snaps, manifest)
    rec_path.write_text(json.dumps(rec["scrubbed"], indent=2) + "\n", encoding="utf-8")


def _build_configs(tmp: Path, cfg: PgHistoryConfig, pw: str, cas_dir: Path, owner_oid: str) -> dict[str, Path]:
    coord_pw = tmp / "coord.pw"
    coord_pw.write_text(pw, encoding="utf-8")
    coord_pw.chmod(0o600)
    ctrl_file = tmp / "control.json"
    ctrl_file.write_text(json.dumps({
        "host": "127.0.0.1", "port": cfg.port, "database": cfg.control_db,
        "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
        "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(coord_pw)},
    }), encoding="utf-8")
    policy_file = tmp / "policy.json"
    policy_file.write_text(json.dumps({
        "ai_processing_enabled": False, "legacy_web_enabled": False,
        "console_enabled": True, "history_pg_enabled": True,
    }), encoding="utf-8")
    web_auth_file = tmp / "web_auth.json"
    web_auth_file.write_text(json.dumps({
        "enabled": True, "app_id": "stage5-app", "app_secret": "stage5-secret",
        "owner_open_id": owner_oid or f"oid-{uuid.uuid4().hex[:8]}",
        "redirect_uri": "http://127.0.0.1/auth/feishu/callback",
    }), encoding="utf-8")
    web_auth_file.chmod(0o600)
    hist_file = tmp / "history.json"
    hist_file.write_text(json.dumps({
        "host": "127.0.0.1", "port": cfg.port, "admin_user": "postgres", "admin_password": pw,
        "control_coordinator_password": pw, "cas_root": str(cas_dir), "allow_loopback_disable_ssl": True,
        "user_history_dbs": cfg.user_history_dbs,
        "pool_limits": {"max_connections": 28, "acquire_timeout_s": 3.0},
    }), encoding="utf-8")
    return {"ctrl": ctrl_file, "policy": policy_file, "web_auth": web_auth_file, "hist": hist_file}


def _run_load_and_revocation(
    cfg: PgHistoryConfig, store: PgConsoleStore, paths: dict[str, Path],
    owner: dict, owner_principal: str, kane_principal: str, kane_orig_enabled: bool,
) -> dict[str, Any]:
    tok_owner, tok_kane = f"tok-o-{uuid.uuid4().hex}", f"tok-k-{uuid.uuid4().hex}"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        mcp_cfg_file = Path(tf.name)
    mcp_cfg_file.write_text(json.dumps({
        "owner_id": "jiangli", "control_config_path": str(paths["ctrl"]),
        "pg_config": json.loads(paths["hist"].read_text(encoding="utf-8")),
        "tokens": {
            hashlib.sha256(tok_owner.encode()).hexdigest(): {"user_id": "jiangli", "device_id": "bench-owner"},
            hashlib.sha256(tok_kane.encode()).hexdigest(): {"user_id": "kane", "device_id": "bench-kane"},
        },
        "request_timeout_seconds": 3.0, "allow_sleep_probe": True,
    }), encoding="utf-8")

    mport, wport = _free_port(), _free_port()
    mcp_proc = subprocess.Popen(
        [sys.executable, "-m", "evolvmem.history_pg.unified_mcp_host", "--host", "127.0.0.1", "--port", str(mport), "--config", str(mcp_cfg_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    wenv = {
        **os.environ,
        "EVOLVMEM_PRODUCT_POLICY_FILE": str(paths["policy"]),
        "EVOLVMEM_CONSOLE_PG_CONFIG": str(paths["ctrl"]),
        "EVOLVMEM_HISTORY_PG_CONFIG": str(paths["hist"]),
        "EVOLVMEM_CONSOLE_OWNER_ID": "jiangli",
        "EVOLVMEM_WEB_AUTH_FILE": str(paths["web_auth"]),
    }
    web_proc = subprocess.Popen(
        [sys.executable, "-m", "evolvmem.history_pg.web_pg_runner", "--host", "127.0.0.1", "--port", str(wport), "--data-dir", str(paths["ctrl"].parent)],
        env=wenv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    web_tok = f"wsess-{uuid.uuid4().hex}"
    prev_web_auth = os.environ.get("EVOLVMEM_WEB_AUTH_FILE")
    os.environ["EVOLVMEM_WEB_AUTH_FILE"] = str(paths["web_auth"])
    try:
        auth_scope = WebAuth(AuthSettings.load(paths["ctrl"].parent)).persistence_scope()
        psess = PgPersistentSessions(store, auth_scope, expected_owner_id="jiangli", console_mode=True)
        psess.initialize()
        psess.put(web_tok, {"open_id": owner["open_id"] or json.loads(paths["web_auth"].read_text())["owner_open_id"], "name": "Owner", "csrf": "c55", "expires": time.time() + 1800})
    finally:
        if prev_web_auth is None:
            os.environ.pop("EVOLVMEM_WEB_AUTH_FILE", None)
        else:
            os.environ["EVOLVMEM_WEB_AUTH_FILE"] = prev_web_auth

    try:
        st1, st2 = 0, 0
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                st1, _, _ = _mcp_post(mport, tok_owner, owner_principal, "ping", {})
                st2, _, _ = _web_get(wport, "/api/knowledge-console/me", web_tok)
                if st1 == 200 and st2 == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert mcp_proc.poll() is None and web_proc.poll() is None and st1 == 200 and st2 == 200, f"st1={st1} st2={st2}"

        q_path = "/api/knowledge-console/libraries/jiangli/history/search?q=" + urllib.parse.quote("布局")
        def _mix_call(i: int) -> tuple[bool, float]:
            if i % 3 == 0:
                st, _, dt = _web_get(wport, q_path, web_tok)
                return st == 200, dt
            if i % 3 == 1:
                st, body, dt = _mcp_post(mport, tok_owner, owner_principal, "tools/call", {"name": "memory_status", "arguments": {"detail": "revision"}})
                return st == 200 and not body["result"]["isError"], dt
            st, body, dt = _mcp_post(mport, tok_owner, owner_principal, "tools/call", {"name": "context_session_start", "arguments": {"project": "stage5-win-proj", "query": "布局", "workspace_path": "/tmp/ws55", "device_id": "bench-owner", "max_chars": 800}})
            return st == 200 and not body["result"]["isError"], dt

        latencies: list[float] = []
        errors = 0
        total_reqs = 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            for ok, dt in ex.map(_mix_call, range(48)):
                total_reqs += 1
                latencies.append(dt)
                if not ok:
                    errors += 1

        kane_call = {"name": "memory_status", "arguments": {"detail": "revision"}}
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = [ex.submit(_mix_call, i) for i in range(120)]
            st_k_before, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})
            st_k_call_before, body_k_before, _ = _mcp_post(mport, tok_kane, kane_principal, "tools/call", kane_call)
            k_now = store.get_user_source("jiangli", "kane")
            store.upsert_user("jiangli", "jiangli", "kane", display_name=k_now["display_name"], open_id=k_now["open_id"], enabled=False, revision=k_now["revision"], create=False)
            st_k_revoked, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})
            st_k_call_revoked, _, _ = _mcp_post(mport, tok_kane, kane_principal, "tools/call", kane_call)
            st_o_during, _, _ = _mcp_post(mport, tok_owner, owner_principal, "ping", {})
            st_o_call_during, body_o_during, _ = _mcp_post(mport, tok_owner, owner_principal, "tools/call", kane_call)
            k_dis = store.get_user_source("jiangli", "kane")
            store.upsert_user("jiangli", "jiangli", "kane", display_name=k_dis["display_name"], open_id=k_dis["open_id"], enabled=True, revision=k_dis["revision"], create=False)
            st_k_restored, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})
            st_k_call_restored, body_k_restored, _ = _mcp_post(mport, tok_kane, kane_principal, "tools/call", kane_call)
            for f in futs:
                ok, dt = f.result()
                total_reqs += 1
                latencies.append(dt)
                if not ok:
                    errors += 1

        st_to, body_to, dt_to = _mcp_post(mport, tok_owner, owner_principal, "tools/call", {"name": "memory_status", "arguments": {"_sleep_seconds": 5.0, "_timeout_seconds": 0.5}}, timeout=5.0)
        to_payload = json.loads(body_to["result"]["content"][0]["text"])
        st_after, body_after, _ = _mcp_post(mport, tok_owner, owner_principal, "tools/call", kane_call)
        sqlite_fds = _sqlite_fds(mcp_proc.pid) + _sqlite_fds(web_proc.pid)
    finally:
        if not kane_orig_enabled:
            k_end = store.get_user_source("jiangli", "kane")
            if k_end and k_end["enabled"]:
                store.upsert_user("jiangli", "jiangli", "kane", display_name=k_end["display_name"], open_id=k_end["open_id"], enabled=False, revision=k_end["revision"], create=False)
        psess.pop(web_tok)
        mcp_proc.terminate(); web_proc.terminate()
        mcp_proc.wait(timeout=5); web_proc.wait(timeout=5)
        mcp_cfg_file.unlink(missing_ok=True)

    rev_ok = (
        st_k_before == 200 and st_k_call_before == 200 and not body_k_before["result"]["isError"]
        and st_k_revoked == 401 and st_k_call_revoked == 401
        and st_o_during == 200 and st_o_call_during == 200 and not body_o_during["result"]["isError"]
        and st_k_restored == 200 and st_k_call_restored == 200 and not body_k_restored["result"]["isError"]
    )
    msg_emb = backfill_message_embeddings(cfg, cfg.history_migration_db, LocalBgeEmbedder(), limit=64)
    return {
        "schema_version": "stage5.load_acceptance.v1",
        "ok": errors == 0 and sqlite_fds == 0 and rev_ok and to_payload.get("error") == "request_timeout" and dt_to < 1.5 and st_after == 200 and not body_after["result"]["isError"],
        "sustained_workers": 12, "burst_workers": 20, "total_requests": total_reqs,
        "error_rate": round(errors / max(1, total_reqs), 6),
        "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95), "max": _pct(latencies, 1.0)},
        "sqlite_fd_count": sqlite_fds,
        "mid_load_revocation": {
            "before_status": st_k_before, "before_call_status": st_k_call_before,
            "revoked_status": st_k_revoked, "revoked_call_status": st_k_call_revoked,
            "owner_status_during": st_o_during, "owner_call_status_during": st_o_call_during,
            "restored_status": st_k_restored, "restored_call_status": st_k_call_restored,
            "concurrent_burst_requests": 120,
        },
        "deadline_cancellation": {"http_status": st_to, "error": to_payload.get("error"), "elapsed_seconds": round(dt_to, 3), "post_timeout_ok": st_after == 200 and not body_after["result"]["isError"]},
        "message_embedding_drain_batch": msg_emb,
        "llm_usage": {"calls": 0, "tokens": 0},
    }


def _run_rollback_drill(cfg: PgHistoryConfig, wdir: Path) -> dict[str, Any]:
    rec_dir = wdir / "recovery-sqlite"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rt_copy, mgmt_copy = rec_dir / "memory_owner.db", rec_dir / "management.db"
    shutil.copy2(wdir / "snapshots/memory_owner.db", rt_copy)
    shutil.copy2(wdir / "snapshots/management.db", mgmt_copy)

    with cfg.connect("evolvmem_history_kane") as kconn:
        kane_before = int(kconn.execute("SELECT count(*) FROM em_runtime.session_archives").fetchone()[0])

    drill_tag = uuid.uuid4().hex[:8]
    dev_id, sess_id = f"win-drill-{drill_tag}", f"drill-{drill_tag}"
    raw_drill = ("\n".join([
        json.dumps({"type": "session", "version": 4, "id": sess_id, "createdAt": 1763165500000, "isSeeded": False, "delegationDepth": 0, "cwd": "/tmp/drill"}),
        json.dumps({"type": "user/message", "seq": 0, "time": 1763165500100, "data": {"role": "user", "source": {"kind": "user"}, "content": [{"type": "text", "text": "Rollback drill message"}]}}),
    ]) + "\n").encode("utf-8")
    drill_sha = hashlib.sha256(raw_drill).hexdigest()
    up_req, p2_req, asg_req, cp_req = f"dr-up-{drill_tag}", f"dr-p2-{drill_tag}", f"dr-asg-{drill_tag}", f"dr-cp-{drill_tag}"
    del_keep_req, del_req, rst_req = f"dr-delk-{drill_tag}", f"dr-del-{drill_tag}", f"dr-rst-{drill_tag}"
    keep_key, rst_key = f"blk-keep-{drill_tag}", f"blk-drill-{drill_tag}"

    capture, cont = PgCaptureStore(cfg), PgContinuityStore(cfg)
    reader = PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db)
    try:
        up_res = capture.upload("jiangli", {
            "device_id": dev_id, "session_id": sess_id, "project": "drill-proj",
            "sha256": drill_sha, "total_bytes": len(raw_drill), "offset": 0,
            "content_b64": base64.b64encode(raw_drill).decode(), "extract": False,
            "source_format": "dsh-session-jsonl-v1", "request_id": up_req, "source_order": 500,
            "current_sha256": drill_sha,
        })
        cont.continuity_checkpoint("jiangli", {
            "project": "drill-proj-2", "workspace_path": f"/tmp/drill2-{drill_tag}", "device_id": dev_id,
            "summary": f"Drill target {drill_tag}", "next_step": "Receive assign", "request_id": p2_req,
        })
        capture.assign("jiangli", {"device_id": dev_id, "session_id": sess_id, "project": "drill-proj-2", "request_id": asg_req})
        cp_res = cont.continuity_checkpoint("jiangli", {
            "project": "drill-proj", "workspace_path": "/tmp/drill", "device_id": dev_id,
            "summary": f"Drill checkpoint {drill_tag}", "next_step": "Verify rollback", "request_id": cp_req,
        })
        reader.manage("delete", kind="block", project="drill-proj", key=keep_key, title="Drill Keep", request_id=del_keep_req)
        reader.manage("delete", kind="block", project="drill-proj", key=rst_key, title="Drill", request_id=del_req)
        reader.manage("restore", kind="block", project="drill-proj", key=rst_key, title="Drill", request_id=rst_req)
    finally:
        reader.close()

    with cfg.connect(cfg.history_migration_db) as hconn:
        ctx_id = int(hconn.execute("SELECT current_context_id FROM em_runtime.continuity_workstreams WHERE id=%s", (cp_res["workstream_id"],)).fetchone()[0])

    rb1 = replay_library_journal_to_sqlite(cfg, cfg.history_migration_db, "jiangli", runtime_sqlite_path=rt_copy, management_sqlite_path=mgmt_copy)
    rb2 = replay_library_journal_to_sqlite(cfg, cfg.history_migration_db, "jiangli", runtime_sqlite_path=rt_copy, management_sqlite_path=mgmt_copy)

    sq = sqlite3.connect(str(rt_copy))
    mg = sqlite3.connect(str(mgmt_copy))
    try:
        arch_row = sq.execute("SELECT project FROM session_archives WHERE id = ?", (up_res["archive_id"],)).fetchone()
        up_row = sq.execute("SELECT project FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?", (dev_id, sess_id, drill_sha)).fetchone()
        hd_row = sq.execute("SELECT sha256 FROM lan_session_heads WHERE device_id=? AND session_id=?", (dev_id, sess_id)).fetchone()
        ws_ok = sq.execute("SELECT 1 FROM continuity_workstreams WHERE id = ?", (cp_res["workstream_id"],)).fetchone() is not None
        focus_ok = sq.execute("SELECT 1 FROM continuity_focus WHERE project='drill-proj' AND workstream_id=?", (cp_res["workstream_id"],)).fetchone() is not None
        events_ok = int(sq.execute("SELECT count(*) FROM continuity_events WHERE workstream_id=?", (cp_res["workstream_id"],)).fetchone()[0]) >= 1
        layers_ok = int(sq.execute("SELECT count(*) FROM context_layers WHERE item_id=?", (ctx_id,)).fetchone()[0]) >= 1
        wreq_ok = int(sq.execute("SELECT count(*) FROM lan_write_requests WHERE request_id IN (?,?,?)", (up_req, asg_req, cp_req)).fetchone()[0]) == 3
        rev_row = sq.execute("SELECT revision FROM lan_memory_revision WHERE id=1").fetchone()
        trash_keep = mg.execute("SELECT 1 FROM trash WHERE kind='block' AND project='drill-proj' AND key=?", (keep_key,)).fetchone() is not None
        trash_rst_absent = mg.execute("SELECT 1 FROM trash WHERE kind='block' AND project='drill-proj' AND key=?", (rst_key,)).fetchone() is None
        ops_ok = int(mg.execute("SELECT count(*) FROM operations WHERE request_id IN (?,?,?)", (del_keep_req, del_req, rst_req)).fetchone()[0]) == 3
    finally:
        sq.close(); mg.close()

    with cfg.connect("evolvmem_history_kane") as kconn:
        kane_after = int(kconn.execute("SELECT count(*) FROM em_runtime.session_archives").fetchone()[0])

    sv = {
        "archive": bool(arch_row and arch_row[0] == "drill-proj-2"),
        "upload_project": bool(up_row and up_row[0] == "drill-proj-2"),
        "session_head": bool(hd_row and hd_row[0] == drill_sha),
        "workstream": ws_ok, "continuity_focus": focus_ok,
        "continuity_events": events_ok, "context_layers": layers_ok,
        "lan_write_requests": wreq_ok, "lan_memory_revision": bool(rev_row and int(rev_row[0]) >= 1),
        "trash_retained_delete": trash_keep, "trash_restored_absent": trash_rst_absent,
        "management_ops": ops_ok,
    }
    return {
        "schema_version": "stage5.rollback_drill.v1",
        "ok": rb1["ok"] and rb2["ok"] and rb2["replayed_count"] == 0 and all(sv.values()) and kane_before == kane_after,
        "library_scope": "owner", "replayed_count": rb1["replayed_count"], "by_op": rb1["by_op"],
        "lost_writes": rb1["lost_writes"], "idempotent_second_run_replayed": rb2["replayed_count"],
        "sqlite_verified": sv, "other_library_untouched": kane_before == kane_after,
        "llm_usage": {"calls": 0, "tokens": 0},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", type=Path, default=Path("/home/jiangli/evolvmem-stage5-20261115"))
    ap.add_argument("--port", type=int, default=55434)
    args = ap.parse_args()
    wdir: Path = args.work_dir
    pw = Path("/tmp/evolvmem-pg-stage5.pw").read_text(encoding="utf-8").strip()
    cas_dir = wdir / "staging-archives"
    cas_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=args.port, admin_user="postgres", admin_password=pw,
        control_coordinator_password=pw, allow_loopback_disable_ssl=True,
        container_name="evolvmem-pg-test-stage5", cas_root=cas_dir,
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        pool_limits=PoolLimits(max_connections=28, acquire_timeout_s=3.0, statement_timeout_ms=3000, lock_timeout_ms=500),
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    with cfg.connect("postgres", autocommit=True, statement_timeout_ms=60000, lock_timeout_ms=30000) as pconn:
        pconn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(cfg.control_coordinator_role), sql.Literal(pw)))
    _ensure_baseline(cfg, wdir)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg_file = tmp / "coord_check.pw"
        cfg_file.write_text(pw, encoding="utf-8"); cfg_file.chmod(0o600)
        ctrl_pre = PgControlConfig.from_dict({
            "host": "127.0.0.1", "port": args.port, "database": cfg.control_db,
            "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
            "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(cfg_file)},
        })
        store = PgConsoleStore(ctrl_pre)
        owner = store.get_user_source("jiangli", "jiangli")
        if owner is None:
            store.bootstrap("jiangli", f"oid-owner-{uuid.uuid4().hex[:8]}", "Owner", str(cas_dir), {"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db})
            owner = store.get_user_source("jiangli", "jiangli")
        _, kane_orig_enabled, kane_principal = _ensure_kane_ready(store, cfg, cas_dir)
        paths = _build_configs(tmp, cfg, pw, cas_dir, owner["open_id"] or "")
        store = PgConsoleStore(PgControlConfig.from_file(paths["ctrl"]))
        load_doc = _run_load_and_revocation(cfg, store, paths, owner, str(owner["principal_id"]), kane_principal, kane_orig_enabled)

    rb_doc = _run_rollback_drill(cfg, wdir)
    disk = shutil.disk_usage(wdir)
    free_gib = round(disk.free / (1024 ** 3), 2)
    disk_doc = {
        "schema_version": "stage5.disk_peak_budget.v1", "ok": free_gib >= 50.0,
        "total_gib": round(disk.total / (1024 ** 3), 2), "used_gib": round(disk.used / (1024 ** 3), 2),
        "free_gib": free_gib, "min_required_gib": 50.0, "llm_usage": {"calls": 0, "tokens": 0},
    }
    (wdir / "load-acceptance.json").write_text(json.dumps(load_doc, indent=2) + "\n", encoding="utf-8")
    (wdir / "rollback-drill.json").write_text(json.dumps(rb_doc, indent=2) + "\n", encoding="utf-8")
    (wdir / "disk-peak-budget.json").write_text(json.dumps(disk_doc, indent=2) + "\n", encoding="utf-8")

    cand_doc = run_verification(
        manifest_path=wdir / "table-classification-manifest.json",
        reconciliation_path=wdir / "migration-reconciliation.json",
        mode="candidate", acceptance_dir=wdir,
    )
    (wdir / "candidate-manifest-verification.json").write_text(json.dumps(cand_doc, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"load": load_doc["ok"], "rollback": rb_doc["ok"], "disk": disk_doc["ok"], "candidate": cand_doc["ok"]}))
    return 0 if (load_doc["ok"] and rb_doc["ok"] and disk_doc["ok"] and cand_doc["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
