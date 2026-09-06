"""Typed legacy mutation contracts for the ContextService compatibility boundary.

Adapters keep their old memory_* shapes; the service boundary speaks only in
these immutable, validated request/result types. Legacy IDs remain the old
API IDs; Context IDs and layer availability ride along so callers can adopt
them at their own pace. Tags normalize to tuples here; MCP/Web convert to and
from their old shapes.
"""

from dataclasses import dataclass

from evolvmem.context_models import (
    ContextLayer,
    ContextStatus,
    ContextValidationError,
    _normalize_layers,
    _normalize_tags,
    _normalize_text,
    _validate_number,
    _validate_positive_int,
)


_LEGACY_TIERS = frozenset({"pinned", "normal", "reference"})


def _validate_tier(value: object) -> str:
    if not isinstance(value, str):
        raise ContextValidationError("tier must be a string")
    tier = value.strip().lower()
    if tier not in _LEGACY_TIERS:
        raise ContextValidationError("tier must be one of pinned, normal, reference")
    return tier


def _validate_optional_tier(value: object) -> str | None:
    if value is None:
        return None
    return _validate_tier(value)


def _validate_expires_at(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContextValidationError("expires_at must be a string or None")
    return value.strip() or None


def _validate_optional_number(
    value: object, field_name: str, *, lower: float, upper: float
) -> float | None:
    if value is None:
        return None
    _validate_number(value, field_name, lower=lower, upper=upper)
    return value  # type: ignore[return-value]


def _validate_id_tuple(value: object, field_name: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise ContextValidationError(
            f"{field_name} must be an iterable of positive integers"
        )
    try:
        ids = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            f"{field_name} must be an iterable of positive integers"
        ) from exc
    for item_id in ids:
        _validate_positive_int(item_id, field_name)
    return ids  # type: ignore[return-value]


def _normalize_write_payload(instance: object) -> None:
    """Shared key/value/metadata normalization for write payloads.

    Covers the fields LegacyAddRequest and LegacyExtractionItem have in
    common; callers normalize any remaining fields (e.g. source_session)
    themselves.
    """
    key = _normalize_text(instance.key, "key")  # type: ignore[attr-defined]
    if not key:
        raise ContextValidationError("key must not be empty")
    object.__setattr__(instance, "key", key)
    value = _normalize_text(instance.value, "value")  # type: ignore[attr-defined]
    if not value:
        raise ContextValidationError("value must not be empty")
    object.__setattr__(instance, "value", value)
    object.__setattr__(
        instance,
        "attribute",
        _normalize_text(instance.attribute, "attribute"),  # type: ignore[attr-defined]
    )
    object.__setattr__(
        instance, "tags", _normalize_tags(instance.tags)  # type: ignore[attr-defined]
    )
    _validate_number(
        instance.importance, "importance", lower=1.0, upper=10.0  # type: ignore[attr-defined]
    )
    object.__setattr__(
        instance, "tier", _validate_tier(instance.tier)  # type: ignore[attr-defined]
    )
    object.__setattr__(
        instance,
        "expires_at",
        _validate_expires_at(instance.expires_at),  # type: ignore[attr-defined]
    )
    _validate_optional_number(
        instance.confidence, "confidence", lower=0.0, upper=1.0  # type: ignore[attr-defined]
    )
    _normalize_transient_project_fields(instance)


def _normalize_transient_project_fields(instance: object) -> None:
    """Normalize the transient project-resolution fields of one write payload.

    ``workspace_path``/``project_hint`` are request-local signals for the
    typed-write project resolver: the path is fingerprinted and discarded
    inside the service, and neither field is ever persisted.
    """
    object.__setattr__(
        instance,
        "workspace_path",
        _normalize_text(instance.workspace_path, "workspace_path"),  # type: ignore[attr-defined]
    )
    object.__setattr__(
        instance,
        "project_hint",
        _normalize_text(instance.project_hint, "project_hint"),  # type: ignore[attr-defined]
    )


@dataclass(frozen=True, slots=True)
class LegacyAddRequest:
    """One legacy memory_add payload plus optional extractor confidence.

    ``workspace_path``/``project_hint`` are transient resolution signals:
    consumed in memory by the service, never written to any table.
    """

    key: str
    value: str
    attribute: str = ""
    tags: tuple[str, ...] = ()
    source_session: str = ""
    importance: float = 5.0
    tier: str = "normal"
    expires_at: str | None = None
    confidence: float | None = None
    workspace_path: str = ""
    project_hint: str = ""

    def __post_init__(self) -> None:
        _normalize_write_payload(self)
        object.__setattr__(
            self,
            "source_session",
            _normalize_text(self.source_session, "source_session"),
        )


@dataclass(frozen=True, slots=True)
class LegacyReplaceRequest:
    """One legacy memory_replace payload; None fields inherit the old row.

    ``workspace_path``/``project_hint`` are transient resolution signals:
    consumed in memory by the service, never written to any table.
    """

    key: str
    new_value: str
    attribute: str | None = None
    tags: tuple[str, ...] | None = None
    source_session: str = ""
    importance: float | None = None
    tier: str | None = None
    expires_at: str | None = None
    confidence: float | None = None
    workspace_path: str = ""
    project_hint: str = ""

    def __post_init__(self) -> None:
        key = _normalize_text(self.key, "key")
        if not key:
            raise ContextValidationError("key must not be empty")
        object.__setattr__(self, "key", key)
        new_value = _normalize_text(self.new_value, "new_value")
        if not new_value:
            raise ContextValidationError("new_value must not be empty")
        object.__setattr__(self, "new_value", new_value)
        if self.attribute is not None:
            object.__setattr__(
                self, "attribute", _normalize_text(self.attribute, "attribute")
            )
        if self.tags is not None:
            object.__setattr__(self, "tags", _normalize_tags(self.tags))
        object.__setattr__(
            self,
            "source_session",
            _normalize_text(self.source_session, "source_session"),
        )
        object.__setattr__(
            self,
            "importance",
            _validate_optional_number(
                self.importance, "importance", lower=1.0, upper=10.0
            ),
        )
        object.__setattr__(self, "tier", _validate_optional_tier(self.tier))
        object.__setattr__(self, "expires_at", _validate_expires_at(self.expires_at))
        _validate_optional_number(self.confidence, "confidence", lower=0.0, upper=1.0)
        _normalize_transient_project_fields(self)


@dataclass(frozen=True, slots=True)
class LegacyRemoveRequest:
    """Soft-delete one legacy projection row and its mapped ContextItem."""

    legacy_id: int

    def __post_init__(self) -> None:
        _validate_positive_int(self.legacy_id, "legacy_id")


@dataclass(frozen=True, slots=True)
class LegacyUpdateRequest:
    """In-place metadata edit; None fields keep their stored values.

    attribute/tags follow the LegacyReplaceRequest optional-field
    conventions; when present they move the mapped ContextItem's derived
    content_type/scope/tags along in the same transaction.
    """

    legacy_id: int
    importance: float | None = None
    tier: str | None = None
    attribute: str | None = None
    tags: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _validate_positive_int(self.legacy_id, "legacy_id")
        object.__setattr__(
            self,
            "importance",
            _validate_optional_number(
                self.importance, "importance", lower=1.0, upper=10.0
            ),
        )
        object.__setattr__(self, "tier", _validate_optional_tier(self.tier))
        if self.attribute is not None:
            object.__setattr__(
                self, "attribute", _normalize_text(self.attribute, "attribute")
            )
        if self.tags is not None:
            object.__setattr__(self, "tags", _normalize_tags(self.tags))


@dataclass(frozen=True, slots=True)
class LegacyStatusRequest:
    """Archive or restore one legacy row and its mapped ContextItem."""

    legacy_id: int

    def __post_init__(self) -> None:
        _validate_positive_int(self.legacy_id, "legacy_id")


@dataclass(frozen=True, slots=True)
class LegacyHardDeleteRequest:
    """Physically remove the exact mapping/projection/ContextItem triple."""

    legacy_id: int

    def __post_init__(self) -> None:
        _validate_positive_int(self.legacy_id, "legacy_id")


@dataclass(frozen=True, slots=True)
class LegacyAccessRequest:
    """Batched access accounting; duplicate IDs collapse to one increment."""

    legacy_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        ids = _validate_id_tuple(self.legacy_ids, "legacy_ids")
        object.__setattr__(self, "legacy_ids", tuple(dict.fromkeys(ids)))


@dataclass(frozen=True, slots=True)
class LegacyMutationResult:
    """Old-API-compatible outcome of one typed legacy mutation.

    ``legacy_id`` is None only for extraction writes quarantined as Core
    candidates (no legacy projection row exists for them); every other
    mutation keeps the old positive-int contract. ``context_status`` marks
    that quarantine explicitly and stays None everywhere else.
    """

    legacy_id: int | None
    context_id: int | None
    old_legacy_id: int | None
    old_context_id: int | None
    available_layers: tuple[ContextLayer, ...]
    changed: bool
    context_status: str | None = None

    def __post_init__(self) -> None:
        if self.legacy_id is not None:
            _validate_positive_int(self.legacy_id, "legacy_id")
        for name in ("context_id", "old_legacy_id", "old_context_id"):
            value = getattr(self, name)
            if value is not None:
                _validate_positive_int(value, name)
        object.__setattr__(
            self,
            "available_layers",
            _normalize_layers(self.available_layers, "available_layers"),
        )
        if type(self.changed) is not bool:
            raise ContextValidationError("changed must be a boolean")
        if self.context_status is not None:
            try:
                ContextStatus(self.context_status)
            except ValueError as exc:
                raise ContextValidationError(
                    "context_status must be a valid ContextStatus value or None"
                ) from exc


@dataclass(frozen=True, slots=True)
class LegacyAccessResult:
    """Which mapped sides actually received an access increment."""

    updated_legacy_ids: tuple[int, ...]
    updated_context_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "updated_legacy_ids",
            _validate_id_tuple(self.updated_legacy_ids, "updated_legacy_ids"),
        )
        object.__setattr__(
            self,
            "updated_context_ids",
            _validate_id_tuple(self.updated_context_ids, "updated_context_ids"),
        )


