from __future__ import annotations

import inspect
import json
from pathlib import Path
import shutil
import subprocess
import time

import psycopg
import pytest

from evolvmem.history_pg import schema as schema_mod, wal_archive as wal_mod
from evolvmem.history_pg.config import PgHistoryConfig, PoolLimits
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.history_pg.wal_archive import (
    archive_wal_segment,
    generate_prod_wal_config_patch,
    run_pitr_restore_drill,
    take_base_backup,
)
from evolvmem.pg_test_guard import ProductionPostgresGuardError
from scripts.linux.manage_test_pg import main as manage_pg_main
from tests.stage5.conftest import (
    STAGE5_PG_CONTAINER,
    STAGE5_PG_PORT,
    STAGE5_PITR_CONTAINER,
    STAGE5_PITR_PORT,
    STAGE5_PW_FILE,
    STAGE5_WORK_DIR,
)


def _read_pw() -> str:
    return STAGE5_PW_FILE.read_text(encoding="utf-8").strip()


def test_prod_guard_blocks_55432_everywhere() -> None:
    with pytest.raises(ProductionPostgresGuardError):
        take_base_backup("evolvmem-pg-prod", STAGE5_WORK_DIR / "tmp_bak", port=55432, password_file=STAGE5_PW_FILE)
    with pytest.raises(ProductionPostgresGuardError):
        PgHistoryConfig(host="127.0.0.1", port=55432, admin_password="x").validate_non_prod_target()


def test_wal_archive_segment_atomic_and_verified(tmp_path: Path) -> None:
    src = tmp_path / "000000010000000000000001"
    src.write_bytes(b"WAL-SEGMENT-BYTES-V1")
    arc = tmp_path / "wal_arc"
    res1 = archive_wal_segment(src, arc, src.name)
    assert res1["status"] == "archived"
    assert (arc / src.name).exists()
    assert (arc / f"{src.name}.sha256").exists()
    res2 = archive_wal_segment(src, arc, src.name)
    assert res2["status"] == "already_archived_verified"
    bad = tmp_path / "bad_seg"
    bad.write_bytes(b"DIFFERENT-BYTES")
    with pytest.raises(ValueError, match="wal_checksum_conflict"):
        archive_wal_segment(bad, arc, src.name)

    special_pw = "p@ss'w word=1"
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_password="a",
        history_reader_password=special_pw, allow_loopback_disable_ssl=True,
    )
    dsn = cfg.dsn_for_role("evolvmem_history_migration", role_kind="reader")
    assert psycopg.conninfo.conninfo_to_dict(dsn)["password"] == special_pw
    for fn in (wal_mod.take_base_backup, schema_mod._run_control_psql):
        src_txt = inspect.getsource(fn)
        assert 'PGPASSWORD=' not in src_txt or 'export PGPASSWORD' in src_txt
        assert '"-e"' not in src_txt
        assert "IFS= read -r PGPASSWORD" in src_txt


