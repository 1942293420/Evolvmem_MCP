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
import urllib.request
import uuid

from psycopg import sql
import pytest

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.schema import apply_history_schemas
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


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
        conn.request(
            "POST",
            "/mcp",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "MCP-Protocol-Version": "2025-11-25",
                "X-EvolvMem-Expected-User": expected_user,
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


def _setup_pg(tmp_path: Path) -> tuple[PgHistoryConfig, PgControlConfig, Path, str]:
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = PgHistoryConfig(
        host="127.0.0.1",
        port=STAGE5_PG_PORT,
        admin_user="postgres",
        admin_password=pw,
        control_coordinator_password=pw,
        allow_loopback_disable_ssl=True,
        container_name=STAGE5_PG_CONTAINER,
        cas_root=cas_dir,
    )
    apply_history_schemas(cfg, include_control=True)
    with cfg.connect("postgres", autocommit=True) as pconn:
        pconn.execute(
            sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                sql.Identifier(cfg.control_coordinator_role), sql.Literal(pw)
            )
        )
    coord_pw = tmp_path / "coord.pw"
    coord_pw.write_text(pw, encoding="utf-8")
    coord_pw.chmod(0o600)
    ctrl_file = tmp_path / "control.json"
    ctrl_file.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": STAGE5_PG_PORT,
        "database": cfg.control_db,
        "coordinator_role": cfg.control_coordinator_role,
        "coordinator_password_ref": "coord",
        "allow_loopback_disable_ssl": True,
        "secret_files": {"coord": str(coord_pw)},
    }), encoding="utf-8")
    ctrl_cfg = PgControlConfig.from_file(ctrl_file)
    store = PgConsoleStore(ctrl_cfg)
    if not store.registry_ready("jiangli"):
        store.bootstrap("jiangli", f"oid-{uuid.uuid4().hex}", "Owner", str(cas_dir), {"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db})
    return cfg, ctrl_cfg, ctrl_file, pw


def test_task54_pg_capture_continuity_backfill_and_unified_mcp_host(tmp_path: Path) -> None:
    from evolvmem.history_pg.capture_store import PgCaptureStore
    from evolvmem.history_pg.continuity_backfill_pg import run_continuity_backfill
    from evolvmem.history_pg.continuity_store import PgContinuityStore
    from evolvmem.lan_sharing import LanError

    for mod_name in ("capture_store", "continuity_store", "continuity_backfill_pg", "unified_mcp_host"):
        src = (Path("evolvmem/history_pg") / f"{mod_name}.py").read_text(encoding="utf-8")
        assert "import sqlite3" not in src and "from sqlite3" not in src

    cfg, _ctrl_cfg, ctrl_file, pw = _setup_pg(tmp_path)
    capture = PgCaptureStore(cfg)
    continuity = PgContinuityStore(cfg)

    dev_id = f"win-t54-{uuid.uuid4().hex[:8]}"
    sess_codex = f"sess-c54-{uuid.uuid4().hex[:8]}"
    raw_codex = _codex_jsonl(sess_codex, "/tmp/ws-task54", "修复 task54 页面布局居中与 PG 捕获")
    sha_codex = hashlib.sha256(raw_codex).hexdigest()

    # Bad SHA rejection
    with pytest.raises(LanError, match="transcript_hash_mismatch"):
        capture.upload("jiangli", {
            "device_id": dev_id, "session_id": sess_codex, "project": "",
            "sha256": "0" * 64, "total_bytes": len(raw_codex), "offset": 0,
            "content_b64": base64.b64encode(raw_codex).decode(), "extract": False,
            "request_id": f"bad-{uuid.uuid4().hex}", "source_order": 100, "current_sha256": "0" * 64,
        })

    # Chunked upload (2 chunks) + idempotent replay
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
    r2_replay = capture.upload("jiangli", args2)
    assert r2["status"] == "archived" and r2["current"] is True and r2["archive_id"] >= 1
    assert r2_replay == r2

    cas_file = cfg.cas_root / sha_codex[:2] / sha_codex[2:4] / f"{sha_codex}.jsonl"
    assert cas_file.exists() and (cas_file.stat().st_mode & 0o777) == 0o600
    assert hashlib.sha256(cas_file.read_bytes()).hexdigest() == sha_codex

    # Older source_order -> stale receipt
    raw_old = _codex_jsonl(sess_codex, "/tmp/ws-task54", "older revision")
    sha_old = hashlib.sha256(raw_old).hexdigest()
    r_old = capture.upload("jiangli", {
        "device_id": dev_id, "session_id": sess_codex, "project": "t54-proj",
        "sha256": sha_old, "total_bytes": len(raw_old), "offset": 0,
        "content_b64": base64.b64encode(raw_old).decode(), "extract": False,
        "request_id": f"old-{uuid.uuid4().hex}", "source_order": 150,
    })
    assert r_old["status"] == "stale"

    # DSH native v4 upload + deterministic continuity_backfill_pg
    sess_dsh = f"sess-d54-{uuid.uuid4().hex[:8]}"
    raw_dsh = _dsh_jsonl(sess_dsh, "/tmp/ws-task54", "DSH native PG continuity verification")
    sha_dsh = hashlib.sha256(raw_dsh).hexdigest()
    r_dsh = capture.upload("jiangli", {
        "device_id": dev_id, "session_id": sess_dsh, "project": "",
        "sha256": sha_dsh, "total_bytes": len(raw_dsh), "offset": 0,
        "content_b64": base64.b64encode(raw_dsh).decode(), "extract": False,
        "source_format": "dsh-session-jsonl-v1",
        "request_id": f"dsh-{uuid.uuid4().hex}", "source_order": 300, "current_sha256": sha_dsh,
    })
    assert r_dsh["status"] == "archived" and r_dsh["source_format"] == "dsh-session-jsonl-v1"

    bf = run_continuity_backfill(cfg, "jiangli", limit=20)
    assert bf["processed"] >= 2 and bf["llm_usage"] == {"calls": 0, "tokens": 0}

    # Continuity checkpoint + session_start + journal verification
    req_cp = f"cp-{uuid.uuid4().hex}"
    continuity.dispatch("jiangli", "continuity_begin", {
        "project": "t54-proj", "workspace_path": "/tmp/ws-task54", "device_id": dev_id,
        "summary": "Stage 5 task54 checkpoint", "request_id": f"bg-{uuid.uuid4().hex}",
    })
    cp_res = continuity.dispatch("jiangli", "continuity_checkpoint", {
        "project": "t54-proj", "workspace_path": "/tmp/ws-task54", "device_id": dev_id,
        "summary": "Task54 verified continuity milestone", "next_step": "Run live MCP host",
        "request_id": req_cp,
    })
    assert cp_res["checkpoint_revision"] >= 1
    fb_res = continuity.context_session_start("jiangli", {
        "project": "unseen-fallback-proj", "query": "fallback",
        "workspace_path": "/tmp/ws-none", "device_id": dev_id, "max_chars": 800,
    })
    assert len(fb_res["block"].strip()) > 0 and fb_res["continuation_code"] == "no_active_workstream"
    with cfg.connect(cfg.history_migration_db) as conn:
        j_cnt = conn.execute(
            "SELECT count(*) FROM em_runtime.cutover_write_journal WHERE request_id IN (%s, %s)",
            (req2, req_cp),
        ).fetchone()[0]
        assert j_cnt == 2

    # Live unified_mcp_host subprocess -> check initialize, tools/list, tools/call & ZERO sqlite FDs
    host_cfg = tmp_path / "mcp_host.json"
    token = f"tok-{uuid.uuid4().hex}"
    tok_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    owner_principal = PgConsoleStore(PgControlConfig.from_file(ctrl_file)).get_user_source("jiangli", "jiangli")["principal_id"]
    port = _free_port()
    host_cfg.write_text(json.dumps({
        "owner_id": "jiangli",
        "control_config_path": str(ctrl_file),
        "pg_config": {
            "host": "127.0.0.1", "port": STAGE5_PG_PORT, "admin_user": "postgres",
            "admin_password": pw, "control_coordinator_password": pw,
            "allow_loopback_disable_ssl": True, "container_name": STAGE5_PG_CONTAINER,
            "cas_root": str(cfg.cas_root),
        },
        "tokens": {tok_sha: {"user_id": "jiangli", "device_id": dev_id}},
        "request_timeout_seconds": 3.0,
    }), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "evolvmem.history_pg.unified_mcp_host", "--host", "127.0.0.1", "--port", str(port), "--config", str(host_cfg)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                st, _ = _rpc(port, token, owner_principal, "ping")
                if st == 200:
                    break
            except Exception:
                time.sleep(0.2)
        assert proc.poll() is None, proc.stderr.read()

        st_init, res_init = _rpc(port, token, owner_principal, "initialize", {"protocolVersion": "2025-11-25"})
        assert st_init == 200 and res_init["result"]["protocolVersion"] == "2025-11-25"
        st_list, res_list = _rpc(port, token, owner_principal, "tools/list")
        names = {t["name"] for t in res_list["result"]["tools"]}
        assert {"memory_status", "context_session_start", "context_project_recall", "experience_recall", "session_archive_upload", "session_archive_status", "continuity_checkpoint"} <= names

        for tname, targs in (
            ("memory_status", {"detail": "revision"}),
            ("context_session_start", {"project": "t54-proj", "query": "task54", "workspace_path": "/tmp/ws-task54", "device_id": dev_id, "max_chars": 2000}),
            ("context_project_recall", {"query": "task54", "max_chars": 1000}),
            ("experience_recall", {"query": "task54", "project": "t54-proj"}),
            ("session_archive_status", {"device_id": dev_id, "session_id": sess_codex}),
            ("session_archive_status", {"device_id": dev_id, "session_id": sess_codex, "handoff": {"sha256": sha_codex, "project": "t54-proj"}}),
        ):
            st_c, res_c = _rpc(port, token, owner_principal, "tools/call", {"name": tname, "arguments": targs})
            assert st_c == 200 and not res_c["result"]["isError"]
            body = json.loads(res_c["result"]["content"][0]["text"])
            assert body["authenticated_user"] == owner_principal
            if tname == "context_session_start":
                assert len(body["block"].strip()) > 0
            if tname == "session_archive_status" and "handoff" in body:
                assert body["handoff"]["reason"] == "ai_processing_disabled"

        st_bad_user, _ = _rpc(port, token, "jiangli", "ping")
        assert st_bad_user == 403
        st_bad_tok, _ = _rpc(port, "wrong-token", owner_principal, "ping")
        assert st_bad_tok == 401

        fds = [os.readlink(p) for p in Path(f"/proc/{proc.pid}/fd").iterdir() if p.is_symlink()]
        sqlite_fds = [f for f in fds if f.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm"))]
        assert sqlite_fds == []
    finally:
        proc.terminate()
        proc.wait(timeout=5)
