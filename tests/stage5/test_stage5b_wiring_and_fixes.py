from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

from psycopg import sql
import pytest

from evolvmem.archive_storage import ArchiveStorage
from evolvmem.config import Config
from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.experience_v2.pipeline.history_reader import open_history_reader
from evolvmem.experience_v2.pipeline.pg_history_reader import PgHistoryEvidenceVerifier, PgHistoryReader
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.continuity_backfill_pg import main as backfill_main
from evolvmem.history_pg.continuity_store import PgContinuityStore
from evolvmem.history_pg.history_web_pg import _reader_from_env, handle_pg_history_view
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.lan_sharing import LanError
from evolvmem.product_policy import ProductPolicy
from scripts.verify_pg_cutover_manifest import run_verification
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rpc(port: int, token: str, expected_user: str, method: str, params: dict | None = None) -> tuple[int, dict]:
    body = json.dumps({"jsonrpc": "2.0", "id": "5b", "method": method, "params": params or {}}).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/mcp", body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2025-11-25",
            "X-EvolvMem-Expected-User": expected_user,
        })
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


def _base_pg_cfg(tmp_path: Path) -> PgHistoryConfig:
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_user="postgres", admin_password=pw,
        control_coordinator_password=pw,
        user_history_dbs={"jiangli": "evolvmem_history_migration"},
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=cas_dir,
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    return cfg


def test_history_web_pg_403_json_and_env_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _base_pg_cfg(tmp_path)
    st, ctype, body = handle_pg_history_view("GET", "/history/projects", {}, owner_verified=False, pg_config=cfg)
    assert st == 403 and ctype == "application/json" and isinstance(body, str)
    assert json.loads(body) == {"ok": False, "error": "forbidden"}

    monkeypatch.setenv("EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE", "personal")
    monkeypatch.setenv("EVOLVMEM_CONSOLE_OWNER_ID", "jiangli")
    monkeypatch.delenv("EVOLVMEM_HISTORY_PG_DB", raising=False)
    with _reader_from_env(None, cfg) as reader:
        assert reader._dbname == cfg.history_migration_db


def test_candidate_hybrid_search_excludes_project_block_trash_and_bad_max_chars(tmp_path: Path) -> None:
    cfg = _base_pg_cfg(tmp_path)
    bid = f"blk-5b-{uuid.uuid4().hex[:8]}"
    proj = f"proj-5b-{uuid.uuid4().hex[:8]}"
    token_q = f"unique5btoken{uuid.uuid4().hex[:6]}"
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute(
            "INSERT INTO em_history.blocks(block_id, title, projects) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (bid, f"{token_q} title", json.dumps([proj])),
        )
        conn.execute("INSERT INTO em_history.projects(project) VALUES (%s) ON CONFLICT DO NOTHING", (proj,))
        conn.execute("INSERT INTO em_history.block_projects(block_id, project) VALUES (%s, %s) ON CONFLICT DO NOTHING", (bid, proj))
        conn.execute(
            "INSERT INTO em_history.management_trash(kind, project, key, title, deleted_at) VALUES ('project_block', %s, %s, 'unlinked', 'now') ON CONFLICT DO NOTHING",
            (proj, bid),
        )
        conn.commit()
    try:
        with PgHistoryCandidateReader(cfg, user_id="jiangli") as reader:
            assert reader.hybrid_search(token_q, project=proj) == []
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_history.management_trash WHERE kind='project_block' AND project=%s AND key=%s", (proj, bid))
            conn.execute("DELETE FROM em_history.block_projects WHERE block_id=%s", (bid,))
            conn.execute("DELETE FROM em_history.projects WHERE project=%s", (proj,))
            conn.execute("DELETE FROM em_history.blocks WHERE block_id=%s", (bid,))
            conn.commit()

    store = PgContinuityStore(cfg)
    with pytest.raises(LanError, match="invalid_arguments"):
        store.context_session_start("jiangli", {"max_chars": "not-an-int"})


