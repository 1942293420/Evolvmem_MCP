"""Orchestrate the reversible Context Core cutover with a journaled gate chain.

The apply path runs the ten design steps in exact order:

1. require an explicit data directory, Codex config file, prior preflight
   report/digest, and the ``--writers-restarted`` acknowledgement;
2. acquire the exclusive cutover lock and rerun preflight, aborting when
   its digest differs from the supplied report;
3. snapshot the Codex target stanza and create/verify the consistent backup;
4. open ContextStore with ``create_schema=False`` and, in one outer
   ``BEGIN IMMEDIATE``, create the schema and run the migrator;
5. validate mapping/layers, run the migrator again, require ``created=0``;
6. stage/verify/atomically replace the Context vector, or stop unless a
   separate explicit ``--allow-fts-only`` approval was supplied (recorded
   as degraded, never vector-healthy);
7. run the canary shadow gates and require projection lag zero plus the
   primary invariant gate;
8. atomically persist EvolvMem ``context_mode=compat``;
9. CAS-apply the Codex primary env/approval and verify both the CLI-visible
   fields and the TOML-only approval field;
10. release the exclusive lock and mark ``awaiting_post_cutover_canary``.

A run without ``apply`` performs no write at all. Failure before step 8
changes neither the EvolvMem mode nor the Codex config; failure at/after
step 9 forces an operational CAS rollback to explicit legacy while the
persistent compat mode and migrated Context data are retained — the
database is never restored or deleted automatically.

The owner-only journal lives inside the verified backup directory and moves
only forward through ``JOURNAL_STATES``; failure and rollback are terminal
side branches carrying the exact failed step. Sensitive material (the
stanza snapshot, the canary body) stays in separate owner-only files; the
journal and every public projection carry only hashes, counts, reason
codes, and timestamps.

Callers must present a clean environment: the operator CLI strips the
``EVOLVMEM_DATA_DIR``/``EVOLVMEM_CONTEXT_MODE``/``EVOLVMEM_ADAPTER``
overrides so explicit paths always win; library callers must do the same.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import ClassVar
import uuid

from evolvmem.codex_config import (
    DEFAULT_SERVER,
    CodexConfigEditor,
    CodexConfigError,
    CodexStanzaDriftError,
    parse_mcp_get_json,
    verify_cli_matches_stanza,
)
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import ContextValidationError
from evolvmem.context_store import ContextStore
from evolvmem.cutover_backup import create_cutover_backup, verify_cutover_backup
from evolvmem.cutover_checks import (
    check_projection_lag,
    collect_primary_gate_evidence,
    compare_shadow,
    evaluate_shadow_gate,
    run_preflight,
    verify_primary_gate,
)
from evolvmem.cutover_lock import CutoverLock, CutoverLockTimeout
from evolvmem.cutover_models import CutoverPreflightReport, validate_public_summary
from evolvmem.cutover_vector import rebuild_context_vector_atomically
from evolvmem.vector_index import VectorIndex

__all__ = [
    "CUTOVER_STEPS",
    "JOURNAL_FILENAME",
    "JOURNAL_STATES",
    "CutoverError",
    "CutoverJournal",
    "CutoverJournalError",
    "CutoverOutcome",
    "CutoverRequest",
    "PreflightEnvelope",
    "load_persisted_config",
    "load_preflight_envelope",
    "preflight_fingerprint",
    "preflight_public_fingerprint",
    "rollback_codex_to_legacy",
    "run_cutover",
    "write_preflight_envelope",
]


CUTOVER_STEPS = (
    "validate_inputs",
    "lock_and_rerun_preflight",
    "snapshot_and_backup",
    "migrate_schema",
    "validate_migration",
    "stage_vector",
    "shadow_gate",
    "persist_compat",
    "codex_primary",
    "release_and_await_canary",
)

JOURNAL_STATES = (
    "planned",
    "locked",
    "backed_up",
    "migrated",
    "vector_ready_or_approved_fts",
    "shadow_passed",
    "compat_persisted",
    "codex_primary",
    "awaiting_post_cutover_canary",
    "complete",
)

TERMINAL_STATES = ("failed", "rolled_back")

JOURNAL_FILENAME = "cutover-journal.json"

_ENVELOPE_SCHEMA = "evolvmem.cutover_preflight_file"
_ENVELOPE_VERSION = 1
_CREATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Fingerprint normalization: the duration and the free-space measurement are
# point-in-time values re-measured (and re-gated by ``ready``/``space_ok``)
# on every locked rerun, so they cannot be part of a stable digest.
_FINGERPRINT_EXCLUDED_FIELDS = ("duration_ms", "free_space_bytes")

# Steps whose failure forces the operational Codex legacy rollback.
_POST_PERSIST_FAILURE_STEPS = ("codex_primary", "release_and_await_canary")
# Live states from which an operational rollback is a valid terminal branch.
_ROLLBACKABLE_STATES = (
    "compat_persisted",
    "codex_primary",
    "awaiting_post_cutover_canary",
    "complete",
)

_HASH_KEYS = (
    "preflight_digest",
    "locked_preflight_digest",
    "backup_manifest_digest",
    "backup_verification_digest",
    "stanza_sha256",
    "compat_config_sha256",
    "codex_primary_stanza_sha256",
    "codex_rollback_stanza_sha256",
)


class CutoverError(Exception):
    """One cutover step failed; carries the exact step and safe reason codes."""

    def __init__(
        self,
        step: str,
        code: str,
        message: str = "",
        *,
        reason_codes=(),
    ) -> None:
        if step not in CUTOVER_STEPS:
            raise ContextValidationError(f"unknown cutover step: {step!r}")
        if not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code):
            raise ContextValidationError("code must be a lower-snake reason code")
        self.step = step
        self.code = code
        codes = [code]
        for extra in reason_codes:
            if not isinstance(extra, str) or not _REASON_CODE_PATTERN.match(extra):
                raise ContextValidationError(
                    "reason_codes must contain only lower-snake reason codes"
                )
            if extra not in codes:
                codes.append(extra)
        self.reason_codes = tuple(codes)
        super().__init__(message or code)


class CutoverJournalError(Exception):
    """The journal state machine or on-disk payload is invalid."""


# ---- shared helpers ----


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_checksum_or_empty(value: object) -> bool:
    return isinstance(value, str) and (value == "" or _is_sha256(value))


def _canonical_json(payload: Mapping) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


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
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        raise


def _require_absolute_path(value: object, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ContextValidationError(f"{field_name} must be a path")
    path = Path(value)
    if not path.is_absolute():
        raise ContextValidationError(f"{field_name} must be an absolute path")
    return path


def _utc(clock) -> str:
    moment = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(moment, datetime):
        raise ContextValidationError("clock must return a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ContextValidationError("clock must return a timezone-aware datetime")
    return moment.astimezone(timezone.utc).strftime(_CREATED_FORMAT)


# ---- preflight fingerprints and the report envelope ----


def preflight_public_fingerprint(public: Mapping) -> str:
    """SHA-256 over the canonical public summary minus point-in-time fields."""
    if not isinstance(public, Mapping):
        raise ContextValidationError("public summary must be a mapping")
    payload = {
        key: value
        for key, value in public.items()
        if key not in _FINGERPRINT_EXCLUDED_FIELDS
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def preflight_fingerprint(report: CutoverPreflightReport) -> str:
    """Stable digest of a preflight report for the locked-rerun comparison."""
    if not isinstance(report, CutoverPreflightReport):
        raise ContextValidationError("report must be a CutoverPreflightReport instance")
    return preflight_public_fingerprint(report.public_dict())


@dataclass(frozen=True, slots=True)
class PreflightEnvelope:
    """The handoff artifact: public report, its digest, and private inputs."""

    digest: str
    data_dir: Path
    codex_config_path: Path
    report: dict


def write_preflight_envelope(
    path,
    report: CutoverPreflightReport,
    *,
    data_dir,
    codex_config_path,
) -> str:
    """Write the owner-only preflight envelope; returns its public digest."""
    target = _require_absolute_path(path, "path")
    if not isinstance(report, CutoverPreflightReport):
        raise ContextValidationError("report must be a CutoverPreflightReport instance")
    data = _require_absolute_path(data_dir, "data_dir")
    codex = _require_absolute_path(codex_config_path, "codex_config_path")
    digest = preflight_fingerprint(report)
    payload = {
        "schema": _ENVELOPE_SCHEMA,
        "version": _ENVELOPE_VERSION,
        "digest": digest,
        "inputs": {"data_dir": str(data), "codex_config": str(codex)},
        "report": report.public_dict(),
    }
    _write_private_file(target, _canonical_json(payload))
    return digest


def load_preflight_envelope(path) -> PreflightEnvelope:
    """Load and authenticate a preflight envelope; tampering is an error."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CutoverError(
            "validate_inputs", "preflight_report_missing"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError(
            "validate_inputs", "preflight_report_malformed"
        ) from exc
    malformed = CutoverError("validate_inputs", "preflight_report_malformed")
    if not isinstance(payload, dict):
        raise malformed
    if (
        payload.get("schema") != _ENVELOPE_SCHEMA
        or payload.get("version") != _ENVELOPE_VERSION
    ):
        raise malformed
    digest = payload.get("digest")
    report = payload.get("report")
    inputs = payload.get("inputs")
    if not _is_sha256(digest) or not isinstance(report, dict):
        raise malformed
    if preflight_public_fingerprint(report) != digest:
        raise CutoverError("validate_inputs", "preflight_report_tampered")
    if not isinstance(inputs, dict):
        raise malformed
    try:
        data_dir = _require_absolute_path(inputs.get("data_dir"), "data_dir")
        codex_config_path = _require_absolute_path(
            inputs.get("codex_config"), "codex_config"
        )
    except ContextValidationError as exc:
        raise malformed from exc
    return PreflightEnvelope(
        digest=digest,
        data_dir=data_dir,
        codex_config_path=codex_config_path,
        report=report,
    )


