"""Behavioral contracts for the side-effect-free cutover gates.

Preflight, projection-lag, shadow, and primary-gate evaluations must be
pure inspections: they never create tables, directories, lock files, or
vector artifacts, and their public report serializations carry only
counts, booleans, reason codes, sizes, checksum prefixes, and durations —
never memory content, queries, archive payloads, secrets, or absolute
paths. All fixtures are synthetic libraries in temporary directories.
"""

from dataclasses import FrozenInstanceError
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import stat

import numpy as np
import pytest

from evolvmem.codex_config import CodexConfigEditor
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import ContextValidationError
from evolvmem.context_store import ContextStore
from evolvmem.cutover_checks import (
    check_projection_lag,
    collect_primary_gate_evidence,
    compare_shadow,
    evaluate_shadow_gate,
    run_preflight,
    verify_primary_gate,
)
from evolvmem.cutover_models import (
    CutoverPreflightReport,
    PrimaryGateEvidence,
    PrimaryGateReport,
    ProjectionLagReport,
    ShadowComparison,
    ShadowGateReport,
    validate_public_summary,
)
from evolvmem.legacy_projection import (
    LegacyProjectionInsert,
    LegacyProjectionRepository,
)
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


VALID_CODEX_CONFIG = """# synthetic Codex config; paths and tokens are fake fixtures
[mcp_servers.evolvmem]
command = "/opt/evolvmem/bin/python3"
args = ["-m", "evolvmem.mcp_server"]
cwd = "/opt/evolvmem"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
    "memory_add",
    "memory_search",
    "memory_status",
    "context_session_start",
    "context_search",
    "context_read",
    "context_status",
]

[mcp_servers.evolvmem.env]
PYTHONPATH = "/opt/evolvmem"
EVOLVMEM_ADAPTER = "claude"
EVOLVMEM_CONTEXT_MODE = "compat"
FAKE_API_TOKEN = "synthetic-token-not-real"
"""

_STANZA_MISSING_CONFIG = """[mcp_servers.docs]
command = "docs-mcp"
"""

_ENABLED_TOOLS_INCOMPLETE = VALID_CODEX_CONFIG.replace(
    '    "context_read",\n', ""
)
_DISABLED_TOOLS_BLOCKING = VALID_CODEX_CONFIG.replace(
    "tool_timeout_sec = 120\n",
    'tool_timeout_sec = 120\ndisabled_tools = ["context_search"]\n',
)


def _write_codex_config(directory: Path, text: str = VALID_CODEX_CONFIG) -> Path:
    path = directory / "codex-config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _legacy_library(config: Config) -> dict[str, int]:
    """Create a WAL legacy database with one row per status class."""
    with MemoryStore(config) as legacy:
        ids = {}
        ids["active"] = legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first for the demo service.",
            attribute="decision",
            tags=["database"],
            source_session="session-a",
            importance=7.0,
        )
        ids["superseded"] = legacy.add(
            key="project:demo:fact:cache",
            value="The cache uses an in-process map.",
            attribute="fact",
            source_session="session-b",
        )
        ids["successor"] = legacy.replace(
            key="project:demo:fact:cache",
            new_value="The cache moved to a shared store.",
            source_session="session-c",
        )
        ids["archived"] = legacy.add(
            key="project:demo:fact:archived",
            value="A historical archived fact.",
            attribute="fact",
        )
        legacy.archive(ids["archived"])
        ids["deleted"] = legacy.add(
            key="project:demo:fact:deleted",
            value="A row removed by an explicit delete.",
            attribute="fact",
        )
        legacy.remove(ids["deleted"])
        ids["cjk"] = legacy.add(
            key="project:demo:fact:refund",
            value="退款政策：所有订单支持七天无理由退款。",
            attribute="fact",
        )
    # The fixture owns the models directory so the no-side-effect test can
    # prove preflight never recreates it.
    shutil.rmtree(config.data_dir / "models", ignore_errors=True)
    return ids


def _legacy_vector_file(config: Config, *, dim: int | None = None) -> None:
    """Persist a real tiny legacy vector index; closed without dirty marker."""
    index = VectorIndex(config)
    dimension = dim or config.embedding_dim
    index.initialize(dim=dimension)
    index.add(1, np.full(dimension, 0.5, dtype=np.float32))
    index.add(2, np.arange(dimension, dtype=np.float32))
    index.save()
    index.close()


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    """Relative path -> (kind, content hash, mode bits) for the whole tree."""
    snapshot: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("dir", oct(stat.S_IMODE(path.stat().st_mode)))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[relative] = (
                "file",
                digest,
                oct(stat.S_IMODE(path.stat().st_mode)),
                path.stat().st_size,
            )
    return snapshot


def _database_snapshot(db_path: Path) -> tuple:
    """Schema objects, per-table row counts, and legacy access telemetry.

    The observer must be side-effect-free too: the fixture library is always
    checkpointed, so an immutable read-only URI creates no -shm/-wal files.
    """
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        objects = tuple(
            conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
        counts = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            counts[name] = conn.execute(
                f'SELECT COUNT(*) FROM "{name}"'
            ).fetchone()[0]
        access = tuple(
            conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(access_count), 0) FROM memories"
            ).fetchone()
        )
        return objects, counts, access
    finally:
        conn.close()


def _forbid_mutations(monkeypatch) -> None:
    """Fail the test if preflight touches any write or initialization API."""

    def boom(*args, **kwargs):
        raise AssertionError(
            "preflight must not call initialization or mutation APIs"
        )

    targets = {
        Config: ("ensure_dirs", "save"),
        ContextStore: (
            "initialize",
            "create_schema_in_transaction",
            "create_item",
            "supersede_active",
            "update_access",
            "record_migration_source",
            "record_legacy_mapping",
            "set_supersession_links",
            "supersede_item",
            "set_item_status",
            "update_item_from_legacy",
            "hard_delete_item",
            "delete_legacy_mapping",
        ),
        VectorIndex: (
            "initialize",
            "add",
            "add_batch",
            "remove",
            "rebuild",
            "save",
            "mark_dirty",
            "preserve_dirty",
            "clear_dirty",
        ),
        MemoryStore: (
            "initialize",
            "add",
            "add_if_changed",
            "replace",
            "remove",
            "update_metadata",
            "archive",
            "update_access",
        ),
        LegacyMemoryMigrator: ("migrate", "migrate_projection_row"),
        LegacyProjectionRepository: (
            "insert",
            "replace",
            "soft_delete",
            "set_status",
            "update_metadata",
            "update_access",
            "hard_delete",
        ),
        CodexConfigEditor: (
            "apply_primary",
            "apply_legacy",
            "restore_snapshot",
            "save_snapshot",
        ),
    }
    for cls, names in targets.items():
        for name in names:
            monkeypatch.setattr(cls, name, boom)