def test_isolated_pg_wal_schema_and_pitr_drill(tmp_path: Path) -> None:
    wal_dir = STAGE5_WORK_DIR / "wal_archive"
    bak_dir = STAGE5_WORK_DIR / "pitr_basebackup"
    manage_pg_main(["stop", "--name", STAGE5_PG_CONTAINER])
    shutil.rmtree(wal_dir, ignore_errors=True)
    shutil.rmtree(bak_dir, ignore_errors=True)
    wal_dir.mkdir(parents=True, exist_ok=True)
    assert manage_pg_main([
        "start", "--name", STAGE5_PG_CONTAINER, "--port", str(STAGE5_PG_PORT),
        "--password-file", str(STAGE5_PW_FILE), "--wal-archive-dir", str(wal_dir),
    ]) == 0

    pw = _read_pw()
    cfg = PgHistoryConfig(
        host="127.0.0.1", port=STAGE5_PG_PORT, admin_user="postgres", admin_password=pw,
        control_db="evolvmem_control", history_migration_db="evolvmem_history_migration",
        shared_public_db="evolvmem_shared_public",
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        device_dbs={"win-dev-sample": "evolvmem_dev_win_sample"},
        cas_root=STAGE5_WORK_DIR / "cas",
        pool_limits=PoolLimits(max_connections=8, statement_timeout_ms=3000, lock_timeout_ms=500),
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER,
    )
    ensure_databases_exist(cfg, include_control=True)
    applied = apply_history_schemas(cfg, include_control=True)
    assert "evolvmem_control" in applied
    assert "evolvmem_history_migration" in applied
    assert "evolvmem_dev_win_sample" in applied

    with psycopg.connect(cfg.admin_dsn("evolvmem_history_migration"), autocommit=True) as conn:
        assert conn.execute("SHOW wal_level").fetchone()[0] == "replica"
        assert conn.execute("SHOW archive_mode").fetchone()[0] == "on"
        ext_names = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        assert {"vector", "pg_trgm"}.issubset(ext_names)
        idx_names = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname IN ('em_history', 'em_runtime')").fetchall()}
        assert any("hnsw" in n for n in idx_names)
        assert any("trgm" in n or "tsv" in n for n in idx_names)
        assert {
            "idx_em_hist_msgs_archive", "idx_em_hist_bp_project", "idx_em_rt_ctx_identity",
            "idx_em_rt_uploads_archive", "idx_em_rt_journal_replay", "idx_em_rt_ws_proj",
        }.issubset(idx_names)

        # Least-privilege RBAC assertions
        can_login = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (cfg.history_owner_role,)).fetchone()[0]
        assert can_login is False
        pub_grants = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = 'PUBLIC' AND table_schema IN ('em_history', 'em_runtime')"
        ).fetchone()[0]
        assert int(pub_grants) == 0

        # Crash-idempotent archive_command shell retry against temp dir
        live_arc_cmd = conn.execute("SHOW archive_command").fetchone()[0]
        sim_arc = tmp_path / "sim_wal_archive"
        sim_arc.mkdir(mode=0o700)
        sim_src = tmp_path / "0000000100000000000000AA"
        sim_src.write_bytes(b"WAL-RETRY-V1")
        cmd_ok = live_arc_cmd.replace("/wal_archive", str(sim_arc)).replace("%p", str(sim_src)).replace("%f", sim_src.name)
        assert subprocess.run(["sh", "-c", cmd_ok], check=False).returncode == 0
        assert ((sim_arc / sim_src.name).stat().st_mode & 0o777) == 0o600
        assert subprocess.run(["sh", "-c", cmd_ok], check=False).returncode == 0
        sim_conflict = tmp_path / "conflict_seg"
        sim_conflict.write_bytes(b"WAL-CONFLICT-V2")
        cmd_bad = live_arc_cmd.replace("/wal_archive", str(sim_arc)).replace("%p", str(sim_conflict)).replace("%f", sim_src.name)
        assert subprocess.run(["sh", "-c", cmd_bad], check=False).returncode != 0
        assert (sim_arc / sim_src.name).read_bytes() == b"WAL-RETRY-V1"

        with psycopg.connect(cfg.dsn_for_role("evolvmem_history_migration", role_kind="reader"), autocommit=True) as rconn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rconn.execute("INSERT INTO em_history.candidate_meta(key, value) VALUES ('ro_test', 'x')")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rconn.execute("DELETE FROM em_history.candidate_meta WHERE key = 'ro_test'")
        with psycopg.connect(cfg.dsn_for_role("evolvmem_history_migration", role_kind="writer"), autocommit=True) as wconn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                wconn.execute("DROP TABLE em_history.candidate_meta")

        bak_meta = take_base_backup(STAGE5_PG_CONTAINER, bak_dir, port=STAGE5_PG_PORT, password_file=STAGE5_PW_FILE)
        assert bak_meta["ok"] is True

        conn.execute("INSERT INTO em_history.candidate_meta(key, value) VALUES ('pitr_marker', 'before_target') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        target_ts = conn.execute("SELECT clock_timestamp()::text").fetchone()[0]
        time.sleep(1.2)
        conn.execute("INSERT INTO em_history.candidate_meta(key, value) VALUES ('pitr_post', 'after_target') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.execute("SELECT pg_switch_wal()")
        deadline = time.monotonic() + 5.0
        segs: list[Path] = []
        while time.monotonic() < deadline:
            segs = [p for p in wal_dir.iterdir() if p.is_file() and len(p.name) == 24]
            if segs:
                break
            time.sleep(0.2)
        assert len(segs) >= 1

    drill = run_pitr_restore_drill(
        base_backup_dir=Path(bak_meta["backup_path"]), wal_archive_dir=wal_dir,
        target_time_iso=target_ts, restore_container_name=STAGE5_PITR_CONTAINER,
        restore_port=STAGE5_PITR_PORT, password_file=STAGE5_PW_FILE,
        verify_db="evolvmem_history_migration",
        pre_target_sql="SELECT value FROM em_history.candidate_meta WHERE key = 'pitr_marker'",
        post_target_sql="SELECT value FROM em_history.candidate_meta WHERE key = 'pitr_post'",
    )
    assert drill["ok"] is True
    assert drill["pitr_target_reached"] is True
    assert drill["pre_target_present"] is True
    assert drill["post_target_absent"] is True
    assert drill["wal_segment_count"] >= 1

    patch = generate_prod_wal_config_patch()
    assert patch["requires_prod_restart"] is True
    assert "archive_mode=on" in " ".join(patch["postgres_flags"])
    (STAGE5_WORK_DIR / "wal-pitr-drill.json").write_text(
        json.dumps({**drill, "prod_wal_patch_ready": True, "llm_usage": {"calls": 0, "tokens": 0}}, indent=2) + "\n",
        encoding="utf-8",
    )