def test_pg_history_reader_decrypts_evom_archive_v2(tmp_path: Path) -> None:
    cfg = _base_pg_cfg(tmp_path)
    dd = tmp_path / "data_dir"
    dd.mkdir(parents=True)
    key = secrets.token_bytes(32)
    (dd / "archive.key").write_bytes(key)
    storage = ArchiveStorage(Config(data_dir=dd, apply_environment=False), key)
    transcript = "\n".join([
        json.dumps({"role": "user", "content": "Check encrypted archive decryption in Stage 5B."}),
        json.dumps({"role": "assistant", "content": "ArchiveStorage unpacked line 2 cleanly."}),
    ])
    envelope = json.dumps({"source_format": "codex-jsonl", "transcript": transcript})
    blob_bytes, manifest = storage.pack(envelope)
    rel_path = "session_archives/enc_test_5b.blob"
    blob_file = dd / rel_path
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(blob_bytes)

    with cfg.connect(cfg.history_migration_db) as conn:
        row = conn.execute(
            "INSERT INTO em_runtime.session_archives(project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, created_at) "
            "VALUES ('proj-enc-5b', 'codex', %s, %s, %s, 'available', '', '2026-11-15T00:00:00Z') RETURNING id",
            (f"ext-5b-{uuid.uuid4().hex[:8]}", rel_path, manifest["sha256"]),
        ).fetchone()
        aid = int(row[0])
        conn.commit()
    try:
        policy = ProductPolicy(
            console_enabled=True, legacy_web_enabled=False, ai_processing_enabled=False,
            experience_v2_enabled=True, experience_gate_mode="review",
            experience_pipeline_enabled=True, experience_backfill_enabled=True, history_pg_enabled=True,
        )
        reader = open_history_reader(dd, policy=policy, pg_config=cfg)
        assert isinstance(reader, PgHistoryReader)
        sess = reader.get_session(f"archive:{aid}")
        assert sess is not None and sess.summary.has_raw_bytes is True and len(sess.records) == 2
        assert "ArchiveStorage unpacked" in sess.records[1].content
        verifier = PgHistoryEvidenceVerifier(reader)
        ok, reason = verifier.verify_all([{"source_ref": f"archive:{aid}", "line_no": 2}])
        assert ok is True and reason == "ok"
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_runtime.session_archives WHERE id=%s", (aid,))
            conn.commit()


