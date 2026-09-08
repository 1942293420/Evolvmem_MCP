"""Typed models for the continuity (workstream checkpoint) domain.

Everything crossing the continuity boundary is a frozen, slots dataclass with
bounded, content-free validation; failures are stable machine-readable codes
carried by ``ContinuityError.code`` — never exception text, paths, or memory
content. The state-transition whitelist lives here as data so the service and
tests share one source of truth.
"""

from dataclasses import dataclass
from enum import Enum


class ContinuityValidationError(ValueError):
    """Raised when continuity-boundary data violates its domain contract."""


_CONTINUITY_ERROR_CODES = frozenset(
    {
        "revision_conflict",
        "invalid_transition",
        "invalid_action",
        "workspace_key_missing",
        "workstream_not_found",
        "focus_conflict",
        "invalid_source",
        "invalid_parent",
        "content_rejected",
        "project_unresolved",
        "invalid_project_name",
        "alias_conflict",
        "project_archived",
    }
)


class ContinuityError(Exception):
    """Stable machine-readable failure code; never carries detail text."""

    def __init__(self, code: str) -> None:
        if code not in _CONTINUITY_ERROR_CODES:
            raise ContinuityValidationError(
                "code must be one of "
                + ", ".join(sorted(_CONTINUITY_ERROR_CODES))
            )
        self.code = code
        super().__init__(code)


class WorkstreamStatus(str, Enum):
    OPEN = "open"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ContinuityAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    PAUSE = "pause"
    RESUME = "resume"
    BLOCK = "block"
    UNBLOCK = "unblock"
    COMPLETE = "complete"
    CANCEL = "cancel"
    SWITCH_FOCUS = "switch_focus"
    CLEAR_FOCUS = "clear_focus"


_TERMINAL_STATUSES = frozenset(
    {WorkstreamStatus.COMPLETED.value, WorkstreamStatus.CANCELLED.value}
)

# Closed transition whitelist: current status -> action -> next status.
# ``update`` always keeps the current status; focus actions are not here
# because they never touch the workflow status.
_STATE_TRANSITIONS: dict[str, dict[str, str]] = {
    WorkstreamStatus.OPEN.value: {
        ContinuityAction.UPDATE.value: WorkstreamStatus.OPEN.value,
        ContinuityAction.PAUSE.value: WorkstreamStatus.PAUSED.value,
        ContinuityAction.BLOCK.value: WorkstreamStatus.BLOCKED.value,
        ContinuityAction.COMPLETE.value: WorkstreamStatus.COMPLETED.value,
        ContinuityAction.CANCEL.value: WorkstreamStatus.CANCELLED.value,
    },
    WorkstreamStatus.PAUSED.value: {
        ContinuityAction.UPDATE.value: WorkstreamStatus.PAUSED.value,
        ContinuityAction.RESUME.value: WorkstreamStatus.OPEN.value,
        ContinuityAction.COMPLETE.value: WorkstreamStatus.COMPLETED.value,
        ContinuityAction.CANCEL.value: WorkstreamStatus.CANCELLED.value,
    },
    WorkstreamStatus.BLOCKED.value: {
        ContinuityAction.UPDATE.value: WorkstreamStatus.BLOCKED.value,
        ContinuityAction.UNBLOCK.value: WorkstreamStatus.OPEN.value,
        ContinuityAction.PAUSE.value: WorkstreamStatus.PAUSED.value,
        ContinuityAction.COMPLETE.value: WorkstreamStatus.COMPLETED.value,
        ContinuityAction.CANCEL.value: WorkstreamStatus.CANCELLED.value,
    },
}

_CONTENT_ACTIONS = frozenset(
    {
        ContinuityAction.UPDATE.value,
        ContinuityAction.PAUSE.value,
        ContinuityAction.RESUME.value,
        ContinuityAction.BLOCK.value,
        ContinuityAction.UNBLOCK.value,
        ContinuityAction.COMPLETE.value,
        ContinuityAction.CANCEL.value,
    }
)
_FOCUS_ACTIONS = frozenset(
    {
        ContinuityAction.SWITCH_FOCUS.value,
        ContinuityAction.CLEAR_FOCUS.value,
    }
)
_ALL_ACTIONS = frozenset(
    {ContinuityAction.CREATE.value} | _CONTENT_ACTIONS | _FOCUS_ACTIONS
)