# ---- preflight: side-effect freedom ----


def test_preflight_leaves_legacy_library_byte_for_byte_untouched(
    test_config, tmp_path, monkeypatch
):
    """Preflight is a pure observation: bytes, schema, and tree stay identical."""
    ids = _legacy_library(test_config)
    _legacy_vector_file(test_config)
    test_config.save()
    # config.save() re-ensures the models directory; remove it again so the
    # test can prove preflight never recreates it.
    shutil.rmtree(test_config.data_dir / "models", ignore_errors=True)
    codex_path = _write_codex_config(tmp_path)

    tree_before = _tree_snapshot(test_config.data_dir)
    codex_tree_before = _tree_snapshot(tmp_path)
    database_before = _database_snapshot(test_config.db_path)
    config_bytes = test_config.config_path.read_bytes()
    config_mode = oct(stat.S_IMODE(test_config.config_path.stat().st_mode))
    vector_bytes = test_config.vector_path.read_bytes()

    _forbid_mutations(monkeypatch)
    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.ready is True
    assert report.reason_codes == ()
    assert report.legacy_rows == len(ids)
    assert report.duplicate_active_count == 0
    assert report.context_schema_present is False
    assert report.context_items == 0
    assert report.old_vector_present is True
    assert report.old_vector_count == 2
    assert report.old_vector_dimension == test_config.embedding_dim
    assert report.old_vector_dirty is False
    assert report.codex_stanza_present is True
    assert report.codex_context_tools_ok is True
    assert report.backup_parent_writable is True

    assert _tree_snapshot(test_config.data_dir) == tree_before
    assert _tree_snapshot(tmp_path) == codex_tree_before
    assert _database_snapshot(test_config.db_path) == database_before
    assert test_config.config_path.read_bytes() == config_bytes
    assert oct(stat.S_IMODE(test_config.config_path.stat().st_mode)) == config_mode
    assert test_config.vector_path.read_bytes() == vector_bytes

    # No Context table, backup/model directory, lock file, vector/temp file.
    forbidden_names = {
        "backups",
        "models",
        "context-core-cutover.lock",
        "context_vectors.usearch",
        "context_vectors.usearch.dirty",
        "vectors.usearch.dirty",
    }
    after = set(_tree_snapshot(test_config.data_dir))
    assert forbidden_names.isdisjoint(after)
    assert not any(name.endswith((".tmp", ".part", ".bak")) for name in after)
    objects = database_before[0]
    assert not any("context" in str(row[1]) for row in objects)


def test_preflight_requires_an_explicit_codex_config_path(test_config, tmp_path):
    """The Codex target is never guessed from user/project precedence."""
    _legacy_library(test_config)
    with pytest.raises(TypeError):
        run_preflight(test_config)
    with pytest.raises(TypeError):
        run_preflight(test_config, codex_config_path=None)


