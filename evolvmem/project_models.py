"""Typed, privacy-safe models for deterministic project resolution.

The resolver consumes only structured signals — never memory text, layers,
or paths. Every candidate project name is normalized through the registry
alias map before comparison. Evidence rows carry exactly
``source/type/source_version/normalized_value`` so they can be persisted
and rendered without leaking content or locations.
"""

from dataclasses import dataclass
from enum import Enum


class ProjectValidationError(ValueError):
    """Raised when project-boundary data violates its domain contract."""


class ProjectResolutionState(str, Enum):
    RESOLVED = "resolved"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"
    GLOBAL = "global"
    IGNORED = "ignored"


_CONFIDENCE_LEVELS = frozenset({"high", "medium", "none"})
_BINDING_STATES = frozenset({"candidate", "active", "revoked"})
_SIGNAL_TRUSTS = frozenset({"strong", "medium", "ignored"})
_EVIDENCE_KEYS = ("source", "type", "source_version", "normalized_value")


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"invalid_{field}")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceBindingSnapshot:
    """One workspace→project binding row as read from the registry."""

    workspace_fingerprint: str
    project: str
    state: str  # candidate | active | revoked
    is_default: bool

    def __post_init__(self) -> None:
        _require_str(self.workspace_fingerprint, "workspace_fingerprint")
        if not self.project:
            raise ProjectValidationError("invalid_project")
        if self.state not in _BINDING_STATES:
            raise ProjectValidationError("invalid_binding_state")
        if not isinstance(self.is_default, bool):
            raise ProjectValidationError("invalid_binding_default")


@dataclass(frozen=True, slots=True)
class ProjectRegistrySnapshot:
    """Immutable view of the registry the resolver may consult."""

    projects: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]
    bindings: tuple[WorkspaceBindingSnapshot, ...]
    generic_names: tuple[str, ...]
    revision: int

    def __post_init__(self) -> None:
        projects = tuple(_require_str(p, "project") for p in self.projects)
        aliases = tuple(
            (_require_str(alias, "alias"), _require_str(project, "project"))
            for alias, project in self.aliases
        )
        seen_aliases: set[str] = set()
        for alias, _project in aliases:
            folded = alias.casefold()
            if folded in seen_aliases:
                raise ProjectValidationError("duplicate_alias")
            seen_aliases.add(folded)
        bindings = tuple(self.bindings)
        for binding in bindings:
            if not isinstance(binding, WorkspaceBindingSnapshot):
                raise ProjectValidationError("invalid_binding")
        generic_names = tuple(
            _require_str(name, "generic_name") for name in self.generic_names
        )
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ProjectValidationError("invalid_registry_revision")
        object.__setattr__(self, "projects", projects)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "generic_names", generic_names)


@dataclass(frozen=True, slots=True)
class ProjectResolutionRequest:
    """Structured resolver input.

    ``archive_project``/``archive_source_version`` describe the session
    archive a historical row came from and are trusted only for known
    generator versions. ``project_hint`` is the explicit project carried by a
    new typed request. ``workspace_fingerprint`` is the owner-keyed HMAC of
    the transient workspace path (the path itself never reaches the
    resolver).
    """

    content_type: str
    scope: str
    key: str
    tags: tuple[str, ...]
    source_session: str
    source_version: str
    archive_project: str = ""
    archive_source_version: str = ""
    project_hint: str = ""
    workspace_fingerprint: str = ""

    def __post_init__(self) -> None:
        for field in (
            "content_type",
            "scope",
            "key",
            "source_session",
            "source_version",
            "archive_project",
            "archive_source_version",
            "project_hint",
            "workspace_fingerprint",
        ):
            _require_str(getattr(self, field), field)
        tags = tuple(_require_str(tag, "tag") for tag in self.tags)
        object.__setattr__(self, "tags", tags)


