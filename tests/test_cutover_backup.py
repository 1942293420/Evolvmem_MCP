"""Behavioral contracts for the consistent, owner-only cutover backup.

The cutover backup must snapshot the live database through the SQLite Backup
API — a raw file copy silently loses committed rows still inside an
uncheckpointed WAL, so the tests pin a fixture where exactly that happens.
Backups live in a unique ``backups/context-core-cutover-<UTC>/`` directory
that is never overwritten or deleted automatically; every artifact is
owner-only (directory 0700, files 0600). The canonical manifest records
relative filenames, sizes, SHA-256 hashes, schema/version/count summaries,
the preflight digest, and the old-vector checksum. The Codex stanza snapshot
may contain secrets but they never reach the manifest, stdout, or public
results. A partial failure leaves an owner-only ``INCOMPLETE`` marker and no
``complete=true`` manifest, and verification independently reopens, checks,
and re-hashes every entry. All fixtures are synthetic.
"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import stat

import numpy as np
import pytest

import evolvmem.cutover_backup as cutover_backup
from evolvmem.codex_config import CodexConfigEditor, CodexMcpSnapshot
from evolvmem.config import Config
from evolvmem.context_models import ContextValidationError
from evolvmem.cutover_backup import (
    BackupManifest,
    BackupVerificationReport,
    CutoverBackupError,
    create_cutover_backup,
    verify_cutover_backup,
)
from evolvmem.cutover_models import validate_public_summary
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


SENSITIVE_ENV_VALUE = "synthetic-cutover-secret-9c17e4b2"  # fake fixture token

_CODEX_CONFIG = (
    """# synthetic Codex config; the token value is a fake fixture
[mcp_servers.evolvmem]
command = "/opt/evolvmem/bin/python3"
args = ["-m", "evolvmem.mcp_server"]
enabled = true

[mcp_servers.evolvmem.env]
EVOLVMEM_ADAPTER = "claude"
EVOLVMEM_CONTEXT_MODE = "legacy"
FAKE_API_TOKEN = \""""
    + SENSITIVE_ENV_VALUE
    + """\"