@dataclass(frozen=True, slots=True)
class LegacyExtractionItem:
    """One extraction-batch payload: the session summary or one candidate.

    ``workspace_path``/``project_hint`` are transient resolution signals:
    consumed in memory by the service, never written to any table.
    """

    key: str
    value: str
    attribute: str = ""
    tags: tuple[str, ...] = ()
    importance: float = 5.0
    tier: str = "normal"
    expires_at: str | None = None
    confidence: float | None = None
    workspace_path: str = ""
    project_hint: str = ""
    experience_case: dict | None = None

    def __post_init__(self) -> None:
        _normalize_write_payload(self)
        if self.experience_case is not None and not isinstance(self.experience_case, dict):
            raise ContextValidationError("experience_case must be a dict")


@dataclass(frozen=True, slots=True)
class LegacyExtractionRequest:
    """One extraction batch: the summary plus policy-gated atomic candidates.

    Message parsing, redaction, ranking, and the deterministic policy gates
    stay in the adapters; everything listed here is already safe to write.
    ``max_writes`` bounds actual candidate writes — skipped duplicates never
    consume the quota. Request-level ``workspace_path``/``project_hint`` are
    transient resolution defaults an item can override; never persisted.
    """

    summary: LegacyExtractionItem
    candidates: tuple[LegacyExtractionItem, ...] = ()
    max_writes: int = 8
    source_session: str = ""
    workspace_path: str = ""
    project_hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.summary, LegacyExtractionItem):
            raise ContextValidationError(
                "summary must be a LegacyExtractionItem instance"
            )
        if isinstance(self.candidates, (str, bytes)):
            raise ContextValidationError(
                "candidates must be an iterable of LegacyExtractionItem"
            )
        try:
            candidates = tuple(self.candidates)
        except TypeError as exc:
            raise ContextValidationError(
                "candidates must be an iterable of LegacyExtractionItem"
            ) from exc
        for candidate in candidates:
            if not isinstance(candidate, LegacyExtractionItem):
                raise ContextValidationError(
                    "candidates must be an iterable of LegacyExtractionItem"
                )
        object.__setattr__(self, "candidates", candidates)
        _validate_positive_int(self.max_writes, "max_writes")
        object.__setattr__(
            self,
            "source_session",
            _normalize_text(self.source_session, "source_session"),
        )
        _normalize_transient_project_fields(self)


