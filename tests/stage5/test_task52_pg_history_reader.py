from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evolvmem.experience_v2.mcp_tools import _build_live_gate_hook
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryReader,
    open_history_reader,
    scan_direct_sqlite_in_pipeline,
)
from evolvmem.experience_v2.pipeline.pg_history_reader import (
    PgHistoryEvidenceVerifier,
    PgHistoryReader,
)
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.product_policy import ProductPolicy
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


def _cfg(cas_dir: Path) -> PgHistoryConfig:
    return PgHistoryConfig(
        host="127.0.0.1",
        port=STAGE5_PG_PORT,
        admin_password=STAGE5_PW_FILE.read_text(encoding="utf-8").strip(),
        user_history_dbs={"jiangli": "evolvmem_history_migration", "kane": "evolvmem_history_kane"},
        allow_loopback_disable_ssl=True,
        container_name=STAGE5_PG_CONTAINER,
        cas_root=cas_dir,
    )


def test_no_direct_sqlite_in_pipeline_after_pg_reader() -> None:
    pipe_dir = Path(__file__).resolve().parents[2] / "evolvmem" / "experience_v2" / "pipeline"
    assert scan_direct_sqlite_in_pipeline(pipe_dir) == []


def test_pg_history_reader_and_verifier_protocols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(cas_dir)
    ensure_databases_exist(cfg)
    apply_history_schemas(cfg)

    lines = [
        json.dumps({"role": "user", "content": "check layout spacing issue", "timestamp": "2026-11-10T01:00:00Z"}),
        json.dumps({"role": "assistant", "content": "reduced left/right whitespace cleanly", "timestamp": "2026-11-10T01:00:05Z"}),
    ]
    raw_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    blob_sha = hashlib.sha256(raw_bytes).hexdigest()
    blob_file = cas_dir / blob_sha[:2] / f"{blob_sha}.jsonl"
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(raw_bytes)

    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute(
            "INSERT INTO em_runtime.session_archives("
            "id, project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, created_at, device_id, cas_blob_sha256"
            ") VALUES (99001, 'proj-52', 'codex', 'ext-52-01', %s, %s, 'available', '', '2026-11-10T01:00:00Z', 'dev-52', %s) "
            "ON CONFLICT (id) DO UPDATE SET cas_blob_sha256 = EXCLUDED.cas_blob_sha256, payload_path = EXCLUDED.payload_path",
            (str(blob_file), blob_sha, blob_sha),
        )
        conn.commit()

    try:
        reader = PgHistoryReader(cfg, dbname=cfg.history_migration_db)
        assert isinstance(reader, HistoryReader)
        verifier = PgHistoryEvidenceVerifier(reader)
        assert callable(getattr(verifier, "verify_all", None))

        sessions = reader.list_sessions(project="proj-52", limit=10)
        assert any(s.source_ref == "archive:99001" for s in sessions)
        sess = reader.get_session("archive:99001")
        assert sess is not None and len(sess.records) == 2

        ok, reason = verifier.verify_all([{"source": "archive:99001#msg=1"}])
        assert ok is True and reason == "ok"
        ok2, _ = verifier.verify_all([{"source_ref": "archive:99001", "line_no": 2}])
        assert ok2 is True
        bad_ok, bad_reason = verifier.verify_all([{"source_ref": "archive:99001", "line_no": 99}])
        assert bad_ok is False and "out_of_range" in bad_reason

        policy = ProductPolicy(history_pg_enabled=True)
        monkeypatch.setenv("EVOLVMEM_CONSOLE_OWNER_ID", "jiangli")
        routed = open_history_reader(tmp_path, policy=policy, pg_config=cfg)
        assert isinstance(routed, PgHistoryReader) and routed._library == "personal"

        routed_kane = open_history_reader(tmp_path, library="kane", policy=policy, pg_config=cfg)
        assert routed_kane._library == "kane" and routed_kane._dbname == "evolvmem_history_kane"

        with pytest.raises(ValueError, match="missing_user_history_id"):
            cfg.resolve_user_history_db("")
        with pytest.raises(ValueError, match="unmapped_user_history_db"):
            cfg.resolve_user_history_db("unmapped-user")
        with pytest.raises(ValueError, match="unmapped_user_history_db"):
            open_history_reader(tmp_path, library="unmapped-lib", policy=policy, pg_config=cfg)

        # Fail-closed live gate hook when history_pg_enabled=True
        pol_file = tmp_path / "policy52.json"
        pol_file.write_text(json.dumps({"history_pg_enabled": True}), encoding="utf-8")
        monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(pol_file))
        monkeypatch.setenv("EVOLVMEM_HISTORY_PG_CONFIG", str(tmp_path / "missing-pg.json"))
        with pytest.raises(FileNotFoundError):
            _build_live_gate_hook(owner_id="jiangli", history_db_paths=[], vec=None, ticket_secret=b"s", gate_mode="strict", category="bugfix")
        hist_file = tmp_path / "pg52.json"
        hist_file.write_text(json.dumps({"host": "127.0.0.1", "port": STAGE5_PG_PORT, "admin_password": cfg.admin_password, "user_history_dbs": {"jiangli": cfg.history_migration_db}, "cas_root": str(cas_dir), "allow_loopback_disable_ssl": True}), encoding="utf-8")
        monkeypatch.setenv("EVOLVMEM_HISTORY_PG_CONFIG", str(hist_file))
        with pytest.raises(ValueError, match="unmapped_user_history_db"):
            _build_live_gate_hook(owner_id="unmapped-owner", history_db_paths=[], vec=None, ticket_secret=b"s", gate_mode="strict", category="bugfix")
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_runtime.session_archives WHERE id = 99001")
            conn.commit()