# ---- the owner-only monotonic journal ----


class CutoverJournal:
    """Owner-only audit journal inside the verified backup directory.

    State moves only forward through ``JOURNAL_STATES``; ``failed`` and
    ``rolled_back`` are terminal side branches carrying the exact failed
    step. Every transition rewrites the journal through a 0600 temp file,
    fsync, and an atomic replace. The on-disk payload additionally carries
    the private routing block (absolute paths the operator supplied);
    ``public_dict()`` omits it and stays inside the privacy-guarded summary
    vocabulary.
    """

    SCHEMA: ClassVar[str] = "evolvmem.cutover_journal"
    VERSION: ClassVar[int] = 1

    def __init__(self) -> None:
        raise TypeError("use CutoverJournal.begin() or CutoverJournal.load()")

    @classmethod
    def _create(
        cls,
        *,
        operation_id: str,
        preflight_digest: str,
        private: Mapping,
        created_utc: str,
    ) -> "CutoverJournal":
        journal = object.__new__(cls)
        journal._operation_id = operation_id
        journal._state = "planned"
        journal._failed_step = ""
        journal._rolled_back_from = ""
        journal._reason_codes: tuple[str, ...] = ()
        journal._created_utc = created_utc
        journal._updated_utc = created_utc
        journal._history: list[dict] = [{"state": "planned", "utc": created_utc}]
        journal._hashes: dict[str, str] = {key: "" for key in _HASH_KEYS}
        journal._hashes["preflight_digest"] = preflight_digest
        journal._gates: dict[str, dict] = {}
        journal._counts: dict[str, int] = {}
        journal._private = dict(private)
        journal._path: Path | None = None
        return journal

    @classmethod
    def begin(cls, *, preflight_digest: str, private: Mapping, clock=None):
        """Start an in-memory journal; it becomes durable at ``bind()``."""
        if not _is_sha256(preflight_digest):
            raise CutoverJournalError("preflight_digest must be a SHA-256 hex")
        if not isinstance(private, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in private.items()
        ):
            raise CutoverJournalError("private must map string keys to strings")
        return cls._create(
            operation_id=uuid.uuid4().hex,
            preflight_digest=preflight_digest,
            private=private,
            created_utc=_utc(clock),
        )

    # -- introspection --

    @property
    def state(self) -> str:
        return self._state

    @property
    def failed_step(self) -> str:
        return self._failed_step

    @property
    def rolled_back_from(self) -> str:
        return self._rolled_back_from

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return self._reason_codes

    @property
    def history(self) -> tuple[dict, ...]:
        return tuple(dict(entry) for entry in self._history)

    @property
    def hashes(self) -> dict[str, str]:
        return dict(self._hashes)

    @property
    def gates(self) -> dict[str, dict]:
        return {name: dict(entry) for name, entry in self._gates.items()}

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def private(self) -> dict[str, str]:
        return dict(self._private)

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def bound(self) -> bool:
        return self._path is not None

    # -- lifecycle --

    def bind(self, directory) -> None:
        """Bind to the verified backup directory and write the first durable copy."""
        if self._path is not None:
            raise CutoverJournalError("journal is already bound")
        directory = Path(directory)
        if not directory.is_dir():
            raise CutoverJournalError("journal directory does not exist")
        path = directory / JOURNAL_FILENAME
        if path.exists():
            raise CutoverJournalError("a cutover journal already exists here")
        self._path = path
        self._persist()

    def advance(
        self,
        state: str,
        *,
        hashes: Mapping | None = None,
        gates: Mapping | None = None,
        counts: Mapping | None = None,
        reason_codes=(),
        clock=None,
    ) -> None:
        """Move exactly one state forward along the documented chain."""
        if self._state in TERMINAL_STATES:
            raise CutoverJournalError("a terminal journal cannot advance")
        if state not in JOURNAL_STATES:
            raise CutoverJournalError(f"unknown journal state: {state!r}")
        expected = JOURNAL_STATES.index(self._state) + 1
        if expected >= len(JOURNAL_STATES) or JOURNAL_STATES[expected] != state:
            raise CutoverJournalError(
                f"journal cannot move from {self._state} to {state}"
            )
        self._merge(hashes=hashes, gates=gates, counts=counts, reason_codes=reason_codes)
        self._state = state
        self._stamp(state, clock)

    def mark_failed(
        self,
        *,
        step: str,
        reason_codes=(),
        hashes: Mapping | None = None,
        gates: Mapping | None = None,
        counts: Mapping | None = None,
        clock=None,
    ) -> None:
        """Terminal failure branch; records the exact failed step."""
        if self._state in TERMINAL_STATES:
            raise CutoverJournalError("journal is already terminal")
        if step not in CUTOVER_STEPS:
            raise CutoverJournalError(f"unknown cutover step: {step!r}")
        self._merge(hashes=hashes, gates=gates, counts=counts, reason_codes=reason_codes)
        self._failed_step = step
        self._state = "failed"
        self._stamp("failed", clock)

    def mark_rolled_back(
        self,
        *,
        failed_step: str = "",
        reason_codes=(),
        rollback_stanza_sha256: str = "",
        clock=None,
    ) -> None:
        """Terminal rollback branch from a post-persist state or failure."""
        if self._state in TERMINAL_STATES and self._state != "failed":
            raise CutoverJournalError("journal is already terminal")
        allowed = self._state in _ROLLBACKABLE_STATES or (
            self._state == "failed"
            and self._failed_step in _POST_PERSIST_FAILURE_STEPS + ("persist_compat",)
        )
        if not allowed:
            raise CutoverJournalError(
                f"journal in state {self._state} cannot roll back"
            )
        if failed_step and failed_step not in CUTOVER_STEPS:
            raise CutoverJournalError(f"unknown cutover step: {failed_step!r}")
        if not _is_checksum_or_empty(rollback_stanza_sha256):
            raise CutoverJournalError("rollback hash must be a SHA-256 hex or empty")
        origin = self._state
        if failed_step:
            self._failed_step = failed_step
        self._merge(reason_codes=reason_codes)
        if rollback_stanza_sha256:
            self._hashes["codex_rollback_stanza_sha256"] = rollback_stanza_sha256
        self._rolled_back_from = origin
        self._state = "rolled_back"
        self._stamp("rolled_back", clock)

    # -- serialization --

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "operation_id": self._operation_id,
            "state": self._state,
            "failed_step": self._failed_step,
            "rolled_back_from": self._rolled_back_from,
            "reason_codes": list(self._reason_codes),
            "created_utc": self._created_utc,
            "updated_utc": self._updated_utc,
            "history": [dict(entry) for entry in self._history],
            "hashes": dict(self._hashes),
            "gates": {name: dict(entry) for name, entry in self._gates.items()},
            "counts": dict(self._counts),
        }
        validate_public_summary(public)
        return public

    def digest(self) -> str:
        blob = json.dumps(
            self.public_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _payload(self) -> dict:
        payload = self.public_dict()
        payload["private"] = dict(self._private)
        return payload

    def _persist(self) -> None:
        if self._path is None:
            return
        _write_private_file(self._path, _canonical_json(self._payload()))

    def _stamp(self, state: str, clock) -> None:
        now = _utc(clock)
        self._updated_utc = now
        self._history.append({"state": state, "utc": now})
        self._persist()

    def _merge(
        self,
        *,
        hashes: Mapping | None = None,
        gates: Mapping | None = None,
        counts: Mapping | None = None,
        reason_codes=(),
    ) -> None:
        if hashes is not None:
            for key, value in hashes.items():
                if key not in _HASH_KEYS:
                    raise CutoverJournalError(f"unknown journal hash key: {key!r}")
                if not _is_checksum_or_empty(value):
                    raise CutoverJournalError(
                        "journal hashes must be SHA-256 hex or empty"
                    )
                self._hashes[key] = value
        if gates is not None:
            for name, entry in gates.items():
                if not isinstance(name, str) or not _SAFE_NAME_PATTERN.match(name):
                    raise CutoverJournalError(f"invalid gate name: {name!r}")
                if not isinstance(entry, Mapping):
                    raise CutoverJournalError("gate entries must be mappings")
                status = entry.get("status")
                digest = entry.get("digest", "")
                if (
                    not isinstance(status, str)
                    or not status
                    or "/" in status
                    or "\\" in status
                    or "\n" in status
                ):
                    raise CutoverJournalError("gate status must be a safe string")
                if not _is_checksum_or_empty(digest):
                    raise CutoverJournalError(
                        "gate digests must be SHA-256 hex or empty"
                    )
                self._gates[name] = {"status": status, "digest": digest}
        if counts is not None:
            for key, value in counts.items():
                if not isinstance(key, str) or not _SAFE_NAME_PATTERN.match(key):
                    raise CutoverJournalError(f"invalid count name: {key!r}")
                if type(value) is not int or value < 0:
                    raise CutoverJournalError("counts must be non-negative integers")
                self._counts[key] = value
        codes = list(self._reason_codes)
        for code in reason_codes:
            if not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code):
                raise CutoverJournalError(
                    "reason_codes must contain only lower-snake reason codes"
                )
            if code not in codes:
                codes.append(code)
        self._reason_codes = tuple(codes)

    @classmethod
    def load(cls, path) -> "CutoverJournal":
        """Reload a journal, validating the full payload shape and chain."""
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise CutoverJournalError("journal file is not readable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverJournalError("journal file is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CutoverJournalError("journal file is malformed")
        if payload.get("schema") != cls.SCHEMA or payload.get("version") != cls.VERSION:
            raise CutoverJournalError("journal file is malformed")

        state = payload.get("state")
        if state not in JOURNAL_STATES + TERMINAL_STATES:
            raise CutoverJournalError("journal state is unknown")
        operation_id = payload.get("operation_id")
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(char not in "0123456789abcdef" for char in operation_id)
        ):
            raise CutoverJournalError("journal operation id is malformed")
        failed_step = payload.get("failed_step")
        rolled_back_from = payload.get("rolled_back_from")
        if not isinstance(failed_step, str) or (
            failed_step and failed_step not in CUTOVER_STEPS
        ):
            raise CutoverJournalError("journal failed step is malformed")
        if not isinstance(rolled_back_from, str) or (
            rolled_back_from and rolled_back_from not in JOURNAL_STATES + ("failed",)
        ):
            raise CutoverJournalError("journal rollback origin is malformed")
        if state == "failed" and not failed_step:
            raise CutoverJournalError("a failed journal must name its failed step")
        if state == "rolled_back" and not rolled_back_from:
            raise CutoverJournalError("a rolled-back journal must name its origin")

        history = payload.get("history")
        if not isinstance(history, list) or not history:
            raise CutoverJournalError("journal history is malformed")
        for entry in history:
            if (
                not isinstance(entry, dict)
                or entry.get("state") not in JOURNAL_STATES + TERMINAL_STATES
                or not isinstance(entry.get("utc"), str)
            ):
                raise CutoverJournalError("journal history is malformed")
        cls._check_history_chain(history)
        if history[-1]["state"] != state:
            raise CutoverJournalError("journal history does not end at its state")

        hashes = payload.get("hashes")
        if not isinstance(hashes, dict) or any(
            key not in _HASH_KEYS or not _is_checksum_or_empty(value)
            for key, value in hashes.items()
        ):
            raise CutoverJournalError("journal hashes are malformed")

        gates = payload.get("gates", {})
        if not isinstance(gates, dict):
            raise CutoverJournalError("journal gates are malformed")
        for name, entry in gates.items():
            if (
                not isinstance(name, str)
                or not _SAFE_NAME_PATTERN.match(name)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("status"), str)
                or not _is_checksum_or_empty(entry.get("digest", ""))
            ):
                raise CutoverJournalError("journal gates are malformed")

        counts = payload.get("counts", {})
        if not isinstance(counts, dict) or any(
            not isinstance(key, str)
            or not _SAFE_NAME_PATTERN.match(key)
            or type(value) is not int
            or value < 0
            for key, value in counts.items()
        ):
            raise CutoverJournalError("journal counts are malformed")

        reason_codes = payload.get("reason_codes")
        if not isinstance(reason_codes, list) or any(
            not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code)
            for code in reason_codes
        ):
            raise CutoverJournalError("journal reason codes are malformed")

        for stamp_key in ("created_utc", "updated_utc"):
            if not isinstance(payload.get(stamp_key), str):
                raise CutoverJournalError("journal timestamps are malformed")

        private = payload.get("private")
        if not isinstance(private, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in private.items()
        ):
            raise CutoverJournalError("journal private block is malformed")

        journal = cls._create(
            operation_id=operation_id,
            preflight_digest=hashes.get("preflight_digest", ""),
            private=private,
            created_utc=payload["created_utc"],
        )
        journal._state = state
        journal._failed_step = failed_step
        journal._rolled_back_from = rolled_back_from
        journal._reason_codes = tuple(reason_codes)
        journal._updated_utc = payload["updated_utc"]
        journal._history = [dict(entry) for entry in history]
        journal._hashes = {key: hashes.get(key, "") for key in _HASH_KEYS}
        journal._gates = {
            name: {"status": entry["status"], "digest": entry.get("digest", "")}
            for name, entry in gates.items()
        }
        journal._counts = dict(counts)
        journal._path = Path(path)
        return journal

    @staticmethod
    def _check_history_chain(history: list[dict]) -> None:
        """Every consecutive pair is a forward chain step or a terminal branch."""
        index = {state: position for position, state in enumerate(JOURNAL_STATES)}
        previous = history[0]["state"]
        if previous != "planned":
            raise CutoverJournalError("journal history must start at planned")
        for entry in history[1:]:
            current = entry["state"]
            if current in TERMINAL_STATES:
                # ``failed -> rolled_back`` is the one valid terminal chain:
                # the write side allows it for post-persist failure steps.
                if previous in TERMINAL_STATES and (previous, current) != (
                    "failed",
                    "rolled_back",
                ):
                    raise CutoverJournalError("terminal journal states cannot chain")
            elif previous in TERMINAL_STATES or index[current] != index[previous] + 1:
                raise CutoverJournalError("journal history is not monotonic")
            previous = current


