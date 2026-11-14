from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat

import pytest

from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig, PoolLimits, device_db_name_for_id
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
    wal_dir = STAGE5_WORK_DIR / "wal_archive"
    manage_pg_main(["stop", "--name", STAGE5_PG_CONTAINER])
    shutil.rmtree(wal_dir, ignore_errors=True)
    wal_dir.mkdir(parents=True, exist_ok=True)
    assert manage_pg_main([
        "start", "--name", STAGE5_PG_CONTAINER, "--port", str(STAGE5_PG_PORT),
        "--password-file", str(STAGE5_PW_FILE), "--wal-archive-dir", str(wal_dir),
    ]) == 0
    pw = STAGE5_PW_FILE.read_text(encoding="utf-8").strip()
    dev_ids = [
        "win-01c2ee8f-41d1-479c-ba46-97c08c527bca",
        "linux-task-ai-ece15068ea09456697339808ed9f6805",
        "win-acceptance-20261003",
    ]
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_password=pw, control_coordinator_password=pw,
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        device_dbs={d: device_db_name_for_id(d) for d in dev_ids},
        cas_root=STAGE5_WORK_DIR / "staging-archives",
        pool_limits=PoolLimits(max_connections=8, statement_timeout_ms=5000, lock_timeout_ms=1000),
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER,
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)
    return cfg


def test_task53_full_live_snapshot_migration_reconciliation_and_hybrid_search() -> None:
    snap_dir = STAGE5_WORK_DIR / "snapshots"
    snapshots = take_online_snapshots(default_live_sources(), snap_dir)
    for meta in snapshots.values():
        p = Path(meta["snapshot_path"])
        assert p.is_file() and stat.S_IMODE(p.stat().st_mode) == 0o600
        assert len(meta["sha256"]) == 64

    manifest = classify_all_tables(snapshots)
    assert manifest["unclassified_tables"] == []
    assert manifest["total_tables"] >= 100
    assert manifest["classified_tables"] == manifest["total_tables"]

    manifest_path = STAGE5_WORK_DIR / "table-classification-manifest.json"
    manifest_path.write_text(json.dumps(manifest["scrubbed"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cfg = _fresh_stage5_pg()
    with LocalBgeEmbedder() as embedder:
        res1 = import_all_snapshots(cfg, snapshots, embedder=embedder, message_embed_limit=256)
        assert res1["llm_usage"] == {"calls": 0, "tokens": 0} and res1["ok"] is True

        # Verify CSPRNG 0600 per-device secrets (not deterministic hash)
        sec_dev_dir = cfg.cas_root / ".secrets" / "devices"
        assert sec_dev_dir.is_dir() and stat.S_IMODE(sec_dev_dir.stat().st_mode) == 0o700
        for dev_id in cfg.device_dbs:
            tok_file = sec_dev_dir / f"{hashlib.sha256(dev_id.encode('utf-8')).hexdigest()[:24]}.tok"
            assert tok_file.is_file() and stat.S_IMODE(tok_file.stat().st_mode) == 0o600
            tok = tok_file.read_text(encoding="utf-8").strip()
            det = hashlib.sha256(f"stage5-imported-device:{dev_id}".encode()).hexdigest()
            assert len(tok) >= 32 and tok != det

        # Idempotent second pass keeps exact row counts without duplicates
        res2 = import_all_snapshots(cfg, snapshots, embedder=None, message_embed_limit=0)
        assert res2["ok"] is True

        rec = reconcile_migration(cfg, snapshots, manifest)
        assert rec["unexplained_diff_count"] == 0, f"Unexpected diffs: {rec['diffs']}"
        assert rec["content_digests_match"] is True
        assert all(rec["digest_checks"].values())
        c = rec["counts"]
        assert c["recovery_reconciliation_pg"] == c["recovery_reconciliation_sqlite"]
        assert c["blocks_pg"] == c["blocks_sqlite"] and c["messages_pg"] == c["messages_sqlite"]
        assert c["rt_task_history_grants_pg"] == c["rt_task_history_grants_sqlite"]
        assert c["shared_public_context_items_pg"] == c["shared_public_context_items_sqlite"]
        assert c["member_context_items_pg"] == c["member_context_items_sqlite"]
        assert c["console_grants_pg"] == c["console_grants_sqlite"]
        assert c["console_libraries_pg"] == c["console_libraries_sqlite"]
        assert c["console_audit_pg"] >= c["console_audit_sqlite"]
        assert c["projects_embedded"] == c["projects_pg"] and c["blocks_embedded"] == c["blocks_pg"]

        rec_path = STAGE5_WORK_DIR / "migration-reconciliation.json"
        rec_path.write_text(json.dumps(rec["scrubbed"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        with cfg.connect(cfg.history_migration_db) as conn:
            new_id = conn.execute(
                "INSERT INTO em_runtime.session_archives(project, adapter, external_session_id, payload_sha256, created_at) "
                "VALUES ('stage5-seq-probe', 'codex', 'seq-probe-1', '0000', '2026-11-15T00:00:00Z') RETURNING id"
            ).fetchone()[0]
            assert new_id > 23509
            conn.rollback()

        with PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db, embedder=embedder) as reader:
            hits1 = reader.hybrid_search("布局", limit=10)
            hits2 = reader.hybrid_search("左右空白", limit=10)
            assert len(hits1) >= 1 and len(hits2) >= 1

    v_res = run_verification(manifest_path=manifest_path, reconciliation_path=rec_path, mode="migrated")
    assert v_res["ok"] is True
    for raw_txt in (manifest_path.read_text(encoding="utf-8"), rec_path.read_text(encoding="utf-8")):
        for forbidden in ("/home/jiangli", "jiangli", "kane"):
            assert forbidden not in raw_txt
