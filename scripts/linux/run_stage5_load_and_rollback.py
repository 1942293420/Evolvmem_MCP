"""Stage 5.5 Load/Deadline Benchmark, Mid-Load Revocation, Embed Drain, and s11.2 Rollback Drill."""
from __future__ import annotations

import argparse
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
from evolvmem.history_pg.migrator_import import backfill_message_embeddings
from evolvmem.history_pg.rollback_replay import replay_library_journal_to_sqlite
from evolvmem.history_pg.schema import apply_history_schemas
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
    return kane, orig_enabled, str(kane["principal_id"])


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
    apply_history_schemas(cfg, include_control=True)
    with cfg.connect("postgres", autocommit=True) as pconn:
        pconn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(cfg.control_coordinator_role), sql.Literal(pw)))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        coord_pw = tmp / "coord.pw"
        coord_pw.write_text(pw, encoding="utf-8")
        coord_pw.chmod(0o600)
        ctrl_file = tmp / "control.json"
        ctrl_file.write_text(json.dumps({
            "host": "127.0.0.1", "port": args.port, "database": cfg.control_db,
            "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
            "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(coord_pw)},
        }), encoding="utf-8")
        ctrl_cfg = PgControlConfig.from_file(ctrl_file)
        store = PgConsoleStore(ctrl_cfg)
        owner = store.get_user_source("jiangli", "jiangli")
        if owner is None:
            store.bootstrap("jiangli", f"oid-owner-{uuid.uuid4().hex[:8]}", "Owner", str(cas_dir), {"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db})
            owner = store.get_user_source("jiangli", "jiangli")
        kane, kane_orig_enabled, kane_principal = _ensure_kane_ready(store, cfg, cas_dir)
        owner_principal = str(owner["principal_id"])

        policy_file = tmp / "policy.json"
        policy_file.write_text(json.dumps({
            "ai_processing_enabled": False, "legacy_web_enabled": False,
            "console_enabled": True, "history_pg_enabled": True,
        }), encoding="utf-8")
        web_auth_file = tmp / "web_auth.json"
        web_auth_file.write_text(json.dumps({
            "enabled": True, "app_id": "stage5-app", "app_secret": "stage5-secret",
            "owner_open_id": owner["open_id"] or f"oid-{uuid.uuid4().hex[:8]}",
            "redirect_uri": "http://127.0.0.1/auth/feishu/callback",
        }), encoding="utf-8")
        web_auth_file.chmod(0o600)
        hist_file = tmp / "history.json"
        hist_file.write_text(json.dumps({
            "host": "127.0.0.1", "port": args.port, "admin_user": "postgres", "admin_password": pw,
            "control_coordinator_password": pw, "cas_root": str(cas_dir), "allow_loopback_disable_ssl": True,
            "user_history_dbs": cfg.user_history_dbs,
            "pool_limits": {"max_connections": 28, "acquire_timeout_s": 3.0},
        }), encoding="utf-8")

        tok_owner, tok_kane = f"tok-o-{uuid.uuid4().hex}", f"tok-k-{uuid.uuid4().hex}"
        mcp_cfg_file = tmp / "mcp.json"
        mcp_cfg_file.write_text(json.dumps({
            "owner_id": "jiangli", "control_config_path": str(ctrl_file),
            "pg_config": json.loads(hist_file.read_text(encoding="utf-8")),
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
            "EVOLVMEM_PRODUCT_POLICY_FILE": str(policy_file),
            "EVOLVMEM_CONSOLE_PG_CONFIG": str(ctrl_file),
            "EVOLVMEM_HISTORY_PG_CONFIG": str(hist_file),
            "EVOLVMEM_CONSOLE_OWNER_ID": "jiangli",
            "EVOLVMEM_WEB_AUTH_FILE": str(web_auth_file),
        }
        web_proc = subprocess.Popen(
            [sys.executable, "-m", "evolvmem.history_pg.web_pg_runner", "--host", "127.0.0.1", "--port", str(wport), "--data-dir", str(tmp)],
            env=wenv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        web_tok = f"wsess-{uuid.uuid4().hex}"
        auth_scope = WebAuth(AuthSettings.load(tmp)).persistence_scope() if AuthSettings.load(tmp) else ""
        # Load web auth session in PG
        prev_web_auth = os.environ.get("EVOLVMEM_WEB_AUTH_FILE")
        os.environ["EVOLVMEM_WEB_AUTH_FILE"] = str(web_auth_file)
        try:
            auth_scope = WebAuth(AuthSettings.load(tmp)).persistence_scope()
            psess = PgPersistentSessions(store, auth_scope, expected_owner_id="jiangli", console_mode=True)
            psess.initialize()
            psess.put(web_tok, {"open_id": owner["open_id"] or json.loads(web_auth_file.read_text())["owner_open_id"], "name": "Owner", "csrf": "c55", "expires": time.time() + 1800})
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
            for workers, n_tasks in ((12, 48), (20, 60)):
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    for ok, dt in ex.map(_mix_call, range(n_tasks)):
                        total_reqs += 1
                        latencies.append(dt)
                        if not ok:
                            errors += 1

            # Mid-load revocation of kane
            k_now = store.get_user_source("jiangli", "kane")
            st_k_before, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})
            store.upsert_user("jiangli", "jiangli", "kane", display_name=k_now["display_name"], open_id=k_now["open_id"], enabled=False, revision=k_now["revision"], create=False)
            st_k_revoked, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})
            st_o_during, _, _ = _mcp_post(mport, tok_owner, owner_principal, "ping", {})
            k_dis = store.get_user_source("jiangli", "kane")
            store.upsert_user("jiangli", "jiangli", "kane", display_name=k_dis["display_name"], open_id=k_dis["open_id"], enabled=True, revision=k_dis["revision"], create=False)
            st_k_restored, _, _ = _mcp_post(mport, tok_kane, kane_principal, "ping", {})

            # Deadline cancellation check (pg_sleep(5) with 0.5s timeout)
            st_to, body_to, dt_to = _mcp_post(mport, tok_owner, owner_principal, "tools/call", {"name": "memory_status", "arguments": {"_sleep_seconds": 5.0, "_timeout_seconds": 0.5}}, timeout=5.0)
            to_payload = json.loads(body_to["result"]["content"][0]["text"])
            st_after, body_after, _ = _mcp_post(mport, tok_owner, owner_principal, "tools/call", {"name": "memory_status", "arguments": {"detail": "revision"}})
            sqlite_fds = _sqlite_fds(mcp_proc.pid) + _sqlite_fds(web_proc.pid)
        finally:
            if not kane_orig_enabled:
                k_end = store.get_user_source("jiangli", "kane")
                if k_end and k_end["enabled"]:
                    store.upsert_user("jiangli", "jiangli", "kane", display_name=k_end["display_name"], open_id=k_end["open_id"], enabled=False, revision=k_end["revision"], create=False)
            psess.pop(web_tok)
            mcp_proc.terminate(); web_proc.terminate()
            mcp_proc.wait(timeout=5); web_proc.wait(timeout=5)

    # Vector backfill drain batch (0 LLM)
    emb = LocalBgeEmbedder()
    msg_emb = backfill_message_embeddings(cfg, cfg.history_migration_db, emb, limit=64)

    # Section 11.2 Write-preserving rollback drill on disposable recovery-sqlite copies
    rec_dir = wdir / "recovery-sqlite"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rt_copy = rec_dir / "memory_owner.db"
    mgmt_copy = rec_dir / "management.db"
    shutil.copy2(wdir / "snapshots/memory_owner.db", rt_copy)
    shutil.copy2(wdir / "snapshots/management.db", mgmt_copy)

    with cfg.connect("evolvmem_history_kane") as kconn:
        kane_before = int(kconn.execute("SELECT count(*) FROM em_runtime.session_archives").fetchone()[0])

    # Perform fresh post-cutover PG writes on owner library
    drill_tag = uuid.uuid4().hex[:8]
    capture = PgCaptureStore(cfg)
    cont = PgContinuityStore(cfg)
    reader = PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db)
    raw_drill = ("\n".join([
        json.dumps({"type": "session", "version": 4, "id": f"drill-{drill_tag}", "createdAt": 1763165500000, "isSeeded": False, "delegationDepth": 0, "cwd": "/tmp/drill"}),
        json.dumps({"type": "user/message", "seq": 0, "time": 1763165500100, "data": {"role": "user", "source": {"kind": "user"}, "content": [{"type": "text", "text": "Rollback drill message"}]}}),
    ]) + "\n").encode("utf-8")
    import base64
    up_res = capture.upload("jiangli", {
        "device_id": f"win-drill-{drill_tag}", "session_id": f"drill-{drill_tag}", "project": "drill-proj",
        "sha256": hashlib.sha256(raw_drill).hexdigest(), "total_bytes": len(raw_drill), "offset": 0,
        "content_b64": base64.b64encode(raw_drill).decode(), "extract": False,
        "source_format": "dsh-session-jsonl-v1", "request_id": f"dr-up-{drill_tag}", "source_order": 500,
        "current_sha256": hashlib.sha256(raw_drill).hexdigest(),
    })
    cp_res = cont.continuity_checkpoint("jiangli", {
        "project": "drill-proj", "workspace_path": "/tmp/drill", "device_id": f"win-drill-{drill_tag}",
        "summary": f"Drill checkpoint {drill_tag}", "next_step": "Verify rollback", "request_id": f"dr-cp-{drill_tag}",
    })
    del_req = f"dr-del-{drill_tag}"
    rst_req = f"dr-rst-{drill_tag}"
    reader.manage("delete", kind="block", project="drill-proj", key=f"blk-drill-{drill_tag}", title="Drill", request_id=del_req)
    reader.manage("restore", kind="block", project="drill-proj", key=f"blk-drill-{drill_tag}", title="Drill", request_id=rst_req)

    rb1 = replay_library_journal_to_sqlite(cfg, cfg.history_migration_db, "jiangli", runtime_sqlite_path=rt_copy, management_sqlite_path=mgmt_copy)
    rb2 = replay_library_journal_to_sqlite(cfg, cfg.history_migration_db, "jiangli", runtime_sqlite_path=rt_copy, management_sqlite_path=mgmt_copy)

    sq = sqlite3.connect(str(rt_copy))
    mg = sqlite3.connect(str(mgmt_copy))
    try:
        has_arch = sq.execute("SELECT 1 FROM session_archives WHERE id = ?", (up_res["archive_id"],)).fetchone() is not None
        has_ws = sq.execute("SELECT 1 FROM continuity_workstreams WHERE id = ?", (cp_res["workstream_id"],)).fetchone() is not None
        has_ops = mg.execute("SELECT count(*) FROM operations WHERE request_id IN (?, ?)", (del_req, rst_req)).fetchone()[0] == 2
    finally:
        sq.close(); mg.close()

    with cfg.connect("evolvmem_history_kane") as kconn:
        kane_after = int(kconn.execute("SELECT count(*) FROM em_runtime.session_archives").fetchone()[0])

    disk = shutil.disk_usage(wdir)
    free_gib = round(disk.free / (1024 ** 3), 2)

    load_doc = {
        "schema_version": "stage5.load_acceptance.v1",
        "ok": errors == 0 and sqlite_fds == 0 and st_k_before == 200 and st_k_revoked == 401 and st_o_during == 200 and st_k_restored == 200 and to_payload.get("error") == "request_timeout" and dt_to < 1.5 and st_after == 200 and not body_after["result"]["isError"],
        "sustained_workers": 12, "burst_workers": 20, "total_requests": total_reqs,
        "error_rate": round(errors / max(1, total_reqs), 6),
        "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95), "max": _pct(latencies, 1.0)},
        "sqlite_fd_count": sqlite_fds,
        "mid_load_revocation": {"before_status": st_k_before, "revoked_status": st_k_revoked, "owner_status_during": st_o_during, "restored_status": st_k_restored},
        "deadline_cancellation": {"http_status": st_to, "error": to_payload.get("error"), "elapsed_seconds": round(dt_to, 3), "post_timeout_ok": st_after == 200 and not body_after["result"]["isError"]},
        "message_embedding_drain_batch": msg_emb,
        "llm_usage": {"calls": 0, "tokens": 0},
    }
    rb_doc = {
        "schema_version": "stage5.rollback_drill.v1",
        "ok": rb1["ok"] and rb2["ok"] and rb2["replayed_count"] == 0 and has_arch and has_ws and has_ops and kane_before == kane_after,
        "library_scope": "owner",
        "replayed_count": rb1["replayed_count"],
        "by_op": rb1["by_op"],
        "lost_writes": rb1["lost_writes"],
        "idempotent_second_run_replayed": rb2["replayed_count"],
        "sqlite_verified": {"archive": has_arch, "workstream": has_ws, "management_ops": has_ops},
        "other_library_untouched": kane_before == kane_after,
        "llm_usage": {"calls": 0, "tokens": 0},
    }
    disk_doc = {
        "schema_version": "stage5.disk_peak_budget.v1",
        "ok": free_gib >= 50.0,
        "total_gib": round(disk.total / (1024 ** 3), 2),
        "used_gib": round(disk.used / (1024 ** 3), 2),
        "free_gib": free_gib,
        "min_required_gib": 50.0,
        "llm_usage": {"calls": 0, "tokens": 0},
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