_RESUME_CODES = frozenset(
    {
        "ok",
        "needs_focus_confirmation",
        "ambiguous",
        "no_continuation",
        "dangling_focus",
        "continuity_not_ready",
    }
)

_STALENESS_CODES = frozenset(
    {
        "fresh",
        "head_advanced",
        "branch_changed",
        "head_diverged",
        "unknown",
        "wrong_workspace",
    }
)

# continuity_find 的发现结果分类：唯一候选 ok；多候选 ambiguous；
# 项目未登记与已登记但无未完成任务是两类不同的失败。
_FIND_CODES = frozenset(
    {
        "ok",
        "ambiguous",
        "project_not_registered",
        "no_open_workstream",
    }
)

_FIND_MATCH_VIAS = frozenset({"name", "alias", "task"})

_FOCUS_STATES = frozenset({"", "none", "ok", "dangling"})

# continuity_import（Codex 中断补录）的结果分类：create/update 之外全部
# 是保守保留分支，绝不覆盖人工 checkpoint、不复活终态任务。
_IMPORT_CODES = frozenset(
    {
        "created",
        "updated",
        "unchanged",
        "terminal_kept",
        "human_checkpoint_kept",
    }
)


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContinuityValidationError(f"invalid_{field}")
    return value


def _require_str_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ContinuityValidationError(f"invalid_{field}")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContinuityValidationError(f"invalid_{field}") from exc
    if any(not isinstance(item, str) for item in items):
        raise ContinuityValidationError(f"invalid_{field}")
    return items


