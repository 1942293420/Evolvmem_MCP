from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from psycopg import sql
import pytest
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.history_web_pg import dispatch_pg_history_api, handle_pg_history_view
from evolvmem.history_pg.matrix_reader import read_matrix
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.knowledge_console import ConsoleService
from evolvmem.product_policy import load_policy
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE, STAGE5_WORK_DIR


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed_history_and_console(cfg: PgHistoryConfig, cas_dir: Path, img_path: Path) -> tuple[PgControlConfig, str, str, str, bool]:
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    with cfg.connect("postgres", autocommit=True) as pconn:
        pconn.execute(
            sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                sql.Identifier(cfg.control_coordinator_role), sql.Literal(cfg.admin_password),
            )
        )

    coord_pw_file = cas_dir.parent / "coord.pw"
    coord_pw_file.write_text(cfg.admin_password, encoding="utf-8")
    coord_pw_file.chmod(0o600)
    ctrl_cfg_path = cas_dir.parent / "control.json"
    ctrl_cfg_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": STAGE5_PG_PORT, "database": cfg.control_db,
        "coordinator_role": cfg.control_coordinator_role, "coordinator_password_ref": "coord",
        "allow_loopback_disable_ssl": True, "secret_files": {"coord": str(coord_pw_file)},
    }), encoding="utf-8")
    ctrl_cfg = PgControlConfig.from_file(ctrl_cfg_path)
    store = PgConsoleStore(ctrl_cfg)
    if not store.registry_ready("jiangli"):
        store.bootstrap(
            "jiangli", "oid-jiangli-52", "我的账号", str(cas_dir),
            {"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db, "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "personal"},
        )
    owner_src = store.get_user_source("jiangli", "jiangli")
    assert owner_src is not None
    if not owner_src["open_id"]:
        store.observe_login("jiangli", "oid-jiangli-52", owner_src["display_name"] or "我的账号")
        store.upsert_user("jiangli", "jiangli", "jiangli", display_name=owner_src["display_name"] or "我的账号", open_id="oid-jiangli-52", enabled=True, revision=owner_src["revision"], create=False)
        owner_src = store.get_user_source("jiangli", "jiangli")
    store.configure_library("jiangli", "jiangli", "jiangli", data_dir=str(cas_dir), history_environ={"EVOLVMEM_HISTORY_PG_DB": cfg.history_migration_db, "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "personal"})

    kane_src = store.get_user_source("jiangli", "kane")
    kane_orig_enabled = bool(kane_src["enabled"]) if kane_src is not None else True
    if kane_src is None:
        store.observe_login("jiangli", "oid-kane-52", "kane")
        store.upsert_user("jiangli", "jiangli", "kane", display_name="kane", open_id="oid-kane-52", enabled=True, revision=None, create=True)
        kane_src = store.get_user_source("jiangli", "kane")
    elif not kane_src["open_id"] or not kane_src["enabled"]:
        target_kane_oid = str(kane_src["open_id"] or "oid-kane-52")
        if not kane_src["open_id"]:
            store.observe_login("jiangli", target_kane_oid, kane_src["display_name"] or "kane")
        store.upsert_user("jiangli", "jiangli", "kane", display_name=kane_src["display_name"] or "kane", open_id=target_kane_oid, enabled=True, revision=kane_src["revision"], create=False)
        kane_src = store.get_user_source("jiangli", "kane")
    store.configure_library("jiangli", "jiangli", "kane", data_dir=str(cas_dir), history_environ={"EVOLVMEM_HISTORY_PG_DB": cfg.resolve_user_history_db("kane"), "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "kane"})

    vec_layout = [0.0] * 768
    vec_layout[0], vec_layout[1] = 0.8, 0.6
    att_sha = hashlib.sha256(img_path.read_bytes()).hexdigest()

    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute("DELETE FROM em_history.management_trash WHERE (kind = 'block' AND key = 'blk-layout-1') OR (kind = 'project' AND key = 'ui-proj')")
        conn.execute(
            "INSERT INTO em_history.projects(project, display_name, status, block_count, message_count, turn_count, attachment_count, first_at, last_at, summary, intro_json) "
            "VALUES ('ui-proj', 'UI Layout Project', 'active', 1, 2, 1, 1, '2026-11-10T01:00:00Z', '2026-11-10T02:00:00Z', '页面布局与左右空白重构', '{}') "
            "ON CONFLICT (project) DO UPDATE SET summary = EXCLUDED.summary"
        )
        conn.execute(
            "INSERT INTO em_history.blocks(block_id, adapter, kind, title, message_count, turn_count, attachment_recovered, observed_first_at, observed_last_at, coverage_state, projects, embedding) "
            "VALUES ('blk-layout-1', 'codex', 'session', '页面布局居中与左右空白收窄', 2, 1, 1, '2026-11-10T01:00:00Z', '2026-11-10T01:05:00Z', 'complete', '[\"ui-proj\"]', %s::vector) "
            "ON CONFLICT (block_id) DO UPDATE SET title = EXCLUDED.title, embedding = EXCLUDED.embedding",
            (str(vec_layout),),
        )
        conn.execute("INSERT INTO em_history.block_projects(block_id, project, evidence) VALUES ('blk-layout-1', 'ui-proj', 'cwd') ON CONFLICT DO NOTHING")
        conn.execute(
            "INSERT INTO em_history.messages(block_id, ordinal, role, content, recorded_at, archive_id, message_index, attachments, embedding) "
            "VALUES ('blk-layout-1', 0, 'user', '修复页面左右空白太大、布局不对称问题', '2026-11-10T01:01:00Z', 99002, 0, %s, %s::vector) "
            "ON CONFLICT (block_id, ordinal) DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding",
            (json.dumps([att_sha]), str(vec_layout)),
        )
        conn.execute("INSERT INTO em_history.attachments(sha256, mime_type, bytes, path, format_ok, occurrences) VALUES (%s, 'image/png', %s, %s, 1, 1) ON CONFLICT (sha256) DO UPDATE SET path = EXCLUDED.path", (att_sha, img_path.stat().st_size, str(img_path)))
        conn.execute("INSERT INTO em_history.coverage_reports(report_key, payload) VALUES ('gap_audit', '{\"ok\": true, \"blocks\": 1}'::jsonb) ON CONFLICT (report_key) DO UPDATE SET payload = EXCLUDED.payload")
        conn.execute("INSERT INTO em_runtime.lan_session_heads(device_id, session_id, sha256, source_order) VALUES ('win-dev-sample', 'sess-52', 'abcd1234', 100) ON CONFLICT DO NOTHING")
        conn.commit()
    return ctrl_cfg, att_sha, str(owner_src["open_id"]), str(kane_src["open_id"]), kane_orig_enabled


def _check_web_api_and_reader_regressions(cfg: PgHistoryConfig, pg_reader: PgHistoryCandidateReader, tmp_path: Path, hist_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_ctx = {"EVOLVMEM_CONSOLE_OWNER_ID": "jiangli", "EVOLVMEM_HISTORY_PG_CONFIG": str(hist_file), "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "personal"}
    st_f, b_f = dispatch_pg_history_api("GET", "/api/history-candidate/projects", {}, {}, environ=env_ctx, owner_verified=False, pg_config=cfg)
    assert st_f == 403 and b_f["error"] == "history_forbidden"

    tok_file = tmp_path / "cand_tok.txt"
    tok_file.write_text("sec-tok-52\n", encoding="utf-8")
    env_tok = {**env_ctx, "EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE": str(tok_file), "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "personal"}
    st_u, _ = dispatch_pg_history_api("GET", "/api/history-candidate/projects", {}, {"x-evolvmem-history-token": "wrong"}, environ=env_tok, owner_verified=False, pg_config=cfg)
    assert st_u == 401
    st_ns, _ = dispatch_pg_history_api("GET", "/api/history-candidate/projects", {}, {"x-evolvmem-history-token": "sec-tok-52", "x-evolvmem-namespace": "kane"}, environ=env_tok, owner_verified=False, pg_config=cfg)
    assert st_ns == 403
    st_ok, _ = dispatch_pg_history_api("GET", "/api/history-candidate/projects", {}, {"x-evolvmem-history-token": "sec-tok-52", "x-evolvmem-namespace": "personal"}, environ=env_tok, owner_verified=False, pg_config=cfg)
    assert st_ok == 200

    st_p403, _ = dispatch_pg_history_api("POST", "/api/history-candidate/management/delete", {"key": "blk-layout-1"}, {"x-evolvmem-action": "history-manage"}, environ=env_ctx, owner_verified=False, pg_config=cfg)
    assert st_p403 == 403
    st_hdr403, _ = dispatch_pg_history_api("POST", "/api/history-candidate/management/delete", {"key": "blk-layout-1"}, {}, environ=env_ctx, owner_verified=True, pg_config=cfg)
    assert st_hdr403 == 403
    st_badint, _ = dispatch_pg_history_api("GET", "/api/history-candidate/block/blk-layout-1/turns", {"limit": "not-int"}, {}, environ=env_ctx, owner_verified=True, pg_config=cfg)
    assert st_badint == 400
    st_view, _, _ = handle_pg_history_view("GET", "/history/projects", {}, owner_verified=False, environ=env_ctx, pg_config=cfg)
    assert st_view == 403

    rtag = uuid.uuid4().hex[:8]
    st_d1, _ = dispatch_pg_history_api("POST", "/api/history-candidate/management/delete", {"kind": "block", "project": "ui-proj", "key": "blk-layout-1", "request_id": f"c52-{rtag}"}, {"X-EvolvMem-Action": "history-manage"}, environ=env_ctx, owner_verified=True, pg_config=cfg)
    assert st_d1 == 200
    st_cfl, _ = dispatch_pg_history_api("POST", "/api/history-candidate/management/delete", {"kind": "block", "project": "ui-proj", "key": "other-key", "request_id": f"c52-{rtag}"}, {"x-evolvmem-action": "history-manage"}, environ=env_ctx, owner_verified=True, pg_config=cfg)
    assert st_cfl == 409
    pg_reader.manage("restore", kind="block", project="ui-proj", key="blk-layout-1")

    # Repeated delete -> restore -> delete WITHOUT request_id
    pg_reader.manage("delete", kind="block", project="ui-proj", key="blk-layout-1")
    assert not pg_reader.block_visible("blk-layout-1")
    ui_entry = [p for p in pg_reader.projects()["projects"] if p["project"] == "ui-proj"][0]
    assert ui_entry["block_count"] == 0
    pg_reader.manage("restore", kind="block", project="ui-proj", key="blk-layout-1")
    assert pg_reader.block_visible("blk-layout-1")
    pg_reader.manage("delete", kind="block", project="ui-proj", key="blk-layout-1")
    assert not pg_reader.block_visible("blk-layout-1")
    pg_reader.manage("restore", kind="block", project="ui-proj", key="blk-layout-1")

    assert pg_reader.block_turn_page("nonexistent-blk-52") == {"ok": False, "error": "block_not_found"}
    bp = pg_reader.block_page("blk-layout-1")
    assert bp["ok"] is True and isinstance(bp["total_messages"], int) and bp["total_messages"] >= 1 and isinstance(bp["has_more"], bool)
    assert pg_reader.hybrid_search("%_", project="ui-proj", limit=5) == []

    # Project soft-delete hiding across CandidateReader + MatrixReader
    mat_before = read_matrix({"id": "jiangli", "is_admin": True}, {"id": "jiangli"}, tmp_path, tmp_path, [], pg_config=cfg)
    pg_reader.manage("delete", kind="project", project="ui-proj", key="ui-proj", title="ui", request_id=f"pdel-{rtag}")
    try:
        assert not any(p["project"] == "ui-proj" for p in pg_reader.projects()["projects"])
        assert pg_reader.project_overview("ui-proj")["ok"] is False
        assert pg_reader.block_page("blk-layout-1")["ok"] is False
        assert pg_reader.block_turn_page("blk-layout-1")["ok"] is False
        assert not any(h["block_id"] == "blk-layout-1" for h in pg_reader.hybrid_search("布局", limit=10))
        mat_after = read_matrix({"id": "jiangli", "is_admin": True}, {"id": "jiangli"}, tmp_path, tmp_path, [], pg_config=cfg)
        assert mat_after["stats"]["coreMemoryCount"] == mat_before["stats"]["coreMemoryCount"] - 1
    finally:
        pg_reader.manage("restore", kind="project", project="ui-proj", key="ui-proj", title="ui", request_id=f"prst-{rtag}")

    # Multi-message block dedup before LIMIT
    with cfg.connect(cfg.history_migration_db) as conn:
        for bid, n_msgs in (("blk-multi-52", 5), ("blk-single-52", 1)):
            conn.execute("INSERT INTO em_history.blocks(block_id, adapter, kind, title, projects) VALUES (%s, 'codex', 'session', %s, '[\"ui-proj\"]') ON CONFLICT (block_id) DO NOTHING", (bid, bid))
            conn.execute("INSERT INTO em_history.block_projects(block_id, project) VALUES (%s, 'ui-proj') ON CONFLICT DO NOTHING", (bid,))
            for ord_i in range(n_msgs):
                conn.execute("INSERT INTO em_history.messages(block_id, ordinal, role, content) VALUES (%s, %s, 'user', 'uniqdedup52 token') ON CONFLICT DO NOTHING", (bid, ord_i))
        conn.commit()
    try:
        hits_dd = pg_reader.hybrid_search("uniqdedup52", limit=2)
        assert {h["block_id"] for h in hits_dd} == {"blk-multi-52", "blk-single-52"}
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_history.messages WHERE block_id IN ('blk-multi-52', 'blk-single-52')")
            conn.execute("DELETE FROM em_history.blocks WHERE block_id IN ('blk-multi-52', 'blk-single-52')")
            conn.commit()


def test_pg_web_runner_zero_sqlite_fds_and_hybrid_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps({
        "console_enabled": True, "legacy_web_enabled": False,
        "ai_processing_enabled": False, "history_pg_enabled": True,
    }))
    monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(pf))
    pol = load_policy()
    assert pol.history_pg_enabled is True and pol.ai_processing_enabled is False and pol.as_dict()["history_pg_enabled"] is True

    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    img_path = tmp_path / "pixel.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde")

    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_password=pw, control_coordinator_password=pw,
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=cas_dir,
    )
    ctrl_cfg, att_sha, owner_oid, kane_oid, kane_orig_enabled = _seed_history_and_console(cfg, cas_dir, img_path)

    ctrl_file = tmp_path / "control.json"
    hist_file = tmp_path / "history.json"
    hist_file.write_text(json.dumps({
        "host": "127.0.0.1", "port": STAGE5_PG_PORT, "admin_user": cfg.admin_user, "admin_password": pw,
        "control_coordinator_role": cfg.control_coordinator_role, "control_coordinator_password": pw,
        "user_history_dbs": cfg.user_history_dbs, "cas_root": str(cas_dir), "allow_loopback_disable_ssl": True,
    }))
    monkeypatch.setenv("EVOLVMEM_HISTORY_PG_CONFIG", str(hist_file))

    svc = ConsoleService(tmp_path / "__unused_pg_console.db", "jiangli", tmp_path, tmp_path / "lan", {}, pg_control=ctrl_cfg)
    owner_sess, kane_sess = {"open_id": owner_oid, "csrf": "t1"}, {"open_id": kane_oid, "csrf": "t2"}

    st_p, _, res_p = svc.dispatch("GET", "/libraries/jiangli/history/projects", {}, owner_sess)
    assert st_p == 200 and any(p["project"] == "ui-proj" for p in res_p["projects"])
    st_s1, _, res_s1 = svc.dispatch("GET", "/libraries/jiangli/history/search", {"q": "布局"}, owner_sess)
    st_s2, _, res_s2 = svc.dispatch("GET", "/libraries/jiangli/history/search", {"q": "左右空白"}, owner_sess)
    assert st_s1 == 200 and any(h["block_id"] == "blk-layout-1" for h in res_s1["items"])
    assert st_s2 == 200 and any(h["block_id"] == "blk-layout-1" for h in res_s2["items"])
    st_a, ctype_a, body_a = svc.dispatch("GET", f"/libraries/jiangli/history/attachment/{att_sha}", {}, owner_sess)
    assert st_a == 200 and ctype_a == "image/png" and len(body_a) > 0

    try:
        st_kane, _, res_kane = svc.dispatch("GET", "/libraries/jiangli/history/projects", {}, kane_sess)
        assert st_kane == 403 and res_kane.get("ok") is False and res_kane.get("error") in (
            "library_read_forbidden", "history_access_denied", "forbidden", "library_access_denied",
        )
    finally:
        if not kane_orig_enabled:
            rst_store = PgConsoleStore(ctrl_cfg)
            k_cur = rst_store.get_user_source("jiangli", "kane")
            if k_cur is not None and k_cur["enabled"]:
                rst_store.upsert_user("jiangli", "jiangli", "kane", display_name=k_cur["display_name"] or "kane", open_id=k_cur["open_id"], enabled=False, revision=k_cur["revision"], create=False)

    mat = read_matrix({"id": "jiangli", "is_admin": True}, {"id": "jiangli"}, tmp_path, tmp_path, [], pg_config=cfg)
    assert mat["pgBacked"] is True and mat["stats"]["coreMemoryCount"] >= 1 and mat["stats"]["lanSessionHeads"] >= 1

    with PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db) as pg_reader:
        hits_vec = pg_reader.hybrid_search("布局", query_vec=[0.8, 0.6] + [0.0] * 766, limit=10)
        assert any(h["block_id"] == "blk-layout-1" for h in hits_vec)
        hits_vec_proj = pg_reader.hybrid_search("布局", project="ui-proj", query_vec=[0.8, 0.6] + [0.0] * 766, limit=5)
        assert hits_vec_proj[0]["block_id"] == "blk-layout-1"
        run_tag = uuid.uuid4().hex[:8]
        pg_reader.manage("delete", kind="block", project="ui-proj", key="blk-layout-1", title="页面布局", request_id=f"req-del-52-{run_tag}")
        assert not pg_reader.block_visible("blk-layout-1")
        pg_reader.manage("restore", kind="block", project="ui-proj", key="blk-layout-1", title="页面布局", request_id=f"req-rst-52-{run_tag}")
        assert pg_reader.block_visible("blk-layout-1")
        _check_web_api_and_reader_regressions(cfg, pg_reader, tmp_path, hist_file, monkeypatch)

    skill_root = Path("/home/jiangli/.claude/evolvmem/skills")
    before_skill_cnt = sum(1 for _ in skill_root.rglob("SKILL.md")) if skill_root.exists() else 0
    port = _free_port()
    env = {
        **os.environ,
        "EVOLVMEM_PRODUCT_POLICY_FILE": str(pf), "EVOLVMEM_CONSOLE_PG_CONFIG": str(ctrl_file),
        "EVOLVMEM_HISTORY_PG_CONFIG": str(hist_file), "EVOLVMEM_CONSOLE_OWNER_ID": "jiangli",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "evolvmem.history_pg.web_pg_runner", "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/knowledge-console/me", timeout=1.0)
                break
            except Exception as exc:
                if hasattr(exc, "code") and exc.code in (401, 403):
                    break
                time.sleep(0.2)
        assert proc.poll() is None, f"web_pg_runner exited prematurely: {proc.stderr.read()}"
        fds_dir = Path(f"/proc/{proc.pid}/fd")
        sqlite_fds = [os.readlink(p) for p in fds_dir.iterdir() if p.is_symlink() and os.readlink(p).endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm"))]
        assert sqlite_fds == [], f"Unexpected SQLite FDs in PG web process: {sqlite_fds}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    after_skill_cnt = sum(1 for _ in skill_root.rglob("SKILL.md")) if skill_root.exists() else 0
    assert before_skill_cnt == after_skill_cnt
    (STAGE5_WORK_DIR / "web-mcp-feature-acceptance.json").write_text(json.dumps({
        "ok": True, "sqlite_fd_count": 0, "ai_processing_enabled": pol.ai_processing_enabled,
        "layout_hits": len(res_s1["items"]), "margin_hits": len(res_s2["items"]),
        "attachment_bytes": len(body_a), "matrix_pg_backed": mat["pgBacked"],
        "skills_untouched": True, "skill_count": after_skill_cnt,
        "llm_usage": {"calls": 0, "tokens": 0},
    }, indent=2) + "\n", encoding="utf-8")