@dataclass(frozen=True, slots=True)
class ProjectSignal:
    """One normalized candidate signal.

    ``normalized_value`` is the alias-normalized project name ("" when the
    candidate was structurally unusable). ``trust`` is strong | medium |
    ignored; ignored signals never appear in decisions or evidence.
    """

    source: str
    type: str
    source_version: str
    normalized_value: str
    trust: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ProjectValidationError("invalid_signal_source")
        if not self.type:
            raise ProjectValidationError("invalid_signal_type")
        _require_str(self.source_version, "signal_source_version")
        _require_str(self.normalized_value, "signal_normalized_value")
        if self.trust not in _SIGNAL_TRUSTS:
            raise ProjectValidationError("invalid_signal_trust")

    @staticmethod
    def sort_key(signal: "ProjectSignal") -> tuple[str, str, str, str]:
        """Deterministic evidence ordering key."""
        return (
            signal.source,
            signal.type,
            signal.source_version,
            signal.normalized_value,
        )

    def public_evidence(self) -> dict[str, str]:
        """Bounded public row: no raw candidate, no path, no content."""
        return {
            "source": self.source,
            "type": self.type,
            "source_version": self.source_version,
            "normalized_value": self.normalized_value,
        }


@dataclass(frozen=True, slots=True)
class ProjectResolutionDecision:
    """Resolver output. Projects stay empty unless deterministically resolved."""

    state: ProjectResolutionState
    resolved_project: str
    proposed_project: str
    confidence: str  # high | medium | none
    method: str
    evidence: tuple[dict[str, str], ...]
    resolver_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ProjectResolutionState(self.state))
        _require_str(self.resolved_project, "resolved_project")
        _require_str(self.proposed_project, "proposed_project")
        if self.confidence not in _CONFIDENCE_LEVELS:
            raise ProjectValidationError("invalid_confidence")
        _require_str(self.method, "method")
        _require_str(self.resolver_version, "resolver_version")
        rows = []
        for row in self.evidence:
            if set(row) != set(_EVIDENCE_KEYS):
                raise ProjectValidationError("invalid_evidence")
            rows.append({key: _require_str(row[key], "evidence") for key in _EVIDENCE_KEYS})
        object.__setattr__(self, "evidence", tuple(rows))
        if self.state is not ProjectResolutionState.RESOLVED and (
            self.resolved_project or self.proposed_project
        ):
            raise ProjectValidationError("unexpected_project")

    @classmethod
    def resolved(
        cls,
        project: str,
        method: str,
        version: str,
        evidence: tuple[dict[str, str], ...],
    ) -> "ProjectResolutionDecision":
        return cls(
            state=ProjectResolutionState.RESOLVED,
            resolved_project=project,
            proposed_project="",
            confidence="high" if method == "strong" else "medium",
            method=method,
            evidence=evidence,
            resolver_version=version,
        )

    @classmethod
    def conflict(
        cls, version: str, evidence: tuple[dict[str, str], ...]
    ) -> "ProjectResolutionDecision":
        return cls(
            state=ProjectResolutionState.CONFLICT,
            resolved_project="",
            proposed_project="",
            confidence="none",
            method="",
            evidence=evidence,
            resolver_version=version,
        )

    @classmethod
    def unresolved(
        cls, version: str, evidence: tuple[dict[str, str], ...]
    ) -> "ProjectResolutionDecision":
        return cls(
            state=ProjectResolutionState.UNRESOLVED,
            resolved_project="",
            proposed_project="",
            confidence="none",
            method="",
            evidence=evidence,
            resolver_version=version,
        )

    @classmethod
    def global_decision(cls, version: str) -> "ProjectResolutionDecision":
        return cls(
            state=ProjectResolutionState.GLOBAL,
            resolved_project="",
            proposed_project="",
            confidence="none",
            method="",
            evidence=(),
            resolver_version=version,
        )

    @classmethod
    def resolver_error(cls, version: str) -> "ProjectResolutionDecision":
        """Fail closed on an internal resolver fault.

        Both projects stay empty so a repair analysis keeps any existing
        project untouched; the only exposed detail is the ``resolver_error``
        method code — never a traceback or exception text.
        """
        return cls(
            state=ProjectResolutionState.UNRESOLVED,
            resolved_project="",
            proposed_project="",
            confidence="none",
            method="resolver_error",
            evidence=(),
            resolver_version=version,
        )