def test_unified_mcp_host_experience_manual_and_cli_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _base_pg_cfg(tmp_path)
    pw = cfg.admin_password
    exp_db = f"evolvmem_exp_test5b_{uuid.uuid4().hex[:6]}"
    with cfg.connect("postgres", autocommit=True, statement_timeout_ms=60000, lock_timeout_ms=30000) as pconn:
        pconn.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(cfg.control_coordinator_role), sql.Literal(pw)))
        pconn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(exp_db)))
    try:
        with cfg.connect(exp_db, autocommit=True, statement_timeout_ms=60000, lock_timeout_ms=30000) as econn:
            econn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            apply_experience_migrations(econn)

        pw_file = tmp_path / "coord.pw"
        pw_file.write_text(pw, encoding="utf-8")
        pw_file.chmod(0o600)
        tsec_file = tmp_path / "ticket.sec"
        tsec_file.write_text(secrets.token_hex(32), encoding="utf-8")
        tsec_file.chmod(0o600)

        ctrl_file = tmp_path / "control.json"
        ctrl_file.write_text(json.dumps({
            "host": "127.0.0.1", "port": STAGE5_PG_PORT, "database": cfg.control_db,
            "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
            "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(pw_file)},
        }), encoding="utf-8")
        exp_file = tmp_path / "experience.json"
        exp_file.write_text(json.dumps({
            "host": "127.0.0.1", "port": STAGE5_PG_PORT, "database": exp_db,
            "coordinator_role": "postgres", "coordinator_password_ref": "coord",
            "reader_role": "postgres", "reader_password_ref": "coord",
            "allow_loopback_disable_ssl": True,
            "secret_files": {"coord": str(pw_file), "experience_ticket": str(tsec_file)},
        }), encoding="utf-8")
        hist_file = tmp_path / "history.json"
        hist_file.write_text(json.dumps({
            "host": "127.0.0.1", "port": STAGE5_PG_PORT, "admin_user": "postgres", "admin_password_ref": "coord",
            "coordinator_password_ref": "coord", "reader_password_ref": "coord",
            "history_writer_password_ref": "coord", "history_reader_password_ref": "coord",
            "user_history_dbs": {"jiangli": cfg.history_migration_db},
            "cas_root": str(cfg.cas_root), "allow_loopback_disable_ssl": True,
            "secret_files": {"coord": str(pw_file)},
        }), encoding="utf-8")

        store = PgConsoleStore(PgControlConfig.from_file(ctrl_file))
        if store.get_user_source("jiangli", "jiangli") is None:
            store.create_user("jiangli", "jiangli", data_dir=str(tmp_path / "dd"))
        owner_principal = store.get_user_source("jiangli", "jiangli")["principal_id"]

        pol_file = tmp_path / "policy.json"
        pol_file.write_text(json.dumps({
            "console_enabled": True, "legacy_web_enabled": False, "ai_processing_enabled": False,
            "experience_v2_enabled": True, "experience_gate_mode": "review",
            "experience_pipeline_enabled": True, "experience_backfill_enabled": True, "history_pg_enabled": True,
        }), encoding="utf-8")

        tok = f"tok-5b-{uuid.uuid4().hex}"
        host_cfg = tmp_path / "unified.json"
        host_cfg.write_text(json.dumps({
            "owner_id": "jiangli",
            "pg_config_path": str(hist_file),
            "control_config_path": str(ctrl_file),
            "experience_config_path": str(exp_file),
            "data_dir": str(tmp_path / "dd"),
            "tokens": {hashlib.sha256(tok.encode("utf-8")).hexdigest(): {"user_id": "jiangli", "device_id": "dev-5b"}},
        }), encoding="utf-8")

        port = _free_port()
        env = dict(os.environ, EVOLVMEM_PRODUCT_POLICY_FILE=str(pol_file))
        env.pop("EVOLVMEM_EXPERIENCE_PG_CONFIG", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "evolvmem.history_pg.unified_mcp_host", "--host", "127.0.0.1", "--port", str(port), "--config", str(host_cfg)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    if _rpc(port, tok, owner_principal, "ping")[0] == 200:
                        break
                except Exception:
                    time.sleep(0.2)
            assert proc.poll() is None, proc.stderr.read()
            _, rlist = _rpc(port, tok, owner_principal, "tools/list")
            names = {t["name"] for t in rlist["result"]["tools"]}
            assert {"experience_recall", "experience_propose", "experience_changes_since", "manual_experience_save", "manual_experience_search"} <= names

            st_save, res_save = _rpc(port, tok, owner_principal, "tools/call", {
                "name": "manual_experience_save",
                "arguments": {
                    "category": "系统环境经验",
                    "problem": "Stage 5B PG host save check",
                    "solution": "Route manual experience via PgExperienceStore cleanly",
                    "reusable_experience": "Wire PG specs inside UnifiedMcpHost",
                    "request_id": f"req5b-{uuid.uuid4().hex[:12]}",
                },
            })
            assert st_save == 200 and not res_save["result"]["isError"], res_save
            st_srch, res_srch = _rpc(port, tok, owner_principal, "tools/call", {
                "name": "manual_experience_search",
                "arguments": {"q": "UnifiedMcpHost"},
            })
            assert st_srch == 200 and not res_srch["result"]["isError"]
            assert json.loads(res_srch["result"]["content"][0]["text"])["total"] >= 1
            st_rec, res_rec = _rpc(port, tok, owner_principal, "tools/call", {
                "name": "experience_recall",
                "arguments": {"query": "UnifiedMcpHost"},
            })
            assert st_rec == 200 and not res_rec["result"]["isError"]
        finally:
            proc.terminate()
            proc.wait(timeout=10)

        # CLI continuity_backfill_pg policy off -> no-op; policy on -> log file written
        pol_off = tmp_path / "pol_off.json"
        pol_off.write_text(json.dumps({
            "console_enabled": True, "legacy_web_enabled": False, "ai_processing_enabled": False,
            "experience_v2_enabled": True, "experience_gate_mode": "review",
            "experience_pipeline_enabled": True, "experience_backfill_enabled": True, "history_pg_enabled": False,
        }), encoding="utf-8")
        log_f = tmp_path / "logs" / "backfill.log"
        monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(pol_off))
        assert backfill_main(["--pg-config", str(hist_file), "--user-id", "jiangli", "--limit", "5", "--log-file", str(log_f)]) == 0
        assert not log_f.exists()

        monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(pol_file))
        assert backfill_main(["--pg-config", str(hist_file), "--user-id", "jiangli", "--limit", "5", "--log-file", str(log_f)]) == 0
        assert log_f.is_file() and json.loads(log_f.read_text(encoding="utf-8").splitlines()[-1])["ok"] is True

        # Cutover manifest verifier (--mode cutover)
        man_p, rec_p = tmp_path / "man.json", tmp_path / "rec.json"
        man_p.write_text(json.dumps({"total_tables": 5, "classified_tables": 5, "unclassified_tables": []}), encoding="utf-8")
        rec_p.write_text(json.dumps({"ok": True, "unexplained_diff_count": 0, "content_digests_match": True, "counts": {"blocks_pg": 10, "blocks_embedded": 10, "projects_pg": 2, "projects_embedded": 2}}), encoding="utf-8")
        assert run_verification(manifest_path=man_p, reconciliation_path=rec_p, mode="cutover")["ok"] is True
        rec_p.write_text(json.dumps({"ok": True, "unexplained_diff_count": 0, "content_digests_match": True, "counts": {"blocks_pg": 10, "blocks_embedded": 9, "projects_pg": 2, "projects_embedded": 2}}), encoding="utf-8")
        assert run_verification(manifest_path=man_p, reconciliation_path=rec_p, mode="cutover")["ok"] is False
    finally:
        with cfg.connect("postgres", autocommit=True, statement_timeout_ms=60000, lock_timeout_ms=30000) as pconn:
            pconn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(exp_db)))


