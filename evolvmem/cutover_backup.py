"""Consistent, owner-only, independently verified Context cutover backups.

A cutover backup snapshots the live database through the SQLite Backup API —
never a raw file copy, which would silently lose committed rows still inside
an uncheckpointed WAL — plus the pre-cutover config, the structured Codex
stanza snapshot, and the legacy vector file when they exist. Every artifact
lives in a unique ``backups/context-core-cutover-<UTC>/`` directory with
owner-only permissions (directory 0700, files 0600); an existing directory
is refused and no earlier backup is ever deleted automatically. The
canonical manifest is written last, then every entry is independently
reopened, checked, and re-hashed; any partial failure leaves an owner-only
``INCOMPLETE`` marker and no ``complete=true`` manifest. Public projections
stay inside the privacy-guarded summary vocabulary: stanza values and other
secrets never leave the owner-only snapshot file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import ClassVar

from evolvmem.codex_config import (
    CodexConfigEditor,
    CodexConfigError,
    CodexMcpSnapshot,
)
from evolvmem.config import Config
from evolvmem.context_models import ContextValidationError
from evolvmem.cutover_models import validate_public_summary

__all__ = [
    "BackupManifest",
    "BackupVerificationReport",
    "CutoverBackupError",
    "create_cutover_backup",
    "verify_cutover_backup",
]

_BACKUP_PARENT_NAME = "backups"
_BACKUP_DIR_PREFIX = "context-core-cutover-"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_CREATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STANZA_FILENAME = "codex-mcp-stanza.json"
_MANIFEST_FILENAME = "manifest.json"
_INCOMPLETE_FILENAME = "INCOMPLETE"
_MANIFEST_SCHEMA = "evolvmem.cutover_backup_manifest"
_MANIFEST_VERSION = 1

# The six base tables constituting the Context schema; mirrors
# cutover_checks._CONTEXT_TABLES so the backup summary speaks the same
# vocabulary as the preflight report.
_CONTEXT_TABLES = (
    "context_items",
    "context_layers",
    "session_archives",
    "context_sources",
    "context_evidence",
    "legacy_memory_migrations",
)

_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_CHECKSUM_PATTERN = re.compile(r"^$|^[0-9a-f]{64}$")


class CutoverBackupError(Exception):
    """Backup creation failed; messages never contain secrets or paths."""


# ---- report models (privacy-guarded like cutover_models) ----


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise ContextValidationError(f"{field_name} must be a boolean")


def _require_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ContextValidationError(f"{field_name} must be a non-negative integer")


def _require_sha256(value: object, field_name: str) -> None:
    if not _is_sha256(value):
        raise ContextValidationError(f"{field_name} must be a lowercase SHA-256 hex")


def _require_checksum_or_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _CHECKSUM_PATTERN.match(value):
        raise ContextValidationError(
            f"{field_name} must be a lowercase SHA-256 hex or empty"
        )


def _require_safe_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ContextValidationError(f"{field_name} must be a non-empty string")
    if any(char in value for char in "/\\\n\r"):
        raise ContextValidationError(f"{field_name} must be a single safe string")


def _require_optional_name(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ContextValidationError(f"{field_name} must be a filename string")
    if value:
        _require_safe_string(value, field_name)


def _require_status_counts(value: object) -> dict[str, int]:
    try:
        counts = dict(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ContextValidationError(
            "legacy_status_counts must map status names to non-negative integers"
        ) from exc
    for status, count in counts.items():
        if not isinstance(status, str) or type(count) is not int or count < 0:
            raise ContextValidationError(
                "legacy_status_counts must map status names to non-negative integers"
            )
    return counts


def _require_reason_codes(value: object) -> tuple[str, ...]:
    try:
        codes = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            "reason_codes must be an iterable of reason codes"
        ) from exc
    for code in codes:
        if not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code):
            raise ContextValidationError(
                "reason_codes must contain only lower-snake reason codes"
            )
    return codes


def _require_duration(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ContextValidationError("duration_ms must be a non-negative finite number")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _canonical_digest(public: dict) -> str:
    payload = json.dumps(public, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """One complete backup's description; every field is public-safe.

    ``manifest_dict()`` is the canonical on-disk payload (relative filenames,
    sizes, SHA-256 hashes, and schema/version/count summaries);
    ``public_dict()`` is the flat privacy-guarded projection for callers and
    the cutover journal. Absent optional artifacts (config, old vector) are
    reported with ``present=False``, zero sizes, and empty hashes — never
    with invented empty files.
    """

    SCHEMA: ClassVar[str] = "evolvmem.cutover_backup"
    VERSION: ClassVar[int] = 1

    complete: bool
    directory_name: str
    created_utc: str
    preflight_digest: str
    database_filename: str
    database_size_bytes: int
    database_sha256: str
    quick_check_ok: bool
    sqlite_user_version: int
    legacy_schema_present: bool
    legacy_rows: int
    legacy_status_counts: dict[str, int]
    context_schema_present: bool
    context_items: int
    config_present: bool
    config_filename: str
    config_size_bytes: int
    config_sha256: str
    stanza_filename: str
    stanza_size_bytes: int
    stanza_file_sha256: str
    stanza_sha256: str
    codex_config_sha256: str
    old_vector_present: bool
    old_vector_filename: str
    old_vector_size_bytes: int
    old_vector_sha256: str
    duration_ms: float

    def __post_init__(self) -> None:
        for name in (
            "complete",
            "quick_check_ok",
            "legacy_schema_present",
            "context_schema_present",
            "config_present",
            "old_vector_present",
        ):
            _require_bool(getattr(self, name), name)
        for name in (
            "database_size_bytes",
            "sqlite_user_version",
            "legacy_rows",
            "context_items",
            "config_size_bytes",
            "stanza_size_bytes",
            "old_vector_size_bytes",
        ):
            _require_non_negative_int(getattr(self, name), name)
        _require_sha256(self.preflight_digest, "preflight_digest")
        _require_sha256(self.database_sha256, "database_sha256")
        _require_sha256(self.stanza_file_sha256, "stanza_file_sha256")
        _require_sha256(self.stanza_sha256, "stanza_sha256")
        _require_sha256(self.codex_config_sha256, "codex_config_sha256")
        _require_checksum_or_empty(self.config_sha256, "config_sha256")
        _require_checksum_or_empty(self.old_vector_sha256, "old_vector_sha256")
        for name in ("directory_name", "created_utc", "database_filename", "stanza_filename"):
            _require_safe_string(getattr(self, name), name)
        _require_optional_name(self.config_filename, "config_filename")
        _require_optional_name(self.old_vector_filename, "old_vector_filename")
        object.__setattr__(
            self, "legacy_status_counts", _require_status_counts(self.legacy_status_counts)
        )
        _require_duration(self.duration_ms)
        object.__setattr__(self, "duration_ms", float(self.duration_ms))
        if not self.config_present and (
            self.config_filename or self.config_size_bytes or self.config_sha256
        ):
            raise ContextValidationError(
                "an absent config must not record a name, size, or hash"
            )
        if not self.old_vector_present and (
            self.old_vector_filename
            or self.old_vector_size_bytes
            or self.old_vector_sha256
        ):
            raise ContextValidationError(
                "an absent old vector must not record a name, size, or hash"
            )

    def manifest_dict(self) -> dict:
        """The canonical on-disk payload; optional artifacts are null."""
        config_entry = None
        if self.config_present:
            config_entry = {
                "filename": self.config_filename,
                "size_bytes": self.config_size_bytes,
                "sha256": self.config_sha256,
            }
        vector_entry = None
        if self.old_vector_present:
            vector_entry = {
                "filename": self.old_vector_filename,
                "size_bytes": self.old_vector_size_bytes,
                "sha256": self.old_vector_sha256,
            }
        return {
            "schema": _MANIFEST_SCHEMA,
            "version": _MANIFEST_VERSION,
            "complete": self.complete,
            "created_utc": self.created_utc,
            "preflight_digest": self.preflight_digest,
            "database": {
                "filename": self.database_filename,
                "size_bytes": self.database_size_bytes,
                "sha256": self.database_sha256,
                "quick_check_ok": self.quick_check_ok,
                "sqlite_user_version": self.sqlite_user_version,
                "legacy_schema_present": self.legacy_schema_present,
                "legacy_rows": self.legacy_rows,
                "legacy_status_counts": dict(self.legacy_status_counts),
                "context_schema_present": self.context_schema_present,
                "context_items": self.context_items,
            },
            "config": config_entry,
            "codex_stanza": {
                "filename": self.stanza_filename,
                "size_bytes": self.stanza_size_bytes,
                "sha256": self.stanza_file_sha256,
                "stanza_sha256": self.stanza_sha256,
                "source_file_sha256": self.codex_config_sha256,
            },
            "old_vector": vector_entry,
        }

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "complete": self.complete,
            "directory_name": self.directory_name,
            "created_utc": self.created_utc,
            "preflight_digest": self.preflight_digest,
            "database_filename": self.database_filename,
            "database_size_bytes": self.database_size_bytes,
            "database_sha256": self.database_sha256,
            "quick_check_ok": self.quick_check_ok,
            "sqlite_user_version": self.sqlite_user_version,
            "legacy_schema_present": self.legacy_schema_present,
            "legacy_rows": self.legacy_rows,
            "legacy_status_counts": dict(self.legacy_status_counts),
            "context_schema_present": self.context_schema_present,
            "context_items": self.context_items,
            "config_present": self.config_present,
            "config_filename": self.config_filename,
            "config_size_bytes": self.config_size_bytes,
            "config_sha256": self.config_sha256,
            "stanza_filename": self.stanza_filename,
            "stanza_size_bytes": self.stanza_size_bytes,
            "stanza_file_sha256": self.stanza_file_sha256,
            "stanza_sha256": self.stanza_sha256,
            "codex_config_sha256": self.codex_config_sha256,
            "old_vector_present": self.old_vector_present,
            "old_vector_filename": self.old_vector_filename,
            "old_vector_size_bytes": self.old_vector_size_bytes,
            "old_vector_sha256": self.old_vector_sha256,
        }
        validate_public_summary(public)
        return public

    def digest(self) -> str:
        """SHA-256 over the canonical JSON of ``public_dict()``."""
        return _canonical_digest(self.public_dict())


@dataclass(frozen=True, slots=True)
class BackupVerificationReport:
    """Independent re-check outcome; booleans are measured, never supplied.

    ``verified`` additionally requires a complete manifest and no leftover
    ``INCOMPLETE`` marker, so a partial or crashed attempt can never pass.
    """

    SCHEMA: ClassVar[str] = "evolvmem.cutover_backup_verification"
    VERSION: ClassVar[int] = 1

    verified: bool
    complete: bool
    manifest_ok: bool
    database_ok: bool
    config_ok: bool
    stanza_ok: bool
    old_vector_ok: bool
    incomplete_marker_present: bool
    files_checked: int
    legacy_rows: int
    context_items: int
    total_size_bytes: int
    manifest_sha256: str
    directory_name: str
    reason_codes: tuple[str, ...]
    duration_ms: float

    def __post_init__(self) -> None:
        for name in (
            "verified",
            "complete",
            "manifest_ok",
            "database_ok",
            "config_ok",
            "stanza_ok",
            "old_vector_ok",
            "incomplete_marker_present",
        ):
            _require_bool(getattr(self, name), name)
        for name in ("files_checked", "legacy_rows", "context_items", "total_size_bytes"):
            _require_non_negative_int(getattr(self, name), name)
        _require_checksum_or_empty(self.manifest_sha256, "manifest_sha256")
        _require_safe_string(self.directory_name, "directory_name")
        object.__setattr__(self, "reason_codes", _require_reason_codes(self.reason_codes))
        _require_duration(self.duration_ms)
        object.__setattr__(self, "duration_ms", float(self.duration_ms))
        if self.verified and not (
            self.complete
            and self.manifest_ok
            and self.database_ok
            and self.config_ok
            and self.stanza_ok
            and self.old_vector_ok
            and not self.incomplete_marker_present
        ):
            raise ContextValidationError(
                "verified requires every component check and no INCOMPLETE marker"
            )

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "verified": self.verified,
            "complete": self.complete,
            "manifest_ok": self.manifest_ok,
            "database_ok": self.database_ok,
            "config_ok": self.config_ok,
            "stanza_ok": self.stanza_ok,
            "old_vector_ok": self.old_vector_ok,
            "incomplete_marker_present": self.incomplete_marker_present,
            "files_checked": self.files_checked,
            "legacy_rows": self.legacy_rows,
            "context_items": self.context_items,
            "total_size_bytes": self.total_size_bytes,
            "manifest_sha256": self.manifest_sha256,
            "directory_name": self.directory_name,
            "reason_codes": list(self.reason_codes),
        }
        validate_public_summary(public)
        return public

    def digest(self) -> str:
        """SHA-256 over the canonical JSON of ``public_dict()``."""
        return _canonical_digest(self.public_dict())


# ---- backup creation ----


def create_cutover_backup(
    config: Config,
    *,
    codex_snapshot: CodexMcpSnapshot,
    preflight_digest: str,
    timestamp: datetime,
) -> BackupManifest:
    """Create a consistent, owner-only backup and verify it independently.

    The source database is opened read-only where possible and snapshotted
    through the SQLite Backup API; both sides are closed and every artifact
    fsynced before the canonical manifest is written last. The just-written
    entries are then independently reopened, checked, and re-hashed. Any
    failure leaves the owner-only ``INCOMPLETE`` marker and no
    ``complete=true`` manifest; an existing backup directory is refused, and
    no earlier backup is ever deleted.
    """
    started = time.monotonic()
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if not isinstance(codex_snapshot, CodexMcpSnapshot):
        raise ContextValidationError(
            "codex_snapshot must be a CodexMcpSnapshot instance"
        )
    _require_sha256(preflight_digest, "preflight_digest")
    created = _require_utc_timestamp(timestamp)

    db_path = config.db_path
    if not db_path.is_file():
        raise CutoverBackupError("database file does not exist")

    backup_parent = config.data_dir / _BACKUP_PARENT_NAME
    directory_name = _BACKUP_DIR_PREFIX + created.strftime(_TIMESTAMP_FORMAT)
    backup_dir = backup_parent / directory_name
    try:
        backup_parent.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_parent, 0o700)
        os.mkdir(backup_dir, 0o700)
        os.chmod(backup_dir, 0o700)
    except FileExistsError:
        raise CutoverBackupError(
            "backup directory already exists; refusing to overwrite"
        ) from None
    except OSError as exc:
        raise CutoverBackupError("could not create the backup directory") from exc
    _fsync_directory(backup_parent)

    marker = backup_dir / _INCOMPLETE_FILENAME
    manifest_path = backup_dir / _MANIFEST_FILENAME
    try:
        _write_private_file(marker, b"incomplete\n")
        _backup_database(db_path, backup_dir / db_path.name)
        config_filename = ""
        if config.config_path.is_file():
            config_filename = config.config_path.name
            _copy_private_file(config.config_path, backup_dir / config_filename)
        # save_snapshot re-verifies the stanza hash and serializes 0600; the
        # editor path argument is unused by it, only the target path matters.
        CodexConfigEditor(backup_dir / _STANZA_FILENAME).save_snapshot(
            codex_snapshot, backup_dir / _STANZA_FILENAME
        )
        old_vector_filename = ""
        if config.vector_path.is_file():
            old_vector_filename = config.vector_path.name
            _copy_private_file(config.vector_path, backup_dir / old_vector_filename)
        manifest = _build_manifest(
            backup_dir,
            created_utc=created.strftime(_CREATED_FORMAT),
            directory_name=directory_name,
            preflight_digest=preflight_digest,
            database_filename=db_path.name,
            config_filename=config_filename,
            old_vector_filename=old_vector_filename,
            codex_snapshot=codex_snapshot,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        # The canonical manifest is the last write of a complete backup.
        _write_private_file(manifest_path, _canonical_json(manifest.manifest_dict()))
        _fsync_directory(backup_dir)
    except CutoverBackupError:
        raise
    except (OSError, sqlite3.Error, CodexConfigError, ContextValidationError) as exc:
        raise CutoverBackupError("backup creation failed") from exc

    # Independently reopen, check, and re-hash every entry. The INCOMPLETE
    # marker is still expected here and is removed only after this passes.
    report = verify_cutover_backup(backup_dir)
    entries_ok = (
        report.complete
        and report.manifest_ok
        and report.database_ok
        and report.config_ok
        and report.stanza_ok
        and report.old_vector_ok
    )
    if not entries_ok:
        # Unwind only this attempt's manifest so a partial backup never
        # leaves a complete=true manifest behind; the marker stays.
        try:
            manifest_path.unlink()
            _fsync_directory(backup_dir)
        except OSError as exc:
            raise CutoverBackupError("backup verification failed") from exc
        raise CutoverBackupError("independent backup verification failed")
    try:
        marker.unlink()
        _fsync_directory(backup_dir)
    except OSError as exc:
        raise CutoverBackupError("could not finalize the backup") from exc
    return manifest


def _require_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ContextValidationError("timestamp must be a datetime")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ContextValidationError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _backup_database(source_path: Path, dest_path: Path) -> None:
    """Snapshot through the Backup API; the source is read-only where possible.

    A raw file copy is never acceptable: committed rows may live only in an
    uncheckpointed WAL. The destination is created owner-only before SQLite
    opens it so the snapshot is never group/world-readable, then fsynced.
    """
    fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    source = _open_source(source_path)
    try:
        destination = sqlite3.connect(str(dest_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.chmod(dest_path, 0o600)
    _fsync_file(dest_path)


def _open_source(db_path: Path) -> sqlite3.Connection:
    """Read-only source: immutable without a live WAL, shared read-only with one.

    ``immutable=1`` is only safe when no live ``-wal`` exists; with one,
    committed rows exist only in the WAL and immutable access would silently
    skip them. When the read-only open is not possible, fall back to a normal
    connection — the Backup API still performs only reads against it.
    """
    wal_path = db_path.with_name(db_path.name + "-wal")
    live_wal = wal_path.is_file() and wal_path.stat().st_size > 0
    uri = db_path.expanduser().resolve().as_uri() + "?mode=ro"
    if not live_wal:
        uri += "&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error):
        return sqlite3.connect(str(db_path))


def _build_manifest(
    backup_dir: Path,
    *,
    created_utc: str,
    directory_name: str,
    preflight_digest: str,
    database_filename: str,
    config_filename: str,
    old_vector_filename: str,
    codex_snapshot: CodexMcpSnapshot,
    duration_ms: float,
) -> BackupManifest:
    """Assemble the manifest from the just-written entries (first pass)."""
    database_path = backup_dir / database_filename
    database_size, database_sha256 = _hash_file(database_path)
    summary = _inspect_backup_database(database_path)

    config_size = 0
    config_sha256 = ""
    if config_filename:
        config_size, config_sha256 = _hash_file(backup_dir / config_filename)

    stanza_size, stanza_file_sha256 = _hash_file(backup_dir / _STANZA_FILENAME)

    old_vector_size = 0
    old_vector_sha256 = ""
    if old_vector_filename:
        old_vector_size, old_vector_sha256 = _hash_file(
            backup_dir / old_vector_filename
        )

    return BackupManifest(
        complete=True,
        directory_name=directory_name,
        created_utc=created_utc,
        preflight_digest=preflight_digest,
        database_filename=database_filename,
        database_size_bytes=database_size,
        database_sha256=database_sha256,
        quick_check_ok=summary["quick_check_ok"],
        sqlite_user_version=summary["sqlite_user_version"],
        legacy_schema_present=summary["legacy_schema_present"],
        legacy_rows=summary["legacy_rows"],
        legacy_status_counts=summary["legacy_status_counts"],
        context_schema_present=summary["context_schema_present"],
        context_items=summary["context_items"],
        config_present=bool(config_filename),
        config_filename=config_filename,
        config_size_bytes=config_size,
        config_sha256=config_sha256,
        stanza_filename=_STANZA_FILENAME,
        stanza_size_bytes=stanza_size,
        stanza_file_sha256=stanza_file_sha256,
        stanza_sha256=codex_snapshot.stanza_sha256,
        codex_config_sha256=codex_snapshot.source_file_sha256,
        old_vector_present=bool(old_vector_filename),
        old_vector_filename=old_vector_filename,
        old_vector_size_bytes=old_vector_size,
        old_vector_sha256=old_vector_sha256,
        duration_ms=duration_ms,
    )


def _inspect_backup_database(db_path: Path) -> dict:
    """Reopen the fresh snapshot read-only: quick_check plus schema/counts."""
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        quick_ok = bool(quick_rows) and all(
            str(row[0]).lower() == "ok" for row in quick_rows
        )
        if not quick_ok:
            raise CutoverBackupError("backup snapshot failed quick_check")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        legacy_present = "memories" in tables
        legacy_rows = 0
        status_counts: dict[str, int] = {}
        if legacy_present:
            legacy_rows = int(
                conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )
            status_counts = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM memories GROUP BY status"
                )
            }
        present_context = sum(1 for name in _CONTEXT_TABLES if name in tables)
        context_items = 0
        if "context_items" in tables:
            context_items = int(
                conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0]
            )
        return {
            "quick_check_ok": quick_ok,
            "sqlite_user_version": user_version,
            "legacy_schema_present": legacy_present,
            "legacy_rows": legacy_rows,
            "legacy_status_counts": status_counts,
            "context_schema_present": present_context == len(_CONTEXT_TABLES),
            "context_items": context_items,
        }
    finally:
        conn.close()


# ---- independent verification ----


def verify_cutover_backup(directory: Path) -> BackupVerificationReport:
    """Independently reopen, check, and re-hash every manifest entry.

    A missing or corrupt backup is reported, never raised: ``verified``
    requires a parseable ``complete=true`` manifest, every listed entry
    matching size and SHA-256, the database snapshot reopening with
    quick_check ok and matching version/schema/counts, the stanza snapshot
    reloading through the canonical hash check, and no ``INCOMPLETE``
    marker.
    """
    started = time.monotonic()
    if not isinstance(directory, Path):
        raise ContextValidationError("directory must be a Path")

    reason_codes: list[str] = []
    manifest_sha256 = ""
    complete = False
    manifest_ok = False
    database_ok = False
    config_ok = False
    stanza_ok = False
    old_vector_ok = False
    incomplete_marker_present = False
    files_checked = 0
    legacy_rows = 0
    context_items = 0
    total_size_bytes = 0

    if not directory.is_dir():
        _add_reason(reason_codes, "backup_directory_missing")
    else:
        incomplete_marker_present = (directory / _INCOMPLETE_FILENAME).is_file()
        manifest_result = _read_manifest(directory / _MANIFEST_FILENAME, reason_codes)
        if manifest_result is not None:
            payload, manifest_sha256 = manifest_result
            manifest_ok = True
            complete = payload["complete"]
            if not complete:
                _add_reason(reason_codes, "manifest_incomplete")

            database_ok, size, counts = _verify_database_entry(
                directory, payload["database"], reason_codes
            )
            files_checked += 1
            total_size_bytes += size
            legacy_rows = counts["legacy_rows"]
            context_items = counts["context_items"]

            config_entry = payload["config"]
            if config_entry is None:
                config_ok = True
            else:
                config_ok, size = _verify_file_entry(
                    directory, config_entry, "config", reason_codes
                )
                files_checked += 1
                total_size_bytes += size

            stanza_ok, size = _verify_stanza_entry(
                directory, payload["codex_stanza"], reason_codes
            )
            files_checked += 1
            total_size_bytes += size

            vector_entry = payload["old_vector"]
            if vector_entry is None:
                old_vector_ok = True
            else:
                old_vector_ok, size = _verify_file_entry(
                    directory, vector_entry, "old_vector", reason_codes
                )
                files_checked += 1
                total_size_bytes += size

    if incomplete_marker_present:
        _add_reason(reason_codes, "incomplete_marker_present")

    verified = (
        complete
        and manifest_ok
        and database_ok
        and config_ok
        and stanza_ok
        and old_vector_ok
        and not incomplete_marker_present
    )
    return BackupVerificationReport(
        verified=verified,
        complete=complete,
        manifest_ok=manifest_ok,
        database_ok=database_ok,
        config_ok=config_ok,
        stanza_ok=stanza_ok,
        old_vector_ok=old_vector_ok,
        incomplete_marker_present=incomplete_marker_present,
        files_checked=files_checked,
        legacy_rows=legacy_rows,
        context_items=context_items,
        total_size_bytes=total_size_bytes,
        manifest_sha256=manifest_sha256,
        directory_name=directory.name,
        reason_codes=tuple(reason_codes),
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


def _read_manifest(
    manifest_path: Path, reason_codes: list[str]
) -> tuple[dict, str] | None:
    """Parse and shape-check the manifest; returns (payload, file SHA-256)."""
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        _add_reason(reason_codes, "manifest_missing")
        return None
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _add_reason(reason_codes, "manifest_malformed")
        return None
    if not _manifest_shape_ok(payload):
        _add_reason(reason_codes, "manifest_malformed")
        return None
    return payload, manifest_sha256


def _manifest_shape_ok(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("schema") != _MANIFEST_SCHEMA
        or payload.get("version") != _MANIFEST_VERSION
    ):
        return False
    if type(payload.get("complete")) is not bool:
        return False
    if not isinstance(payload.get("created_utc"), str):
        return False
    if not _is_sha256(payload.get("preflight_digest")):
        return False
    database = payload.get("database")
    if not isinstance(database, dict) or not _entry_shape_ok(database):
        return False
    if type(database.get("quick_check_ok")) is not bool:
        return False
    if not _is_non_negative_int(database.get("sqlite_user_version")):
        return False
    if type(database.get("legacy_schema_present")) is not bool:
        return False
    if not _is_non_negative_int(database.get("legacy_rows")):
        return False
    status_counts = database.get("legacy_status_counts")
    if not isinstance(status_counts, dict) or any(
        not isinstance(status, str) or not _is_non_negative_int(count)
        for status, count in status_counts.items()
    ):
        return False
    if type(database.get("context_schema_present")) is not bool:
        return False
    if not _is_non_negative_int(database.get("context_items")):
        return False
    config_entry = payload.get("config")
    if config_entry is not None and not _entry_shape_ok(config_entry):
        return False
    stanza = payload.get("codex_stanza")
    if not isinstance(stanza, dict) or not _entry_shape_ok(stanza):
        return False
    if not _is_sha256(stanza.get("stanza_sha256")) or not _is_sha256(
        stanza.get("source_file_sha256")
    ):
        return False
    vector_entry = payload.get("old_vector")
    if vector_entry is not None and not _entry_shape_ok(vector_entry):
        return False
    return True


def _entry_shape_ok(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    filename = entry.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
        or "\\" in filename
    ):
        return False
    if not _is_non_negative_int(entry.get("size_bytes")):
        return False
    return _is_sha256(entry.get("sha256"))


def _is_non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _verify_file_entry(
    directory: Path, entry: dict, kind: str, reason_codes: list[str]
) -> tuple[bool, int]:
    """Size/SHA-256 re-check of one manifest entry; returns (ok, actual size)."""
    path = directory / entry["filename"]
    if not path.is_file():
        _add_reason(reason_codes, f"{kind}_missing")
        return False, 0
    actual_size = path.stat().st_size
    ok = True
    if actual_size != entry["size_bytes"]:
        _add_reason(reason_codes, f"{kind}_size_mismatch")
        ok = False
    if _file_sha256(path) != entry["sha256"]:
        _add_reason(reason_codes, f"{kind}_sha256_mismatch")
        ok = False
    return ok, actual_size


def _verify_database_entry(
    directory: Path, entry: dict, reason_codes: list[str]
) -> tuple[bool, int, dict[str, int]]:
    """Hash plus an independent reopen: quick_check, version, schema, counts."""
    ok, actual_size = _verify_file_entry(directory, entry, "database", reason_codes)
    counts = {"legacy_rows": 0, "context_items": 0}
    if not ok:
        return False, actual_size, counts
    path = directory / entry["filename"]
    conn: sqlite3.Connection | None = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        if not quick_rows or any(str(row[0]).lower() != "ok" for row in quick_rows):
            _add_reason(reason_codes, "database_quick_check_failed")
            return False, actual_size, counts
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != entry["sqlite_user_version"]:
            _add_reason(reason_codes, "database_version_mismatch")
            ok = False
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if ("memories" in tables) != entry["legacy_schema_present"]:
            _add_reason(reason_codes, "database_schema_mismatch")
            ok = False
        present_context = sum(1 for name in _CONTEXT_TABLES if name in tables)
        if (present_context == len(_CONTEXT_TABLES)) != entry["context_schema_present"]:
            _add_reason(reason_codes, "database_schema_mismatch")
            ok = False
        if "memories" in tables:
            counts["legacy_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )
            status_counts = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM memories GROUP BY status"
                )
            }
            if status_counts != dict(entry["legacy_status_counts"]):
                _add_reason(reason_codes, "database_count_mismatch")
                ok = False
        if counts["legacy_rows"] != entry["legacy_rows"]:
            _add_reason(reason_codes, "database_count_mismatch")
            ok = False
        if "context_items" in tables:
            counts["context_items"] = int(
                conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0]
            )
        if counts["context_items"] != entry["context_items"]:
            _add_reason(reason_codes, "database_count_mismatch")
            ok = False
        return ok, actual_size, counts
    except (OSError, sqlite3.Error):
        _add_reason(reason_codes, "database_unreadable")
        return False, actual_size, counts
    finally:
        if conn is not None:
            conn.close()


def _verify_stanza_entry(
    directory: Path, entry: dict, reason_codes: list[str]
) -> tuple[bool, int]:
    """File hash plus the canonical stanza-snapshot reload check."""
    ok, actual_size = _verify_file_entry(directory, entry, "stanza", reason_codes)
    if not ok:
        return False, actual_size
    try:
        snapshot = CodexConfigEditor.load_snapshot(directory / entry["filename"])
    except CodexConfigError:
        _add_reason(reason_codes, "stanza_reload_failed")
        return False, actual_size
    if (
        snapshot.stanza_sha256 != entry["stanza_sha256"]
        or snapshot.source_file_sha256 != entry["source_file_sha256"]
    ):
        _add_reason(reason_codes, "stanza_hash_mismatch")
        return False, actual_size
    return True, actual_size


def _add_reason(reason_codes: list[str], code: str) -> None:
    if code not in reason_codes:
        reason_codes.append(code)


# ---- filesystem helpers (owner-only writes, durable fsync) ----


def _write_private_file(path: Path, data: bytes) -> None:
    """0600 same-directory temp, fsync, atomic replace, directory fsync."""
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        if tmp_name is not None:
            _remove_temp(tmp_name)
        raise


def _copy_private_file(source: Path, destination: Path) -> None:
    """Byte-exact owner-only copy of an artifact the caller found present."""
    _write_private_file(destination, source.read_bytes())


def _hash_file(path: Path) -> tuple[int, str]:
    return path.stat().st_size, _file_sha256(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: dict) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _remove_temp(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