# ---- request and outcome models ----


@dataclass(frozen=True, slots=True)
class CutoverRequest:
    """Explicit operator intent; nothing is guessed or defaulted."""

    config: Config
    codex_config_path: Path
    preflight_report_path: Path
    writers_restarted: bool
    apply: bool
    allow_fts_only: bool = False
    lock_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.config, Config):
            raise ContextValidationError("config must be a Config instance")
        object.__setattr__(
            self,
            "codex_config_path",
            _require_absolute_path(self.codex_config_path, "codex_config_path"),
        )
        object.__setattr__(
            self,
            "preflight_report_path",
            _require_absolute_path(self.preflight_report_path, "preflight_report_path"),
        )
        for name in ("writers_restarted", "apply", "allow_fts_only"):
            if type(getattr(self, name)) is not bool:
                raise ContextValidationError(f"{name} must be a boolean")
        if self.apply and not self.writers_restarted:
            raise ContextValidationError(
                "apply requires the explicit writers_restarted acknowledgement"
            )
        if self.allow_fts_only and not self.apply:
            raise ContextValidationError(
                "allow_fts_only is meaningful only beside an explicit apply"
            )
        if (
            type(self.lock_timeout_seconds) not in (int, float)
            or not math.isfinite(self.lock_timeout_seconds)
            or self.lock_timeout_seconds < 0
        ):
            raise ContextValidationError(
                "lock_timeout_seconds must be a non-negative finite number"
            )


