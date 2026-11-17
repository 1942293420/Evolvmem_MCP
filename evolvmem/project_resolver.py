"""Deterministic, pure project resolution.

One canonical algorithm for every write path (adapters, migration, backfill,
review). It consumes only structured signals from ``ProjectResolutionRequest``
plus the registry snapshot, normalizes every candidate through the same alias
map, and applies the design's precedence:

- at least one strong signal and all trusted nonempty signals agree → resolved;
- no strong signal but at least two independent medium types agree → resolved
  (method records the exact combination);
- trusted nonempty signals disagree → conflict (never a guess);
- no usable signal → unresolved;
- global scopes/types stay global; internal content types bypass historical
  heuristics and only accept the explicit typed/binding signals.

Strong forms: the registered active-default workspace binding (or a hint that
exactly matches an active binding), the typed-request project hint, and a
trusted-generator archive project — each only when the normalized value is not
a generic name. Medium forms: canonical ``project:{name}:...`` key,
``分类:{name}`` tag, legacy ``{name}:progress:log:...`` key under a registered
project, and a bare tag equal to a registered project. Candidates produced by
known coarse-cwd generator versions, generic/home directory names, and
alias-colliding names are ignored outright: syntactic validity is never proof
of trust. An internal fault fails closed to ``resolver_error`` with empty
projects so repair analysis preserves any existing project.
"""

import re
from typing import ClassVar, Iterable

from evolvmem.project_models import (
    ProjectRegistrySnapshot,
    ProjectResolutionDecision,
    ProjectResolutionRequest,
    ProjectSignal,
)


_INTERNAL_CONTENT_TYPES = frozenset({"project_summary", "workstream_checkpoint"})
_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CANONICAL_KEY_PATTERN = re.compile(r"^project:([a-z0-9][a-z0-9._-]{0,63}):.+$")
_LEGACY_KEY_PATTERN = re.compile(
    r"^([a-z0-9][a-z0-9._-]{0,63}):progress:log:.+$"
)
_CATEGORY_TAG_PREFIX = "分类:"
_MAX_TAGS = 64
_MAX_EVIDENCE = 16