def test_pg_history_reader_cas_nested_trash_and_dedupe(tmp_path: Path) -> None:
    cas_dir = tmp_path / "cas2"
    cfg = _cfg(cas_dir)
    ensure_databases_exist(cfg)
    apply_history_schemas(cfg)

    nested_lines = [
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "nested-codex-ok"}]}}),
        json.dumps({"type": "user/message", "data": {"role": "user", "content": [{"type": "text", "text": "nested-dsh-ok"}]}}),
    ]
    raw = ("\n".join(nested_lines) + "\n").encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    cas2 = cas_dir / sha[:2] / sha[2:4] / f"{sha}.jsonl"
    cas2.parent.mkdir(parents=True, exist_ok=True)
    cas2.write_bytes(raw)

    with cfg.connect(cfg.history_migration_db) as conn:
        for aid, ext_id, ts, proj in (
            (99010, "ext-new-52", "2026-11-12T00:00:00Z", "proj-cas-52"),
            (99011, "ext-old-52", "2026-11-01T00:00:00Z", "proj-cas-52"),
            (99012, "ext-tr-52", "2026-11-12T00:00:00Z", "proj-trash-52"),
        ):
            conn.execute(
                "INSERT INTO em_runtime.session_archives(id, project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, created_at, device_id, cas_blob_sha256) "
                "VALUES (%s, %s, 'codex', %s, '', %s, 'available', '', %s, 'dev-52', %s) ON CONFLICT (id) DO NOTHING",
                (aid, proj, ext_id, sha, ts, sha),
            )
        for u_sha, r_at in (("1" * 64, 100.0), ("2" * 64, 200.0)):
            conn.execute(
                "INSERT INTO em_runtime.lan_session_uploads(device_id, session_id, sha256, project, declared_project, attribution_reason, received_at, backfill_status, backfill_result, total_bytes, received_bytes, archive_id, extraction_status, extraction_result, error, source_order, extracted_at, source_format) "
                "VALUES ('dev-52', 'ext-new-52', %s, 'proj-cas-52', 'proj-cas-52', 'test', %s, 'done', '', 10, 10, 99010, 'done', '', '', 0, '', 'codex-jsonl') ON CONFLICT DO NOTHING",
                (u_sha, r_at),
            )
        for bid, proj in (("blk-trash-52", "proj-cas-52"), ("blk-proj-52", "proj-trash-52")):
            conn.execute(
                "INSERT INTO em_history.blocks(block_id, title, adapter, native_session_id, projects, block_json) VALUES (%s, %s, 'codex', 'nat-52', %s, '{}') ON CONFLICT (block_id) DO NOTHING",
                (bid, bid, json.dumps([proj])),
            )
            conn.execute("INSERT INTO em_history.block_projects(block_id, project) VALUES (%s, %s) ON CONFLICT DO NOTHING", (bid, proj))
            conn.execute("INSERT INTO em_history.messages(block_id, ordinal, role, content) VALUES (%s, 0, 'user', 'msg-52') ON CONFLICT DO NOTHING", (bid,))
        conn.commit()

    reader = PgHistoryReader(cfg, dbname=cfg.history_migration_db)
    try:
        sess = reader.get_session("archive:99010")
        assert sess is not None and sess.records[0].content == "nested-codex-ok" and sess.records[1].content == "nested-dsh-ok"
        deduped = reader.list_sessions(project="proj-cas-52", dedupe_logical_sessions=True)
        assert [s.source_ref for s in deduped] == ["archive:99010", "archive:99011"]
        all_rows = reader.list_sessions(project="proj-cas-52", dedupe_logical_sessions=False)
        assert len(all_rows) == 2
        since_rows = reader.list_sessions(project="proj-cas-52", since="2026-11-10T00:00:00Z", dedupe_logical_sessions=False)
        assert [s.source_ref for s in since_rows] == ["archive:99010"]

        assert reader.get_session("block:blk-trash-52") is not None
        assert reader.get_session("block:blk-proj-52") is not None
        assert reader.get_session("archive:99012") is not None

        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("INSERT INTO em_history.management_trash(kind, project, key, title, deleted_at) VALUES ('block', 'proj-cas-52', 'blk-trash-52', 't', 'now') ON CONFLICT DO NOTHING")
            conn.execute("INSERT INTO em_history.management_trash(kind, project, key, title, deleted_at) VALUES ('project', 'proj-trash-52', 'proj-trash-52', 't', 'now') ON CONFLICT DO NOTHING")
            conn.commit()

        assert reader.get_session("block:blk-trash-52") is None
        assert reader.get_session("block:blk-proj-52") is None
        assert reader.get_session("archive:99012") is None
        assert reader.list_sessions(project="proj-trash-52") == []
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute("DELETE FROM em_history.management_trash WHERE (kind='block' AND key='blk-trash-52') OR (kind='project' AND key='proj-trash-52')")
            conn.execute("DELETE FROM em_history.messages WHERE block_id IN ('blk-trash-52', 'blk-proj-52')")
            conn.execute("DELETE FROM em_history.blocks WHERE block_id IN ('blk-trash-52', 'blk-proj-52')")
            conn.execute("DELETE FROM em_runtime.lan_session_uploads WHERE device_id='dev-52' AND session_id='ext-new-52'")
            conn.execute("DELETE FROM em_runtime.session_archives WHERE id IN (99010, 99011, 99012)")
            conn.commit()