@dataclass(frozen=True, slots=True)
class CutoverOutcome:
    """Public outcome of one cutover run; every field is public-safe."""

    SCHEMA: ClassVar[str] = "evolvmem.cutover_outcome"
    VERSION: ClassVar[int] = 1

    applied: bool
    ready: bool
    state: str
    operation_id: str
    failed_step: str
    reason_codes: tuple[str, ...]
    steps_completed: tuple[str, ...]
    preflight_ready: bool
    preflight_digest_match: bool
    backup_directory_name: str
    journal_digest: str
    fts_only: bool
    codex_rollback: str
    duration_ms: float

    def __post_init__(self) -> None:
        for name in ("applied", "ready", "preflight_ready", "preflight_digest_match", "fts_only"):
            if type(getattr(self, name)) is not bool:
                raise ContextValidationError(f"{name} must be a boolean")
        for name in ("state", "operation_id", "failed_step", "backup_directory_name", "journal_digest", "codex_rollback"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ContextValidationError(f"{name} must be a string")
            if any(char in value for char in "/\\\n\r"):
                raise ContextValidationError(f"{name} must be a single safe string")
        for name in ("reason_codes", "steps_completed"):
            codes = getattr(self, name)
            try:
                items = tuple(codes)
            except TypeError as exc:
                raise ContextValidationError(f"{name} must be an iterable") from exc
            for item in items:
                if not isinstance(item, str) or not _REASON_CODE_PATTERN.match(item):
                    raise ContextValidationError(
                        f"{name} must contain only lower-snake codes"
                    )
            object.__setattr__(self, name, items)
        if (
            type(self.duration_ms) not in (int, float)
            or not math.isfinite(self.duration_ms)
            or self.duration_ms < 0
        ):
            raise ContextValidationError("duration_ms must be a non-negative finite number")
        object.__setattr__(self, "duration_ms", float(self.duration_ms))

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "applied": self.applied,
            "ready": self.ready,
            "state": self.state,
            "operation_id": self.operation_id,
            "failed_step": self.failed_step,
            "reason_codes": list(self.reason_codes),
            "steps_completed": list(self.steps_completed),
            "preflight_ready": self.preflight_ready,
            "preflight_digest_match": self.preflight_digest_match,
            "backup_directory_name": self.backup_directory_name,
            "journal_digest": self.journal_digest,
            "fts_only": self.fts_only,
            "codex_rollback": self.codex_rollback,
        }
        validate_public_summary(public)
        return public