def test_preflight_opens_the_database_readonly_and_query_only(
    test_config, tmp_path, monkeypatch
):
    """SQLite access is pinned to mode=ro URIs with PRAGMA query_only=ON."""
    _legacy_library(test_config)
    codex_path = _write_codex_config(tmp_path)
    recorded_connects: list[tuple[str, dict]] = []
    recorded_sql: list[str] = []
    real_connect = sqlite3.connect

    class GuardedConnection:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def execute(self, sql, *args, **kwargs):
            recorded_sql.append(sql)
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

    def guarded_connect(target, *args, **kwargs):
        recorded_connects.append((str(target), dict(kwargs)))
        return GuardedConnection(real_connect(target, *args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    report = run_preflight(test_config, codex_config_path=codex_path)
    assert report.database_ok is True

    assert recorded_connects, "preflight must actually open the database"
    for dsn, kwargs in recorded_connects:
        assert dsn.startswith("file:"), dsn
        assert "mode=ro" in dsn
        assert kwargs.get("uri") is True
    normalized = [statement.strip().upper() for statement in recorded_sql]
    assert any(
        statement.startswith("PRAGMA QUERY_ONLY") for statement in normalized
    )
    forbidden_prefixes = (
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "DROP",
        "ALTER",
        "REPLACE",
        "VACUUM",
        "REINDEX",
        "ATTACH",
        "BEGIN",
        "COMMIT",
    )
    assert not any(
        statement.startswith(prefix)
        for statement in normalized
        for prefix in forbidden_prefixes
    )


# ---- preflight: individual check contracts ----


def test_preflight_reports_a_missing_database_without_creating_it(
    test_config, tmp_path
):
    """A missing database is a failing check, never a new empty file."""
    test_config.data_dir.mkdir(parents=True, exist_ok=True)
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.database_ok is False
    assert report.ready is False
    assert "database_missing" in report.reason_codes
    assert not test_config.db_path.exists()


def test_preflight_reports_an_unreadable_codex_stanza(test_config, tmp_path):
    """A missing target stanza blocks cutover without editing anything."""
    _legacy_library(test_config)
    codex_path = _write_codex_config(tmp_path, _STANZA_MISSING_CONFIG)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.codex_stanza_present is False
    assert report.codex_ok is False
    assert report.ready is False
    assert "codex_stanza_missing" in report.reason_codes


@pytest.mark.parametrize(
    "text",
    [_ENABLED_TOOLS_INCOMPLETE, _DISABLED_TOOLS_BLOCKING],
    ids=["enabled_tools_omits_context_read", "disabled_tools_blocks_search"],
)
def test_preflight_reports_codex_tool_policy_blocking_context_tools(
    test_config, tmp_path, text
):
    """All four context tools must be enableable without policy edits."""
    _legacy_library(test_config)
    codex_path = _write_codex_config(tmp_path, text)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.codex_stanza_present is True
    assert report.codex_context_tools_ok is False
    assert report.codex_ok is False
    assert report.ready is False
    assert "codex_tools_blocked" in report.reason_codes


def test_preflight_reports_invalid_config_without_fixing_it(
    test_config, tmp_path
):
    """Unknown modes and broken weights surface as safe diagnostics."""
    _legacy_library(test_config)
    test_config.context_mode = "bogus-mode"
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.config_ok is False
    assert report.ready is False
    assert "config_invalid" in report.reason_codes
    assert any("context_mode" in message for message in report.config_diagnostics)
    assert test_config.context_mode == "bogus-mode"  # reported, not coerced


def test_preflight_checks_old_vector_metadata_without_initializing_it(
    test_config, tmp_path
):
    """Count/dimension/dirty come from read-only file inspection."""
    _legacy_library(test_config)
    _legacy_vector_file(test_config, dim=8)  # deliberately wrong dimension
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.old_vector_present is True
    assert report.old_vector_count == 2
    assert report.old_vector_dimension == 8
    assert report.vector_ok is False
    assert report.ready is False
    assert "old_vector_dimension_mismatch" in report.reason_codes


def test_preflight_reports_old_vector_dirty_marker_as_metadata(
    test_config, tmp_path
):
    """A legacy dirty marker is recorded but does not fail the cutover preflight."""
    _legacy_library(test_config)
    _legacy_vector_file(test_config)
    dirty_path = test_config.vector_path.with_suffix(".usearch.dirty")
    dirty_path.touch()
    codex_path = _write_codex_config(tmp_path)
    dirty_bytes = dirty_path.read_bytes()

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.old_vector_dirty is True
    assert report.vector_ok is True
    assert report.ready is True
    assert dirty_path.read_bytes() == dirty_bytes


def test_preflight_requires_space_for_two_dbs_the_old_vector_and_margin(
    test_config, tmp_path, monkeypatch
):
    """Free space must cover 2 * db + old vector + 64 MiB of slack."""
    _legacy_library(test_config)
    _legacy_vector_file(test_config)
    codex_path = _write_codex_config(tmp_path)

    db_size = test_config.db_path.stat().st_size
    vector_size = test_config.vector_path.stat().st_size
    expected_required = 2 * db_size + vector_size + 64 * 1024 * 1024

    real_disk_usage = shutil.disk_usage

    def cramped(path):
        usage = real_disk_usage(path)
        return type(usage)(usage.total, usage.total - 1024, 1024)

    import evolvmem.cutover_checks as cutover_checks

    monkeypatch.setattr(cutover_checks.shutil, "disk_usage", cramped)
    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.required_space_bytes == expected_required
    assert report.free_space_bytes == 1024
    assert report.space_ok is False
    assert report.ready is False
    assert "insufficient_free_space" in report.reason_codes


def test_preflight_requires_an_owner_writable_backup_parent_without_creating_it(
    test_config, tmp_path
):
    """The backups parent must be owner-writable in advance; nothing is made."""
    _legacy_library(test_config)
    codex_path = _write_codex_config(tmp_path)
    os.chmod(test_config.data_dir, 0o555)
    try:
        report = run_preflight(test_config, codex_config_path=codex_path)
    finally:
        os.chmod(test_config.data_dir, 0o755)

    assert report.backup_parent_writable is False
    assert report.space_ok is False
    assert report.ready is False
    assert "backup_parent_not_writable" in report.reason_codes
    assert not (test_config.data_dir / "backups").exists()


def test_preflight_counts_duplicate_active_legacy_identities(
    test_config, tmp_path
):
    """Duplicate active identities are counted before migration resolves them."""
    with MemoryStore(test_config) as legacy:
        legacy.add(key="dup:key", value="first duplicate active row.")
        legacy.add(key="dup:key", value="second duplicate active row.")
        legacy.add(key="unique:key", value="a unique active row.")
    shutil.rmtree(test_config.data_dir / "models", ignore_errors=True)
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.legacy_rows == 3
    assert report.legacy_status_counts == {"active": 3}
    assert report.duplicate_active_count == 1
    assert report.database_ok is True


def test_preflight_reports_partial_context_schema_as_inconsistent(
    test_config, tmp_path
):
    """A half-created Context schema is a failed check, not a repair invitation."""
    _legacy_library(test_config)
    conn = sqlite3.connect(str(test_config.db_path))
    try:
        conn.execute("CREATE TABLE context_items (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.context_schema_present is False
    assert report.schema_ok is False
    assert report.ready is False
    assert "context_schema_partial" in report.reason_codes


def test_preflight_reports_existing_context_state_when_present(
    test_config, tmp_path
):
    """A migrated library is a valid preflight subject with Context counts."""
    _legacy_library(test_config)
    with ContextStore(test_config) as store:
        report_migration = LegacyMemoryMigrator(store, test_config).migrate()
        assert report_migration.created == 6
    shutil.rmtree(test_config.data_dir / "models", ignore_errors=True)
    codex_path = _write_codex_config(tmp_path)

    report = run_preflight(test_config, codex_config_path=codex_path)

    assert report.context_schema_present is True
    assert report.context_items == 6
    assert report.ready is True


# ---- report model contracts ----


def _preflight_report(**overrides):
    values = dict(
        ready=True,
        config_ok=True,
        database_ok=True,
        schema_ok=True,
        vector_ok=True,
        codex_ok=True,
        space_ok=True,
        reason_codes=(),
        config_diagnostics=(),
        legacy_schema_present=True,
        legacy_rows=6,
        legacy_status_counts={"active": 3, "superseded": 1},
        duplicate_active_count=0,
        context_schema_present=False,
        context_items=0,
        db_size_bytes=4096,
        db_sha256_prefix="0123456789abcdef",
        old_vector_present=True,
        old_vector_size_bytes=2048,
        old_vector_sha256_prefix="fedcba9876543210",
        old_vector_count=2,
        old_vector_dimension=512,
        old_vector_dirty=False,
        free_space_bytes=1 << 30,
        required_space_bytes=64 * 1024 * 1024 + 3 * 4096,
        backup_parent_writable=True,
        codex_stanza_present=True,
        codex_context_tools_ok=True,
        codex_config_sha256_prefix="0011223344556677",
        duration_ms=1.5,
    )
    values.update(overrides)
    return CutoverPreflightReport(**values)


def test_preflight_report_is_frozen_and_validates_its_fields():
    """Reports are immutable evidence with typed, bounded fields."""
    report = _preflight_report()
    with pytest.raises(FrozenInstanceError):
        report.ready = False

    with pytest.raises(ContextValidationError, match="reason_codes"):
        _preflight_report(reason_codes=("Not A Code!",))

    with pytest.raises(ContextValidationError, match="non-negative"):
        _preflight_report(legacy_rows=-1)

    with pytest.raises(ContextValidationError, match="boolean"):
        _preflight_report(ready=1)

    with pytest.raises(ContextValidationError, match="duration_ms"):
        _preflight_report(duration_ms=float("nan"))

    with pytest.raises(ContextValidationError, match="checksum"):
        _preflight_report(db_sha256_prefix="not-a-hex-prefix!")


def test_public_dict_carries_only_the_allowed_summary_vocabulary():
    """Public serialization is counts/booleans/codes/sizes/prefixes/duration."""
    report = _preflight_report()
    public = report.public_dict()

    assert set(public) == {
        "schema",
        "version",
        "duration_ms",
        "ready",
        "config_ok",
        "database_ok",
        "schema_ok",
        "vector_ok",
        "codex_ok",
        "space_ok",
        "reason_codes",
        "config_diagnostics",
        "legacy_schema_present",
        "legacy_rows",
        "legacy_status_counts",
        "duplicate_active_count",
        "context_schema_present",
        "context_items",
        "db_size_bytes",
        "db_sha256_prefix",
        "old_vector_present",
        "old_vector_size_bytes",
        "old_vector_sha256_prefix",
        "old_vector_count",
        "old_vector_dimension",
        "old_vector_dirty",
        "free_space_bytes",
        "required_space_bytes",
        "backup_parent_writable",
        "codex_stanza_present",
        "codex_context_tools_ok",
        "codex_config_sha256_prefix",
    }
    assert public["schema"] == "evolvmem.cutover_preflight"
    assert public["version"] == 1
    validate_public_summary(public)
    json.dumps(public)  # plain JSON, no custom types


def test_report_digest_is_canonical_and_content_sensitive():
    """Equal reports share a digest; any field change moves it."""
    first = _preflight_report()
    same = _preflight_report()
    different = _preflight_report(legacy_rows=7)

    assert first.digest() == same.digest()
    assert first.digest() != different.digest()
    assert first.digest() == hashlib.sha256(
        json.dumps(
            first.public_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "key, value",
    [
        ("query", "what did we decide"),
        ("content", "classified zebra body"),
        ("value", "memory body text"),
        ("archive_payload", "{}"),
        ("secret", "synthetic-token-not-real"),
        ("path", "/home/demo-user/evolvmem"),
        ("db_path", "/data/memory.db"),
        ("counts", {"active": 1, "note": "/absolute/path"}),
        ("reason_codes", ("see /home/demo-user/x",)),
        ("reason_codes", ("line one\nline two",)),
        ("note", "C:\\data\\memory.db"),
    ],
    ids=lambda item: str(item)[:40],
)
def test_public_summary_validation_rejects_sensitive_keys_and_values(key, value):
    """Content, queries, archive payloads, secrets, and paths never serialize."""
    with pytest.raises(ContextValidationError):
        validate_public_summary({"schema": "x", "version": 1, key: value})


def test_public_summary_validation_accepts_the_plain_vocabulary():
    """Counts, booleans, codes, sizes, prefixes, and durations pass."""
    validate_public_summary(
        {
            "schema": "evolvmem.cutover_preflight",
            "version": 1,
            "ready": True,
            "legacy_rows": 6,
            "legacy_status_counts": {"active": 3},
            "old_vector_dimension": None,
            "reason_codes": ["database_missing"],
            "db_sha256_prefix": "0123456789abcdef",
            "duration_ms": 0.25,
        }
    )


# ---- projection lag: one counter per mismatch class ----


def _migrated_library(config: Config) -> tuple[dict[str, int], ContextStore]:
    """Healthy dual library: every legacy row migrated into Context Core."""
    ids = _legacy_library(config)
    store = ContextStore(config)
    store.initialize()
    report = LegacyMemoryMigrator(store, config).migrate()
    assert report.created == len(ids)
    return ids, store


def _insert_raw_legacy_row(store: ContextStore, key: str, value: str) -> int:
    """A legacy projection write that deliberately bypasses the migrator."""
    with store.transaction():
        return store.legacy_projection().insert(
            LegacyProjectionInsert(key=key, value=value)
        )


def test_healthy_migrated_library_has_zero_projection_lag(test_config):
    """Every mismatch class is zero right after a complete, idempotent migration."""
    ids, store = _migrated_library(test_config)
    try:
        report = check_projection_lag(test_config, store)
        assert report.projection_lag == 0
        assert report.missing_mapping == 0
        assert report.duplicate_mapping_target == 0
        assert report.layer_mismatch == 0
        assert report.status_mismatch == 0
        assert report.l1_mismatch == 0
        assert report.supersession_mismatch == 0
        assert report.orphan_mapping == 0
        assert report.dangling_item_mapping == 0
        assert report.legacy_rows == len(ids)
        assert report.mapping_rows == len(ids)

        second = LegacyMemoryMigrator(store, test_config).migrate()
        assert second.created == 0  # idempotent re-run
        assert check_projection_lag(test_config, store).projection_lag == 0
    finally:
        store.close()


def test_healthy_duplicate_active_migration_has_zero_projection_lag(test_config):
    """The duplicate-active→candidate mapping policy is consistency, not lag."""
    with MemoryStore(test_config) as legacy:
        legacy.add(key="dup:key", value="first duplicate active row.")
        legacy.add(key="dup:key", value="second duplicate active row.")
    store = ContextStore(test_config)
    store.initialize()
    try:
        report_migration = LegacyMemoryMigrator(store, test_config).migrate()
        assert report_migration.duplicate_active_count == 1
        report = check_projection_lag(test_config, store)
        assert report.projection_lag == 0
        assert report.status_mismatch == 0
    finally:
        store.close()


def test_projection_lag_counts_a_missing_mapping(test_config):
    """A legacy row never migrated (or lazily skipped) is exactly one lag."""
    _migrated_ids, store = _migrated_library(test_config)
    try:
        _insert_raw_legacy_row(store, "late:key", "a row written around the dual path")
        report = check_projection_lag(test_config, store)
        assert report.missing_mapping == 1
        assert report.projection_lag == 1
    finally:
        store.close()


def test_projection_lag_counts_a_duplicate_mapping_target(test_config):
    """Two legacy rows mapped to one ContextItem break the 1:1 projection."""
    _ids, store = _migrated_library(test_config)
    try:
        with store.transaction():
            first = store.legacy_projection().insert(
                LegacyProjectionInsert(
                    key="dup-target:a", value="a row that owns its mapped item"
                )
            )
            item_id = LegacyMemoryMigrator(store, test_config).migrate_projection_row(
                store.legacy_projection().get_by_id(first)
            )
            second = store.legacy_projection().insert(
                LegacyProjectionInsert(
                    key="dup-target:b", value="a row sharing the same mapped item"
                )
            )
            store.record_legacy_mapping(second, item_id)
        report = check_projection_lag(test_config, store)
        assert report.duplicate_mapping_target == 1
        # The sharing row's derived L1 genuinely differs from the item's.
        assert report.l1_mismatch == 1
        assert report.missing_mapping == 0
        assert report.projection_lag == 2
    finally:
        store.close()


def test_projection_lag_counts_a_missing_layer(test_config):
    """A mapped item must have exactly L0/L1/L2; the schema forbids extras."""
    ids, store = _migrated_library(test_config)
    try:
        item_id = store.resolve_legacy_mapping(ids["active"])
        with store.transaction():
            store._connection().execute(
                "DELETE FROM context_layers WHERE item_id=? AND layer='l2'",
                (item_id,),
            )
        report = check_projection_lag(test_config, store)
        assert report.layer_mismatch == 1
        assert report.projection_lag == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "fixture_name, tampered_status",
    [
        ("active", "archived"),
        ("active", "deleted"),
        ("active", "superseded"),
        ("archived", "active"),
        ("deleted", "active"),
    ],
)
def test_projection_lag_counts_status_mismatch(
    test_config, fixture_name, tampered_status
):
    """active/superseded/archived/deleted drift on either side is lag."""
    ids, store = _migrated_library(test_config)
    try:
        with store.transaction():
            store._connection().execute(
                "UPDATE memories SET status=? WHERE id=?",
                (tampered_status, ids[fixture_name]),
            )
        report = check_projection_lag(test_config, store)
        assert report.status_mismatch == 1
        assert report.projection_lag == 1
    finally:
        store.close()


def test_projection_lag_counts_derived_l1_mismatch(test_config):
    """Core L1 must equal the deterministic layers_from_legacy_value projection."""
    ids, store = _migrated_library(test_config)
    try:
        item_id = store.resolve_legacy_mapping(ids["active"])
        with store.transaction():
            store._connection().execute(
                "UPDATE context_layers SET content='tampered l1 summary' "
                "WHERE item_id=? AND layer='l1'",
                (item_id,),
            )
        report = check_projection_lag(test_config, store)
        assert report.l1_mismatch == 1
        assert report.projection_lag == 1
    finally:
        store.close()


@pytest.mark.parametrize("column", ["supersedes", "superseded_by"])
def test_projection_lag_counts_supersession_link_mismatch(test_config, column):
    """Both directions of the replacement chain must mirror the legacy links."""
    ids, store = _migrated_library(test_config)
    try:
        legacy_id = ids["successor"] if column == "supersedes" else ids["superseded"]
        item_id = store.resolve_legacy_mapping(legacy_id)
        with store.transaction():
            store._connection().execute(
                f"UPDATE context_items SET {column}=NULL WHERE id=?", (item_id,)
            )
        report = check_projection_lag(test_config, store)
        assert report.supersession_mismatch == 1
        assert report.projection_lag == 1
    finally:
        store.close()


def test_projection_lag_counts_an_orphan_mapping(test_config):
    """A mapping whose legacy row vanished is projection corruption."""
    ids, store = _migrated_library(test_config)
    try:
        item_id = store.resolve_legacy_mapping(ids["active"])
        with store.transaction():
            store.record_legacy_mapping(99999, item_id)
        report = check_projection_lag(test_config, store)
        assert report.orphan_mapping == 1
        assert report.mapping_rows == len(ids) + 1
        # The orphan also makes the item a duplicate target.
        assert report.duplicate_mapping_target == 1
        assert report.projection_lag == 2
    finally:
        store.close()


def test_projection_lag_counts_a_mapping_to_a_nonexistent_item(test_config):
    """A mapping whose ContextItem vanished is lag, counted exactly once."""
    _ids, store = _migrated_library(test_config)
    try:
        # Simulate corruption: the foreign key must be off to create this state.
        store._connection().execute("PRAGMA foreign_keys=OFF")
        with store.transaction():
            legacy_id = store.legacy_projection().insert(
                LegacyProjectionInsert(
                    key="dangling:key", value="a row mapped to a vanished item"
                )
            )
            store.record_legacy_mapping(legacy_id, 99999)
        report = check_projection_lag(test_config, store)
        assert report.dangling_item_mapping == 1
        assert report.missing_mapping == 0
        assert report.projection_lag == 1
    finally:
        store.close()


def test_explicit_dual_hard_delete_is_legitimately_excluded(test_config):
    """A row removed by the ordered mapping→legacy→item delete leaves no lag."""
    ids, store = _migrated_library(test_config)
    try:
        legacy_id = ids["archived"]
        item_id = store.resolve_legacy_mapping(legacy_id)
        assert item_id is not None
        with store.transaction():
            # The exact ContextService.legacy_hard_delete write order.
            store.delete_legacy_mapping(legacy_id)
            store.legacy_projection().hard_delete(legacy_id)
            store.hard_delete_item(item_id)
        report = check_projection_lag(test_config, store)
        assert report.projection_lag == 0
        assert report.legacy_rows == len(ids) - 1
        assert report.mapping_rows == len(ids) - 1
    finally:
        store.close()


def test_vector_caches_are_reported_separately_never_as_projection_truth(
    test_config,
):
    """Dirty/stale vector caches affect health fields only, never the lag total."""

    class FakeVector:
        def __init__(self, count, dirty):
            self._count = count
            self._dirty = dirty

        def count(self):
            return self._count

        def is_dirty(self):
            return self._dirty

    class BrokenVector:
        def count(self):
            raise RuntimeError("not initialized")

        def is_dirty(self):
            raise RuntimeError("not initialized")

    ids, store = _migrated_library(test_config)
    try:
        report = check_projection_lag(
            test_config,
            store,
            legacy_vector_index=FakeVector(count=999, dirty=True),
            context_vector_index=FakeVector(count=0, dirty=True),
        )
        assert report.projection_lag == 0
        assert report.legacy_vector_count == 999
        assert report.legacy_vector_dirty is True
        assert report.context_vector_count == 0
        assert report.context_vector_dirty is True

        uninspected = check_projection_lag(test_config, store)
        assert uninspected.legacy_vector_count is None
        assert uninspected.legacy_vector_dirty is None

        broken = check_projection_lag(
            test_config,
            store,
            legacy_vector_index=BrokenVector(),
            context_vector_index=BrokenVector(),
        )
        assert broken.projection_lag == 0
        assert broken.legacy_vector_count is None
        assert broken.legacy_vector_dirty is None
    finally:
        store.close()


def test_projection_lag_report_public_summary_and_digest(test_config):
    """The lag report serializes as bounded counts plus optional vector health."""
    ids, store = _migrated_library(test_config)
    try:
        report = check_projection_lag(test_config, store)
        public = report.public_dict()
        assert set(public) == {
            "schema",
            "version",
            "duration_ms",
            "projection_lag",
            "missing_mapping",
            "duplicate_mapping_target",
            "layer_mismatch",
            "status_mismatch",
            "l1_mismatch",
            "supersession_mismatch",
            "orphan_mapping",
            "dangling_item_mapping",
            "legacy_rows",
            "mapping_rows",
            "legacy_vector_count",
            "legacy_vector_dirty",
            "context_vector_count",
            "context_vector_dirty",
        }
        assert public["schema"] == "evolvmem.projection_lag"
        validate_public_summary(public)
        assert report.digest() == report.digest()
        assert len(report.digest()) == 64

        same = check_projection_lag(test_config, store)
        assert set(same.public_dict()) == set(public)
    finally:
        store.close()


def test_projection_lag_report_validates_its_total():
    """The total is the sum of the classes; anything else is a caller bug."""

    def report(**overrides):
        values = dict(
            projection_lag=0,
            missing_mapping=0,
            duplicate_mapping_target=0,
            layer_mismatch=0,
            status_mismatch=0,
            l1_mismatch=0,
            supersession_mismatch=0,
            orphan_mapping=0,
            dangling_item_mapping=0,
            legacy_rows=0,
            mapping_rows=0,
            legacy_vector_count=None,
            legacy_vector_dirty=None,
            context_vector_count=None,
            context_vector_dirty=None,
            duration_ms=0.0,
        )
        values.update(overrides)
        return ProjectionLagReport(**values)

    assert report(missing_mapping=1, projection_lag=1).projection_lag == 1
    with pytest.raises(ContextValidationError, match="sum"):
        report(missing_mapping=1, projection_lag=0)
    with pytest.raises(FrozenInstanceError):
        report().projection_lag = 5  # type: ignore[misc]


# ---- shadow comparison: pure ID-overlap evaluation ----


def test_shadow_freezes_mapped_exact_and_cjk_top1_at_100_percent():
    """Exact and CJK queries must keep their mapped top-1 on every comparison."""
    exact = compare_shadow([42, 7], [900, 901], {42: 900, 7: 901}, expected_relevant=1)
    cjk = compare_shadow([5], [77], {5: 77}, expected_relevant=1)
    assert exact.top1_match is True
    assert cjk.top1_match is True

    gate = evaluate_shadow_gate([exact, cjk])
    assert gate.thresholds_met is True
    assert gate.reason_codes == ()
    assert gate.comparisons == 2
    assert gate.exact_total == 2
    assert gate.exact_top1_matches == 2
    assert gate.semantic_total == 0


def test_shadow_gate_fails_when_a_mapped_exact_top1_is_lost():
    """A single missed exact top-1 fails the gate, whatever the overlap."""
    comparison = compare_shadow([42, 7], [901, 900], {42: 900, 7: 901}, expected_relevant=1)
    assert comparison.top1_match is False

    gate = evaluate_shadow_gate([comparison])
    assert gate.thresholds_met is False
    assert gate.reason_codes == ("shadow_exact_top1_failed",)
    assert gate.exact_top1_matches == 0


def test_shadow_semantic_overlap_at_5_meets_the_point_eight_threshold():
    """Five expected relevant items with four mapped shared hits is exactly 0.80."""
    comparison = compare_shadow(
        [1, 2, 3, 4, 5],
        [11, 12, 13, 14, 99],
        {1: 11, 2: 12, 3: 13, 4: 14, 5: 15},
        expected_relevant=5,
    )
    assert comparison.overlap_at_5 == pytest.approx(0.8)
    gate = evaluate_shadow_gate([comparison])
    assert gate.thresholds_met is True
    assert gate.semantic_total == 1
    assert gate.semantic_overlap_passes == 1


def test_shadow_semantic_overlap_below_point_eight_fails():
    """Three of five shared hits (0.60) cannot pass the semantic gate."""
    comparison = compare_shadow(
        [1, 2, 3, 4, 5],
        [11, 12, 13, 98, 99],
        {1: 11, 2: 12, 3: 13, 4: 14, 5: 15},
        expected_relevant=5,
    )
    assert comparison.overlap_at_5 == pytest.approx(0.6)
    gate = evaluate_shadow_gate([comparison])
    assert gate.thresholds_met is False
    assert gate.reason_codes == ("shadow_overlap_below_threshold",)


def test_shadow_below_threshold_pure_vector_drops_are_not_failures():
    """Core legitimately dropped sub-0.80 pure-vector IDs; they leave the count."""
    mapping = {1: 11, 2: 12, 3: 13, 4: 14, 5: 15}
    legacy = [1, 2, 3, 4, 5]
    core = [11, 12, 13]  # 14 and 15 fell below context_vector_min_similarity

    excluded = compare_shadow(
        legacy,
        core,
        mapping,
        expected_relevant=5,
        below_threshold_core_ids=frozenset({14, 15}),
    )
    assert excluded.below_threshold_excluded == 2
    assert excluded.overlap_at_5 == pytest.approx(1.0)
    assert evaluate_shadow_gate([excluded]).thresholds_met is True

    counted = compare_shadow(legacy, core, mapping, expected_relevant=5)
    assert counted.overlap_at_5 == pytest.approx(0.6)
    assert evaluate_shadow_gate([counted]).thresholds_met is False


def test_shadow_all_below_threshold_legacy_hits_are_vacuously_consistent():
    """When every mapped legacy hit was threshold-dropped, nothing is comparable."""
    comparison = compare_shadow(
        [1], [], {1: 11}, expected_relevant=5, below_threshold_core_ids=frozenset({11})
    )
    assert comparison.below_threshold_excluded == 1
    assert comparison.overlap_at_5 == pytest.approx(1.0)
    assert comparison.top1_match is False  # one side non-empty: no vacuous top-1


def test_shadow_empty_or_unknown_mapping_cannot_verify_hits():
    """Unmapped legacy IDs are counted and can never produce a verified top-1."""
    empty_mapping = compare_shadow([1, 2], [11, 12], {}, expected_relevant=1)
    assert empty_mapping.mapped_legacy_count == 0
    assert empty_mapping.unmapped_legacy_count == 2
    assert empty_mapping.top1_match is False
    assert empty_mapping.overlap_at_5 == 0.0
    assert evaluate_shadow_gate([empty_mapping]).thresholds_met is False

    unknown_top1 = compare_shadow([9, 1], [11], {1: 11}, expected_relevant=1)
    assert unknown_top1.top1_match is False  # legacy top-1 itself is unmappable
    assert unknown_top1.mapped_legacy_count == 1
    assert unknown_top1.unmapped_legacy_count == 1


def test_shadow_both_sides_empty_is_a_vacuous_top1_agreement():
    """Two empty rankings agree trivially but show zero overlap evidence."""
    comparison = compare_shadow([], [], {}, expected_relevant=1)
    assert comparison.legacy_count == 0
    assert comparison.core_count == 0
    assert comparison.top1_match is True
    assert comparison.overlap_at_5 == 0.0


def test_shadow_gate_requires_actual_comparison_evidence():
    """No shadow comparisons means the thresholds were never demonstrated."""
    gate = evaluate_shadow_gate([])
    assert gate.thresholds_met is False
    assert gate.reason_codes == ("shadow_evidence_missing",)
    assert gate.comparisons == 0


def test_shadow_gate_combines_exact_and_semantic_evidence():
    """Mixed batches pass only when every per-query rule holds."""
    comparisons = (
        compare_shadow([42], [900], {42: 900}, expected_relevant=1),
        compare_shadow(
            [1, 2, 3, 4, 5],
            [11, 12, 13, 14, 99],
            {1: 11, 2: 12, 3: 13, 4: 14, 5: 15},
            expected_relevant=8,
        ),
    )
    gate = evaluate_shadow_gate(comparisons)
    assert gate.thresholds_met is True
    assert gate.comparisons == 2
    assert gate.exact_total == 1
    assert gate.semantic_total == 1

    broken = evaluate_shadow_gate(
        comparisons
        + (compare_shadow([7], [71], {7: 70}, expected_relevant=1),)
    )
    assert broken.thresholds_met is False
    assert broken.reason_codes == ("shadow_exact_top1_failed",)
    assert broken.exact_total == 2
    assert broken.exact_top1_matches == 1


def test_compare_shadow_validates_its_inputs():
    """IDs, mapping, and expectations are typed; junk fails closed."""
    with pytest.raises(ContextValidationError, match="expected_relevant"):
        compare_shadow([1], [1], {1: 1}, expected_relevant=-1)
    with pytest.raises(ContextValidationError, match="legacy_ids"):
        compare_shadow(["one"], [1], {1: 1}, expected_relevant=1)
    with pytest.raises(ContextValidationError, match="mapping"):
        compare_shadow([1], [1], {1: "one"}, expected_relevant=1)
    with pytest.raises(ContextValidationError, match="below_threshold_core_ids"):
        compare_shadow([1], [1], {1: 1}, expected_relevant=1,
                       below_threshold_core_ids=frozenset({"x"}))


def test_shadow_reports_carry_no_query_or_body_fields():
    """Shadow serializations are counts/booleans/ratios — never query text."""
    comparison = compare_shadow(
        [1, 2], [11, 12], {1: 11, 2: 12}, expected_relevant=1
    )
    public = comparison.public_dict()
    assert set(public) == {
        "schema",
        "version",
        "expected_relevant",
        "legacy_count",
        "core_count",
        "mapped_legacy_count",
        "unmapped_legacy_count",
        "below_threshold_excluded",
        "top1_match",
        "overlap_at_5",
    }
    validate_public_summary(public)
    serialized = json.dumps(public)
    for leaked in ("query", "content", "退款", "zebra", "/"):
        assert leaked not in serialized
    assert comparison.digest() == comparison.digest()

    gate_public = evaluate_shadow_gate([comparison]).public_dict()
    assert set(gate_public) == {
        "schema",
        "version",
        "comparisons",
        "exact_total",
        "exact_top1_matches",
        "semantic_total",
        "semantic_overlap_passes",
        "thresholds_met",
        "reason_codes",
    }
    validate_public_summary(gate_public)

    with pytest.raises(FrozenInstanceError):
        comparison.top1_match = False  # type: ignore[misc]
    with pytest.raises(ContextValidationError, match="overlap_at_5"):
        ShadowComparison(
            expected_relevant=1,
            legacy_count=0,
            core_count=0,
            mapped_legacy_count=0,
            unmapped_legacy_count=0,
            below_threshold_excluded=0,
            top1_match=True,
            overlap_at_5=1.5,
        )
    with pytest.raises(ContextValidationError, match="add up"):
        ShadowComparison(
            expected_relevant=1,
            legacy_count=2,
            core_count=0,
            mapped_legacy_count=1,
            unmapped_legacy_count=0,
            below_threshold_excluded=0,
            top1_match=False,
            overlap_at_5=0.0,
        )
    with pytest.raises(ContextValidationError, match="exact_total \\+ semantic_total"):
        ShadowGateReport(
            comparisons=3,
            exact_total=1,
            exact_top1_matches=1,
            semantic_total=1,
            semantic_overlap_passes=1,
            thresholds_met=True,
            reason_codes=(),
        )


# ---- primary gate: reusable invariant verdict over measured evidence ----


def _healthy_evidence(**overrides) -> PrimaryGateEvidence:
    values = dict(
        quick_check_ok=True,
        legacy_rows_unmapped=0,
        duplicate_mapping_targets=0,
        mapped_items_with_wrong_layers=0,
        second_migration_created=0,
        projection_lag=0,
        vector_healthy=True,
        fts_only_approved=False,
        shadow_thresholds_met=True,
        config_diagnostics=(),
    )
    values.update(overrides)
    return PrimaryGateEvidence(**values)


def test_primary_gate_passes_only_when_every_invariant_holds():
    """The full healthy evidence set yields ready_primary and no reasons."""
    report = verify_primary_gate(_healthy_evidence())
    assert report.ready_primary is True
    assert report.reason_codes == ()
    assert report.quick_check_ok is True
    assert report.mapping_complete is True
    assert report.layers_complete is True
    assert report.migration_idempotent is True
    assert report.projection_lag_zero is True
    assert report.vector_ready is True
    assert report.shadow_thresholds_met is True
    assert report.config_clean is True


@pytest.mark.parametrize(
    "overrides, reason_code",
    [
        ({"quick_check_ok": False}, "quick_check_failed"),
        ({"legacy_rows_unmapped": 1}, "legacy_mapping_incomplete"),
        ({"duplicate_mapping_targets": 1}, "duplicate_mapping_target"),
        ({"mapped_items_with_wrong_layers": 1}, "layer_invariant_failed"),
        ({"second_migration_created": 1}, "migration_not_idempotent"),
        ({"projection_lag": 1}, "projection_lag_nonzero"),
        ({"vector_healthy": False}, "context_vector_unhealthy"),
        ({"shadow_thresholds_met": False}, "shadow_thresholds_unmet"),
        (
            {"config_diagnostics": ("context_mode must be one of 'legacy', 'compat', 'shadow', 'primary'",)},
            "config_diagnostics_present",
        ),
    ],
    ids=lambda item: str(item)[:44],
)
def test_primary_gate_fails_per_invariant_with_a_stable_reason_code(
    overrides, reason_code
):
    """Each failed invariant alone flips ready_primary and names its reason."""
    report = verify_primary_gate(_healthy_evidence(**overrides))
    assert report.ready_primary is False
    assert reason_code in report.reason_codes


def test_primary_gate_accepts_a_journaled_fts_only_approval_for_vector():
    """Vector unhealthy plus an explicit FTS-only approval keeps vector_ready."""
    report = verify_primary_gate(
        _healthy_evidence(vector_healthy=False, fts_only_approved=True)
    )
    assert report.vector_ready is True
    assert report.ready_primary is True
    assert "context_vector_unhealthy" not in report.reason_codes


def test_primary_gate_rejects_non_evidence_and_validates_types():
    """The gate takes typed evidence; a bare ready flag is not evidence."""
    with pytest.raises(ContextValidationError, match="PrimaryGateEvidence"):
        verify_primary_gate({"ready_primary": True})
    with pytest.raises(ContextValidationError, match="boolean"):
        _healthy_evidence(vector_healthy=1)
    with pytest.raises(ContextValidationError, match="non-negative"):
        _healthy_evidence(projection_lag=-1)
    with pytest.raises(FrozenInstanceError):
        _healthy_evidence().projection_lag = 1  # type: ignore[misc]


def test_primary_gate_report_public_summary_and_digest():
    """The gate report serializes as booleans, counts, codes, and diagnostics."""
    report = verify_primary_gate(_healthy_evidence())
    public = report.public_dict()
    assert set(public) == {
        "schema",
        "version",
        "ready_primary",
        "quick_check_ok",
        "mapping_complete",
        "layers_complete",
        "migration_idempotent",
        "projection_lag_zero",
        "vector_ready",
        "shadow_thresholds_met",
        "config_clean",
        "legacy_rows_unmapped",
        "duplicate_mapping_targets",
        "mapped_items_with_wrong_layers",
        "second_migration_created",
        "projection_lag",
        "reason_codes",
        "config_diagnostics",
    }
    assert public["schema"] == "evolvmem.primary_gate"
    validate_public_summary(public)
    assert report.digest() == verify_primary_gate(_healthy_evidence()).digest()
    assert (
        report.digest()
        != verify_primary_gate(_healthy_evidence(projection_lag=1)).digest()
    )


def test_collect_evidence_from_a_healthy_migrated_library(test_config):
    """Read-only collection derives every DB-side invariant from real state."""
    _ids, store = _migrated_library(test_config)
    try:
        second = LegacyMemoryMigrator(store, test_config).migrate()
        evidence = collect_primary_gate_evidence(
            test_config,
            store,
            second_migration_created=second.created,
            shadow_thresholds_met=True,
            context_vector_index=_CountingVector(count=3, path=test_config.context_vector_path),
        )
        assert evidence.quick_check_ok is True
        assert evidence.legacy_rows_unmapped == 0
        assert evidence.duplicate_mapping_targets == 0
        assert evidence.mapped_items_with_wrong_layers == 0
        assert evidence.second_migration_created == 0
        assert evidence.projection_lag == 0
        assert evidence.vector_healthy is True
        assert evidence.shadow_thresholds_met is True
        assert evidence.config_diagnostics == ()
        assert verify_primary_gate(evidence).ready_primary is True
    finally:
        store.close()


def test_collect_evidence_never_fakes_ceremony_proofs(test_config):
    """Without measured shadow/idempotence evidence the gate cannot pass."""
    _ids, store = _migrated_library(test_config)
    try:
        evidence = collect_primary_gate_evidence(test_config, store)
        assert evidence.shadow_thresholds_met is False
        assert evidence.vector_healthy is False  # no index inspected
        report = verify_primary_gate(evidence)
        assert report.ready_primary is False
        assert "shadow_thresholds_unmet" in report.reason_codes
        assert "context_vector_unhealthy" in report.reason_codes
    finally:
        store.close()


def test_collect_evidence_derives_idempotence_from_mapping_coverage(test_config):
    """An unmapped row is exactly what a second migration run would create."""
    _ids, store = _migrated_library(test_config)
    try:
        _insert_raw_legacy_row(store, "late:key", "a row written around the dual path")
        evidence = collect_primary_gate_evidence(test_config, store)
        assert evidence.legacy_rows_unmapped == 1
        assert evidence.second_migration_created == 1  # derived, not measured
        report = verify_primary_gate(evidence)
        assert report.ready_primary is False
        assert "legacy_mapping_incomplete" in report.reason_codes
        assert "migration_not_idempotent" in report.reason_codes
        assert "projection_lag_nonzero" in report.reason_codes
    finally:
        store.close()


def test_collect_evidence_judges_context_vector_health_from_metadata(test_config):
    """Vector healthy requires the right path, no dirty marker, and count parity."""

    class DirtyVector(_CountingVector):
        def is_dirty(self):
            return True

    _ids, store = _migrated_library(test_config)
    try:
        healthy = _CountingVector(count=3, path=test_config.context_vector_path)
        evidence = collect_primary_gate_evidence(
            test_config, store, context_vector_index=healthy
        )
        assert evidence.vector_healthy is True

        dirty = collect_primary_gate_evidence(
            test_config,
            store,
            context_vector_index=DirtyVector(count=3, path=test_config.context_vector_path),
        )
        assert dirty.vector_healthy is False

        wrong_count = collect_primary_gate_evidence(
            test_config,
            store,
            context_vector_index=_CountingVector(
                count=99, path=test_config.context_vector_path
            ),
        )
        assert wrong_count.vector_healthy is False

        wrong_path = collect_primary_gate_evidence(
            test_config,
            store,
            context_vector_index=_CountingVector(count=3, path=test_config.vector_path),
        )
        assert wrong_path.vector_healthy is False
    finally:
        store.close()


class _CountingVector:
    """Duck-typed context vector metadata: path, count, dirty."""

    def __init__(self, *, count, path):
        self._count = count
        self.path = Path(path).resolve()

    def count(self):
        return self._count

    def is_dirty(self):
        return False
