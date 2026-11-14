from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import psycopg
import pytest

from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig, PoolLimits
from evolvmem.history_pg.local_embedder import LocalBgeEmbedder
from evolvmem.history_pg.migrator_import import import_all_snapshots
from evolvmem.history_pg.migrator_reconcile import reconcile_migration
from evolvmem.history_pg.migrator_snapshot import (
    classify_all_tables,
    default_live_sources,
    take_online_snapshots,
)
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from scripts.linux.manage_test_pg import main as manage_pg_main
from scripts.verify_pg_cutover_manifest import run_verification
from tests.stage5.conftest import (
    STAGE5_PG_CONTAINER,
    STAGE5_PG_PORT,
    STAGE5_PW_FILE,
    STAGE5_WORK_DIR,
)


def _fresh_stage5_pg() -> PgHistoryConfig:
    manage_pg_main(["stop", "--name", STAGE5_PG_CONTAINER])
    assert manage_pg_main([
        "start", "--name", STAGE5_PG_CONTAINER, "--port", str(STAGE5_PG_PORT), "--password-file", str(STAGE5_PW_FILE),
    ]) == 0
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    dev_ids = [
        "win-01c2ee8f-41d1-479c-ba46-97c08c527bca",
        "linux-task-ai-ece15068ea09456697339808ed9f6805",
        "win-acceptance-20261003",
    ]
    from evolvmem.history_pg.config import device_db_name_for_id
    cfg = PgHistoryConfig(
        host="127.0.0.1",
        port=STAGE5_PG_PORT,
        admin_password=pw,
        control_coordinator_password=pw,
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        device_dbs={d: device_db_name_for_id(d) for d in dev_ids},
        cas_root=STAGE5_WORK_DIR / "staging-archives",
        pool_limits=PoolLimits(max_connections=8, statement_timeout_ms=5000, lock_timeout_ms=1000),
        allow_loopback_disable_ssl=True,
        container_name=STAGE5_PG_CONTAINER,
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    return cfg


def test_task53_full_live_snapshot_migration_reconciliation_and_hybrid_search() -> None:
    snap_dir = STAGE5_WORK_DIR / "snapshots"
    snapshots = take_online_snapshots(default_live_sources(), snap_dir)
    for meta in snapshots.values():
        p = Path(meta["snapshot_path"])
        assert p.is_file()
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert len(meta["sha256"]) == 64

    manifest = classify_all_tables(snapshots)
    assert manifest["unclassified_tables"] == []
    assert manifest["total_tables"] >= 100
    assert manifest["classified_tables"] == manifest["total_tables"]

    manifest_path = STAGE5_WORK_DIR / "table-classification-manifest.json"
    manifest_path.write_text(json.dumps(manifest["scrubbed"], indent=2, ensure_ascii=False), encoding="utf-8")

    cfg = _fresh_stage5_pg()
    with LocalBgeEmbedder() as embedder:
        res1 = import_all_snapshots(cfg, snapshots, embedder=embedder, message_embed_limit=256)
        assert res1["llm_usage"] == {"calls": 0, "tokens": 0}
        assert res1["ok"] is True

        # Idempotent second pass must keep exact same row counts without duplicates
        res2 = import_all_snapshots(cfg, snapshots, embedder=None, message_embed_limit=0)
        assert res2["ok"] is True

        rec = reconcile_migration(cfg, snapshots, manifest)
        assert rec["unexplained_diff_count"] == 0, f"Unexpected diffs: {rec['diffs']}"
        assert rec["counts"]["recovery_reconciliation_pg"] == rec["counts"]["recovery_reconciliation_sqlite"]
        assert rec["counts"]["blocks_pg"] == rec["counts"]["blocks_sqlite"]
        assert rec["counts"]["messages_pg"] == rec["counts"]["messages_sqlite"]
        assert rec["counts"]["console_audit_pg"] >= rec["counts"]["console_audit_sqlite"]

        rec_path = STAGE5_WORK_DIR / "migration-reconciliation.json"
        rec_path.write_text(json.dumps(rec["scrubbed"], indent=2, ensure_ascii=False), encoding="utf-8")

        # Verify sequence advancement prevents PK collision on next default identity insert
        with cfg.connect(cfg.history_migration_db) as conn:
            new_id = conn.execute(
                "INSERT INTO em_runtime.session_archives(project, adapter, external_session_id, payload_sha256, created_at) "
                "VALUES ('stage5-seq-probe', 'codex', 'seq-probe-1', '0000', '2026-11-15T00:00:00Z') RETURNING id"
            ).fetchone()[0]
            assert new_id > 23509
            conn.rollback()

        # Real 768-dim hybrid Chinese search on migrated live snapshots
        with PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db, embedder=embedder) as reader:
            hits1 = reader.hybrid_search("布局", limit=10)
            hits2 = reader.hybrid_search("左右空白", limit=10)
            assert len(hits1) >= 1 and len(hits2) >= 1

    # Manifest verification CLI gate
    v_res = run_verification(manifest_path=manifest_path, reconciliation_path=rec_path, mode="migrated")
    assert v_res["ok"] is True
    raw_manifest_txt = manifest_path.read_text(encoding="utf-8")
    raw_rec_txt = rec_path.read_text(encoding="utf-8")
    for forbidden in ("/home/jiangli", "jiangli"):
        assert forbidden not in raw_manifest_txt
        assert forbidden not in raw_rec_txt