# ---- operational Codex rollback (shared by the orchestrator and tools) ----


def rollback_codex_to_legacy(editor: CodexConfigEditor) -> tuple[str, str]:
    """CAS the target stanza back to explicit legacy when it says primary.

    Returns ``(action, after_stanza_sha256)`` where action is ``cas_legacy``
    or ``not_needed`` (the stanza never reached primary, so nothing is
    rewritten). The post-write stanza is re-read and must say ``legacy``.
    This is an operational mode switch, never an installation-stanza restore
    and never a database restore.
    """
    if not isinstance(editor, CodexConfigEditor) and not all(
        hasattr(editor, name) for name in ("snapshot", "apply_legacy")
    ):
        raise ContextValidationError("editor must provide snapshot/apply_legacy")
    current = editor.snapshot()
    env = current.stanza.get("env")
    mode = env.get("EVOLVMEM_CONTEXT_MODE") if isinstance(env, Mapping) else None
    if mode != "primary":
        return ("not_needed", "")
    result = editor.apply_legacy(expected_stanza_sha256=current.stanza_sha256)
    after = editor.snapshot()
    after_env = after.stanza.get("env")
    if not isinstance(after_env, Mapping) or after_env.get("EVOLVMEM_CONTEXT_MODE") != "legacy":
        raise CodexConfigError("rollback verification found a non-legacy stanza")
    return ("cas_legacy", result.after_stanza_sha256)


# ---- persisted compat mode ----


def load_persisted_config(config: Config) -> Config:
    """Load the on-disk config pinned to the explicit data directory.

    Mirrors ``Config.from_file`` field application without letting the
    process environment or the default data directory redirect the target.
    """
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    persisted = Config(data_dir=config.data_dir)
    path = config.config_path
    if path.is_file():
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            for key, value in data.items():
                if hasattr(persisted, key):
                    setattr(persisted, key, value)
    return persisted


def _persist_compat(config: Config) -> str:
    """Atomically persist ``context_mode=compat``; returns the file SHA-256."""
    persisted = load_persisted_config(config)
    persisted.context_mode = "compat"
    persisted.save()
    raw = config.config_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError("persist_compat", "compat_persist_failed") from exc
    if payload.get("context_mode") != "compat":
        raise CutoverError("persist_compat", "compat_persist_failed")
    return hashlib.sha256(raw).hexdigest()


# ---- default collaborators ----


