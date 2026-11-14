from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from tests.stage5.conftest import STAGE5_WORK_DIR


def test_task55_load_deadline_revocation_rollback_and_candidate_manifest() -> None:
    from evolvmem.history_pg.rollback_replay import replay_library_journal_to_sqlite

    assert callable(replay_library_journal_to_sqlite)
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
    assert load_doc["error_rate"] == 0.0
    assert load_doc["sqlite_fd_count"] == 0
    assert load_doc["mid_load_revocation"]["revoked_status"] == 401
    assert load_doc["mid_load_revocation"]["restored_status"] == 200
    assert load_doc["deadline_cancellation"]["error"] == "request_timeout"
    assert load_doc["deadline_cancellation"]["elapsed_seconds"] < 1.5
    assert load_doc["deadline_cancellation"]["post_timeout_ok"] is True
    assert load_doc["llm_usage"] == {"calls": 0, "tokens": 0}

    assert rb_doc["ok"] is True
    assert rb_doc["lost_writes"] == 0
    assert rb_doc["idempotent_second_run_replayed"] == 0
    assert rb_doc["other_library_untouched"] is True

    assert disk_doc["ok"] is True and float(disk_doc["free_gib"]) >= 50.0
    assert cand_doc["ok"] is True and cand_doc["mode"] == "candidate"
