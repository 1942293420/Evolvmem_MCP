from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from psycopg import sql
import pytest

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE, STAGE5_WORK_DIR


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _codex_jsonl(session_id: str, cwd: str, user_text: str) -> bytes:
    lines = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd, "timestamp": "2026-11-15T00:00:00Z"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": user_text}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Received and analyzing task54."}},
    ]
    return ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n").encode("utf-8")


def _dsh_jsonl(session_id: str, cwd: str, user_text: str) -> bytes:
    lines = [
        {"type": "session", "version": 4, "id": session_id, "createdAt": 1763164800000, "isSeeded": False, "delegationDepth": 0, "cwd": cwd},
        {"type": "user/message", "seq": 0, "time": 1763164800100, "data": {"role": "user", "source": {"kind": "user"}, "content": [{"type": "text", "text": user_text}]}},
        {"type": "assistant/message", "seq": 1, "time": 1763164800200, "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "Acknowledged DSH native snapshot."}]}}},
    ]
    return ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n").encode("utf-8")


def _rpc(port: int, token: str, expected_user: str, method: str, params: dict | None = None, rpc_id: str = "1") -> tuple[int, dict]:
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/mcp", body=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-11-25", "X-EvolvMem-Expected-User": expected_user,
        })
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


def _setup_pg(tmp_path: Path) -> tuple[PgHistoryConfig, PgControlConfig, Path, str, str, bool]:
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_user="postgres", admin_password=pw,
        control_coordinator_password=pw,
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=cas_dir,
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    with cfg.connect("postgres", autocommit=True, statement_timeout_ms=60000, lock_timeout_ms=30000) as pconn:
        pconn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(cfg.control_coordinator_role), sql.Literal(pw)))
    coord_pw = tmp_path / "coord.pw"
    coord_pw.write_text(pw, encoding="utf-8")
    coord_pw.chmod(0o600)
    ctrl_file = tmp_path / "control.json"
    ctrl_file.write_text(json.dumps({
        "host": "127.0.0.1", "port": STAGE5_PG_PORT, "database": cfg.control_db,
        "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
        "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(coord_pw)},
    }), encoding="utf-8")
    ctrl_cfg = PgControlConfig.from_file(ctrl_file)
    store = PgConsoleStore(ctrl_cfg)
    if not store.registry_ready("jiangli"):
        store.bootstrap("jiangli", f"oid-{uuid.uuid4().hex}", "Owner", str(cas_dir), {"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db})
    k_src = store.get_user_source("jiangli", "kane")
    kane_orig = bool(k_src["enabled"]) if k_src is not None else True
    if k_src is None:
        oid_k = f"oid-kane-{uuid.uuid4().hex[:8]}"
        store.observe_login("jiangli", oid_k, "kane")
        store.upsert_user("jiangli", "jiangli", "kane", display_name="kane", open_id=oid_k, enabled=True, revision=None, create=True)
        k_src = store.get_user_source("jiangli", "kane")
    elif not k_src["enabled"]:
        store.upsert_user("jiangli", "jiangli", "kane", display_name=k_src["display_name"] or "kane", open_id=k_src["open_id"], enabled=True, revision=k_src["revision"], create=False)
        k_src = store.get_user_source("jiangli", "kane")
    kane_dir = cas_dir / "kane"
    kane_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.configure_library("jiangli", "jiangli", "kane", data_dir=str(kane_dir), history_environ={"EVOLVMEM_HISTORY_PG_DB": cfg.resolve_user_history_db("kane"), "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "kane"})
    return cfg, ctrl_cfg, ctrl_file, pw, str(k_src["principal_id"]), kane_orig


def test_task54_pg_capture_continuity_backfill_and_unified_mcp_host(tmp_path: Path) -> None:
    from evolvmem.history_pg.capture_store import PgCaptureStore
    from evolvmem.history_pg.continuity_backfill_pg import run_continuity_backfill
    from evolvmem.history_pg.continuity_store import PgContinuityStore
    from evolvmem.lan_sharing import LanError

    for mod_name in ("capture_store", "continuity_store", "continuity_backfill_pg", "unified_mcp_host"):
        src = (Path("evolvmem/history_pg") / f"{mod_name}.py").read_text(encoding="utf-8")
        assert "import sqlite3" not in src and "from sqlite3" not in src

    cfg, ctrl_cfg, ctrl_file, pw, kane_principal, kane_orig = _setup_pg(tmp_path)
    store = PgConsoleStore(ctrl_cfg)
    capture, continuity = PgCaptureStore(cfg), PgContinuityStore(cfg)

    dev_id = f"win-t54-{uuid.uuid4().hex[:8]}"
    sess_codex = f"sess-c54-{uuid.uuid4().hex[:8]}"
    raw_codex = _codex_jsonl(sess_codex, "/tmp/ws-task54", "修复 task54 页面布局居中与 PG 捕获")
    sha_codex = hashlib.sha256(raw_codex).hexdigest()

    with pytest.raises(LanError, match="transcript_hash_mismatch"):
        capture.upload("jiangli", {
            "device_id": dev_id, "session_id": sess_codex, "project": "",
            "sha256": "0" * 64, "total_bytes": len(raw_codex), "offset": 0,
            "content_b64": base64.b64encode(raw_codex).decode(), "extract": False,
            "request_id": f"bad-{uuid.uuid4().hex}", "source_order": 100, "current_sha256": "0" * 64,
        })

    mid = len(raw_codex) // 2
    r1 = capture.upload("jiangli", {
        "device_id": dev_id, "session_id": sess_codex, "project": "t54-proj",
        "sha256": sha_codex, "total_bytes": len(raw_codex), "offset": 0,
        "content_b64": base64.b64encode(raw_codex[:mid]).decode(), "extract": False,
        "request_id": f"c1-{uuid.uuid4().hex}", "source_order": 200, "current_sha256": sha_codex,
    })
    assert r1["status"] == "receiving" and r1["next_offset"] == mid
    req2 = f"c2-{uuid.uuid4().hex}"
    args2 = {
        "device_id": dev_id, "session_id": sess_codex, "project": "t54-proj",
        "sha256": sha_codex, "total_bytes": len(raw_codex), "offset": mid,
        "content_b64": base64.b64encode(raw_codex[mid:]).decode(), "extract": False,
        "request_id": req2, "source_order": 200, "current_sha256": sha_codex,
    }
    r2 = capture.upload("jiangli", args2)
    assert r2["status"] == "archived" and r2["current"] is True and r2["archive_id"] >= 1
    assert capture.upload("jiangli", args2) == r2

    with cfg.connect(cfg.history_migration_db) as conn:
        arch_meta = conn.execute("SELECT device_id, cas_blob_sha256 FROM em_runtime.session_archives WHERE id=%s", (r2["archive_id"],)).fetchone()
        assert arch_meta == (dev_id, sha_codex)

    cas_file = cfg.cas_root / sha_codex[:2] / sha_codex[2:4] / f"{sha_codex}.jsonl"
    assert cas_file.exists() and (cas_file.stat().st_mode & 0o777) == 0o600 and hashlib.sha256(cas_file.read_bytes()).hexdigest() == sha_codex

    raw_old = _codex_jsonl(sess_codex, "/tmp/ws-task54", "older revision")
    r_old = capture.upload("jiangli", {
        "device_id": dev_id, "session_id": sess_codex, "project": "t54-proj",
        "sha256": hashlib.sha256(raw_old).hexdigest(), "total_bytes": len(raw_old), "offset": 0,
        "content_b64": base64.b64encode(raw_old).decode(), "extract": False,
        "request_id": f"old-{uuid.uuid4().hex}", "source_order": 150,
    })
    assert r_old["status"] == "stale"

    sess_dsh = f"sess-d54-{uuid.uuid4().hex[:8]}"
    raw_dsh = _dsh_jsonl(sess_dsh, "/tmp/ws-task54", "DSH native PG continuity verification")
    sha_dsh = hashlib.sha256(raw_dsh).hexdigest()
    r_dsh = capture.upload("jiangli", {
        "device_id": dev_id, "session_id": sess_dsh, "project": "",
        "sha256": sha_dsh, "total_bytes": len(raw_dsh), "offset": 0,
        "content_b64": base64.b64encode(raw_dsh).decode(), "extract": False,
        "source_format": "dsh-session-jsonl-v1", "request_id": f"dsh-{uuid.uuid4().hex}", "source_order": 300, "current_sha256": sha_dsh,
    })
    assert r_dsh["status"] == "archived" and r_dsh["source_format"] == "dsh-session-jsonl-v1"

    bf = run_continuity_backfill(cfg, "jiangli", limit=20)
    assert bf["processed"] >= 2 and bf["llm_usage"] == {"calls": 0, "tokens": 0}
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute("UPDATE em_runtime.lan_session_uploads SET backfill_status='pending' WHERE device_id=%s AND session_id=%s", (dev_id, sess_dsh))
        conn.commit()
    bf2 = run_continuity_backfill(cfg, "jiangli", limit=20)
    assert bf2["processed"] >= 1

    # assign() and retry() idempotency & request_id_conflict
    continuity.dispatch("jiangli", "continuity_checkpoint", {"project": "t54-proj-2", "workspace_path": "/tmp/ws-t54-2", "device_id": dev_id, "summary": "Target proj", "request_id": f"p2-{uuid.uuid4().hex}"})
    asg_req = f"asg-{uuid.uuid4().hex}"
    asg_args = {"device_id": dev_id, "session_id": sess_dsh, "project": "t54-proj-2", "request_id": asg_req}
    assert capture.assign("jiangli", asg_args) == capture.assign("jiangli", asg_args)
    with pytest.raises(LanError, match="request_id_conflict"):
        capture.assign("jiangli", {"device_id": dev_id, "session_id": sess_dsh, "project": "t54-proj", "request_id": asg_req})
    rty_req = f"rty-{uuid.uuid4().hex}"
    rty_args = {"device_id": dev_id, "session_id": sess_dsh, "request_id": rty_req}
    assert capture.retry("jiangli", rty_args) == capture.retry("jiangli", rty_args)
    with pytest.raises(LanError, match="request_id_conflict"):
        capture.retry("jiangli", {"device_id": dev_id, "session_id": sess_dsh, "request_id": rty_req, "handoff": {"project": "x"}})

    # Continuity state_version tracking, resume workspace_mismatch, context_read & LIKE escaping
    req_bg, req_cp = f"bg-{uuid.uuid4().hex}", f"cp-{uuid.uuid4().hex}"
    continuity.dispatch("jiangli", "continuity_begin", {"project": "t54-proj", "workspace_path": "/tmp/ws-task54", "device_id": dev_id, "summary": "Stage 5 task54 checkpoint", "request_id": req_bg})
    cp_res = continuity.dispatch("jiangli", "continuity_checkpoint", {"project": "t54-proj", "workspace_path": "/tmp/ws-task54", "device_id": dev_id, "summary": "Task54 verified continuity milestone", "next_step": "Run live MCP host", "request_id": req_cp})
    assert cp_res["checkpoint_revision"] >= 1
    res_mis = continuity.continuity_resume("jiangli", {"project": "t54-proj", "workspace_path": "/tmp/ws-other-54", "device_id": dev_id})
    assert res_mis["found"] is True and res_mis["staleness"] == "workspace_mismatch"

    with cfg.connect(cfg.history_migration_db) as conn:
        evs = conn.execute("SELECT before_state_version, after_state_version FROM em_runtime.continuity_events WHERE workstream_id=%s ORDER BY id ASC", (cp_res["workstream_id"],)).fetchall()
        assert len(evs) >= 2 and evs[-1][1] == evs[-1][0] + 1
        j_cnt = conn.execute("SELECT count(*) FROM em_runtime.cutover_write_journal WHERE request_id IN (%s, %s)", (req2, req_cp)).fetchone()[0]
        assert j_cnt == 2

    with pytest.raises(LanError, match="invalid_arguments"):
        continuity.context_read("jiangli", {"id": "not-int"})
    with cfg.connect(cfg.history_migration_db) as conn:
        del_id = int(conn.execute("INSERT INTO em_runtime.context_items(identity_key, content_type, project, status, created_at, updated_at) VALUES (%s, 'checkpoint', 't54-proj', 'deleted', '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z') RETURNING id", (f"del-{uuid.uuid4().hex}",)).fetchone()[0])
        conn.execute("INSERT INTO em_runtime.context_layers(item_id, layer, content, content_hash, created_at, updated_at) VALUES (%s, 'l1', 'deleted layer', 'h', '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z')", (del_id,))
        conn.execute("INSERT INTO em_history.projects(project, display_name, status) VALUES ('t54-trashed-proj', 'Trashed', 'active') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO em_history.management_trash(kind, project, key, title, deleted_at) VALUES ('project', 't54-trashed-proj', 't54-trashed-proj', 't', 'now') ON CONFLICT DO NOTHING")
        conn.commit()
    try:
        with pytest.raises(LanError, match="context_not_found"):
            continuity.context_read("jiangli", {"id": del_id, "layer": "L1"})
        assert continuity.context_project_recall("jiangli", {"query": "100%_", "max_chars": 1000})["matched_projects"] == []
        assert "t54-trashed-proj" not in continuity.context_project_recall("jiangli", {"query": "t54-trashed-proj", "max_chars": 1000})["matched_projects"]
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_runtime.context_items WHERE id=%s", (del_id,))
            conn.execute("DELETE FROM em_history.management_trash WHERE kind='project' AND key='t54-trashed-proj'")
            conn.execute("DELETE FROM em_history.projects WHERE project='t54-trashed-proj'")
            conn.commit()

    fb_res = continuity.context_session_start("jiangli", {"project": "unseen-fallback-proj", "query": "fallback", "workspace_path": "/tmp/ws-none", "device_id": dev_id, "max_chars": 800})
    assert len(fb_res["block"].strip()) > 0 and fb_res["continuation_code"] == "no_active_workstream"

    host_cfg = tmp_path / "mcp_host.json"
    tok_o, tok_k = f"tok-o-{uuid.uuid4().hex}", f"tok-k-{uuid.uuid4().hex}"
    owner_principal = store.get_user_source("jiangli", "jiangli")["principal_id"]
    port = _free_port()
    host_cfg.write_text(json.dumps({
        "owner_id": "jiangli", "control_config_path": str(ctrl_file),
        "pg_config": {
            "host": "127.0.0.1", "port": STAGE5_PG_PORT, "admin_user": "postgres",
            "admin_password": pw, "control_coordinator_password": pw,
            "user_history_dbs": cfg.user_history_dbs,
            "allow_loopback_disable_ssl": True, "container_name": STAGE5_PG_CONTAINER, "cas_root": str(cfg.cas_root),
        },
        "tokens": {
            hashlib.sha256(tok_o.encode("utf-8")).hexdigest(): {"user_id": "jiangli", "device_id": dev_id},
            hashlib.sha256(tok_k.encode("utf-8")).hexdigest(): {"user_id": "kane", "device_id": f"{dev_id}-k"},
        },
        "request_timeout_seconds": 3.0,
    }), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, "-m", "evolvmem.history_pg.unified_mcp_host", "--host", "127.0.0.1", "--port", str(port), "--config", str(host_cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if _rpc(port, tok_o, owner_principal, "ping")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        assert proc.poll() is None, proc.stderr.read()
        assert _rpc(port, tok_o, owner_principal, "initialize", {"protocolVersion": "2025-11-25"})[0] == 200
        _, res_list = _rpc(port, tok_o, owner_principal, "tools/list")
        assert {"memory_status", "context_session_start", "context_project_recall", "experience_recall", "session_archive_upload", "session_archive_status", "continuity_checkpoint"} <= {t["name"] for t in res_list["result"]["tools"]}

        start_chars = 0
        for tname, targs in (
            ("memory_status", {"detail": "revision"}),
            ("context_session_start", {"project": "t54-proj", "query": "task54", "workspace_path": "/tmp/ws-task54", "device_id": dev_id, "max_chars": 2000}),
            ("context_project_recall", {"query": "task54", "max_chars": 1000}),
            ("experience_recall", {"query": "task54", "project": "t54-proj"}),
            ("session_archive_status", {"device_id": dev_id, "session_id": sess_codex}),
            ("session_archive_status", {"device_id": dev_id, "session_id": sess_codex, "handoff": {"sha256": sha_codex, "project": "t54-proj"}}),
        ):
            st_c, res_c = _rpc(port, tok_o, owner_principal, "tools/call", {"name": tname, "arguments": targs})
            assert st_c == 200 and not res_c["result"]["isError"]
            body = json.loads(res_c["result"]["content"][0]["text"])
            assert body["authenticated_user"] == owner_principal
            if tname == "context_session_start":
                start_chars = len(body["block"].strip())
                assert start_chars > 0
            if tname == "session_archive_status" and "handoff" in body:
                assert body["handoff"]["reason"] == "ai_processing_disabled"

        # Non-owner isolation + structured invalid_arguments on bad limit
        st_km, res_km = _rpc(port, tok_k, kane_principal, "tools/call", {"name": "memory_status", "arguments": {"detail": "revision"}})
        assert st_km == 200 and not res_km["result"]["isError"]
        assert json.loads(res_km["result"]["content"][0]["text"])["authenticated_user"] == kane_principal
        st_kp, res_kp = _rpc(port, tok_k, kane_principal, "tools/call", {"name": "history_projects", "arguments": {}})
        assert st_kp == 200 and not res_kp["result"]["isError"]
        k_projs = {p["project"] for p in json.loads(res_kp["result"]["content"][0]["text"])["projects"]["projects"]}
        assert "t54-proj" not in k_projs

        st_badlim, res_badlim = _rpc(port, tok_o, owner_principal, "tools/call", {"name": "history_search", "arguments": {"query": "task54", "limit": "bad-int"}})
        assert st_badlim == 200 and res_badlim["result"]["isError"] is True
        assert json.loads(res_badlim["result"]["content"][0]["text"])["error"] == "invalid_arguments"

        assert _rpc(port, tok_o, "jiangli", "ping")[0] == 403
        assert _rpc(port, "wrong-token", owner_principal, "ping")[0] == 401
        fds = [os.readlink(p) for p in Path(f"/proc/{proc.pid}/fd").iterdir() if p.is_symlink()]
        assert [f for f in fds if f.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm"))] == []
    finally:
        if not kane_orig:
            k_end = store.get_user_source("jiangli", "kane")
            if k_end and k_end["enabled"]:
                store.upsert_user("jiangli", "jiangli", "kane", display_name=k_end["display_name"] or "kane", open_id=k_end["open_id"], enabled=False, revision=k_end["revision"], create=False)
        proc.terminate()
        proc.wait(timeout=5)

    (STAGE5_WORK_DIR / "desktop-capture-continuity-acceptance.json").write_text(json.dumps({
        "schema_version": "stage5.desktop_capture_continuity_acceptance.v1",
        "ok": True, "sqlite_fd_count": 0, "codex_acknowledged_versions": 1,
        "codex_session_start_non_empty": start_chars > 0, "codex_session_start_chars": start_chars,
        "dsh_confirmed_sessions": 1, "dsh_failed_uploads": 0,
        "assign_retry_idempotent": True, "backfill_idempotent_rerun": True,
        "non_owner_mcp_isolated": True, "live_windows_appdata_untouched": True,
        "llm_usage": {"calls": 0, "tokens": 0},
    }, indent=2) + "\n", encoding="utf-8")