@dataclass(frozen=True, slots=True)
class LegacyExtractionResult:
    """Ordered batch outcome; ``persisted`` counts actual new legacy IDs.

    ``summary`` is None when an equivalent active summary already satisfied
    the batch; ``candidates`` holds one result per candidate actually written,
    in request order — skipped duplicates never appear.
    """

    summary: LegacyMutationResult | None
    candidates: tuple[LegacyMutationResult, ...]
    persisted: int

    def __post_init__(self) -> None:
        if self.summary is not None and not isinstance(
            self.summary, LegacyMutationResult
        ):
            raise ContextValidationError(
                "summary must be a LegacyMutationResult or None"
            )
        if isinstance(self.candidates, (str, bytes)):
            raise ContextValidationError(
                "candidates must be an iterable of LegacyMutationResult"
            )
        try:
            candidates = tuple(self.candidates)
        except TypeError as exc:
            raise ContextValidationError(
                "candidates must be an iterable of LegacyMutationResult"
            ) from exc
        for candidate in candidates:
            if not isinstance(candidate, LegacyMutationResult):
                raise ContextValidationError(
                    "candidates must be an iterable of LegacyMutationResult"
                )
        object.__setattr__(self, "candidates", candidates)
        if type(self.persisted) is not int or self.persisted < 0:
            raise ContextValidationError(
                "persisted must be a non-negative integer"
            )
        written = (1 if self.summary is not None else 0) + len(candidates)
        if self.persisted != written:
            raise ContextValidationError(
                "persisted must equal the number of actual writes"
            )