class ProjectResolver:
    """Stateless resolver; version policy is injected, never global state."""

    VERSION: ClassVar[str] = "project-resolver.v1"

    def __init__(
        self,
        *,
        trusted_archive_versions: Iterable[str] = (),
        coarse_cwd_versions: Iterable[str] = (),
    ) -> None:
        self._trusted_archive_versions = frozenset(trusted_archive_versions)
        self._coarse_cwd_versions = frozenset(coarse_cwd_versions)

    def resolve(
        self,
        request: ProjectResolutionRequest,
        registry: ProjectRegistrySnapshot,
    ) -> ProjectResolutionDecision:
        try:
            if request.scope == "global" or request.content_type in {
                "constraint",
                "preference",
                "user_profile",
            }:
                return ProjectResolutionDecision.global_decision(self.VERSION)
            signals = self._canonical_signals(request, registry)
            trusted = tuple(signal for signal in signals if signal.trust != "ignored")
            names = {signal.normalized_value for signal in trusted if signal.normalized_value}
            evidence = tuple(
                signal.public_evidence()
                for signal in sorted(trusted, key=ProjectSignal.sort_key)
            )[:_MAX_EVIDENCE]
            if len(names) > 1:
                return ProjectResolutionDecision.conflict(self.VERSION, evidence)
            if not names:
                return ProjectResolutionDecision.unresolved(self.VERSION, evidence)
            project = next(iter(names))
            strong = tuple(signal for signal in trusted if signal.trust == "strong")
            medium_sources = {signal.type for signal in trusted if signal.trust == "medium"}
            if strong:
                return ProjectResolutionDecision.resolved(project, "strong", self.VERSION, evidence)
            if len(medium_sources) >= 2:
                method = "+".join(sorted(medium_sources))
                return ProjectResolutionDecision.resolved(project, method, self.VERSION, evidence)
            return ProjectResolutionDecision.unresolved(self.VERSION, evidence)
        except Exception:
            return ProjectResolutionDecision.resolver_error(self.VERSION)

    def _canonical_signals(
        self,
        request: ProjectResolutionRequest,
        registry: ProjectRegistrySnapshot,
    ) -> tuple[ProjectSignal, ...]:
        projects = {project.casefold() for project in registry.projects}
        alias_map = {
            alias.casefold(): project.casefold()
            for alias, project in registry.aliases
        }
        generic = {name.casefold() for name in registry.generic_names}
        internal = request.content_type in _INTERNAL_CONTENT_TYPES

        signals: list[ProjectSignal] = []
        binding = self._binding_signal(request, registry, alias_map, projects, generic)
        if binding is not None:
            signals.append(binding)
        hint = self._hint_signal(request, registry, alias_map, projects, generic)
        if hint is not None:
            signals.append(hint)
        if not internal:
            archive = self._archive_signal(request, alias_map, projects, generic)
            if archive is not None:
                signals.append(archive)
            signals.extend(
                self._key_signals(request, alias_map, projects, generic)
            )
            signals.extend(
                self._tag_signals(request, alias_map, projects, generic)
            )

        seen: set[tuple[str, str, str, str, str]] = set()
        deduped: list[ProjectSignal] = []
        for signal in signals:
            marker = (
                signal.source,
                signal.type,
                signal.source_version,
                signal.normalized_value,
                signal.trust,
            )
            if marker not in seen:
                seen.add(marker)
                deduped.append(signal)
        return tuple(deduped)

    # --- strong signals -------------------------------------------------

    def _binding_signal(
        self,
        request: ProjectResolutionRequest,
        registry: ProjectRegistrySnapshot,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
    ) -> ProjectSignal | None:
        """Active default binding, consulted only when no hint was given.

        With a hint, bindings act as a gate on the hint instead (see
        ``_hint_signal``); candidate/revoked rows, a non-default active row,
        and multiple active bindings without a default yield no signal —
        never a guess.
        """
        if request.project_hint or not request.workspace_fingerprint:
            return None
        defaults = tuple(
            binding
            for binding in registry.bindings
            if binding.workspace_fingerprint == request.workspace_fingerprint
            and binding.state == "active"
            and binding.is_default
        )
        if len(defaults) != 1:
            return None
        chosen = defaults[0]
        return self._make_signal(
            source="workspace_binding",
            type="workspace_binding",
            source_version="",
            raw=chosen.project,
            base_trust="strong",
            alias_map=alias_map,
            projects=projects,
            generic=generic,
        )

    def _hint_signal(
        self,
        request: ProjectResolutionRequest,
        registry: ProjectRegistrySnapshot,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
    ) -> ProjectSignal | None:
        """Typed-request project hint; gated by this workspace's active bindings."""
        if not request.project_hint:
            return None
        normalized = self._normalize(request.project_hint, alias_map, projects)
        active_projects = {
            binding.project.casefold()
            for binding in registry.bindings
            if binding.workspace_fingerprint == request.workspace_fingerprint
            and binding.state == "active"
        }
        if active_projects and normalized not in active_projects:
            normalized = ""  # hint mismatch: ambiguous, never guessed
        return self._make_signal(
            source="project_hint",
            type="typed_project",
            source_version=request.source_version,
            raw=request.project_hint,
            base_trust="strong",
            alias_map=alias_map,
            projects=projects,
            generic=generic,
            normalized_override=normalized,
        )

    def _archive_signal(
        self,
        request: ProjectResolutionRequest,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
    ) -> ProjectSignal | None:
        """Session-archive project, strong only from a trusted generator."""
        if not request.archive_project:
            return None
        trusted = request.archive_source_version in self._trusted_archive_versions
        return self._make_signal(
            source="archive",
            type="archive_project",
            source_version=request.archive_source_version,
            raw=request.archive_project,
            base_trust="strong" if trusted else "ignored",
            alias_map=alias_map,
            projects=projects,
            generic=generic,
        )

    # --- medium signals ---------------------------------------------------

    def _key_signals(
        self,
        request: ProjectResolutionRequest,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
    ) -> list[ProjectSignal]:
        key = request.key.strip().casefold()
        if not key:
            return []
        trust = self._medium_trust(request.source_version)
        canonical = _CANONICAL_KEY_PATTERN.match(key)
        if canonical is not None:
            return [
                self._make_signal(
                    source="key",
                    type="canonical_key",
                    source_version=request.source_version,
                    raw=canonical.group(1),
                    base_trust=trust,
                    alias_map=alias_map,
                    projects=projects,
                    generic=generic,
                )
            ]
        legacy = _LEGACY_KEY_PATTERN.match(key)
        if legacy is not None:
            normalized = self._normalize(legacy.group(1), alias_map, projects)
            if normalized and normalized in projects:
                return [
                    self._make_signal(
                        source="key",
                        type="legacy_key",
                        source_version=request.source_version,
                        raw=legacy.group(1),
                        base_trust=trust,
                        alias_map=alias_map,
                        projects=projects,
                        generic=generic,
                    )
                ]
        return []

    def _tag_signals(
        self,
        request: ProjectResolutionRequest,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
    ) -> list[ProjectSignal]:
        signals: list[ProjectSignal] = []
        trust = self._medium_trust(request.source_version)
        for tag in request.tags[:_MAX_TAGS]:
            if tag.startswith(_CATEGORY_TAG_PREFIX):
                raw = tag[len(_CATEGORY_TAG_PREFIX) :].strip().casefold()
                if not _NAME_PATTERN.fullmatch(raw):
                    continue
                signals.append(
                    self._make_signal(
                        source="tags",
                        type="category_tag",
                        source_version=request.source_version,
                        raw=raw,
                        base_trust=trust,
                        alias_map=alias_map,
                        projects=projects,
                        generic=generic,
                    )
                )
                continue
            raw = tag.strip().casefold()
            if not _NAME_PATTERN.fullmatch(raw):
                continue
            normalized = self._normalize(raw, alias_map, projects)
            if normalized and normalized in projects:
                signals.append(
                    self._make_signal(
                        source="tags",
                        type="bare_tag",
                        source_version=request.source_version,
                        raw=raw,
                        base_trust=trust,
                        alias_map=alias_map,
                        projects=projects,
                        generic=generic,
                    )
                )
        return signals

    # --- normalization and trust -------------------------------------------

    def _medium_trust(self, source_version: str) -> str:
        """Known coarse-cwd generators never produce trusted key/tag signals."""
        if source_version in self._coarse_cwd_versions:
            return "ignored"
        return "medium"

    @staticmethod
    def _normalize(
        raw: str,
        alias_map: dict[str, str],
        projects: set[str],
    ) -> str:
        """Alias-normalize a candidate; ambiguous names normalize to "".

        A name that is both a registered project and an alias for a different
        project is a collision: normalizing it would steal one identity, so
        the candidate is dropped instead of guessed.
        """
        name = raw.strip().casefold()
        if not name:
            return ""
        target = alias_map.get(name, name)
        if name in projects and target != name:
            return ""
        return target

    def _make_signal(
        self,
        *,
        source: str,
        type: str,
        source_version: str,
        raw: str,
        base_trust: str,
        alias_map: dict[str, str],
        projects: set[str],
        generic: set[str],
        normalized_override: str | None = None,
    ) -> ProjectSignal:
        normalized = (
            self._normalize(raw, alias_map, projects)
            if normalized_override is None
            else normalized_override
        )
        trust = base_trust
        if not normalized or normalized in generic:
            trust = "ignored"
        return ProjectSignal(
            source=source,
            type=type,
            source_version=source_version,
            normalized_value=normalized,
            trust=trust,
        )
