"""Typed, database-free domain models for layered context items."""

from dataclasses import dataclass
from enum import Enum
import math


class ContextValidationError(ValueError):
    """Raised when caller-provided context data violates its domain contract."""


class ContextContentType(str, Enum):
    DECISION = "decision"
    FACT = "fact"
    EXPERIENCE = "experience"
    PLAYBOOK = "playbook"
    WORKFLOW_POLICY = "workflow_policy"
    CONSTRAINT = "constraint"
    PREFERENCE = "preference"
    USER_PROFILE = "user_profile"
    REFERENCE = "reference"
    SESSION_SUMMARY = "session_summary"


class ContextStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ContextScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class ContextTier(str, Enum):
    PINNED = "pinned"
    NORMAL = "normal"
    REFERENCE = "reference"


class ContextLayer(str, Enum):
    L0 = "l0"
    L1 = "l1"
    L2 = "l2"


@dataclass(frozen=True, slots=True)
class ContextLayers:
    l0: str
    l1: str
    l2: str
    generator: str


@dataclass(frozen=True, slots=True)
class ContextItemDraft:
    identity_key: str
    content_type: ContextContentType
    layers: ContextLayers
    project: str = ""
    scope: ContextScope = ContextScope.PROJECT
    status: ContextStatus = ContextStatus.CANDIDATE
    tier: ContextTier = ContextTier.NORMAL
    tags: tuple[str, ...] = ()
    importance: float = 5.0
    confidence: float = 0.5
    expires_at: str | None = None
    supersedes: int | None = None

    def __post_init__(self) -> None:
        identity_key = _normalize_text(self.identity_key, "identity_key")
        if not identity_key:
            raise ContextValidationError("identity_key must not be empty")
        object.__setattr__(self, "identity_key", identity_key)

        if not isinstance(self.layers, ContextLayers):
            raise ContextValidationError("layers must be a ContextLayers instance")
        layers = ContextLayers(
            l0=_normalize_text(self.layers.l0, "l0"),
            l1=_normalize_text(self.layers.l1, "l1"),
            l2=_normalize_text(self.layers.l2, "l2"),
            generator=self.layers.generator,
        )
        for name in ("l0", "l1", "l2"):
            if not getattr(layers, name):
                raise ContextValidationError(f"{name} must not be empty")
        object.__setattr__(self, "layers", layers)

        object.__setattr__(self, "tags", _normalize_tags(self.tags))
        _validate_number(self.importance, "importance", lower=1.0, upper=10.0)
        _validate_number(self.confidence, "confidence", lower=0.0, upper=1.0)


@dataclass(frozen=True, slots=True)
class ContextItem:
    id: int
    identity_key: str
    content_type: ContextContentType
    project: str
    scope: ContextScope
    status: ContextStatus
    tier: ContextTier
    tags: tuple[str, ...]
    importance: float
    confidence: float
    source_state: str
    source_count: int
    success_count: int
    failure_count: int
    access_count: int
    last_accessed: str | None
    last_verified_at: str | None
    expires_at: str | None
    supersedes: int | None
    superseded_by: int | None
    created_at: str
    updated_at: str
    layers: ContextLayers | None


@dataclass(frozen=True, slots=True)
class ContextSearchHit:
    item_id: int
    score: float
    match_layers: tuple[ContextLayer, ...]
    content_type: ContextContentType
    project: str
    status: ContextStatus


@dataclass(frozen=True, slots=True)
class ContextVectorDocument:
    item_id: int
    l0: str


def _normalize_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ContextValidationError(f"{field_name} must be a string")
    return value.replace("\r\n", "\n").strip()


def _normalize_tags(tags: object) -> tuple[str, ...]:
    if isinstance(tags, str):
        raise ContextValidationError("tags must be an iterable of strings")
    try:
        normalized = tuple(_normalize_text(tag, "tag") for tag in tags)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError("tags must be an iterable of strings") from exc
    return tuple(dict.fromkeys(tag for tag in normalized if tag))


def _validate_number(value: object, field_name: str, *, lower: float, upper: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ContextValidationError(f"{field_name} must be between {lower:g} and {upper:g}")
