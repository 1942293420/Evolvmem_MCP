from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pytest

from evolvmem.experience_v2.gate.rules import EvidenceVerifier
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
from evolvmem.product_policy import ProductPolicy
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE, STAGE5_WORK_DIR


def _cfg(cas_dir: Path) -> PgHistoryConfig:
    return PgHistoryConfig(
        host="127.0.0.1",
        port=STAGE5_PG_PORT,
        admin_password=STAGE5_PW_FILE.read_text(encoding="utf-8").strip(),
        allow_loopback_disable_ssl=True,
        container_name=STAGE5_PG_CONTAINER,
        cas_root=cas_dir,
    )


def test_no_direct_sqlite_in_pipeline_after_pg_reader() -> None:
    pipe_dir = Path(__file__).resolve().parents[2] / "evolvmem" / "experience_v2" / "pipeline"
    assert scan_direct_sqlite_in_pipeline(pipe_dir) == []


def test_pg_history_reader_and_verifier_protocols(tmp_path: Path) -> None:
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(cas_dir)
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

    reader = PgHistoryReader(cfg, dbname=cfg.history_migration_db)
    assert isinstance(reader, HistoryReader)
    verifier = PgHistoryEvidenceVerifier(reader)
    assert callable(getattr(verifier, 'verify_all', None))

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
    routed = open_history_reader(tmp_path, policy=policy, pg_config=cfg)
    assert isinstance(routed, PgHistoryReader)
