"""Typed, database-free domain models for layered context items."""

from dataclasses import dataclass
from enum import Enum
import math


class ContextValidationError(ValueError):
    """Raised when caller-provided context data violates its domain contract."""


_CONTEXT_SERVICE_ERROR_CODES = frozenset(
    {
        "not_initialized",
        "invalid_mode",
        "initialize_conflict",
        "context_not_enabled",
        "degraded_legacy",
        # 续接就绪码：continuity schema 未建或 workspace identity key 不可用；
        # 允许经 ContextServiceStatus.reason_codes / ContextServiceError 承载
        "continuity_not_ready",
        "workspace_key_missing",
    }
)


class ContextServiceError(RuntimeError):
    """Typed service-boundary failure with a stable, content-free reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in _CONTEXT_SERVICE_ERROR_CODES:
            raise ContextValidationError(
                "code must be one of "
                + ", ".join(sorted(_CONTEXT_SERVICE_ERROR_CODES))
            )
        self.code = code
        super().__init__(message or code)


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
    PROJECT_SUMMARY = "project_summary"
    WORKSTREAM_CHECKPOINT = "workstream_checkpoint"


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


class ContextMode(str, Enum):
    LEGACY = "legacy"
    COMPAT = "compat"
    SHADOW = "shadow"
    PRIMARY = "primary"


def parse_context_mode(raw: object) -> ContextMode | None:
    """Parse a configured context mode; unknown values fail closed to None.

    Callers must treat None as "context features disabled" and keep their
    legacy path working — never coerce an unknown value to primary/shadow.
    """
    if not isinstance(raw, str):
        return None
    try:
        return ContextMode(raw)
    except ValueError:
        return None


class ContextMatchType(str, Enum):
    LEXICAL = "lexical"
    VECTOR = "vector"
    PINNED_POLICY = "pinned_policy"


class ContextSelectionReason(str, Enum):
    PINNED_POLICY = "pinned_policy"
    LEXICAL = "lexical"
    VECTOR = "vector"


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


@dataclass(frozen=True, slots=True)
class ContextScoreComponents:
    """Normalized 0..1 ranking components for one search candidate."""

    relevance: float
    project: float
    type_priority: float
    confidence: float
    importance: float
    evidence: float
    recency: float
    frequency: float

    def __post_init__(self) -> None:
        for name in (
            "relevance",
            "project",
            "type_priority",
            "confidence",
            "importance",
            "evidence",
            "recency",
            "frequency",
        ):
            _validate_number(
                getattr(self, name), f"score component {name}", lower=0.0, upper=1.0
            )


@dataclass(frozen=True, slots=True)
class ContextRetrievalRecord:
    """Metadata plus L0 for one candidate; never carries L1/L2 text."""

    item: ContextItem
    l0: str
    available_layers: tuple[ContextLayer, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.item, ContextItem):
            raise ContextValidationError("item must be a ContextItem instance")
        if not isinstance(self.l0, str):
            raise ContextValidationError("l0 must be a string")
        object.__setattr__(
            self,
            "available_layers",
            _normalize_layers(self.available_layers, "available_layers"),
        )


@dataclass(frozen=True, slots=True)
class ContextSearchRequest:
    query: str
    project: str = ""
    top_k: int = 10
    content_types: tuple[ContextContentType, ...] = ()
    cross_project: bool = False

    def __post_init__(self) -> None:
        query = _normalize_text(self.query, "query")
        if not query:
            raise ContextValidationError("query must not be empty")
        object.__setattr__(self, "query", query)
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        object.__setattr__(self, "project", self.project.strip())
        if type(self.top_k) is not int or not 1 <= self.top_k <= 20:
            raise ContextValidationError("top_k must be between 1 and 20")
        object.__setattr__(
            self, "content_types", _normalize_content_types(self.content_types)
        )
        if type(self.cross_project) is not bool:
            raise ContextValidationError("cross_project must be a boolean")


@dataclass(frozen=True, slots=True)
class ContextSearchResult:
    id: int
    identity_key: str
    l0: str
    content_type: ContextContentType
    scope: ContextScope
    project: str
    status: ContextStatus
    tier: ContextTier
    confidence: float
    importance: float
    score: float
    score_components: ContextScoreComponents
    match_types: tuple[ContextMatchType, ...]
    match_layers: tuple[ContextLayer, ...]
    available_layers: tuple[ContextLayer, ...]

    def __post_init__(self) -> None:
        _validate_positive_int(self.id, "id")
        if not isinstance(self.identity_key, str) or not self.identity_key.strip():
            raise ContextValidationError("identity_key must not be empty")
        if not isinstance(self.l0, str):
            raise ContextValidationError("l0 must be a string")
        if not isinstance(self.content_type, ContextContentType):
            raise ContextValidationError("content_type must be a ContextContentType")
        if not isinstance(self.scope, ContextScope):
            raise ContextValidationError("scope must be a ContextScope")
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        if not isinstance(self.status, ContextStatus):
            raise ContextValidationError("status must be a ContextStatus")
        if not isinstance(self.tier, ContextTier):
            raise ContextValidationError("tier must be a ContextTier")
        _validate_number(self.confidence, "confidence", lower=0.0, upper=1.0)
        _validate_number(self.importance, "importance", lower=1.0, upper=10.0)
        _validate_number(self.score, "score", lower=0.0, upper=1.0)
        if not isinstance(self.score_components, ContextScoreComponents):
            raise ContextValidationError(
                "score_components must be a ContextScoreComponents instance"
            )
        object.__setattr__(
            self, "match_types", _normalize_match_types(self.match_types)
        )
        object.__setattr__(
            self, "match_layers", _normalize_layers(self.match_layers, "match_layers")
        )
        object.__setattr__(
            self,
            "available_layers",
            _normalize_layers(self.available_layers, "available_layers"),
        )


@dataclass(frozen=True, slots=True)
class ContextReadRequest:
    id: int
    layer: ContextLayer = ContextLayer.L1

    def __post_init__(self) -> None:
        _validate_positive_int(self.id, "id")
        if not isinstance(self.layer, ContextLayer) or self.layer not in (
            ContextLayer.L1,
            ContextLayer.L2,
        ):
            raise ContextValidationError(
                "layer must be ContextLayer.L1 or ContextLayer.L2"
            )


_CONTEXT_READ_ERROR_CODES = frozenset(
    {"not_found", "not_readable", "expired", "invalid_layer"}
)


@dataclass(frozen=True, slots=True)
class ContextReadResult:
    """One exact layer or a structured failure; never a nearby substitute."""

    id: int
    layer: ContextLayer
    content: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_positive_int(self.id, "id")
        if not isinstance(self.layer, ContextLayer):
            raise ContextValidationError("layer must be a ContextLayer")
        if not isinstance(self.content, str):
            raise ContextValidationError("content must be a string")
        if self.error_code is not None and self.error_code not in _CONTEXT_READ_ERROR_CODES:
            raise ContextValidationError(
                "error_code must be one of "
                + ", ".join(sorted(_CONTEXT_READ_ERROR_CODES))
            )


@dataclass(frozen=True, slots=True)
class ContextSessionStartRequest:
    project: str
    query: str
    max_chars: int | None = None
    # 瞬态工作区路径：仅用于续接路由，指纹化后即弃，永不落库
    workspace_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        object.__setattr__(self, "project", self.project.strip())
        query = _normalize_text(self.query, "query")
        if not query:
            raise ContextValidationError("query must not be empty")
        object.__setattr__(self, "query", query)
        if self.max_chars is not None:
            _validate_positive_int(self.max_chars, "max_chars")
        if not isinstance(self.workspace_path, str):
            raise ContextValidationError("workspace_path must be a string")
        object.__setattr__(
            self, "workspace_path", self.workspace_path.strip()
        )


@dataclass(frozen=True, slots=True)
class ContextExclusionCount:
    reason: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ContextValidationError("reason must not be empty")
        _validate_non_negative_int(self.count, "count")


# session_start 续接路由的稳定码："" 表示未触发续接分支；"stale" 是
# session 层的映射码（resume ok 但 checkpoint 已确认偏离），其余与
# ContinuityResumeResult.code 一一对应。
_SESSION_CONTINUATION_CODES = frozenset(
    {
        "",
        "ok",
        "needs_focus_confirmation",
        "ambiguous",
        "no_continuation",
        "dangling_focus",
        "stale",
        "continuity_not_ready",
    }
)


@dataclass(frozen=True, slots=True)
class ContextSessionStartResult:
    block: str
    selected_ids: tuple[int, ...]
    used_chars: int
    excluded_counts: tuple[ContextExclusionCount, ...]
    # 续接路由结果：code 稳定码 + 有界结构（五要素/修订/状态/候选元数据），
    # 绝不含 L2 原文、绝对路径或工作区指纹；未触发续接分支时为 ""/None。
    continuation_code: str = ""
    continuation: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.block, str):
            raise ContextValidationError("block must be a string")
        try:
            selected_ids = tuple(self.selected_ids)
        except TypeError as exc:
            raise ContextValidationError(
                "selected_ids must be an iterable of positive integers"
            ) from exc
        for selected_id in selected_ids:
            _validate_positive_int(selected_id, "selected_ids")
        object.__setattr__(self, "selected_ids", selected_ids)
        _validate_non_negative_int(self.used_chars, "used_chars")
        try:
            excluded_counts = tuple(self.excluded_counts)
        except TypeError as exc:
            raise ContextValidationError(
                "excluded_counts must be an iterable of ContextExclusionCount"
            ) from exc
        if any(not isinstance(item, ContextExclusionCount) for item in excluded_counts):
            raise ContextValidationError(
                "excluded_counts must be an iterable of ContextExclusionCount"
            )
        object.__setattr__(self, "excluded_counts", excluded_counts)
        if self.continuation_code not in _SESSION_CONTINUATION_CODES:
            raise ContextValidationError(
                "continuation_code must be one of "
                + ", ".join(sorted(_SESSION_CONTINUATION_CODES))
            )
        if self.continuation is not None and not isinstance(self.continuation, dict):
            raise ContextValidationError("continuation must be a dict or None")


@dataclass(frozen=True, slots=True)
class ContextServiceStatus:
    """Content-free service snapshot: modes, counts, flags, and diagnostics."""

    mode: ContextMode
    adapter: str
    ready: bool
    status_counts: dict[str, int]
    mapping_count: int
    projection_lag: int
    context_vector_ready: bool
    context_vector_dirty: bool
    legacy_vector_ready: bool
    legacy_vector_dirty: bool
    diagnostics: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ContextMode):
            raise ContextValidationError("mode must be a ContextMode")
        if not isinstance(self.adapter, str):
            raise ContextValidationError("adapter must be a string")
        if type(self.ready) is not bool:
            raise ContextValidationError("ready must be a boolean")
        try:
            status_counts = dict(self.status_counts)
        except (TypeError, ValueError) as exc:
            raise ContextValidationError(
                "status_counts must map strings to non-negative integers"
            ) from exc
        if any(
            not isinstance(key, str) or type(count) is not int or count < 0
            for key, count in status_counts.items()
        ):
            raise ContextValidationError(
                "status_counts must map strings to non-negative integers"
            )
        object.__setattr__(self, "status_counts", status_counts)
        _validate_non_negative_int(self.mapping_count, "mapping_count")
        _validate_non_negative_int(self.projection_lag, "projection_lag")
        for name in (
            "context_vector_ready",
            "context_vector_dirty",
            "legacy_vector_ready",
            "legacy_vector_dirty",
        ):
            if type(getattr(self, name)) is not bool:
                raise ContextValidationError(f"{name} must be a boolean")
        try:
            diagnostics = tuple(self.diagnostics)
        except TypeError as exc:
            raise ContextValidationError(
                "diagnostics must be an iterable of strings"
            ) from exc
        if any(not isinstance(message, str) for message in diagnostics):
            raise ContextValidationError("diagnostics must be an iterable of strings")
        object.__setattr__(self, "diagnostics", diagnostics)
        try:
            reason_codes = tuple(self.reason_codes)
        except TypeError as exc:
            raise ContextValidationError(
                "reason_codes must be an iterable of known service codes"
            ) from exc
        if any(
            not isinstance(code, str) or code not in _CONTEXT_SERVICE_ERROR_CODES
            for code in reason_codes
        ):
            raise ContextValidationError(
                "reason_codes must be an iterable of known service codes"
            )
        object.__setattr__(self, "reason_codes", reason_codes)


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


def _validate_positive_int(value: object, field_name: str) -> None:
    """Reject booleans even though Python models them as integers."""
    if type(value) is not int or value <= 0:
        raise ContextValidationError(f"{field_name} must be a positive integer")


def _validate_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ContextValidationError(f"{field_name} must be a non-negative integer")


def _normalize_enum_tuple(
    value: object, enum_type: type, field_name: str
) -> tuple:
    if isinstance(value, (str, bytes)):
        raise ContextValidationError(
            f"{field_name} must be an iterable of {enum_type.__name__}"
        )
    try:
        members = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            f"{field_name} must be an iterable of {enum_type.__name__}"
        ) from exc
    if any(not isinstance(member, enum_type) for member in members):
        raise ContextValidationError(
            f"{field_name} must be an iterable of {enum_type.__name__}"
        )
    return members


def _normalize_content_types(value: object) -> tuple[ContextContentType, ...]:
    return _normalize_enum_tuple(value, ContextContentType, "content_types")  # type: ignore[return-value]


def _normalize_match_types(value: object) -> tuple[ContextMatchType, ...]:
    return _normalize_enum_tuple(value, ContextMatchType, "match_types")  # type: ignore[return-value]


def _normalize_layers(value: object, field_name: str) -> tuple[ContextLayer, ...]:
    return _normalize_enum_tuple(value, ContextLayer, field_name)  # type: ignore[return-value]