def _default_cli_probe(codex_config_path, *, codex_bin: str = "codex"):
    """Read the switched stanza back through ``codex mcp get --json``.

    The CLI 0.147 reads CODEX_HOME/config.toml; an explicit config with any
    other filename has no CLI-visible projection, so dual verification is
    unavailable and the step fails closed.
    """
    path = Path(codex_config_path)
    if path.name != "config.toml":
        raise CutoverError("codex_primary", "codex_cli_probe_unavailable")
    env = dict(os.environ)
    env["CODEX_HOME"] = str(path.parent)
    try:
        proc = subprocess.run(
            [codex_bin, "mcp", "get", DEFAULT_SERVER, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CutoverError("codex_primary", "codex_cli_probe_failed") from exc
    if proc.returncode != 0:
        raise CutoverError("codex_primary", "codex_cli_probe_failed")
    try:
        return parse_mcp_get_json(proc.stdout)
    except CodexConfigError as exc:
        raise CutoverError("codex_primary", "codex_cli_probe_failed") from exc


def _default_primary_gate(
    config: Config,
    store: ContextStore,
    *,
    second_migration_created: int,
    fts_only_approved: bool,
    shadow_thresholds_met: bool,
    vector_staged: bool,
):
    index = None
    if vector_staged:
        candidate = VectorIndex(config, path=config.context_vector_path)
        try:
            candidate.initialize(dim=config.embedding_dim)
            index = candidate
        except Exception:
            try:
                candidate.close()
            except Exception:
                pass  # unhealthy evidence is reported, never hidden
    evidence = collect_primary_gate_evidence(
        config,
        store,
        context_vector_index=index,
        second_migration_created=second_migration_created,
        fts_only_approved=fts_only_approved,
        shadow_thresholds_met=shadow_thresholds_met,
    )
    return verify_primary_gate(evidence)


def _default_shadow_runner(
    config: Config,
    store: ContextStore,
    *,
    embedding_engine=None,
    journal_directory=None,
    clock=None,
):
    """Canary-based shadow evidence over the migrated library.

    One explicitly authorized high-entropy canary is created after
    migration, must stay the mapped top-1 in both the legacy and Core
    rankings for exact and CJK probes, and is exact-cleaned before any
    config switch. Semantic overlap@5 remains the already-passed
    isolated-corpus gate; no real-library content enters the report.
    """
    from evolvmem.cutover_canary import CutoverCanary, CutoverCanaryError
    from evolvmem.memory_store import MemoryStore

    if journal_directory is None:
        raise CutoverError("shadow_gate", "shadow_evidence_missing")
    try:
        canary = CutoverCanary(
            config,
            store=store,
            embedding_engine=embedding_engine,
            journal_directory=journal_directory,
            externally_locked=True,
            clock=clock,
        )
        handle = canary.prepare(authorized=True)
    except CutoverCanaryError as exc:
        raise CutoverError("shadow_gate", "canary_prepare_failed") from exc
    try:
        mapping = {
            int(row["legacy_memory_id"]): int(row["context_item_id"])
            for row in store._connection().execute(
                "SELECT legacy_memory_id, context_item_id FROM legacy_memory_migrations"
            )
        }
        legacy = MemoryStore(config)
        legacy.initialize()
        try:
            comparisons = [
                compare_shadow(
                    [
                        int(row["id"])
                        for row in legacy.search_fts(query, top_k=5)
                    ],
                    list(canary.probe(query, top_k=5)),
                    mapping,
                    expected_relevant=1,
                )
                for query in (handle.exact_query, handle.cjk_query)
            ]
        finally:
            legacy.close()
    finally:
        try:
            canary.cleanup(handle)
        except CutoverCanaryError as exc:
            raise CutoverError("shadow_gate", "canary_cleanup_failed") from exc
    return evaluate_shadow_gate(comparisons)


# ---- the orchestrator ----


@contextmanager
def _step(step: str, code: str):
    """Map any non-Cutover failure inside a step onto its exact boundary."""
    try:
        yield
    except CutoverError:
        raise
    except Exception as exc:
        raise CutoverError(step, code) from exc


def run_cutover(
    request: CutoverRequest,
    *,
    lock=None,
    preflight_runner=None,
    backup_creator=None,
    backup_verifier=None,
    store_factory=None,
    migrator_factory=None,
    lag_checker=None,
    vector_stager=None,
    shadow_runner=None,
    primary_gate_runner=None,
    compat_persister=None,
    codex_editor_factory=None,
    cli_probe=None,
    journal_factory=None,
    embedding_engine=None,
    clock=None,
) -> CutoverOutcome:
    """Run the ten-step cutover, or a zero-write dry-run without ``apply``."""
    started = time.monotonic()
    if not isinstance(request, CutoverRequest):
        raise ContextValidationError("request must be a CutoverRequest instance")
    config = request.config

    preflight_runner = preflight_runner or run_preflight

    # Step 1: explicit paths and prior report, verified before anything runs.
    try:
        with _step("validate_inputs", "input_validation_failed"):
            if not config.data_dir.is_dir():
                raise CutoverError("validate_inputs", "data_dir_missing")
            if not request.codex_config_path.is_file():
                raise CutoverError("validate_inputs", "codex_config_missing")
            envelope = load_preflight_envelope(request.preflight_report_path)
    except CutoverError as exc:
        return CutoverOutcome(
            applied=request.apply,
            ready=False,
            state="failed",
            operation_id="",
            failed_step=exc.step,
            reason_codes=exc.reason_codes,
            steps_completed=(),
            preflight_ready=False,
            preflight_digest_match=False,
            backup_directory_name="",
            journal_digest="",
            fts_only=False,
            codex_rollback="",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    if not request.apply:
        # Dry-run: a read-only rerun plus a digest comparison; no lock, no
        # backup, no journal, no config writes of any kind.
        report = preflight_runner(config, codex_config_path=request.codex_config_path)
        digest_match = preflight_fingerprint(report) == envelope.digest
        reason_codes = list(report.reason_codes)
        if not digest_match and "preflight_digest_mismatch" not in reason_codes:
            reason_codes.append("preflight_digest_mismatch")
        return CutoverOutcome(
            applied=False,
            ready=report.ready and digest_match,
            state="planned",
            operation_id="",
            failed_step="",
            reason_codes=tuple(reason_codes),
            steps_completed=(),
            preflight_ready=report.ready,
            preflight_digest_match=digest_match,
            backup_directory_name="",
            journal_digest="",
            fts_only=False,
            codex_rollback="",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    return _run_apply(
        request,
        envelope=envelope,
        started=started,
        lock=lock,
        preflight_runner=preflight_runner,
        backup_creator=backup_creator or create_cutover_backup,
        backup_verifier=backup_verifier or verify_cutover_backup,
        store_factory=store_factory or (lambda: ContextStore(config)),
        migrator_factory=migrator_factory
        or (lambda store, cfg: LegacyMemoryMigrator(store, cfg)),
        lag_checker=lag_checker or check_projection_lag,
        vector_stager=vector_stager or rebuild_context_vector_atomically,
        shadow_runner=shadow_runner or _default_shadow_runner,
        primary_gate_runner=primary_gate_runner or _default_primary_gate,
        compat_persister=compat_persister or _persist_compat,
        codex_editor_factory=codex_editor_factory or CodexConfigEditor,
        cli_probe=cli_probe
        or (lambda: _default_cli_probe(request.codex_config_path)),
        journal_factory=journal_factory or CutoverJournal.begin,
        embedding_engine=embedding_engine,
        clock=clock,
    )


def _run_apply(
    request: CutoverRequest,
    *,
    envelope: PreflightEnvelope,
    started: float,
    lock,
    preflight_runner,
    backup_creator,
    backup_verifier,
    store_factory,
    migrator_factory,
    lag_checker,
    vector_stager,
    shadow_runner,
    primary_gate_runner,
    compat_persister,
    codex_editor_factory,
    cli_probe,
    journal_factory,
    embedding_engine,
    clock,
) -> CutoverOutcome:
    config = request.config
    journal = journal_factory(
        preflight_digest=envelope.digest,
        private={
            "data_dir": str(config.data_dir),
            "codex_config_path": str(request.codex_config_path),
        },
        clock=clock,
    )
    if lock is None:
        lock = CutoverLock(config)

    steps_completed: list[str] = ["validate_inputs"]
    store = None
    editor = None
    backup_directory_name = ""
    fts_only = False
    preflight_ready = False
    preflight_digest_match = False

    def outcome(state, failed_step, reason_codes, codex_rollback=""):
        return CutoverOutcome(
            applied=True,
            ready=state == "awaiting_post_cutover_canary",
            state=state,
            operation_id=journal.operation_id,
            failed_step=failed_step,
            reason_codes=tuple(reason_codes),
            steps_completed=tuple(steps_completed),
            preflight_ready=preflight_ready,
            preflight_digest_match=preflight_digest_match,
            backup_directory_name=backup_directory_name,
            journal_digest=journal.digest() if journal.bound else "",
            fts_only=fts_only,
            codex_rollback=codex_rollback,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def fail(exc: CutoverError) -> CutoverOutcome:
        if exc.step in _POST_PERSIST_FAILURE_STEPS and editor is not None:
            # At/after the Codex primary step: force explicit legacy by CAS,
            # retain the persistent compat mode and the migrated data, and
            # never restore or delete the database.
            try:
                action, after_hash = rollback_codex_to_legacy(editor)
            except Exception:
                codes = exc.reason_codes + ("codex_rollback_failed",)
                _mark_terminal(
                    journal, "failed", step=exc.step, reason_codes=codes
                )
                return outcome("failed", exc.step, codes, codex_rollback="failed")
            codes = exc.reason_codes
            if action == "not_needed":
                codes = codes + ("codex_primary_not_applied",)
            _mark_terminal(
                journal,
                "rolled_back",
                failed_step=exc.step,
                reason_codes=codes,
                rollback_stanza_sha256=after_hash,
            )
            return outcome("rolled_back", exc.step, codes, codex_rollback=action)
        _mark_terminal(journal, "failed", step=exc.step, reason_codes=exc.reason_codes)
        return outcome("failed", exc.step, exc.reason_codes)

    try:
        # Step 2: exclusive lock, then the locked preflight rerun.
        with _step("lock_and_rerun_preflight", "lock_failed"):
            try:
                with lock.exclusive(
                    timeout_seconds=request.lock_timeout_seconds
                ):
                    report = preflight_runner(
                        config, codex_config_path=request.codex_config_path
                    )
                    locked_digest = preflight_fingerprint(report)
                    preflight_ready = report.ready
                    preflight_digest_match = locked_digest == envelope.digest
                    if not preflight_digest_match:
                        raise CutoverError(
                            "lock_and_rerun_preflight", "preflight_digest_mismatch"
                        )
                    if not report.ready:
                        raise CutoverError(
                            "lock_and_rerun_preflight",
                            "preflight_not_ready",
                            reason_codes=report.reason_codes,
                        )
                    journal.advance(
                        "locked",
                        hashes={"locked_preflight_digest": locked_digest},
                        gates={
                            "preflight": {"status": "passed", "digest": locked_digest}
                        },
                        counts={
                            "legacy_rows": report.legacy_rows,
                            "context_items": report.context_items,
                        },
                        clock=clock,
                    )
                    steps_completed.append("lock_and_rerun_preflight")

                    # Step 3: stanza snapshot plus a fresh verified backup.
                    with _step("snapshot_and_backup", "backup_failed"):
                        editor = codex_editor_factory(request.codex_config_path)
                        snapshot = editor.snapshot()
                        timestamp = (
                            clock()
                            if clock is not None
                            else datetime.now(timezone.utc)
                        )
                        manifest = backup_creator(
                            config,
                            codex_snapshot=snapshot,
                            preflight_digest=envelope.digest,
                            timestamp=timestamp,
                        )
                        backup_directory_name = manifest.directory_name
                        backup_dir = (
                            config.data_dir / "backups" / manifest.directory_name
                        )
                        verification = backup_verifier(backup_dir)
                        if not verification.verified:
                            raise CutoverError(
                                "snapshot_and_backup",
                                "backup_verification_failed",
                                reason_codes=verification.reason_codes,
                            )
                        journal.bind(backup_dir)
                        journal.advance(
                            "backed_up",
                            hashes={
                                "backup_manifest_digest": manifest.digest(),
                                "backup_verification_digest": verification.digest(),
                                "stanza_sha256": snapshot.stanza_sha256,
                            },
                            gates={
                                "backup": {
                                    "status": "passed",
                                    "digest": verification.digest(),
                                }
                            },
                            counts={
                                "legacy_rows": manifest.legacy_rows,
                                "context_items": manifest.context_items,
                            },
                            clock=clock,
                        )
                    steps_completed.append("snapshot_and_backup")

                    # Step 4: one outer BEGIN IMMEDIATE creates the schema and
                    # runs the migrator against the existing database.
                    with _step("migrate_schema", "migration_failed"):
                        store = store_factory()
                        store.initialize(create_schema=False)
                        migrator = migrator_factory(store, config)
                        with store.transaction():
                            store.create_schema_in_transaction()
                            migration = migrator.migrate()
                        journal.advance(
                            "migrated",
                            gates={"migration": {"status": "passed", "digest": ""}},
                            counts={
                                "migration_scanned": migration.scanned,
                                "migration_created": migration.created,
                                "duplicate_active_count": migration.duplicate_active_count,
                            },
                            clock=clock,
                        )
                    steps_completed.append("migrate_schema")

                    # Step 5: mapping/layer validation plus a zero-create rerun.
                    with _step("validate_migration", "migration_validation_failed"):
                        lag = lag_checker(config, store)
                        if (
                            lag.missing_mapping
                            or lag.duplicate_mapping_target
                            or lag.orphan_mapping
                            or lag.dangling_item_mapping
                        ):
                            raise CutoverError(
                                "validate_migration", "legacy_mapping_incomplete"
                            )
                        if lag.layer_mismatch:
                            raise CutoverError(
                                "validate_migration", "layer_invariant_failed"
                            )
                        second = migrator.migrate()
                        if second.created != 0:
                            raise CutoverError(
                                "validate_migration", "migration_not_idempotent"
                            )
                    steps_completed.append("validate_migration")

                    # Step 6: atomic Context vector staging, or an explicitly
                    # approved degraded FTS-only stop — never vector-healthy.
                    with _step("stage_vector", "context_vector_unhealthy"):
                        stage = vector_stager(
                            config,
                            store,
                            embedding_engine,
                            allow_fts_only=request.allow_fts_only,
                        )
                        if stage.status == "failed":
                            raise CutoverError(
                                "stage_vector",
                                "context_vector_unhealthy",
                                reason_codes=stage.reason_codes,
                            )
                        fts_only = stage.status == "fts_only"
                        journal.advance(
                            "vector_ready_or_approved_fts",
                            gates={
                                "vector": {
                                    "status": stage.status,
                                    "digest": stage.digest(),
                                },
                                "second_migration": {"status": "passed", "digest": ""},
                            },
                            counts={
                                "vector_documents": stage.document_count,
                                "second_migration_created": second.created,
                            },
                            reason_codes=stage.reason_codes,
                            clock=clock,
                        )
                    steps_completed.append("stage_vector")

                    # Step 7: canary shadow gates, projection lag zero, and the
                    # reusable primary invariant gate.
                    with _step("shadow_gate", "shadow_gate_failed"):
                        shadow = shadow_runner(
                            config,
                            store,
                            embedding_engine=embedding_engine,
                            journal_directory=journal.path.parent
                            if journal.path is not None
                            else None,
                            clock=clock,
                        )
                        if not shadow.thresholds_met:
                            raise CutoverError(
                                "shadow_gate",
                                "shadow_thresholds_unmet",
                                reason_codes=shadow.reason_codes,
                            )
                        lag_after = lag_checker(config, store)
                        if lag_after.projection_lag != 0:
                            raise CutoverError("shadow_gate", "projection_lag_nonzero")
                        primary = primary_gate_runner(
                            config,
                            store,
                            second_migration_created=second.created,
                            fts_only_approved=fts_only,
                            shadow_thresholds_met=shadow.thresholds_met,
                            vector_staged=stage.status == "staged",
                        )
                        if not primary.ready_primary:
                            raise CutoverError(
                                "shadow_gate",
                                "primary_gate_not_ready",
                                reason_codes=primary.reason_codes,
                            )
                        journal.advance(
                            "shadow_passed",
                            gates={
                                "shadow": {"status": "passed", "digest": shadow.digest()},
                                "primary_gate": {
                                    "status": "passed",
                                    "digest": primary.digest(),
                                },
                            },
                            counts={
                                "projection_lag": lag_after.projection_lag,
                                "shadow_comparisons": shadow.comparisons,
                            },
                            clock=clock,
                        )
                    steps_completed.append("shadow_gate")

                    # Step 8: the atomic persistent compat switch.
                    with _step("persist_compat", "compat_persist_failed"):
                        compat_hash = compat_persister(config)
                        journal.advance(
                            "compat_persisted",
                            hashes={"compat_config_sha256": compat_hash},
                            clock=clock,
                        )
                    steps_completed.append("persist_compat")

                    # Step 9: CAS-apply Codex primary and verify both sources.
                    try:
                        apply_result = editor.apply_primary(snapshot)
                    except CodexStanzaDriftError as exc:
                        raise CutoverError(
                            "codex_primary", "codex_stanza_drift"
                        ) from exc
                    except CodexConfigError as exc:
                        raise CutoverError(
                            "codex_primary", "codex_primary_apply_failed"
                        ) from exc
                    with _step("codex_primary", "codex_primary_verify_failed"):
                        current = editor.snapshot()
                        env = current.stanza.get("env")
                        if not isinstance(env, Mapping) or (
                            env.get("EVOLVMEM_CONTEXT_MODE") != "primary"
                            or env.get("EVOLVMEM_ADAPTER") != "codex"
                        ):
                            raise CutoverError(
                                "codex_primary", "codex_primary_verify_failed"
                            )
                        if current.stanza.get("default_tools_approval_mode") != "writes":
                            # The TOML-only approval field; the CLI never echoes it.
                            raise CutoverError(
                                "codex_primary", "codex_primary_verify_failed"
                            )
                        try:
                            cli_result = cli_probe()
                        except CutoverError:
                            raise
                        except Exception as exc:
                            raise CutoverError(
                                "codex_primary", "codex_cli_probe_failed"
                            ) from exc
                        verification = verify_cli_matches_stanza(
                            cli_result, current.stanza
                        )
                        if not verification.ok:
                            raise CutoverError("codex_primary", "codex_cli_mismatch")
                        journal.advance(
                            "codex_primary",
                            hashes={
                                "codex_primary_stanza_sha256": apply_result.after_stanza_sha256
                            },
                            clock=clock,
                        )
                    steps_completed.append("codex_primary")
            except CutoverLockTimeout as exc:
                raise CutoverError(
                    "lock_and_rerun_preflight", "cutover_lock_timeout"
                ) from exc
        # Step 10: the lock is released; only then the journal marks the
        # operation as awaiting the post-cutover canary.
        with _step("release_and_await_canary", "journal_failed"):
            journal.advance("awaiting_post_cutover_canary", clock=clock)
        steps_completed.append("release_and_await_canary")
        return outcome("awaiting_post_cutover_canary", "", ())
    except CutoverError as exc:
        return fail(exc)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass  # closing must never mask the recorded outcome


def _mark_terminal(journal, state: str, **kwargs) -> None:
    """Best-effort terminal journal mark; journaling never masks the outcome."""
    try:
        if state == "failed":
            journal.mark_failed(**kwargs)
        else:
            journal.mark_rolled_back(**kwargs)
    except Exception:
        pass  # the outcome carries the failure even if the journal write fails
