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


@dataclass(frozen=True, slots=True)
class LegacyAddRequest:
    """One legacy memory_add payload plus optional extractor confidence."""

    key: str
    value: str
    attribute: str = ""
    tags: tuple[str, ...] = ()
    source_session: str = ""
    importance: float = 5.0
    tier: str = "normal"
    expires_at: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        key = _normalize_text(self.key, "key")
        if not key:
            raise ContextValidationError("key must not be empty")
        object.__setattr__(self, "key", key)
        value = _normalize_text(self.value, "value")
        if not value:
            raise ContextValidationError("value must not be empty")
        object.__setattr__(self, "value", value)
        object.__setattr__(
            self, "attribute", _normalize_text(self.attribute, "attribute")
        )
        object.__setattr__(self, "tags", _normalize_tags(self.tags))
        object.__setattr__(
            self,
            "source_session",
            _normalize_text(self.source_session, "source_session"),
        )
        _validate_number(self.importance, "importance", lower=1.0, upper=10.0)
        object.__setattr__(self, "tier", _validate_tier(self.tier))
        object.__setattr__(self, "expires_at", _validate_expires_at(self.expires_at))
        _validate_optional_number(self.confidence, "confidence", lower=0.0, upper=1.0)


@dataclass(frozen=True, slots=True)
class LegacyReplaceRequest:
    """One legacy memory_replace payload; None fields inherit the old row."""

    key: str
    new_value: str
    attribute: str | None = None
    tags: tuple[str, ...] | None = None
    source_session: str = ""
    importance: float | None = None
    tier: str | None = None
    expires_at: str | None = None
    confidence: float | None = None

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


@dataclass(frozen=True, slots=True)
class LegacyRemoveRequest:
    """Soft-delete one legacy projection row and its mapped ContextItem."""

    legacy_id: int

    def __post_init__(self) -> None:
        _validate_positive_int(self.legacy_id, "legacy_id")


@dataclass(frozen=True, slots=True)
class LegacyUpdateRequest:
    """In-place importance/tier edit; None fields keep their stored values."""

    legacy_id: int
    importance: float | None = None
    tier: str | None = None

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
    """Old-API-compatible outcome of one typed legacy mutation."""

    legacy_id: int
    context_id: int | None
    old_legacy_id: int | None
    old_context_id: int | None
    available_layers: tuple[ContextLayer, ...]
    changed: bool

    def __post_init__(self) -> None:
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
