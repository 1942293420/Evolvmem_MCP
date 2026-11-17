"""Immutable, privacy-safe report models for the Context Core cutover gates.

Every report is a frozen dataclass. ``public_dict()`` projects only the
allowed public summary vocabulary — schema/version markers, counts,
booleans, reason codes, sizes, checksum prefixes, and durations — and
``validate_public_summary()`` enforces that no key or value resembles
memory content, a query, an archive payload, a secret, or an absolute
path. ``digest()`` is the SHA-256 of the canonical JSON public summary, so
reports can be linked into backup manifests without leaking detail.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import ClassVar, Mapping

from evolvmem.context_models import ContextValidationError


_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_PUBLIC_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CHECKSUM_PREFIX_PATTERN = re.compile(r"^$|^[0-9a-f]{8,64}$")
_MAX_PUBLIC_STRING_CHARS = 512

# Keys that could carry memory content, queries, archive payloads, secrets,
# or filesystem locations are never part of a public summary.
_PUBLIC_KEY_DENYLIST = frozenset(
    {
        "query",
        "queries",
        "content",
        "value",
        "body",
        "text",
        "payload",
        "archive_payload",
        "secret",
        "secrets",
        "token",
        "api_key",
        "password",
        "stanza",
        "env",
        "command",
        "args",
        "path",
        "paths",
        "db_path",
        "config_path",
        "vector_path",
        "context_vector_path",
        "data_dir",
        "cwd",
        "key",
        "keys",
        "identity_key",
        "tags",
        "l0",
        "l1",
        "l2",
    }
)


def validate_public_summary(summary: Mapping) -> None:
    """Reject public serializations that could leak content or locations.

    Keys must be lower-snake names outside the sensitive denylist; string
    values must be single-line and free of path separators so no absolute
    path (or content fragment shaped like one) can ride along.
    """
    if not isinstance(summary, Mapping):
        raise ContextValidationError("public summary must be a mapping")
    for key, value in summary.items():
        _validate_public_key(key)
        _validate_public_value(value, key)


def _validate_public_key(key: object) -> None:
    if not isinstance(key, str) or not _PUBLIC_KEY_PATTERN.match(key):
        raise ContextValidationError(f"invalid public summary key: {key!r}")
    if key in _PUBLIC_KEY_DENYLIST:
        raise ContextValidationError(f"sensitive public summary key: {key!r}")


def _validate_public_value(value: object, field_name: str) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if value < 0:
            raise ContextValidationError(f"{field_name} must be non-negative")
        return
    if type(value) is float:
        if not math.isfinite(value) or value < 0.0:
            raise ContextValidationError(f"{field_name} must be a non-negative finite number")
        return
    if isinstance(value, str):
        _validate_public_string(value, field_name)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_public_value(item, field_name)
        return
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            _validate_public_key(nested_key)
            _validate_public_value(nested_value, nested_key)
        return
    raise ContextValidationError(
        f"{field_name} must be a count, boolean, code, size, prefix, or duration"
    )


def _validate_public_string(value: str, field_name: str) -> None:
    if "\n" in value or "\r" in value:
        raise ContextValidationError(f"{field_name} must be a single-line string")
    if len(value) > _MAX_PUBLIC_STRING_CHARS:
        raise ContextValidationError(f"{field_name} exceeds the public string budget")
    # No path separators at all: absolute paths (POSIX or Windows) and
    # content fragments containing them cannot survive this rule.
    if "/" in value or "\\" in value:
        raise ContextValidationError(f"{field_name} must not contain a path separator")


def _canonical_digest(public: Mapping) -> str:
    payload = json.dumps(
        public, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise ContextValidationError(f"{field_name} must be a boolean")


def _require_optional_bool(value: object, field_name: str) -> None:
    if value is not None and type(value) is not bool:
        raise ContextValidationError(f"{field_name} must be a boolean or None")


def _require_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ContextValidationError(f"{field_name} must be a non-negative integer")


def _require_optional_non_negative_int(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_negative_int(value, field_name)


def _require_ratio(value: object, field_name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ContextValidationError(f"{field_name} must be between 0 and 1")


def _require_duration(value: object) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ContextValidationError("duration_ms must be a non-negative finite number")


def _require_reason_codes(value: object, field_name: str = "reason_codes") -> tuple[str, ...]:
    try:
        codes = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(f"{field_name} must be an iterable of reason codes") from exc
    for code in codes:
        if not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code):
            raise ContextValidationError(
                f"{field_name} must contain only lower-snake reason codes"
            )
    return codes


def _require_diagnostics(value: object) -> tuple[str, ...]:
    try:
        messages = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            "config_diagnostics must be an iterable of safe diagnostic strings"
        ) from exc
    for message in messages:
        if not isinstance(message, str):
            raise ContextValidationError(
                "config_diagnostics must be an iterable of safe diagnostic strings"
            )
        _validate_public_string(message, "config_diagnostics")
    return messages


def _require_checksum_prefix(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _CHECKSUM_PREFIX_PATTERN.match(value):
        raise ContextValidationError(
            f"{field_name} must be a lowercase hex checksum prefix"
        )


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


class _PublicReport:
    """Shared canonical digest over the validated public projection."""

    def public_dict(self) -> dict:  # pragma: no cover - interface declaration
        raise NotImplementedError

    def digest(self) -> str:
        """SHA-256 over the canonical JSON of ``public_dict()``."""
        return _canonical_digest(self.public_dict())


@dataclass(frozen=True, slots=True)
class CutoverPreflightReport(_PublicReport):
    """Side-effect-free preflight outcome; all fields are public-safe."""

    SCHEMA: ClassVar[str] = "evolvmem.cutover_preflight"
    VERSION: ClassVar[int] = 1

    ready: bool
    config_ok: bool
    database_ok: bool
    schema_ok: bool
    vector_ok: bool
    codex_ok: bool
    space_ok: bool
    reason_codes: tuple[str, ...]
    config_diagnostics: tuple[str, ...]
    legacy_schema_present: bool
    legacy_rows: int
    legacy_status_counts: dict[str, int]
    duplicate_active_count: int
    context_schema_present: bool
    context_items: int
    db_size_bytes: int
    db_sha256_prefix: str
    old_vector_present: bool
    old_vector_size_bytes: int
    old_vector_sha256_prefix: str
    old_vector_count: int | None
    old_vector_dimension: int | None
    old_vector_dirty: bool
    free_space_bytes: int
    required_space_bytes: int
    backup_parent_writable: bool
    codex_stanza_present: bool
    codex_context_tools_ok: bool
    codex_config_sha256_prefix: str
    duration_ms: float

    def __post_init__(self) -> None:
        for name in (
            "ready",
            "config_ok",
            "database_ok",
            "schema_ok",
            "vector_ok",
            "codex_ok",
            "space_ok",
            "legacy_schema_present",
            "context_schema_present",
            "old_vector_present",
            "old_vector_dirty",
            "backup_parent_writable",
            "codex_stanza_present",
            "codex_context_tools_ok",
        ):
            _require_bool(getattr(self, name), name)
        object.__setattr__(self, "reason_codes", _require_reason_codes(self.reason_codes))
        object.__setattr__(
            self, "config_diagnostics", _require_diagnostics(self.config_diagnostics)
        )
        for name in (
            "legacy_rows",
            "duplicate_active_count",
            "context_items",
            "db_size_bytes",
            "old_vector_size_bytes",
            "free_space_bytes",
            "required_space_bytes",
        ):
            _require_non_negative_int(getattr(self, name), name)
        object.__setattr__(
            self, "legacy_status_counts", _require_status_counts(self.legacy_status_counts)
        )
        _require_optional_non_negative_int(self.old_vector_count, "old_vector_count")
        _require_optional_non_negative_int(
            self.old_vector_dimension, "old_vector_dimension"
        )
        for name in ("db_sha256_prefix", "old_vector_sha256_prefix", "codex_config_sha256_prefix"):
            _require_checksum_prefix(getattr(self, name), name)
        _require_duration(self.duration_ms)
        object.__setattr__(self, "duration_ms", float(self.duration_ms))

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "ready": self.ready,
            "config_ok": self.config_ok,
            "database_ok": self.database_ok,
            "schema_ok": self.schema_ok,
            "vector_ok": self.vector_ok,
            "codex_ok": self.codex_ok,
            "space_ok": self.space_ok,
            "reason_codes": list(self.reason_codes),
            "config_diagnostics": list(self.config_diagnostics),
            "legacy_schema_present": self.legacy_schema_present,
            "legacy_rows": self.legacy_rows,
            "legacy_status_counts": dict(self.legacy_status_counts),
            "duplicate_active_count": self.duplicate_active_count,
            "context_schema_present": self.context_schema_present,
            "context_items": self.context_items,
            "db_size_bytes": self.db_size_bytes,
            "db_sha256_prefix": self.db_sha256_prefix,
            "old_vector_present": self.old_vector_present,
            "old_vector_size_bytes": self.old_vector_size_bytes,
            "old_vector_sha256_prefix": self.old_vector_sha256_prefix,
            "old_vector_count": self.old_vector_count,
            "old_vector_dimension": self.old_vector_dimension,
            "old_vector_dirty": self.old_vector_dirty,
            "free_space_bytes": self.free_space_bytes,
            "required_space_bytes": self.required_space_bytes,
            "backup_parent_writable": self.backup_parent_writable,
            "codex_stanza_present": self.codex_stanza_present,
            "codex_context_tools_ok": self.codex_context_tools_ok,
            "codex_config_sha256_prefix": self.codex_config_sha256_prefix,
        }
        validate_public_summary(public)
        return public


@dataclass(frozen=True, slots=True)
class ProjectionLagReport(_PublicReport):
    """Per-class projection/ Core divergence counts; vector health is separate."""

    SCHEMA: ClassVar[str] = "evolvmem.projection_lag"
    VERSION: ClassVar[int] = 1

    projection_lag: int
    missing_mapping: int
    duplicate_mapping_target: int
    layer_mismatch: int
    status_mismatch: int
    l1_mismatch: int
    supersession_mismatch: int
    orphan_mapping: int
    dangling_item_mapping: int
    legacy_rows: int
    mapping_rows: int
    legacy_vector_count: int | None
    legacy_vector_dirty: bool | None
    context_vector_count: int | None
    context_vector_dirty: bool | None
    duration_ms: float

    def __post_init__(self) -> None:
        for name in (
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
        ):
            _require_non_negative_int(getattr(self, name), name)
        if self.projection_lag != (
            self.missing_mapping
            + self.duplicate_mapping_target
            + self.layer_mismatch
            + self.status_mismatch
            + self.l1_mismatch
            + self.supersession_mismatch
            + self.orphan_mapping
            + self.dangling_item_mapping
        ):
            raise ContextValidationError(
                "projection_lag must equal the sum of its mismatch classes"
            )
        _require_optional_non_negative_int(self.legacy_vector_count, "legacy_vector_count")
        _require_optional_non_negative_int(self.context_vector_count, "context_vector_count")
        _require_optional_bool(self.legacy_vector_dirty, "legacy_vector_dirty")
        _require_optional_bool(self.context_vector_dirty, "context_vector_dirty")
        _require_duration(self.duration_ms)
        object.__setattr__(self, "duration_ms", float(self.duration_ms))

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "projection_lag": self.projection_lag,
            "missing_mapping": self.missing_mapping,
            "duplicate_mapping_target": self.duplicate_mapping_target,
            "layer_mismatch": self.layer_mismatch,
            "status_mismatch": self.status_mismatch,
            "l1_mismatch": self.l1_mismatch,
            "supersession_mismatch": self.supersession_mismatch,
            "orphan_mapping": self.orphan_mapping,
            "dangling_item_mapping": self.dangling_item_mapping,
            "legacy_rows": self.legacy_rows,
            "mapping_rows": self.mapping_rows,
            "legacy_vector_count": self.legacy_vector_count,
            "legacy_vector_dirty": self.legacy_vector_dirty,
            "context_vector_count": self.context_vector_count,
            "context_vector_dirty": self.context_vector_dirty,
        }
        validate_public_summary(public)
        return public


@dataclass(frozen=True, slots=True)
class ShadowComparison(_PublicReport):
    """Content-free comparison of one legacy/Core retrieval pair after mapping."""

    SCHEMA: ClassVar[str] = "evolvmem.shadow_comparison"
    VERSION: ClassVar[int] = 1

    expected_relevant: int
    legacy_count: int
    core_count: int
    mapped_legacy_count: int
    unmapped_legacy_count: int
    below_threshold_excluded: int
    top1_match: bool
    overlap_at_5: float

    def __post_init__(self) -> None:
        for name in (
            "expected_relevant",
            "legacy_count",
            "core_count",
            "mapped_legacy_count",
            "unmapped_legacy_count",
            "below_threshold_excluded",
        ):
            _require_non_negative_int(getattr(self, name), name)
        if self.mapped_legacy_count + self.unmapped_legacy_count != self.legacy_count:
            raise ContextValidationError(
                "mapped and unmapped legacy counts must add up to legacy_count"
            )
        _require_bool(self.top1_match, "top1_match")
        _require_ratio(self.overlap_at_5, "overlap_at_5")
        object.__setattr__(self, "overlap_at_5", float(self.overlap_at_5))

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "expected_relevant": self.expected_relevant,
            "legacy_count": self.legacy_count,
            "core_count": self.core_count,
            "mapped_legacy_count": self.mapped_legacy_count,
            "unmapped_legacy_count": self.unmapped_legacy_count,
            "below_threshold_excluded": self.below_threshold_excluded,
            "top1_match": self.top1_match,
            "overlap_at_5": self.overlap_at_5,
        }
        validate_public_summary(public)
        return public


@dataclass(frozen=True, slots=True)
class ShadowGateReport(_PublicReport):
    """Aggregate shadow verdict against the design acceptance thresholds."""

    SCHEMA: ClassVar[str] = "evolvmem.shadow_gate"
    VERSION: ClassVar[int] = 1

    comparisons: int
    exact_total: int
    exact_top1_matches: int
    semantic_total: int
    semantic_overlap_passes: int
    thresholds_met: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "comparisons",
            "exact_total",
            "exact_top1_matches",
            "semantic_total",
            "semantic_overlap_passes",
        ):
            _require_non_negative_int(getattr(self, name), name)
        if self.comparisons != self.exact_total + self.semantic_total:
            raise ContextValidationError(
                "comparisons must equal exact_total + semantic_total"
            )
        if self.exact_top1_matches > self.exact_total:
            raise ContextValidationError("exact_top1_matches cannot exceed exact_total")
        if self.semantic_overlap_passes > self.semantic_total:
            raise ContextValidationError(
                "semantic_overlap_passes cannot exceed semantic_total"
            )
        _require_bool(self.thresholds_met, "thresholds_met")
        object.__setattr__(self, "reason_codes", _require_reason_codes(self.reason_codes))

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "comparisons": self.comparisons,
            "exact_total": self.exact_total,
            "exact_top1_matches": self.exact_top1_matches,
            "semantic_total": self.semantic_total,
            "semantic_overlap_passes": self.semantic_overlap_passes,
            "thresholds_met": self.thresholds_met,
            "reason_codes": list(self.reason_codes),
        }
        validate_public_summary(public)
        return public


@dataclass(frozen=True, slots=True)
class PrimaryGateEvidence:
    """Measured inputs for ``verify_primary_gate``; evidence, never a verdict.

    Ceremony-only proofs (a measured second migration run, a journaled
    FTS-only approval, a shadow gate verdict) are explicit fields — callers
    must derive or measure them, never fabricate them.
    """

    quick_check_ok: bool
    legacy_rows_unmapped: int
    duplicate_mapping_targets: int
    mapped_items_with_wrong_layers: int
    second_migration_created: int
    projection_lag: int
    vector_healthy: bool
    fts_only_approved: bool
    shadow_thresholds_met: bool
    config_diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "quick_check_ok",
            "vector_healthy",
            "fts_only_approved",
            "shadow_thresholds_met",
        ):
            _require_bool(getattr(self, name), name)
        for name in (
            "legacy_rows_unmapped",
            "duplicate_mapping_targets",
            "mapped_items_with_wrong_layers",
            "second_migration_created",
            "projection_lag",
        ):
            _require_non_negative_int(getattr(self, name), name)
        object.__setattr__(
            self, "config_diagnostics", _require_diagnostics(self.config_diagnostics)
        )


@dataclass(frozen=True, slots=True)
class PrimaryGateReport(_PublicReport):
    """Reusable primary-invariant verdict; ready only when every check passes."""

    SCHEMA: ClassVar[str] = "evolvmem.primary_gate"
    VERSION: ClassVar[int] = 1

    ready_primary: bool
    quick_check_ok: bool
    mapping_complete: bool
    layers_complete: bool
    migration_idempotent: bool
    projection_lag_zero: bool
    vector_ready: bool
    shadow_thresholds_met: bool
    config_clean: bool
    legacy_rows_unmapped: int
    duplicate_mapping_targets: int
    mapped_items_with_wrong_layers: int
    second_migration_created: int
    projection_lag: int
    reason_codes: tuple[str, ...]
    config_diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "ready_primary",
            "quick_check_ok",
            "mapping_complete",
            "layers_complete",
            "migration_idempotent",
            "projection_lag_zero",
            "vector_ready",
            "shadow_thresholds_met",
            "config_clean",
        ):
            _require_bool(getattr(self, name), name)
        for name in (
            "legacy_rows_unmapped",
            "duplicate_mapping_targets",
            "mapped_items_with_wrong_layers",
            "second_migration_created",
            "projection_lag",
        ):
            _require_non_negative_int(getattr(self, name), name)
        object.__setattr__(self, "reason_codes", _require_reason_codes(self.reason_codes))
        object.__setattr__(
            self, "config_diagnostics", _require_diagnostics(self.config_diagnostics)
        )

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "ready_primary": self.ready_primary,
            "quick_check_ok": self.quick_check_ok,
            "mapping_complete": self.mapping_complete,
            "layers_complete": self.layers_complete,
            "migration_idempotent": self.migration_idempotent,
            "projection_lag_zero": self.projection_lag_zero,
            "vector_ready": self.vector_ready,
            "shadow_thresholds_met": self.shadow_thresholds_met,
            "config_clean": self.config_clean,
            "legacy_rows_unmapped": self.legacy_rows_unmapped,
            "duplicate_mapping_targets": self.duplicate_mapping_targets,
            "mapped_items_with_wrong_layers": self.mapped_items_with_wrong_layers,
            "second_migration_created": self.second_migration_created,
            "projection_lag": self.projection_lag,
            "reason_codes": list(self.reason_codes),
            "config_diagnostics": list(self.config_diagnostics),
        }
        validate_public_summary(public)
        return public