def test_lan_stdio_mcp_route_and_changes_since_request_id_strip(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from evolvmem import lan_server
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost

    pw_f = tmp_path / "pw"
    pw_f.write_text("dummy", encoding="utf-8")
    pw_f.chmod(0o600)
    ctrl_cfg = PgControlConfig.from_dict({
        "host": "127.0.0.1", "port": 55434, "database": "evolvmem_control",
        "coordinator_role": "c", "coordinator_password_ref": "c",
        "reader_role": "r", "reader_password_ref": "r",
        "secret_files": {"c": str(pw_f), "r": str(pw_f)},
        "allow_loopback_disable_ssl": True,
    })
    tok = "tok-stdio-5b-test"
    tok_sha = hashlib.sha256(tok.encode("utf-8")).hexdigest()
    host = UnifiedMcpHost(PgHistoryConfig(), ctrl_cfg, owner_id="owner1", tokens={tok_sha: {"user_id": "owner1", "device_id": "dev1"}})
    host.control_store = SimpleNamespace(
        get_user_source=lambda owner, uid: {"id": "owner1", "principal_id": "11111111-1111-1111-1111-111111111111", "enabled": True} if uid == "owner1" else None,
        user_enabled=lambda owner, uid: uid == "owner1",
    )
    host._exp_settings = lambda: object()

    seen_calls = []
    def _fake_v2(name, req_args, **kwargs):
        seen_calls.append((name, dict(req_args)))
        assert name == "experience_changes_since"
        assert "request_id" not in req_args
        return {"changes": [], "next_seq": 0}

    monkeypatch.setattr("evolvmem.history_pg.unified_mcp_host.exp_v2_mcp.call_experience_v2_tool", _fake_v2)

    server = lan_server.make_http_server(None, host="127.0.0.1", port=0, library_host=host, allow_legacy=False)
    thr = threading.Thread(target=server.serve_forever, daemon=True)
    thr.start()
    try:
        tok_file = tmp_path / "tok_file"
        tok_file.write_text(tok, encoding="utf-8")
        tok_file.chmod(0o600)

        for route in ("/mcp", "/library/mcp"):
            client_cfg = tmp_path / f"client_{route.strip('/').replace('/', '_')}.json"
            client_cfg.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}{route}", "token_file": str(tok_file)}), encoding="utf-8")
            data_dir = tmp_path / f"data_{route.strip('/').replace('/', '_')}"
            data_dir.mkdir()
            (data_dir / "config.json").write_text(json.dumps({"lan_mcp_client_config": str(client_cfg)}), encoding="utf-8")
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "experience_changes_since", "arguments": {"since_seq": 0}}},
            ]
            proc = subprocess.run(
                [sys.executable, "-c", "from evolvmem import mcp_server; mcp_server.main()"],
                input="\n".join(json.dumps(r) for r in reqs) + "\n",
                text=True, capture_output=True, timeout=15, env={**os.environ, "EVOLVMEM_DATA_DIR": str(data_dir)},
            )
            assert proc.returncode == 0, proc.stderr
            resps = [json.loads(line) for line in proc.stdout.splitlines()]
            assert [r["id"] for r in resps] == [1, 2, 3], resps
            assert "experience_changes_since" in {t["name"] for t in resps[1]["result"]["tools"]}
            assert not resps[2]["result"].get("isError"), resps[2]

        assert len(seen_calls) == 2

        bad_cfg = tmp_path / "client_evil.json"
        bad_cfg.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/evil", "token_file": str(tok_file)}), encoding="utf-8")
        bad_dir = tmp_path / "data_evil"
        bad_dir.mkdir()
        (bad_dir / "config.json").write_text(json.dumps({"lan_mcp_client_config": str(bad_cfg)}), encoding="utf-8")
        proc_bad = subprocess.run(
            [sys.executable, "-c", "from evolvmem import mcp_server; mcp_server.main()"],
            input=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}) + "\n",
            text=True, capture_output=True, timeout=15, env={**os.environ, "EVOLVMEM_DATA_DIR": str(bad_dir)},
        )
        assert proc_bad.returncode == 0, proc_bad.stderr
        assert tok not in proc_bad.stdout and tok not in proc_bad.stderr
        assert json.loads(proc_bad.stdout.strip())["error"]["message"] == "LAN MCP unavailable"
    finally:
        server.shutdown()
        server.server_close()
