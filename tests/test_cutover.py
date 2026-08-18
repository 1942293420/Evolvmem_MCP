"""Behavioral contracts for the cutover orchestrator and its journal.

The orchestrator is dry-run by default: without ``apply`` it performs no
write of any kind. The apply path runs the ten design steps in exact order
under the exclusive cutover lock — locked preflight rerun compared by
digest, stanza snapshot plus verified backup, single-transaction schema +
idempotent migration, mapping/layer validation with a zero-create second
migration, atomic Context vector staging (or an explicitly approved
degraded FTS-only stop), canary shadow gates with projection lag zero,
atomic persistent ``context_mode=compat``, CAS Codex primary with
dual-source verification, then lock release and the
``awaiting_post_cutover_canary`` journal mark.

Failure before the compat persist changes neither the EvolvMem mode nor
the Codex config. Failure at/after the Codex primary step forces an
operational CAS rollback to explicit legacy, retains the persistent compat
mode and the migrated Context data, and never restores or deletes the
database. The owner-only journal inside the verified backup directory
moves only forward through the documented state chain; failure and
rollback are terminal side branches carrying the exact failed step. All
fixtures are synthetic and live in temporary directories.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from evolvmem.codex_config import (
    CodexConfigApplyResult,
    CodexConfigEditor,
    CodexMcpGetResult,
    CodexMcpGetTransport,
    CodexMcpSnapshot,
    CodexStanzaDriftError,
)
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMigrationReport
from evolvmem.context_models import ContextMode, ContextValidationError
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.cutover_backup import (
    BackupManifest,
    BackupVerificationReport,
    CutoverBackupError,
)
from evolvmem.cutover_checks import run_preflight
from evolvmem.cutover_lock import CutoverLock, CutoverLockTimeout
from evolvmem.cutover_models import (
    CutoverPreflightReport,
    PrimaryGateReport,
    ProjectionLagReport,
    ShadowGateReport,
    validate_public_summary,
)
from evolvmem.cutover_vector import ContextVectorStageReport
from evolvmem.legacy_models import LegacyAddRequest
from evolvmem.legacy_projection import LegacyProjectionInsert
from evolvmem.memory_store import MemoryStore

from evolvmem.cutover import (
    CUTOVER_STEPS,
    CutoverError,
    CutoverJournal,
    CutoverJournalError,
    CutoverRequest,
    JOURNAL_FILENAME,
    JOURNAL_STATES,
    load_persisted_config,
    preflight_fingerprint,
    run_cutover,
    write_preflight_envelope,
)


# ---- shared synthetic fixtures ----

CODEX_CONFIG_TEXT = """# synthetic Codex config; paths and tokens are fake fixtures
[mcp_servers.evolvmem]
command = "/opt/evolvmem/bin/python3"
args = ["-m", "evolvmem.mcp_server"]
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
EVOLVMEM_CONTEXT_MODE = "legacy"
FAKE_API_TOKEN = "synthetic-token-not-real"
"""

_ZERO = "0" * 64
_STEP = {
    "validate_inputs": "validate_inputs",
    "lock": "lock_and_rerun_preflight",
    "backup": "snapshot_and_backup",
    "migrate": "migrate_schema",
    "validate": "validate_migration",
    "vector": "stage_vector",
    "shadow": "shadow_gate",
    "persist": "persist_compat",
    "codex": "codex_primary",
    "release": "release_and_await_canary",
}


def _stanza_sha256(stanza) -> str:
    blob = json.dumps(
        stanza, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_codex_config(directory: Path, text: str = CODEX_CONFIG_TEXT) -> Path:
    path = directory / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _preflight_report(**overrides) -> CutoverPreflightReport:
    fields = dict(
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
        legacy_rows=3,
        legacy_status_counts={"active": 3},
        duplicate_active_count=0,
        context_schema_present=False,
        context_items=0,
        db_size_bytes=4096,
        db_sha256_prefix="0123456789abcdef",
        old_vector_present=False,
        old_vector_size_bytes=0,
        old_vector_sha256_prefix="",
        old_vector_count=None,
        old_vector_dimension=None,
        old_vector_dirty=False,
        free_space_bytes=1 << 30,
        required_space_bytes=1 << 20,
        backup_parent_writable=True,
        codex_stanza_present=True,
        codex_context_tools_ok=True,
        codex_config_sha256_prefix="abcdef0123456789",
        duration_ms=1.0,
    )
    fields.update(overrides)
    return CutoverPreflightReport(**fields)


def _lag_report(**overrides) -> ProjectionLagReport:
    fields = dict(
        projection_lag=0,
        missing_mapping=0,
        duplicate_mapping_target=0,
        layer_mismatch=0,
        status_mismatch=0,
        l1_mismatch=0,
        supersession_mismatch=0,
        orphan_mapping=0,
        dangling_item_mapping=0,
        legacy_rows=3,
        mapping_rows=3,
        legacy_vector_count=None,
        legacy_vector_dirty=None,
        context_vector_count=None,
        context_vector_dirty=None,
        duration_ms=0.5,
    )
    fields.update(overrides)
    return ProjectionLagReport(**fields)


def _backup_manifest(directory_name: str = "context-core-cutover-20260818T000000Z"):
    return BackupManifest(
        complete=True,
        directory_name=directory_name,
        created_utc="2026-08-18T00:00:00Z",
        preflight_digest=_ZERO,
        database_filename="memory.db",
        database_size_bytes=4096,
        database_sha256=_ZERO,
        quick_check_ok=True,
        sqlite_user_version=0,
        legacy_schema_present=True,
        legacy_rows=3,
        legacy_status_counts={"active": 3},
        context_schema_present=False,
        context_items=0,
        config_present=False,
        config_filename="",
        config_size_bytes=0,
        config_sha256="",
        stanza_filename="codex-mcp-stanza.json",
        stanza_size_bytes=100,
        stanza_file_sha256=_ZERO,
        stanza_sha256=_ZERO,
        codex_config_sha256=_ZERO,
        old_vector_present=False,
        old_vector_filename="",
        old_vector_size_bytes=0,
        old_vector_sha256="",
        duration_ms=1.0,
    )


def _verification_report(directory_name: str = "context-core-cutover-20260818T000000Z"):
    return BackupVerificationReport(
        verified=True,
        complete=True,
        manifest_ok=True,
        database_ok=True,
        config_ok=True,
        stanza_ok=True,
        old_vector_ok=True,
        incomplete_marker_present=False,
        files_checked=2,
        legacy_rows=3,
        context_items=0,
        total_size_bytes=8192,
        manifest_sha256=_ZERO,
        directory_name=directory_name,
        reason_codes=(),
        duration_ms=1.0,
    )


def _stage_report(**overrides) -> ContextVectorStageReport:
    fields = dict(
        status="staged",
        document_count=3,
        vector_ready=True,
        fts_only=False,
        dirty_cleared=True,
        reason_codes=(),
        duration_ms=1.0,
    )
    fields.update(overrides)
    return ContextVectorStageReport(**fields)


def _shadow_report(**overrides) -> ShadowGateReport:
    fields = dict(
        comparisons=2,
        exact_total=2,
        exact_top1_matches=2,
        semantic_total=0,
        semantic_overlap_passes=0,
        thresholds_met=True,
        reason_codes=(),
    )
    fields.update(overrides)
    return ShadowGateReport(**fields)


def _primary_report(**overrides) -> PrimaryGateReport:
    fields = dict(
        ready_primary=True,
        quick_check_ok=True,
        mapping_complete=True,
        layers_complete=True,
        migration_idempotent=True,
        projection_lag_zero=True,
        vector_ready=True,
        shadow_thresholds_met=True,
        config_clean=True,
        legacy_rows_unmapped=0,
        duplicate_mapping_targets=0,
        mapped_items_with_wrong_layers=0,
        second_migration_created=0,
        projection_lag=0,
        reason_codes=(),
        config_diagnostics=(),
    )
    fields.update(overrides)
    return PrimaryGateReport(**fields)


def _migration_report(created: int = 3) -> LegacyMigrationReport:
    return LegacyMigrationReport(
        legacy_table_found=True,
        scanned=3,
        created=created,
        already_migrated=0,
        duplicate_active_count=0,
    )


# ---- fake collaborators recording the exact call order ----


class FakeLock:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    @contextmanager
    def exclusive(self, *, timeout_seconds=30.0):
        self.events.append("lock.acquire")
        if self.error is not None:
            self.events.append("lock.release")
            raise self.error
        try:
            yield
        finally:
            self.events.append("lock.release")


class FakeStore:
    def __init__(self, events):
        self.events = events
        self.create_schema_seen = False

    def initialize(self, *, create_schema=True):
        self.events.append(("store.initialize", create_schema))

    @contextmanager
    def transaction(self):
        self.events.append("store.tx.begin")
        try:
            yield self
            self.events.append("store.tx.commit")
        except Exception:
            self.events.append("store.tx.rollback")
            raise

    def create_schema_in_transaction(self):
        self.create_schema_seen = True
        self.events.append("store.schema")

    def close(self):
        self.events.append("store.close")


class FakeMigrator:
    def __init__(self, events, second_created=0, error=None):
        self.events = events
        self.calls = 0
        self.second_created = second_created
        self.error = error

    def migrate(self):
        self.calls += 1
        self.events.append(f"migrate.{self.calls}")
        if self.error is not None:
            raise self.error
        return _migration_report(created=3 if self.calls == 1 else self.second_created)


class FakeEditor:
    """In-memory Codex editor double with the real hash chain semantics."""

    def __init__(self, events, stanza=None):
        self.events = events
        self.stanza = dict(
            stanza
            or {
                "command": "/opt/evolvmem/bin/python3",
                "args": ["-m", "evolvmem.mcp_server"],
                "enabled": True,
                "enabled_tools": [
                    "memory_add",
                    "memory_search",
                    "memory_status",
                    "context_session_start",
                    "context_search",
                    "context_read",
                    "context_status",
                ],
                "env": {
                    "PYTHONPATH": "/opt/evolvmem",
                    "EVOLVMEM_ADAPTER": "claude",
                    "EVOLVMEM_CONTEXT_MODE": "legacy",
                },
            }
        )

    def _snapshot(self) -> CodexMcpSnapshot:
        plain = json.loads(json.dumps(self.stanza))
        return CodexMcpSnapshot(
            stanza=plain,
            stanza_sha256=_stanza_sha256(plain),
            source_file_sha256=_ZERO,
        )

    def snapshot(self):
        self.events.append("editor.snapshot")
        return self._snapshot()

    def apply_primary(self, snapshot):
        self.events.append("editor.apply_primary")
        before = _stanza_sha256(self.stanza)
        if before != snapshot.stanza_sha256:
            raise CodexStanzaDriftError("drift")
        env = dict(self.stanza.get("env") or {})
        env["EVOLVMEM_ADAPTER"] = "codex"
        env["EVOLVMEM_CONTEXT_MODE"] = "primary"
        self.stanza["env"] = env
        self.stanza["default_tools_approval_mode"] = "writes"
        return CodexConfigApplyResult(
            before_stanza_sha256=before,
            after_stanza_sha256=_stanza_sha256(self.stanza),
        )

    def apply_legacy(self, *, expected_stanza_sha256):
        self.events.append("editor.apply_legacy")
        before = _stanza_sha256(self.stanza)
        if before != expected_stanza_sha256:
            raise CodexStanzaDriftError("drift")
        env = dict(self.stanza.get("env") or {})
        env["EVOLVMEM_CONTEXT_MODE"] = "legacy"
        self.stanza["env"] = env
        return CodexConfigApplyResult(
            before_stanza_sha256=before,
            after_stanza_sha256=_stanza_sha256(self.stanza),
        )


def _cli_result_from_stanza(stanza) -> CodexMcpGetResult:
    env = stanza.get("env")
    return CodexMcpGetResult(
        name="evolvmem",
        enabled=True,
        transport=CodexMcpGetTransport(
            type="stdio",
            command=stanza.get("command"),
            args=tuple(stanza.get("args") or ()),
            env=dict(env) if isinstance(env, dict) else None,
            cwd=stanza.get("cwd"),
        ),
        enabled_tools=tuple(stanza["enabled_tools"])
        if stanza.get("enabled_tools")
        else None,
        disabled_tools=None,
        startup_timeout_sec=stanza.get("startup_timeout_sec"),
        tool_timeout_sec=stanza.get("tool_timeout_sec"),
    )


@dataclass
class FakeCollaborators:
    """One injectable double set; every boundary records into ``events``."""

    events: list

    @classmethod
    def create(cls) -> "FakeCollaborators":
        events: list = []
        return cls(events)

    def kwargs(self, **overrides):
        events = self.events
        lock = FakeLock(events)
        store = FakeStore(events)
        migrator = FakeMigrator(events)
        editor = FakeEditor(events)
        report = _preflight_report()
        backup_dir_name = "context-core-cutover-20260818T000000Z"

        def backup_creator(config, *, codex_snapshot, preflight_digest, timestamp):
            events.append("backup.create")
            backup_dir = config.data_dir / "backups" / backup_dir_name
            backup_dir.mkdir(parents=True, exist_ok=True)
            return _backup_manifest(backup_dir_name)

        options = dict(
            lock=lock,
            preflight_runner=lambda config, *, codex_config_path: (
                events.append("preflight"),
                report,
            )[1],
            backup_creator=backup_creator,
            backup_verifier=lambda directory: (
                events.append("backup.verify"),
                _verification_report(backup_dir_name),
            )[1],
            store_factory=lambda: store,
            migrator_factory=lambda store_, config: migrator,
            lag_checker=lambda config, store_: (
                events.append(f"lag.{sum(1 for e in events if str(e).startswith('lag.')) + 1}"),
                _lag_report(),
            )[1],
            vector_stager=lambda config, store_, engine, *, allow_fts_only: (
                events.append("vector.stage"),
                _stage_report(),
            )[1],
            shadow_runner=lambda config, store_, **kw: (
                events.append("shadow"),
                _shadow_report(),
            )[1],
            primary_gate_runner=lambda config, store_, **kw: (
                events.append("primary_gate"),
                _primary_report(),
            )[1],
            compat_persister=lambda config: (
                events.append("persist_compat"),
                "c" * 64,
            )[1],
            codex_editor_factory=lambda path: editor,
            cli_probe=lambda: (
                events.append("cli_probe"),
                _cli_result_from_stanza(editor.stanza),
            )[1],
        )
        options.update(overrides)
        return options


def _request(config: Config, tmp_path: Path, report=None, **overrides) -> CutoverRequest:
    codex_path = tmp_path / "config.toml"
    envelope_path = tmp_path / "preflight.json"
    if not envelope_path.exists():
        write_preflight_envelope(
            envelope_path,
            report or _preflight_report(),
            data_dir=config.data_dir,
            codex_config_path=codex_path,
        )
    fields = dict(
        config=config,
        codex_config_path=codex_path,
        preflight_report_path=envelope_path,
        writers_restarted=True,
        apply=True,
    )
    fields.update(overrides)
    return CutoverRequest(**fields)


def _journal_path(config: Config, directory_name: str) -> Path:
    return config.data_dir / "backups" / directory_name / JOURNAL_FILENAME


# ---- CutoverRequest validation: explicit everything, no guessing ----


def test_request_requires_absolute_explicit_paths(test_config, tmp_path):
    codex_path = _write_codex_config(tmp_path)
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope,
        _preflight_report(),
        data_dir=test_config.data_dir,
        codex_config_path=codex_path,
    )
    with pytest.raises(ContextValidationError):
        CutoverRequest(
            config=test_config,
            codex_config_path=Path("relative-config.toml"),
            preflight_report_path=envelope,
            writers_restarted=True,
            apply=True,
        )
    with pytest.raises(ContextValidationError):
        CutoverRequest(
            config=test_config,
            codex_config_path=codex_path,
            preflight_report_path=Path("relative-preflight.json"),
            writers_restarted=True,
            apply=True,
        )
    with pytest.raises(ContextValidationError):
        CutoverRequest(
            config="not-a-config",
            codex_config_path=codex_path,
            preflight_report_path=envelope,
            writers_restarted=True,
            apply=True,
        )


def test_apply_requires_writers_restarted_acknowledgement(test_config, tmp_path):
    codex_path = _write_codex_config(tmp_path)
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope,
        _preflight_report(),
        data_dir=test_config.data_dir,
        codex_config_path=codex_path,
    )
    with pytest.raises(ContextValidationError):
        CutoverRequest(
            config=test_config,
            codex_config_path=codex_path,
            preflight_report_path=envelope,
            writers_restarted=False,
            apply=True,
        )


def test_preflight_fingerprint_ignores_only_point_in_time_measurements():
    first = _preflight_report()
    second = _preflight_report(duration_ms=99.0, free_space_bytes=(1 << 30) - 4096)
    assert preflight_fingerprint(first) == preflight_fingerprint(second)
    drifted = _preflight_report(legacy_rows=4)
    assert preflight_fingerprint(first) != preflight_fingerprint(drifted)


# ---- dry-run: zero writes of any kind ----


def _tree_snapshot(root: Path) -> dict:
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            snapshot[relative] = ("dir", None)
        else:
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return snapshot


def test_dry_run_performs_no_write(test_config, tmp_path):
    codex_path = _write_codex_config(tmp_path)
    with MemoryStore(test_config) as legacy:
        legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first for the demo service.",
            attribute="decision",
            importance=7.0,
        )
    report = run_preflight(test_config, codex_config_path=codex_path)
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope, report, data_dir=test_config.data_dir, codex_config_path=codex_path
    )
    before = _tree_snapshot(tmp_path)

    outcome = run_cutover(
        CutoverRequest(
            config=test_config,
            codex_config_path=codex_path,
            preflight_report_path=envelope,
            writers_restarted=False,
            apply=False,
        )
    )

    assert outcome.applied is False
    assert outcome.state == "planned"
    assert outcome.steps_completed == ()
    assert outcome.preflight_ready is True
    assert outcome.preflight_digest_match is True
    assert _tree_snapshot(tmp_path) == before  # not even a lock file


def test_dry_run_reports_a_stale_preflight_digest(test_config, tmp_path):
    codex_path = _write_codex_config(tmp_path)
    with MemoryStore(test_config) as legacy:
        legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first for the demo service.",
            attribute="decision",
        )
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope,
        run_preflight(test_config, codex_config_path=codex_path),
        data_dir=test_config.data_dir,
        codex_config_path=codex_path,
    )
    with MemoryStore(test_config) as legacy:
        legacy.add(
            key="project:demo:fact:second",
            value="A second row written after the preflight report.",
            attribute="fact",
        )

    outcome = run_cutover(
        CutoverRequest(
            config=test_config,
            codex_config_path=codex_path,
            preflight_report_path=envelope,
            writers_restarted=False,
            apply=False,
        )
    )
    assert outcome.preflight_digest_match is False
    assert "preflight_digest_mismatch" in outcome.reason_codes


# ---- gate order: the ten steps in exact sequence ----


def test_apply_runs_the_ten_steps_in_order(test_config, tmp_path):
    _write_codex_config(tmp_path)
    fakes = FakeCollaborators.create()
    request = _request(test_config, tmp_path)

    outcome = run_cutover(request, **fakes.kwargs())

    assert outcome.state == "awaiting_post_cutover_canary"
    assert outcome.ready is True
    assert outcome.steps_completed == tuple(CUTOVER_STEPS)
    assert fakes.events == [
        "lock.acquire",
        "preflight",
        "editor.snapshot",
        "backup.create",
        "backup.verify",
        ("store.initialize", False),
        "store.tx.begin",
        "store.schema",
        "migrate.1",
        "store.tx.commit",
        "lag.1",
        "migrate.2",
        "vector.stage",
        "shadow",
        "lag.2",
        "primary_gate",
        "persist_compat",
        "editor.apply_primary",
        "editor.snapshot",
        "cli_probe",
        "lock.release",
        "store.close",
    ]
    # The final journal mark lands after the lock release.
    journal_path = _journal_path(test_config, "context-core-cutover-20260818T000000Z")
    assert CutoverJournal.load(journal_path).state == "awaiting_post_cutover_canary"


def test_stale_preflight_digest_aborts_inside_the_lock(test_config, tmp_path):
    _write_codex_config(tmp_path)
    stale = _preflight_report(legacy_rows=99)
    fakes = FakeCollaborators.create()
    request = _request(test_config, tmp_path, report=stale)

    outcome = run_cutover(request, **fakes.kwargs())

    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["lock"]
    assert "preflight_digest_mismatch" in outcome.reason_codes
    assert fakes.events == ["lock.acquire", "preflight", "lock.release"]


def test_not_ready_preflight_aborts_inside_the_lock(test_config, tmp_path):
    _write_codex_config(tmp_path)
    fakes = FakeCollaborators.create()
    unready = _preflight_report(ready=False, reason_codes=("database_missing",))
    request = _request(test_config, tmp_path, report=unready)
    # The envelope holds the unready digest; the locked rerun reproduces it.
    outcome = run_cutover(
        request,
        **fakes.kwargs(
            preflight_runner=lambda config, *, codex_config_path: unready
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["lock"]
    assert "preflight_not_ready" in outcome.reason_codes


# ---- failure injection at every step boundary ----


def _apply_with_failure(test_config, tmp_path, **overrides):
    _write_codex_config(tmp_path)
    fakes = FakeCollaborators.create()
    request = _request(test_config, tmp_path)
    outcome = run_cutover(request, **fakes.kwargs(**overrides))
    return outcome, fakes.events


def test_lock_contention_fails_before_any_write(test_config, tmp_path):
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        lock=FakeLock(
            events_holder := [], error=CutoverLockTimeout("contended")
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["lock"]
    assert "cutover_lock_timeout" in outcome.reason_codes
    assert events_holder == ["lock.acquire", "lock.release"]


def test_backup_failure_stops_before_migration(test_config, tmp_path):
    def failing_creator(config, *, codex_snapshot, preflight_digest, timestamp):
        raise CutoverBackupError("independent backup verification failed")

    outcome, events = _apply_with_failure(
        test_config, tmp_path, backup_creator=failing_creator
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["backup"]
    assert "backup_failed" in outcome.reason_codes
    assert "migrate.1" not in events
    # No verified backup directory means no journal file exists at all.
    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert journals == []


def test_backup_verification_failure_stops_before_migration(test_config, tmp_path):
    report = replace(
        _verification_report(),
        verified=False,
        database_ok=False,
        reason_codes=("database_sha256_mismatch",),
    )
    outcome, events = _apply_with_failure(
        test_config, tmp_path, backup_verifier=lambda directory: report
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["backup"]
    assert "backup_verification_failed" in outcome.reason_codes
    assert "migrate.1" not in events


def test_migration_failure_rolls_back_the_outer_transaction(test_config, tmp_path):
    migrator_error = RuntimeError("synthetic migration failure")
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        migrator_factory=lambda store, config: FakeMigrator(
            [], error=migrator_error
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["migrate"]
    assert "store.tx.rollback" in events
    assert "store.tx.commit" not in events
    # The journal survives inside the verified backup and names the step.
    journal_path = _journal_path(test_config, "context-core-cutover-20260818T000000Z")
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "failed"
    assert journal.failed_step == _STEP["migrate"]


def test_second_migration_creating_items_blocks_the_cutover(test_config, tmp_path):
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        migrator_factory=lambda store, config: FakeMigrator([], second_created=1),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["validate"]
    assert "migration_not_idempotent" in outcome.reason_codes
    assert "vector.stage" not in events


def test_mapping_or_layer_mismatch_blocks_the_cutover(test_config, tmp_path):
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        lag_checker=lambda config, store: _lag_report(
            projection_lag=1, missing_mapping=1
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["validate"]
    assert "legacy_mapping_incomplete" in outcome.reason_codes
    assert "vector.stage" not in events


def test_vector_stage_failure_stops_without_fts_approval(test_config, tmp_path):
    def failed_stager(config, store, engine, *, allow_fts_only):
        return _stage_report(
            status="failed",
            document_count=0,
            vector_ready=False,
            fts_only=False,
            dirty_cleared=False,
            reason_codes=("engine_unavailable",),
        )

    outcome, events = _apply_with_failure(
        test_config, tmp_path, vector_stager=failed_stager
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["vector"]
    assert "context_vector_unhealthy" in outcome.reason_codes
    assert "shadow" not in events
    assert "persist_compat" not in events


def test_shadow_threshold_miss_blocks_before_any_mode_change(test_config, tmp_path):
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        shadow_runner=lambda config, store, **kw: _shadow_report(
            exact_top1_matches=1,
            thresholds_met=False,
            reason_codes=("shadow_exact_top1_failed",),
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["shadow"]
    assert "shadow_exact_top1_failed" in outcome.reason_codes
    assert "persist_compat" not in events
    assert "editor.apply_primary" not in events


def test_projection_lag_after_shadow_blocks_the_cutover(test_config, tmp_path):
    calls = []

    def lag(config, store):
        calls.append(1)
        if len(calls) == 2:
            return _lag_report(projection_lag=1, l1_mismatch=1)
        return _lag_report()

    outcome, events = _apply_with_failure(test_config, tmp_path, lag_checker=lag)
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["shadow"]
    assert "projection_lag_nonzero" in outcome.reason_codes
    assert "persist_compat" not in events


def test_primary_gate_not_ready_blocks_the_cutover(test_config, tmp_path):
    outcome, events = _apply_with_failure(
        test_config,
        tmp_path,
        primary_gate_runner=lambda config, store, **kw: _primary_report(
            ready_primary=False,
            vector_ready=False,
            reason_codes=("context_vector_unhealthy",),
        ),
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["shadow"]
    assert "primary_gate_not_ready" in outcome.reason_codes
    assert "persist_compat" not in events


def test_compat_persist_failure_leaves_codex_untouched(test_config, tmp_path):
    def failing_persister(config):
        raise CutoverError("persist_compat", "compat_persist_failed")

    outcome, events = _apply_with_failure(
        test_config, tmp_path, compat_persister=failing_persister
    )
    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["persist"]
    assert "compat_persist_failed" in outcome.reason_codes
    assert "editor.apply_primary" not in events
    assert outcome.codex_rollback == ""  # nothing primary was applied


# ---- step 9/10 failure: operational CAS rollback, compat retained ----


def _real_mode_fixture(test_config, tmp_path):
    """Real config/codex files with the migration side faked out."""
    codex_path = _write_codex_config(tmp_path)
    fakes = FakeCollaborators.create()
    return codex_path, fakes


def test_codex_primary_apply_drift_keeps_drifted_stanza_and_compat(
    test_config, tmp_path
):
    codex_path, fakes = _real_mode_fixture(test_config, tmp_path)
    editor = FakeEditor(fakes.events)

    def drifting_stager(config, store, engine, *, allow_fts_only):
        editor.stanza["tool_timeout_sec"] = 999  # target stanza drift
        return _stage_report()

    request = _request(test_config, tmp_path)
    outcome = run_cutover(
        request,
        **fakes.kwargs(
            vector_stager=drifting_stager,
            codex_editor_factory=lambda path: editor,
        ),
    )

    assert outcome.state == "rolled_back"
    assert outcome.failed_step == _STEP["codex"]
    assert "codex_stanza_drift" in outcome.reason_codes
    # The drifted stanza is never clobbered, and no fake legacy CAS ran.
    assert editor.stanza["tool_timeout_sec"] == 999
    assert editor.stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    assert "editor.apply_legacy" not in fakes.events
    assert "codex_primary_not_applied" in outcome.reason_codes


def test_cli_mismatch_after_primary_forces_explicit_legacy(test_config, tmp_path):
    codex_path, fakes = _real_mode_fixture(test_config, tmp_path)
    editor = FakeEditor(fakes.events)

    def bad_probe():
        fakes.events.append("cli_probe")
        result = _cli_result_from_stanza(editor.stanza)
        return replace(result, name="somebody-else")

    request = _request(test_config, tmp_path)
    outcome = run_cutover(
        request,
        **fakes.kwargs(codex_editor_factory=lambda path: editor, cli_probe=bad_probe),
    )

    assert outcome.state == "rolled_back"
    assert outcome.failed_step == _STEP["codex"]
    assert "codex_cli_mismatch" in outcome.reason_codes
    assert outcome.codex_rollback == "cas_legacy"
    # The CAS rollback set the mode explicitly back to legacy.
    assert editor.stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    assert "editor.apply_legacy" in fakes.events
    # The persistent compat mode and migrated data are retained, and the
    # database is never restored or deleted automatically.
    journal_path = _journal_path(test_config, "context-core-cutover-20260818T000000Z")
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "rolled_back"
    assert journal.failed_step == _STEP["codex"]
    assert journal.hashes["codex_rollback_stanza_sha256"] != ""


def test_step_ten_failure_rolls_back_an_applied_primary(test_config, tmp_path):
    codex_path, fakes = _real_mode_fixture(test_config, tmp_path)
    editor = FakeEditor(fakes.events)

    class StepTenJournal:
        """Real journal until the final mark, which fails."""

        def __init__(self, inner):
            self._inner = inner

        def bind(self, directory):
            return self._inner.bind(directory)

        @property
        def path(self):
            return self._inner.path

        @property
        def bound(self):
            return self._inner.bound

        @property
        def operation_id(self):
            return self._inner.operation_id

        def advance(self, state, **kw):
            if state == "awaiting_post_cutover_canary":
                raise OSError("synthetic journal write failure")
            return self._inner.advance(state, **kw)

        def mark_failed(self, **kw):
            return self._inner.mark_failed(**kw)

        def mark_rolled_back(self, **kw):
            return self._inner.mark_rolled_back(**kw)

        def public_dict(self):
            return self._inner.public_dict()

        def digest(self):
            return self._inner.digest()

    def journal_factory(**kw):
        return StepTenJournal(CutoverJournal.begin(**kw))

    request = _request(test_config, tmp_path)
    outcome = run_cutover(
        request,
        **fakes.kwargs(
            codex_editor_factory=lambda path: editor,
            cli_probe=lambda: (
                fakes.events.append("cli_probe"),
                _cli_result_from_stanza(editor.stanza),
            )[1],
            journal_factory=journal_factory,
        ),
    )

    assert outcome.state == "rolled_back"
    assert outcome.failed_step == _STEP["release"]
    assert outcome.codex_rollback == "cas_legacy"
    assert editor.stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"


def test_rollback_failure_is_terminal_failed_with_evidence(test_config, tmp_path):
    codex_path, fakes = _real_mode_fixture(test_config, tmp_path)
    editor = FakeEditor(fakes.events)

    def bad_probe():
        fakes.events.append("cli_probe")
        result = _cli_result_from_stanza(editor.stanza)
        return replace(result, name="somebody-else")

    def failing_apply_legacy(**kw):
        fakes.events.append("editor.apply_legacy")
        raise CodexStanzaDriftError("someone raced the rollback")

    editor.apply_legacy = failing_apply_legacy

    request = _request(test_config, tmp_path)
    outcome = run_cutover(
        request,
        **fakes.kwargs(codex_editor_factory=lambda path: editor, cli_probe=bad_probe),
    )

    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["codex"]
    assert "codex_rollback_failed" in outcome.reason_codes
    assert outcome.codex_rollback == "failed"


# ---- pre-persist failures never touch mode or Codex config on the real path ----


def test_real_path_shadow_miss_changes_neither_mode_nor_config(
    test_config, tmp_path
):
    codex_path = _write_codex_config(tmp_path)
    with MemoryStore(test_config) as legacy:
        legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first for the demo service.",
            attribute="decision",
            importance=7.0,
        )
    report = run_preflight(test_config, codex_config_path=codex_path)
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope, report, data_dir=test_config.data_dir, codex_config_path=codex_path
    )
    codex_before = codex_path.read_bytes()

    outcome = run_cutover(
        CutoverRequest(
            config=test_config,
            codex_config_path=codex_path,
            preflight_report_path=envelope,
            writers_restarted=True,
            apply=True,
            allow_fts_only=True,
        ),
        shadow_runner=lambda config, store, **kw: _shadow_report(
            exact_top1_matches=0,
            thresholds_met=False,
            reason_codes=("shadow_exact_top1_failed",),
        ),
    )

    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["shadow"]
    assert codex_path.read_bytes() == codex_before
    assert not test_config.config_path.exists()  # no mode was ever persisted
    # The migrated Context data stays; nothing is restored or deleted.
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.legacy_memory_row_count() == 1
        assert store.count_by_status().get("active", 0) == 1
    finally:
        store.close()


# ---- journal: owner-only, monotonic, atomic, privacy-guarded ----


def _begun_journal(tmp_path: Path, **kw) -> CutoverJournal:
    journal = CutoverJournal.begin(
        preflight_digest=hashlib.sha256(b"synthetic preflight").hexdigest(),
        private={"data_dir": str(tmp_path), "codex_config_path": str(tmp_path / "c")},
        **kw,
    )
    return journal


def test_journal_file_is_owner_only_and_publicly_safe(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    journal.advance("locked")
    journal.advance("backed_up", hashes={"backup_manifest_digest": _ZERO})

    path = backup_dir / JOURNAL_FILENAME
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    public = journal.public_dict()
    validate_public_summary(public)
    # The private routing block never enters the public projection.
    assert "private" not in json.dumps(public)
    assert str(tmp_path) not in json.dumps(public)


def test_journal_moves_only_forward_through_the_documented_chain(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    assert journal.state == "planned"
    with pytest.raises(CutoverJournalError):
        journal.advance("backed_up")  # skipping "locked" is refused
    journal.advance("locked")
    with pytest.raises(CutoverJournalError):
        journal.advance("locked")  # no sideways or repeated transitions
    with pytest.raises(CutoverJournalError):
        journal.advance("nonsense-state")
    journal.advance("backed_up")
    assert journal.state == "backed_up"


def test_journal_terminal_branches_carry_the_exact_failed_step(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    journal.advance("locked")
    journal.mark_failed(step="snapshot_and_backup", reason_codes=("backup_failed",))
    assert journal.state == "failed"
    assert journal.failed_step == "snapshot_and_backup"
    with pytest.raises(CutoverJournalError):
        journal.advance("backed_up")  # terminal states accept no forward move
    with pytest.raises(CutoverJournalError):
        journal.mark_failed(step="migrate_schema")

    other = _begun_journal(tmp_path)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    other.bind(second_dir)
    for state in ("locked", "backed_up", "migrated"):
        other.advance(state)
    with pytest.raises(CutoverJournalError):
        other.mark_rolled_back()  # nothing past the persist boundary yet


@pytest.mark.parametrize(
    "failed_step",
    ("persist_compat", "codex_primary", "release_and_await_canary"),
)
def test_journal_rolled_back_from_a_failed_step_reloads(tmp_path, failed_step):
    """A failed -> rolled_back terminal branch must survive the load round trip."""
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    journal.advance("locked")
    journal.mark_failed(step=failed_step, reason_codes=("step_failed",))
    journal.mark_rolled_back(reason_codes=("operational_rollback",))
    assert journal.state == "rolled_back"
    assert journal.rolled_back_from == "failed"

    loaded = CutoverJournal.load(backup_dir / JOURNAL_FILENAME)
    assert loaded.state == "rolled_back"
    assert loaded.rolled_back_from == "failed"
    assert loaded.failed_step == failed_step
    assert [entry["state"] for entry in loaded.history] == [
        "planned",
        "locked",
        "failed",
        "rolled_back",
    ]
    assert loaded.reason_codes == ("step_failed", "operational_rollback")


def test_journal_persists_every_step_atomically_and_reloads(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    for state in JOURNAL_STATES[1:5]:
        journal.advance(state, counts={"legacy_rows": 3})
    path = backup_dir / JOURNAL_FILENAME
    leftovers = [p for p in backup_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []  # no temp files survive an atomic replace
    loaded = CutoverJournal.load(path)
    assert loaded.state == JOURNAL_STATES[4]
    assert loaded.operation_id == journal.operation_id
    assert loaded.hashes["preflight_digest"] == journal.hashes["preflight_digest"]
    assert [entry["state"] for entry in loaded.history] == list(JOURNAL_STATES[:5])


def test_journal_load_rejects_tampered_or_foreign_files(tmp_path):
    with pytest.raises(CutoverJournalError):
        CutoverJournal.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "other"}', encoding="utf-8")
    with pytest.raises(CutoverJournalError):
        CutoverJournal.load(bad)


def test_journal_records_gate_hashes_counts_and_reason_codes(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = _begun_journal(tmp_path)
    journal.bind(backup_dir)
    journal.advance(
        "locked",
        hashes={"locked_preflight_digest": _ZERO},
        gates={"preflight": {"status": "passed", "digest": _ZERO}},
        counts={"legacy_rows": 7},
        reason_codes=("approved_fts_only",),
    )
    loaded = CutoverJournal.load(backup_dir / JOURNAL_FILENAME)
    assert loaded.hashes["locked_preflight_digest"] == _ZERO
    assert loaded.gates["preflight"]["status"] == "passed"
    assert loaded.counts["legacy_rows"] == 7
    assert loaded.reason_codes == ("approved_fts_only",)


# ---- real-path happy cutover over a synthetic temporary library ----


def _seed_library(config: Config) -> None:
    with MemoryStore(config) as legacy:
        legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first for the demo service.",
            attribute="decision",
            importance=7.0,
        )
        legacy.add(
            key="project:demo:fact:refund",
            value="退款政策：所有订单支持七天无理由退款。",
            attribute="fact",
        )


def _cli_probe_for(codex_path: Path):
    def probe():
        stanza = CodexConfigEditor(codex_path).snapshot().stanza
        return _cli_result_from_stanza(stanza)

    return probe


def _real_request(test_config, tmp_path, **overrides):
    codex_path = _write_codex_config(tmp_path)
    envelope = tmp_path / "preflight.json"
    write_preflight_envelope(
        envelope,
        run_preflight(test_config, codex_config_path=codex_path),
        data_dir=test_config.data_dir,
        codex_config_path=codex_path,
    )
    fields = dict(
        config=test_config,
        codex_config_path=codex_path,
        preflight_report_path=envelope,
        writers_restarted=True,
        apply=True,
        allow_fts_only=True,
    )
    fields.update(overrides)
    return CutoverRequest(**fields), codex_path


def test_real_cutover_persists_compat_and_switches_codex(test_config, tmp_path):
    _seed_library(test_config)
    request, codex_path = _real_request(test_config, tmp_path)

    outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))

    assert outcome.state == "awaiting_post_cutover_canary"
    assert outcome.ready is True
    assert outcome.steps_completed == tuple(CUTOVER_STEPS)
    assert outcome.fts_only is True  # degraded, never vector-healthy

    persisted = json.loads(test_config.config_path.read_text(encoding="utf-8"))
    assert persisted["context_mode"] == "compat"
    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "primary"
    assert stanza["env"]["EVOLVMEM_ADAPTER"] == "codex"
    assert stanza["default_tools_approval_mode"] == "writes"

    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.legacy_memory_row_count() == 2
        assert store.count_by_status().get("active", 0) == 2
    finally:
        store.close()

    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert len(journals) == 1
    journal = CutoverJournal.load(journals[0])
    assert journal.state == "awaiting_post_cutover_canary"
    assert journal.hashes["codex_primary_stanza_sha256"] != ""
    assert journal.gates["vector"]["status"] == "fts_only"  # degraded on record
    assert stat.S_IMODE(journals[0].stat().st_mode) == 0o600
    validate_public_summary(journal.public_dict())


def test_real_cutover_without_fts_approval_stops_at_the_vector_gate(
    test_config, tmp_path
):
    _seed_library(test_config)
    request, codex_path = _real_request(test_config, tmp_path, allow_fts_only=False)
    codex_before = codex_path.read_bytes()

    outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))

    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["vector"]
    assert "context_vector_unhealthy" in outcome.reason_codes
    assert codex_path.read_bytes() == codex_before
    assert not test_config.config_path.exists()
    # The unexplained dirty marker is preserved as the durable retry signal.
    marker = test_config.context_vector_path.with_suffix(
        f"{test_config.context_vector_path.suffix}.dirty"
    )
    assert marker.exists()


def test_real_cutover_blocks_on_projection_lag_after_shadow(test_config, tmp_path):
    from evolvmem.cutover_checks import check_projection_lag

    _seed_library(test_config)
    request, codex_path = _real_request(test_config, tmp_path)
    codex_before = codex_path.read_bytes()
    calls = []

    def sabotaged_lag(config, store):
        calls.append(1)
        if len(calls) == 2:
            # A legacy row that appeared without a mapping after migration.
            with store.transaction():
                store.legacy_projection().insert(
                    LegacyProjectionInsert(
                        key="project:demo:fact:unmapped",
                        value="An unmapped row inserted after migration ran.",
                        attribute="fact",
                    )
                )
        return check_projection_lag(config, store)

    outcome = run_cutover(
        request, cli_probe=_cli_probe_for(codex_path), lag_checker=sabotaged_lag
    )

    assert outcome.state == "failed"
    assert outcome.failed_step == _STEP["shadow"]
    assert "projection_lag_nonzero" in outcome.reason_codes
    assert codex_path.read_bytes() == codex_before
    assert not test_config.config_path.exists()
    # The database is left exactly as the sabotage plus migration made it.
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.legacy_memory_row_count() == 3
    finally:
        store.close()


# ---- mode round-trip: legacy -> shadow -> primary -> legacy keeps writes ----


def test_mode_round_trip_preserves_a_primary_write_through_projection(
    test_config, tmp_path
):
    _seed_library(test_config)

    # legacy mode: reads come from the legacy backend only.
    legacy_service = ContextService(test_config)
    legacy_service.initialize(mode=ContextMode.LEGACY, adapter="codex")
    try:
        result = legacy_service.legacy_add(
            LegacyAddRequest(
                key="project:demo:fact:legacy-only",
                value="Written while the mode was still legacy.",
                attribute="fact",
            )
        )
        assert result.context_id is None
    finally:
        legacy_service.close()

    request, codex_path = _real_request(test_config, tmp_path)
    outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))
    assert outcome.state == "awaiting_post_cutover_canary"

    # shadow mode: explicit Context reads serve the migrated core.
    shadow = ContextService(test_config)
    shadow.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        from evolvmem.context_models import ContextSearchRequest

        hits = shadow.search(
            ContextSearchRequest(query="SQLite", top_k=5, cross_project=True)
        )
        assert any(hit.identity_key == "project:demo:decision:database" for hit in hits)
    finally:
        shadow.close()

    # primary mode: a new write lands on both sides through the projection.
    primary = ContextService(test_config)
    primary.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    try:
        written = primary.legacy_add(
            LegacyAddRequest(
                key="project:demo:decision:written-in-primary",
                value="This decision was written while Codex was primary.",
                attribute="decision",
                importance=8.0,
            )
        )
        assert written.context_id is not None
    finally:
        primary.close()

    # operational rollback: Codex returns to explicit legacy by journal CAS,
    # and the operator returns the persisted mode to legacy.
    from evolvmem.cutover_cli import rollback_journal_file

    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert len(journals) == 1
    rollback_outcome = rollback_journal_file(journals[0], apply=True)
    assert rollback_outcome["state"] == "rolled_back"
    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"

    persisted = Config(data_dir=test_config.data_dir)
    reloaded = load_persisted_config(persisted)
    reloaded.context_mode = "legacy"
    reloaded.save()

    # legacy mode again: the primary-era write is readable through the
    # legacy projection — nothing written during primary is lost.
    backend = MemoryStore(test_config)
    backend.initialize()
    try:
        row = backend.get_by_id(written.legacy_id)
        assert row is not None
        assert row["value"] == "This decision was written while Codex was primary."
        hits = backend.search_fts("written while Codex was primary", top_k=5)
        assert any(int(hit["id"]) == written.legacy_id for hit in hits)
    finally:
        backend.close()

    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.resolve_legacy_mapping(written.legacy_id) == written.context_id
    finally:
        store.close()


# ---- concurrency: the cutover waits; writes wait/time out cleanly ----

_HOLD_SCRIPT = """
import sys, time
from pathlib import Path
from evolvmem.config import Config
from evolvmem.cutover_lock import CutoverLock

