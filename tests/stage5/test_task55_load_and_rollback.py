from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid

from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.rollback_replay import replay_library_journal_to_sqlite
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE, STAGE5_WORK_DIR


def test_task55_load_deadline_revocation_rollback_and_candidate_manifest() -> None:
    out_dir = STAGE5_WORK_DIR
    res = subprocess.run(
        [sys.executable, "scripts/linux/run_stage5_load_and_rollback.py", "--work-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"

    load_doc = json.loads((out_dir / "load-acceptance.json").read_text(encoding="utf-8"))
    rb_doc = json.loads((out_dir / "rollback-drill.json").read_text(encoding="utf-8"))
    disk_doc = json.loads((out_dir / "disk-peak-budget.json").read_text(encoding="utf-8"))
    cand_doc = json.loads((out_dir / "candidate-manifest-verification.json").read_text(encoding="utf-8"))

    assert load_doc["ok"] is True
    assert load_doc["sustained_workers"] == 12 and load_doc["burst_workers"] == 20
    assert load_doc["error_rate"] == 0.0 and load_doc["sqlite_fd_count"] == 0
    rev = load_doc["mid_load_revocation"]
    assert rev["before_status"] == 200 and rev["before_call_status"] == 200
    assert rev["revoked_status"] == 401 and rev["revoked_call_status"] == 401
    assert rev["owner_status_during"] == 200 and rev["owner_call_status_during"] == 200
    assert rev["restored_status"] == 200 and rev["restored_call_status"] == 200
    assert rev["concurrent_burst_requests"] >= 100
    assert load_doc["deadline_cancellation"]["error"] == "request_timeout"
    assert load_doc["deadline_cancellation"]["elapsed_seconds"] < 1.5
    assert load_doc["deadline_cancellation"]["post_timeout_ok"] is True
    assert load_doc["llm_usage"] == {"calls": 0, "tokens": 0}

    assert rb_doc["ok"] is True and rb_doc["lost_writes"] == 0
    assert rb_doc["idempotent_second_run_replayed"] == 0 and rb_doc["other_library_untouched"] is True
    sv = rb_doc["sqlite_verified"]
    expected_keys = {
        "archive", "upload_project", "session_head", "workstream", "continuity_focus",
        "continuity_events", "context_layers", "lan_write_requests", "lan_memory_revision",
        "trash_retained_delete", "trash_restored_absent", "management_ops",
    }
    assert expected_keys <= set(sv.keys()) and all(sv[k] is True for k in expected_keys)

    assert disk_doc["ok"] is True and float(disk_doc["free_gib"]) >= 50.0
    assert cand_doc["ok"] is True and cand_doc["mode"] == "candidate"

    for fname in ("load-acceptance.json", "rollback-drill.json", "disk-peak-budget.json", "candidate-manifest-verification.json"):
        raw = (out_dir / fname).read_text(encoding="utf-8")
        for forbidden in ("/home/jiangli", "jiangli", "kane", "ASUS"):
            assert forbidden not in raw, f"PII {forbidden!r} leaked in {fname}"


def test_task55_rollback_replay_fails_closed_without_mgmt_and_on_unknown_op(tmp_path: Path) -> None:
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_password=pw,
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=tmp_path / "cas",
    )
    ensure_databases_exist(cfg)
    apply_history_schemas(cfg)

    rt_sq = tmp_path / "rt.db"
    import sqlite3
    sqlite3.connect(str(rt_sq)).close()

    tag = uuid.uuid4().hex[:8]
    r_mgmt, r_unk = f"fc-mgmt-{tag}", f"fc-unk-{tag}"
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute(
            "INSERT INTO em_runtime.cutover_write_journal(library_key, request_id, actor_id, op_type, target_table, target_key, payload) "
            "VALUES ('jiangli', %s, 'jiangli', 'history_manage_delete', 'em_history.management_trash', 'k1', '{\"kind\":\"block\",\"project\":\"p1\",\"key\":\"k1\"}'), "
            "('jiangli', %s, 'jiangli', 'unknown_future_op_type', 'em_runtime.unknown', 'k2', '{}')",
            (r_mgmt, r_unk),
        )
        conn.commit()
    try:
        res = replay_library_journal_to_sqlite(cfg, cfg.history_migration_db, "jiangli", runtime_sqlite_path=rt_sq, management_sqlite_path=None)
        assert res["ok"] is False and res["lost_writes"] >= 2
        with cfg.connect(cfg.history_migration_db) as conn:
            flags = conn.execute(
                "SELECT request_id, replayed_to_sqlite FROM em_runtime.cutover_write_journal WHERE request_id IN (%s, %s)",
                (r_mgmt, r_unk),
            ).fetchall()
            assert dict(flags) == {r_mgmt: False, r_unk: False}
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_runtime.cutover_write_journal WHERE request_id IN (%s, %s)", (r_mgmt, r_unk))
            conn.commit()