"""
)

_TIMESTAMP = datetime(2026, 8, 18, 16, 40, 9, tzinfo=timezone.utc)
_EXPECTED_DIR_NAME = "context-core-cutover-20260818T164009Z"
_EXPECTED_CREATED_UTC = "2026-08-18T16:40:09Z"
_PREFLIGHT_DIGEST = hashlib.sha256(b"synthetic preflight public summary").hexdigest()
_WAL_SENTINEL_VALUE = "UNCHECKPOINTED-WAL-ROW-7d2f synthetic committed value."


def _codex_snapshot(tmp_path: Path) -> CodexMcpSnapshot:
    config_path = tmp_path / "codex-config.toml"
    config_path.write_text(_CODEX_CONFIG, encoding="utf-8")
    return CodexConfigEditor(config_path).snapshot()


def _populate(store: MemoryStore) -> dict[str, int]:
    ids = {}
    ids["active"] = store.add(
        key="project:demo:decision:database",
        value="Use SQLite first for the demo service.",
        attribute="decision",
        importance=7.0,
    )
    ids["wal"] = store.add(
        key="project:demo:fact:wal",
        value=_WAL_SENTINEL_VALUE,
        attribute="fact",
    )
    ids["archived"] = store.add(
        key="project:demo:fact:archived",
        value="A historical archived fact.",
        attribute="fact",
    )
    store.archive(ids["archived"])
    return ids


def _checkpointed_library(config: Config) -> dict[str, int]:
    """Committed and closed library; closing the last WAL connection checkpoints."""
    with MemoryStore(config) as store:
        return _populate(store)


def _open_wal_library(config: Config) -> tuple[MemoryStore, dict[str, int]]:
    """Live WAL library whose committed rows have never been checkpointed.

    The store stays open with autocheckpoint disabled, so the main database
    file provably lacks the committed rows; only the Backup API (never a raw
    file copy) can capture them. The caller must close the store.
    """
    store = MemoryStore(config)
    store.initialize()
    store._conn.execute("PRAGMA wal_autocheckpoint=0")
    return store, _populate(store)


def _legacy_vector_file(config: Config) -> None:
    """Persist a real tiny legacy vector index; closed without dirty marker."""
    index = VectorIndex(config)
    index.initialize(dim=config.embedding_dim)
    index.add(1, np.full(config.embedding_dim, 0.5, dtype=np.float32))
    index.add(2, np.arange(config.embedding_dim, dtype=np.float32))
    index.save()
    index.close()


def _create(
    config: Config,
    tmp_path: Path,
    *,
    timestamp: datetime = _TIMESTAMP,
) -> BackupManifest:
    return create_cutover_backup(
        config,
        codex_snapshot=_codex_snapshot(tmp_path),
        preflight_digest=_PREFLIGHT_DIGEST,
        timestamp=timestamp,
    )


def _backup_dir(config: Config, manifest: BackupManifest) -> Path:
    return config.data_dir / "backups" / manifest.directory_name


def _only_backup_dir(config: Config) -> Path:
    entries = list((config.data_dir / "backups").iterdir())
    assert len(entries) == 1
    return entries[0]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_manifest(backup_dir: Path, mutate) -> None:
    path = backup_dir / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---- WAL consistency: Backup API captures what a file copy loses ----


def test_backup_api_captures_uncheckpointed_wal_rows(test_config, tmp_path):
    store, ids = _open_wal_library(test_config)
    try:
        # Precondition: a raw copy of the main file is a broken fake — the
        # committed rows live only in the uncheckpointed WAL.
        fake = tmp_path / "raw-copy-fake.db"
        fake.write_bytes(test_config.db_path.read_bytes())
        fake_conn = sqlite3.connect(str(fake))
        try:
            fake_count = fake_conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
        except sqlite3.Error:
            fake_count = None
        finally:
            fake_conn.close()
        assert fake_count != len(ids)

        manifest = _create(test_config, tmp_path)
        backup_dir = _backup_dir(test_config, manifest)
        backup_db = backup_dir / "memory.db"

        # The snapshot is standalone: no WAL/SHM sidecars, immutable-openable.
        assert not (backup_dir / "memory.db-wal").exists()
        assert not (backup_dir / "memory.db-shm").exists()
        uri = backup_db.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            quick = [str(row[0]).lower() for row in conn.execute("PRAGMA quick_check")]
            assert quick == ["ok"]
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            assert count == len(ids)
            value = conn.execute(
                "SELECT value FROM memories WHERE id = ?", (ids["wal"],)
            ).fetchone()[0]
            assert value == _WAL_SENTINEL_VALUE
        finally:
            conn.close()

        report = verify_cutover_backup(backup_dir)
        assert report.verified is True
        assert report.complete is True

        # The source library is untouched and still readable through its WAL.
        assert store._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == len(
            ids
        )
    finally:
        store.close()


# ---- layout, uniqueness, permissions ----


def test_backup_directory_and_files_are_owner_only(test_config, tmp_path):
    _checkpointed_library(test_config)
    test_config.save()
    _legacy_vector_file(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    assert backup_dir.parent == test_config.data_dir / "backups"
    assert backup_dir.name == _EXPECTED_DIR_NAME
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup_dir.parent.stat().st_mode) == 0o700

    names = {path.name for path in backup_dir.iterdir()}
    assert names == {
        "memory.db",
        "config.json",
        "codex-mcp-stanza.json",
        "vectors.usearch",
        "manifest.json",
    }
    for name in names:
        assert stat.S_IMODE((backup_dir / name).stat().st_mode) == 0o600
    # No leftover marker or temp files after a complete backup.
    assert not (backup_dir / "INCOMPLETE").exists()
    assert not any(name.endswith(".tmp") for name in names)


def test_backup_collision_refused_and_earlier_backup_never_deleted(
    test_config, tmp_path
):
    _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)
    manifest_bytes = (backup_dir / "manifest.json").read_bytes()

    with pytest.raises(CutoverBackupError):
        _create(test_config, tmp_path)

    # The refused attempt changed nothing and no temp directory leaked.
    assert (backup_dir / "manifest.json").read_bytes() == manifest_bytes
    assert _only_backup_dir(test_config) == backup_dir
    assert verify_cutover_backup(backup_dir).verified is True

    later = _create(
        test_config, tmp_path, timestamp=_TIMESTAMP + timedelta(seconds=1)
    )
    assert later.directory_name != manifest.directory_name
    siblings = list((test_config.data_dir / "backups").iterdir())
    assert len(siblings) == 2
    # The earlier backup is never deleted or rewritten by a later one.
    assert (backup_dir / "manifest.json").read_bytes() == manifest_bytes
    assert verify_cutover_backup(backup_dir).verified is True
    assert verify_cutover_backup(_backup_dir(test_config, later)).verified is True


def test_timestamp_is_normalized_to_utc(test_config, tmp_path):
    _checkpointed_library(test_config)
    local = datetime(2026, 8, 18, 18, 40, 9, tzinfo=timezone(timedelta(hours=2)))
    manifest = _create(test_config, tmp_path, timestamp=local)
    assert manifest.directory_name == _EXPECTED_DIR_NAME
    assert manifest.created_utc == _EXPECTED_CREATED_UTC


# ---- manifest content ----


def test_manifest_records_relative_names_hashes_and_summaries(test_config, tmp_path):
    ids = _checkpointed_library(test_config)
    test_config.save()
    _legacy_vector_file(test_config)
    snapshot = _codex_snapshot(tmp_path)
    manifest = create_cutover_backup(
        test_config,
        codex_snapshot=snapshot,
        preflight_digest=_PREFLIGHT_DIGEST,
        timestamp=_TIMESTAMP,
    )
    backup_dir = _backup_dir(test_config, manifest)
    payload = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

    assert payload["schema"] == "evolvmem.cutover_backup_manifest"
    assert payload["version"] == 1
    assert payload["complete"] is True
    assert payload["created_utc"] == _EXPECTED_CREATED_UTC
    assert payload["preflight_digest"] == _PREFLIGHT_DIGEST

    database = payload["database"]
    db_file = backup_dir / database["filename"]
    assert database["filename"] == "memory.db"
    assert database["size_bytes"] == db_file.stat().st_size
    assert database["sha256"] == _sha256(db_file)
    assert database["quick_check_ok"] is True
    assert type(database["sqlite_user_version"]) is int
    assert database["legacy_schema_present"] is True
    assert database["legacy_rows"] == len(ids)
    assert database["legacy_status_counts"] == {"active": 2, "archived": 1}
    assert database["context_schema_present"] is False
    assert database["context_items"] == 0

    config_entry = payload["config"]
    assert config_entry["filename"] == "config.json"
    assert config_entry["size_bytes"] == test_config.config_path.stat().st_size
    assert config_entry["sha256"] == _sha256(test_config.config_path)

    stanza_entry = payload["codex_stanza"]
    stanza_file = backup_dir / stanza_entry["filename"]
    assert stanza_entry["filename"] == "codex-mcp-stanza.json"
    assert stanza_entry["size_bytes"] == stanza_file.stat().st_size
    assert stanza_entry["sha256"] == _sha256(stanza_file)
    assert stanza_entry["stanza_sha256"] == snapshot.stanza_sha256
    assert stanza_entry["source_file_sha256"] == snapshot.source_file_sha256

    vector_entry = payload["old_vector"]
    assert vector_entry["filename"] == "vectors.usearch"
    assert vector_entry["size_bytes"] == test_config.vector_path.stat().st_size
    assert vector_entry["sha256"] == _sha256(test_config.vector_path)

    # Every recorded name is a bare relative filename, never a path.
    for entry in (database, config_entry, stanza_entry, vector_entry):
        filename = entry["filename"]
        assert Path(filename).name == filename
        assert "/" not in filename and "\\" not in filename

    # The manifest never leaks absolute locations.
    manifest_text = json.dumps(payload)
    assert str(test_config.data_dir) not in manifest_text
    assert str(tmp_path) not in manifest_text

    # The public result mirrors the same safe summary vocabulary.
    public = manifest.public_dict()
    validate_public_summary(public)
    assert public["preflight_digest"] == _PREFLIGHT_DIGEST
    assert public["directory_name"] == _EXPECTED_DIR_NAME
    assert public["legacy_rows"] == len(ids)
    assert public["config_present"] is True
    assert public["old_vector_present"] is True


def test_absent_config_and_vector_are_not_invented(test_config, tmp_path):
    _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    names = {path.name for path in backup_dir.iterdir()}
    assert names == {"memory.db", "codex-mcp-stanza.json", "manifest.json"}
    for path in backup_dir.iterdir():
        assert path.stat().st_size > 0  # no invented empty files

    payload = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert payload["config"] is None
    assert payload["old_vector"] is None

    public = manifest.public_dict()
    assert public["config_present"] is False
    assert public["config_size_bytes"] == 0
    assert public["config_sha256"] == ""
    assert public["old_vector_present"] is False
    assert public["old_vector_size_bytes"] == 0
    assert public["old_vector_sha256"] == ""

    report = verify_cutover_backup(backup_dir)
    assert report.verified is True
    assert report.config_ok is True
    assert report.old_vector_ok is True


# ---- privacy ----


def test_stanza_snapshot_stays_owner_only_and_secret_never_leaks(
    test_config, tmp_path, capsys
):
    _checkpointed_library(test_config)
    test_config.save()
    _legacy_vector_file(test_config)
    snapshot = _codex_snapshot(tmp_path)
    manifest = create_cutover_backup(
        test_config,
        codex_snapshot=snapshot,
        preflight_digest=_PREFLIGHT_DIGEST,
        timestamp=_TIMESTAMP,
    )
    backup_dir = _backup_dir(test_config, manifest)
    stanza_path = backup_dir / "codex-mcp-stanza.json"

    # The owner-only snapshot keeps the full stanza, secrets included.
    assert SENSITIVE_ENV_VALUE.encode() in stanza_path.read_bytes()
    assert stat.S_IMODE(stanza_path.stat().st_mode) == 0o600
    reloaded = CodexConfigEditor.load_snapshot(stanza_path)
    assert reloaded.stanza_sha256 == snapshot.stanza_sha256
    assert reloaded.source_file_sha256 == snapshot.source_file_sha256

    report = verify_cutover_backup(backup_dir)
    assert report.verified is True

    # Neither create nor verify prints anything at all.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    public_surfaces = [
        (backup_dir / "manifest.json").read_text(encoding="utf-8"),
        json.dumps(manifest.public_dict(), sort_keys=True),
        json.dumps(report.public_dict(), sort_keys=True),
        manifest.digest(),
        report.digest(),
    ]
    for surface in public_surfaces:
        assert SENSITIVE_ENV_VALUE not in surface
        assert str(test_config.data_dir) not in surface
        assert str(tmp_path) not in surface


def test_public_summaries_are_guarded_and_reports_frozen(test_config, tmp_path):
    _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)

    validate_public_summary(manifest.public_dict())
    assert re.fullmatch(r"[0-9a-f]{64}", manifest.digest())
    assert manifest.digest() == manifest.digest()  # deterministic
    with pytest.raises(FrozenInstanceError):
        manifest.complete = False

    report = verify_cutover_backup(_backup_dir(test_config, manifest))
    validate_public_summary(report.public_dict())
    assert re.fullmatch(r"[0-9a-f]{64}", report.digest())
    with pytest.raises(FrozenInstanceError):
        report.verified = True


# ---- partial failure ----


def test_partial_failure_leaves_incomplete_marker_without_manifest(
    test_config, tmp_path
):
    _checkpointed_library(test_config)
    snapshot = _codex_snapshot(tmp_path)
    tampered = CodexMcpSnapshot(
        stanza=snapshot.stanza,
        stanza_sha256="0" * 64,  # does not match the stanza content
        source_file_sha256=snapshot.source_file_sha256,
    )

    with pytest.raises(CutoverBackupError):
        create_cutover_backup(
            test_config,
            codex_snapshot=tampered,
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp=_TIMESTAMP,
        )

    partial = _only_backup_dir(test_config)
    assert stat.S_IMODE(partial.stat().st_mode) == 0o700
    marker = partial / "INCOMPLETE"
    assert marker.is_file()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert not (partial / "manifest.json").exists()

    report = verify_cutover_backup(partial)
    assert report.verified is False
    assert report.complete is False
    assert report.incomplete_marker_present is True
    assert "manifest_missing" in report.reason_codes
    assert "incomplete_marker_present" in report.reason_codes
    validate_public_summary(report.public_dict())


def test_failed_independent_verification_keeps_marker_and_drops_manifest(
    test_config, tmp_path, monkeypatch
):
    _checkpointed_library(test_config)
    real_verify = cutover_backup.verify_cutover_backup

    def failed_verify(directory: Path) -> BackupVerificationReport:
        report = real_verify(directory)
        return replace(
            report,
            database_ok=False,
            verified=False,
            reason_codes=report.reason_codes + ("database_sha256_mismatch",),
        )

    monkeypatch.setattr(cutover_backup, "verify_cutover_backup", failed_verify)
    with pytest.raises(CutoverBackupError):
        _create(test_config, tmp_path)

    partial = _only_backup_dir(test_config)
    assert (partial / "INCOMPLETE").is_file()
    assert stat.S_IMODE((partial / "INCOMPLETE").stat().st_mode) == 0o600
    assert not (partial / "manifest.json").exists()


# ---- independent verification ----


def test_verify_detects_config_tampering(test_config, tmp_path):
    _checkpointed_library(test_config)
    test_config.save()
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    target = backup_dir / "config.json"
    target.write_bytes(target.read_bytes() + b"tampered")

    report = verify_cutover_backup(backup_dir)
    assert report.verified is False
    assert report.config_ok is False
    assert "config_sha256_mismatch" in report.reason_codes
    # Untouched entries still check out.
    assert report.database_ok is True
    assert report.stanza_ok is True
    validate_public_summary(report.public_dict())


def test_verify_detects_database_tampering(test_config, tmp_path):
    _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    # Replace the snapshot with a different, perfectly valid SQLite database.
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    (backup_dir / "memory.db").write_bytes(other.read_bytes())

    report = verify_cutover_backup(backup_dir)
    assert report.verified is False
    assert report.database_ok is False
    assert "database_sha256_mismatch" in report.reason_codes


def test_verify_recounts_database_rows_independently(test_config, tmp_path):
    ids = _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    # Forge only the manifest claim; entry hashes stay consistent, so only a
    # genuine reopen-and-recount can catch this.
    _rewrite_manifest(
        backup_dir,
        lambda payload: payload["database"].__setitem__(
            "legacy_rows", len(ids) + 1
        ),
    )

    report = verify_cutover_backup(backup_dir)
    assert report.verified is False
    assert report.database_ok is False
    assert "database_count_mismatch" in report.reason_codes


def test_verify_detects_missing_vector_entry(test_config, tmp_path):
    _checkpointed_library(test_config)
    _legacy_vector_file(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    (backup_dir / "vectors.usearch").unlink()

    report = verify_cutover_backup(backup_dir)
    assert report.verified is False
    assert report.old_vector_ok is False
    assert "old_vector_missing" in report.reason_codes


def test_incomplete_marker_blocks_verified_status(test_config, tmp_path):
    _checkpointed_library(test_config)
    manifest = _create(test_config, tmp_path)
    backup_dir = _backup_dir(test_config, manifest)

    marker = backup_dir / "INCOMPLETE"
    marker.write_text("incomplete\n", encoding="utf-8")

    report = verify_cutover_backup(backup_dir)
    assert report.complete is True  # the manifest is complete
    assert report.database_ok is True
    assert report.incomplete_marker_present is True
    assert report.verified is False
    assert "incomplete_marker_present" in report.reason_codes


def test_verify_missing_directory_reports_not_verified(tmp_path):
    report = verify_cutover_backup(tmp_path / "context-core-cutover-20000101T000000Z")
    assert report.verified is False
    assert report.complete is False
    assert report.incomplete_marker_present is False
    assert "backup_directory_missing" in report.reason_codes
    validate_public_summary(report.public_dict())


# ---- input validation ----


def test_create_validates_inputs(test_config, tmp_path):
    _checkpointed_library(test_config)
    snapshot = _codex_snapshot(tmp_path)

    with pytest.raises(ContextValidationError):
        create_cutover_backup(
            object(),
            codex_snapshot=snapshot,
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp=_TIMESTAMP,
        )
    with pytest.raises(ContextValidationError):
        create_cutover_backup(
            test_config,
            codex_snapshot=object(),
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp=_TIMESTAMP,
        )
    with pytest.raises(ContextValidationError):
        create_cutover_backup(
            test_config,
            codex_snapshot=snapshot,
            preflight_digest="not-a-digest",
            timestamp=_TIMESTAMP,
        )
    with pytest.raises(ContextValidationError):
        create_cutover_backup(
            test_config,
            codex_snapshot=snapshot,
            preflight_digest=_PREFLIGHT_DIGEST.upper(),
            timestamp=_TIMESTAMP,
        )
    with pytest.raises(ContextValidationError):  # naive datetime
        create_cutover_backup(
            test_config,
            codex_snapshot=snapshot,
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp=datetime(2026, 8, 18, 16, 40, 9),
        )
    with pytest.raises(ContextValidationError):  # not a datetime at all
        create_cutover_backup(
            test_config,
            codex_snapshot=snapshot,
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp="2026-08-18T16:40:09Z",
        )

    # Failed validation never creates a backup directory.
    assert not (test_config.data_dir / "backups").exists()

    manifest = create_cutover_backup(
        test_config,
        codex_snapshot=snapshot,
        preflight_digest=_PREFLIGHT_DIGEST,
        timestamp=_TIMESTAMP,
    )
    assert manifest.complete is True

    with pytest.raises(ContextValidationError):
        verify_cutover_backup("not-a-path")


def test_missing_database_fails_before_creating_directory(tmp_path):
    config = Config(data_dir=tmp_path / "fresh-data")
    snapshot = _codex_snapshot(tmp_path)
    with pytest.raises(CutoverBackupError):
        create_cutover_backup(
            config,
            codex_snapshot=snapshot,
            preflight_digest=_PREFLIGHT_DIGEST,
            timestamp=_TIMESTAMP,
        )
    assert not (config.data_dir / "backups").exists()