data_dir, mode, ready_path, release_path = sys.argv[1:5]
lock = CutoverLock(Config(data_dir=Path(data_dir)))
manager = lock.shared() if mode == "shared" else lock.exclusive()
with manager:
    Path(ready_path).write_text("held", encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while not Path(release_path).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
"""


def _subprocess_env():
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn_holder(temp_dir: Path, mode: str):
    ready = temp_dir / f"{mode}.ready"
    release = temp_dir / f"{mode}.release"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD_SCRIPT,
            str(temp_dir),
            mode,
            str(ready),
            str(release),
        ],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 15.0
    while not ready.exists():
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            raise AssertionError(
                f"lock holder exited early: {stderr.decode(errors='replace')}"
            )
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            raise AssertionError("lock holder never acquired the lock")
        time.sleep(0.02)
    return proc, release


def _stop_holder(proc, release: Path) -> None:
    release.write_text("go", encoding="utf-8")
    proc.communicate(timeout=15)


def test_cutover_waits_for_shared_writers_then_times_out_cleanly(
    test_config, tmp_path
):
    _seed_library(test_config)
    request, codex_path = _real_request(
        test_config, tmp_path, lock_timeout_seconds=0.4
    )
    proc, release = _spawn_holder(test_config.data_dir, "shared")
    try:
        started = time.monotonic()
        outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))
        waited = time.monotonic() - started
        assert outcome.state == "failed"
        assert outcome.failed_step == _STEP["lock"]
        assert "cutover_lock_timeout" in outcome.reason_codes
        assert 0.3 <= waited < 15.0  # bounded wait, never an infinite block
    finally:
        _stop_holder(proc, release)
    assert proc.returncode == 0

    # Once the writers drain, the same cutover proceeds.
    outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))
    assert outcome.state == "awaiting_post_cutover_canary"


def test_new_writes_wait_bounded_while_the_cutover_holds_the_exclusive_lock(
    test_config, tmp_path
):
    _seed_library(test_config)
    store = ContextStore(test_config)
    store.initialize()
    store.close()
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")

    class ShortTimeoutLock:
        def __init__(self, inner, timeout):
            self._inner = inner
            self._timeout = timeout

        def shared(self, **kw):
            return self._inner.shared(timeout_seconds=self._timeout)

        def exclusive(self, **kw):
            return self._inner.exclusive(timeout_seconds=self._timeout)

    service._cutover_lock = ShortTimeoutLock(CutoverLock(test_config), 0.4)
    proc, release = _spawn_holder(test_config.data_dir, "exclusive")
    try:
        started = time.monotonic()
        with pytest.raises(CutoverLockTimeout):
            service.legacy_add(
                LegacyAddRequest(
                    key="project:demo:fact:blocked-write",
                    value="A write attempted while the cutover lock is held.",
                    attribute="fact",
                )
            )
        waited = time.monotonic() - started
        assert 0.3 <= waited < 15.0  # the write waited, then timed out cleanly
    finally:
        _stop_holder(proc, release)
        service.close()
    assert proc.returncode == 0

    # After the cutover releases, the same write succeeds.
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        result = service.legacy_add(
            LegacyAddRequest(
                key="project:demo:fact:blocked-write",
                value="A write attempted while the cutover lock is held.",
                attribute="fact",
            )
        )
        assert result.changed is True
        assert result.context_id is not None
    finally:
        service.close()