def _require_id_tuple(value: object, field: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise ContinuityValidationError(f"invalid_{field}")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContinuityValidationError(f"invalid_{field}") from exc
    if any(type(item) is not int or item <= 0 for item in items):
        raise ContinuityValidationError(f"invalid_{field}")
    return items


def _require_revision(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ContinuityValidationError(f"invalid_{field}")
    return value


@dataclass(frozen=True, slots=True)
class ContinuityCheckpointRequest:
    """One ``continuity_checkpoint`` call: action discriminator plus CAS tokens.

    ``expected_checkpoint_revision``/``expected_state_version`` guard the
    workstream row (both must be 0 for create); ``expected_focus_revision``
    guards the focus pointer and is required whenever the action touches it
    (create/mutation with ``make_focus=True``, switch_focus, clear_focus).
    """

    action: str
    workspace_path: str
    project_hint: str = ""
    workstream_id: str = ""
    objective: str = ""
    accepted_decisions: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    current_step: str = ""
    next_action: str = ""
    blockers: tuple[str, ...] = ()
    parent_workstream_id: str = ""
    source_context_ids: tuple[int, ...] = ()
    make_focus: bool = False
    expected_checkpoint_revision: int = 0
    expected_state_version: int = 0
    expected_focus_revision: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "action",
            "workspace_path",
            "project_hint",
            "workstream_id",
            "objective",
            "current_step",
            "next_action",
            "parent_workstream_id",
        ):
            _require_str(getattr(self, field), field)
        for field in ("accepted_decisions", "completed_steps", "blockers"):
            object.__setattr__(
                self, field, _require_str_tuple(getattr(self, field), field)
            )
        object.__setattr__(
            self,
            "source_context_ids",
            _require_id_tuple(self.source_context_ids, "source_context_ids"),
        )
        if type(self.make_focus) is not bool:
            raise ContinuityValidationError("invalid_make_focus")
        _require_revision(
            self.expected_checkpoint_revision, "expected_checkpoint_revision"
        )
        _require_revision(self.expected_state_version, "expected_state_version")
        if self.expected_focus_revision is not None:
            _require_revision(
                self.expected_focus_revision, "expected_focus_revision"
            )


@dataclass(frozen=True, slots=True)
class ContinuityCheckpointResult:
    """Mutation outcome: resulting pointer/revision state, never content."""

    workstream_id: str = ""
    checkpoint_revision: int = 0
    state_version: int = 0
    focus_revision: int = 0
    status: str = ""
    context_id: int = 0

    def __post_init__(self) -> None:
        _require_str(self.workstream_id, "workstream_id")
        for field in (
            "checkpoint_revision",
            "state_version",
            "focus_revision",
        ):
            _require_revision(getattr(self, field), field)
        _require_str(self.status, "status")
        if type(self.context_id) is not int or self.context_id < 0:
            raise ContinuityValidationError("invalid_context_id")


@dataclass(frozen=True, slots=True)
class ContinuityResumeRequest:
    workspace_path: str
    project_hint: str = ""

    def __post_init__(self) -> None:
        _require_str(self.workspace_path, "workspace_path")
        _require_str(self.project_hint, "project_hint")


@dataclass(frozen=True, slots=True)
class WorkstreamSummary:
    """Bounded candidate projection: metadata plus L0 only, never L1/L2."""

    workstream_id: str
    project: str
    status: str
    checkpoint_revision: int
    state_version: int
    l0: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_str(self.workstream_id, "workstream_id")
        _require_str(self.project, "project")
        if self.status not in {status.value for status in WorkstreamStatus}:
            raise ContinuityValidationError("invalid_status")
        _require_revision(self.checkpoint_revision, "checkpoint_revision")
        _require_revision(self.state_version, "state_version")
        _require_str(self.l0, "l0")
        _require_str(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class ContinuityResumeResult:
    """Resume outcome. ``checkpoint`` carries L0/L1 plus the L2 authoritative
    fields only — never the raw L2 document; it is None for wrong_workspace
    and for every non-ok code."""

    code: str
    workstream_id: str = ""
    context_id: int = 0
    checkpoint_revision: int = 0
    state_version: int = 0
    focus_revision: int = 0
    status: str = ""
    staleness: str = ""
    checkpoint: dict | None = None
    candidates: tuple[WorkstreamSummary, ...] = ()

    def __post_init__(self) -> None:
        if self.code not in _RESUME_CODES:
            raise ContinuityValidationError(
                "code must be one of " + ", ".join(sorted(_RESUME_CODES))
            )
        _require_str(self.workstream_id, "workstream_id")
        if type(self.context_id) is not int or self.context_id < 0:
            raise ContinuityValidationError("invalid_context_id")
        for field in (
            "checkpoint_revision",
            "state_version",
            "focus_revision",
        ):
            _require_revision(getattr(self, field), field)
        _require_str(self.status, "status")
        if self.staleness not in _STALENESS_CODES and self.staleness != "":
            raise ContinuityValidationError("invalid_staleness")
        if self.checkpoint is not None and not isinstance(self.checkpoint, dict):
            raise ContinuityValidationError("invalid_checkpoint")
        try:
            candidates = tuple(self.candidates)
        except TypeError as exc:
            raise ContinuityValidationError("invalid_candidates") from exc
        if any(not isinstance(item, WorkstreamSummary) for item in candidates):
            raise ContinuityValidationError("invalid_candidates")
        object.__setattr__(self, "candidates", candidates)


# Project name / alias policy: explicit caller declaration, never a path and
# never empty; the same rule serves registration, aliases and find queries.
_PROJECT_NAME_MAX_CHARS = 120


def validate_project_name(value: object, field: str = "project") -> str:
    """Return the stripped project/alias name or raise a stable code error.

    Path-shaped or empty names are rejected so a workspace path can never
    double as a project declaration and no generic name binds everything.
    """
    text = _require_str(value, field).strip()
    if (
        not text
        or len(text) > _PROJECT_NAME_MAX_CHARS
        or "/" in text
        or "\\" in text
        or text.startswith("~")
        or any(ord(ch) < 32 for ch in text)
    ):
        raise ContinuityError("invalid_project_name")
    return text


@dataclass(frozen=True, slots=True)
class ContinuityBeginRequest:
    """One ``continuity_begin`` call: explicit project declaration plus the
    first checkpoint content. Registration, alias, binding and the workstream
    are all idempotent under this single entry point."""

    workspace_path: str
    project: str
    alias: str = ""
    objective: str = ""
    accepted_decisions: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    current_step: str = ""
    next_action: str = ""
    blockers: tuple[str, ...] = ()
    make_focus: bool = True

    def __post_init__(self) -> None:
        _require_str(self.workspace_path, "workspace_path")
        object.__setattr__(self, "project", validate_project_name(self.project))
        if _require_str(self.alias, "alias").strip():
            object.__setattr__(self, "alias", validate_project_name(self.alias, "alias"))
        else:
            object.__setattr__(self, "alias", "")
        for field in ("objective", "current_step", "next_action"):
            _require_str(getattr(self, field), field)
        for field in ("accepted_decisions", "completed_steps", "blockers"):
            object.__setattr__(
                self, field, _require_str_tuple(getattr(self, field), field)
            )
        if type(self.make_focus) is not bool:
            raise ContinuityValidationError("invalid_make_focus")


@dataclass(frozen=True, slots=True)
class ContinuityBeginResult:
    """Begin outcome: pointer state plus idempotency flags, never content."""

    project: str
    workstream_id: str
    checkpoint_revision: int
    state_version: int
    focus_revision: int
    status: str
    context_id: int
    created: bool
    updated: bool
    registered: bool
    alias_added: bool
    bound: bool

    def __post_init__(self) -> None:
        _require_str(self.project, "project")
        _require_str(self.workstream_id, "workstream_id")
        for field in ("checkpoint_revision", "state_version", "focus_revision"):
            _require_revision(getattr(self, field), field)
        _require_str(self.status, "status")
        if type(self.context_id) is not int or self.context_id < 0:
            raise ContinuityValidationError("invalid_context_id")
        for field in ("created", "updated", "registered", "alias_added", "bound"):
            if type(getattr(self, field)) is not bool:
                raise ContinuityValidationError(f"invalid_{field}")


@dataclass(frozen=True, slots=True)
class ContinuityFindRequest:
    """One ``continuity_find`` call: project name, alias or task keyword."""

    query: str
    workspace_path: str = ""
    limit: int = 10

    def __post_init__(self) -> None:
        _require_str(self.query, "query")
        _require_str(self.workspace_path, "workspace_path")
        if type(self.limit) is not int or not 1 <= self.limit <= 50:
            raise ContinuityValidationError("invalid_limit")


@dataclass(frozen=True, slots=True)
class FindWorkstreamSummary:
    """Cross-project candidate projection: L0 plus workspace verification."""

    workstream_id: str
    project: str
    status: str
    checkpoint_revision: int
    state_version: int
    l0: str
    updated_at: str
    workspace_match: bool
    staleness: str

    def __post_init__(self) -> None:
        _require_str(self.workstream_id, "workstream_id")
        _require_str(self.project, "project")
        if self.status not in {status.value for status in WorkstreamStatus}:
            raise ContinuityValidationError("invalid_status")
        _require_revision(self.checkpoint_revision, "checkpoint_revision")
        _require_revision(self.state_version, "state_version")
        _require_str(self.l0, "l0")
        _require_str(self.updated_at, "updated_at")
        if type(self.workspace_match) is not bool:
            raise ContinuityValidationError("invalid_workspace_match")
        if self.staleness not in _STALENESS_CODES and self.staleness != "":
            raise ContinuityValidationError("invalid_staleness")


@dataclass(frozen=True, slots=True)
class FindProjectCandidate:
    """One matched project: how it matched plus its unfinished workstreams."""

    project: str
    matched_via: tuple[str, ...]
    focus_state: str
    workstreams: tuple[FindWorkstreamSummary, ...]

    def __post_init__(self) -> None:
        _require_str(self.project, "project")
        try:
            via = tuple(self.matched_via)
        except TypeError as exc:
            raise ContinuityValidationError("invalid_matched_via") from exc
        if not via or any(item not in _FIND_MATCH_VIAS for item in via):
            raise ContinuityValidationError("invalid_matched_via")
        object.__setattr__(self, "matched_via", via)
        if self.focus_state not in _FOCUS_STATES:
            raise ContinuityValidationError("invalid_focus_state")
        try:
            workstreams = tuple(self.workstreams)
        except TypeError as exc:
            raise ContinuityValidationError("invalid_workstreams") from exc
        if any(not isinstance(item, FindWorkstreamSummary) for item in workstreams):
            raise ContinuityValidationError("invalid_workstreams")
        object.__setattr__(self, "workstreams", workstreams)


@dataclass(frozen=True, slots=True)
class ContinuityFindResult:
    """Find outcome. ``checkpoint`` is set only for a unique evidenced
    candidate and carries the same bounded shape as the resume checkpoint."""

    code: str
    candidates: tuple[FindProjectCandidate, ...] = ()
    checkpoint: dict | None = None

    def __post_init__(self) -> None:
        if self.code not in _FIND_CODES:
            raise ContinuityValidationError(
                "code must be one of " + ", ".join(sorted(_FIND_CODES))
            )
        try:
            candidates = tuple(self.candidates)
        except TypeError as exc:
            raise ContinuityValidationError("invalid_candidates") from exc
        if any(not isinstance(item, FindProjectCandidate) for item in candidates):
            raise ContinuityValidationError("invalid_candidates")
        object.__setattr__(self, "candidates", candidates)
        if self.checkpoint is not None and not isinstance(self.checkpoint, dict):
            raise ContinuityValidationError("invalid_checkpoint")


@dataclass(frozen=True, slots=True)
class ContinuityImportRequest:
    """One conservative backfill import for an interrupted external session.

    ``workstream_id`` is caller-deterministic (``ws_`` + 16 lowercase hex) so
    repeated scans never duplicate a task; ``applied_revision`` is the
    checkpoint revision this source previously wrote, so any newer revision
    proves a human checkpoint that must be preserved."""

    workspace_path: str
    project_hint: str
    workstream_id: str
    objective: str = ""
    completed_steps: tuple[str, ...] = ()
    current_step: str = ""
    next_action: str = ""
    blockers: tuple[str, ...] = ()
    applied_revision: int = 0

    def __post_init__(self) -> None:
        _require_str(self.workspace_path, "workspace_path")
        _require_str(self.project_hint, "project_hint")
        import re

        if not re.fullmatch(r"ws_[0-9a-f]{16}", _require_str(
            self.workstream_id, "workstream_id"
        )):
            raise ContinuityValidationError("invalid_workstream_id")
        for field in ("objective", "current_step", "next_action"):
            _require_str(getattr(self, field), field)
        for field in ("completed_steps", "blockers"):
            object.__setattr__(
                self, field, _require_str_tuple(getattr(self, field), field)
            )
        _require_revision(self.applied_revision, "applied_revision")


@dataclass(frozen=True, slots=True)
class ContinuityImportResult:
    """Import outcome: a stable code plus the resulting pointer state."""

    code: str
    workstream_id: str = ""
    checkpoint_revision: int = 0
    state_version: int = 0
    status: str = ""
    context_id: int = 0

    def __post_init__(self) -> None:
        if self.code not in _IMPORT_CODES:
            raise ContinuityValidationError(
                "code must be one of " + ", ".join(sorted(_IMPORT_CODES))
            )
        _require_str(self.workstream_id, "workstream_id")
        _require_revision(self.checkpoint_revision, "checkpoint_revision")
        _require_revision(self.state_version, "state_version")
        _require_str(self.status, "status")
        if type(self.context_id) is not int or self.context_id < 0:
            raise ContinuityValidationError("invalid_context_id")
