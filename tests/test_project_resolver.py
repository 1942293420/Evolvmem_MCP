"""ProjectResolver precedence and conflict matrix.

The resolver is pure and deterministic: it consumes only structured signals
(never memory text), normalizes every candidate through the registry alias
map, and never guesses — distinct trusted names are a conflict, a lone
medium signal is unresolved, and internal content types bypass historical
heuristics entirely.
"""

import pytest

from evolvmem.project_models import (
    ProjectRegistrySnapshot,
    ProjectResolutionRequest,
    ProjectResolutionState,
    WorkspaceBindingSnapshot,
)
from evolvmem.project_resolver import ProjectResolver


def _registry(**overrides):
    base = {
        "projects": ("eva", "hermes"),
        "aliases": (("evolv", "eva"),),
        "bindings": (),
        "generic_names": ("home", "jiangli", "project", "src", "workspace"),
        "revision": 7,
    }
    base.update(overrides)
    return ProjectRegistrySnapshot(**base)


def _request(**overrides):
    base = {
        "content_type": "session_summary",
        "scope": "project",
        "key": "",
        "tags": (),
        "source_session": "session_1",
        "source_version": "kimi-v3",
    }
    base.update(overrides)
    return ProjectResolutionRequest(**base)


def _binding(fingerprint, project, state="active", is_default=False):
    return WorkspaceBindingSnapshot(
        workspace_fingerprint=fingerprint,
        project=project,
        state=state,
        is_default=is_default,
    )


def test_resolver_requires_two_medium_signals_and_never_guesses_conflict():
    registry = ProjectRegistrySnapshot(
        projects=("eva", "hermes"),
        aliases=(("evolv", "eva"),),
        bindings=(
            WorkspaceBindingSnapshot(
                workspace_fingerprint="hmac-sha256:" + "1" * 64,
                project="eva",
                state="candidate",
                is_default=False,
            ),
        ),
        generic_names=("home", "jiangli", "project", "src", "workspace"),
        revision=7,
    )
    request = ProjectResolutionRequest(
        content_type="session_summary",
        scope="project",
        key="project:evolv:progress:log:1",
        tags=("分类:evolv",),
        source_session="session_1",
        source_version="kimi-v3",
        project_hint="hermes",
        workspace_fingerprint="hmac-sha256:" + "1" * 64,
    )

    decision = ProjectResolver().resolve(request, registry)

    assert decision.state is ProjectResolutionState.CONFLICT
    assert decision.resolved_project == ""
    assert decision.proposed_project == ""
    assert all(set(row) == {"source", "type", "source_version", "normalized_value"} for row in decision.evidence)


def test_active_default_binding_is_a_strong_signal():
    fingerprint = "hmac-sha256:" + "2" * 64
    registry = _registry(
        bindings=(_binding(fingerprint, "eva", is_default=True),),
    )

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint), registry
    )

    assert decision.state is ProjectResolutionState.RESOLVED
    assert decision.resolved_project == "eva"
    assert decision.method == "strong"
    assert decision.confidence == "high"


def test_non_default_active_binding_without_hint_is_not_used():
    fingerprint = "hmac-sha256:" + "2" * 64
    registry = _registry(bindings=(_binding(fingerprint, "eva"),))

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint), registry
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED


def test_matching_active_hint_resolves():
    fingerprint = "hmac-sha256:" + "2" * 64
    registry = _registry(bindings=(_binding(fingerprint, "hermes"),))

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint, project_hint="hermes"),
        registry,
    )

    assert decision.state is ProjectResolutionState.RESOLVED
    assert decision.resolved_project == "hermes"
    assert decision.method == "strong"


def test_hint_mismatch_with_active_binding_is_ambiguous_never_guessed():
    fingerprint = "hmac-sha256:" + "2" * 64
    registry = _registry(
        bindings=(_binding(fingerprint, "eva", is_default=True),),
    )

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint, project_hint="hermes"),
        registry,
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.resolved_project == ""
    assert decision.proposed_project == ""


def test_trusted_typed_project_hint_alone_resolves():
    decision = ProjectResolver().resolve(
        _request(project_hint="eva"), _registry()
    )

    assert decision.state is ProjectResolutionState.RESOLVED
    assert decision.resolved_project == "eva"
    assert decision.confidence == "high"


def test_trusted_archive_project_is_strong():
    resolver = ProjectResolver(trusted_archive_versions=("archive-gen-v2",))

    decision = resolver.resolve(
        _request(
            archive_project="evolv",
            archive_source_version="archive-gen-v2",
        ),
        _registry(),
    )

    assert decision.state is ProjectResolutionState.RESOLVED
    assert decision.resolved_project == "eva"
    assert decision.method == "strong"


def test_untrusted_archive_version_is_ignored():
    resolver = ProjectResolver(trusted_archive_versions=("archive-gen-v2",))

    decision = resolver.resolve(
        _request(
            archive_project="eva",
            archive_source_version="archive-gen-v1",
        ),
        _registry(),
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()


@pytest.mark.parametrize(
    ("key", "tags", "project", "method"),
    [
        (
            "project:eva:knowledge:current",
            ("分类:eva",),
            "eva",
            "canonical_key+category_tag",
        ),
        (
            "project:evolv:progress:log:1",
            ("eva",),
            "eva",
            "bare_tag+canonical_key",
        ),
        (
            "eva:progress:log:2026-09-01-0930",
            ("分类:evolv",),
            "eva",
            "category_tag+legacy_key",
        ),
        (
            "hermes:progress:log:2026-09-01-0930",
            ("hermes",),
            "hermes",
            "bare_tag+legacy_key",
        ),
    ],
)
def test_all_four_medium_signal_forms_resolve_on_two_source_agreement(
    key, tags, project, method
):
    decision = ProjectResolver().resolve(
        _request(key=key, tags=tags), _registry()
    )

    assert decision.state is ProjectResolutionState.RESOLVED
    assert decision.resolved_project == project
    assert decision.method == method
    assert decision.confidence == "medium"


@pytest.mark.parametrize(
    ("key", "tags", "expected_type"),
    [
        ("project:eva:knowledge:current", (), "canonical_key"),
        ("", ("分类:eva",), "category_tag"),
        ("eva:progress:log:2026-09-01-0930", (), "legacy_key"),
        ("", ("eva",), "bare_tag"),
    ],
)
def test_single_medium_signal_never_resolves_alone(key, tags, expected_type):
    decision = ProjectResolver().resolve(
        _request(key=key, tags=tags), _registry()
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.resolved_project == ""
    assert [row["type"] for row in decision.evidence] == [expected_type]


def test_two_medium_signals_of_the_same_type_are_not_two_sources():
    decision = ProjectResolver().resolve(
        _request(tags=("分类:eva", "分类:evolv")), _registry()
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED


def test_alias_collision_between_project_and_alias_is_ambiguous():
    # "hermes" is both a registered project and an alias for "eva":
    # normalization would steal one project's identity, so the signal drops.
    registry = _registry(aliases=(("hermes", "eva"),))

    decision = ProjectResolver().resolve(
        _request(project_hint="hermes"), registry
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()


def test_candidate_only_binding_contributes_no_signal():
    fingerprint = "hmac-sha256:" + "1" * 64
    registry = _registry(
        bindings=(_binding(fingerprint, "eva", state="candidate"),),
    )

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint), registry
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()


def test_multiple_active_bindings_without_default_are_ambiguous():
    fingerprint = "hmac-sha256:" + "2" * 64
    registry = _registry(
        bindings=(
            _binding(fingerprint, "eva"),
            _binding(fingerprint, "hermes"),
        ),
    )

    decision = ProjectResolver().resolve(
        _request(workspace_fingerprint=fingerprint), registry
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.resolved_project == ""
    assert decision.proposed_project == ""


def test_known_coarse_cwd_generator_versions_are_never_trusted():
    resolver = ProjectResolver(coarse_cwd_versions=("kimi-extraction-v1",))
    registry = _registry()
    coarse = _request(
        key="project:eva:progress:log:1",
        tags=("分类:eva",),
        source_version="kimi-extraction-v1",
    )

    decision = resolver.resolve(coarse, registry)

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()

    control = resolver.resolve(
        _request(
            key="project:eva:progress:log:1",
            tags=("分类:eva",),
            source_version="kimi-v3",
        ),
        registry,
    )
    assert control.state is ProjectResolutionState.RESOLVED
    assert control.resolved_project == "eva"


@pytest.mark.parametrize("name", ["src", "jiangli", "home"])
def test_generic_names_are_never_trusted(name):
    decision = ProjectResolver().resolve(
        _request(
            key=f"project:{name}:progress:log:1",
            tags=(f"分类:{name}",),
            project_hint=name,
        ),
        _registry(),
    )

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()


@pytest.mark.parametrize(
    ("content_type", "scope"),
    [
        ("fact", "global"),
        ("constraint", "project"),
        ("preference", "project"),
        ("user_profile", "project"),
    ],
)
def test_global_content_stays_global_even_with_strong_signals(
    content_type, scope
):
    decision = ProjectResolver().resolve(
        _request(
            content_type=content_type,
            scope=scope,
            key="project:eva:knowledge:current",
            project_hint="eva",
        ),
        _registry(),
    )

    assert decision.state is ProjectResolutionState.GLOBAL
    assert decision.resolved_project == ""
    assert decision.evidence == ()


def test_internal_project_summary_bypasses_historical_heuristics():
    registry = _registry()
    heuristic_only = _request(
        content_type="project_summary",
        key="project:eva:knowledge:current",
        tags=("分类:eva",),
    )

    decision = ProjectResolver().resolve(heuristic_only, registry)

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.evidence == ()

    explicit = _request(
        content_type="project_summary",
        key="project:eva:knowledge:current",
        tags=("分类:eva",),
        project_hint="eva",
    )
    resolved = ProjectResolver().resolve(explicit, registry)
    assert resolved.state is ProjectResolutionState.RESOLVED
    assert resolved.resolved_project == "eva"


def test_evidence_is_bounded_and_deterministically_ordered():
    tags = tuple(f"分类:team-{index:02d}" for index in range(24))
    request = _request(tags=tags)
    registry = _registry()
    resolver = ProjectResolver()

    decision = resolver.resolve(request, registry)
    again = resolver.resolve(request, registry)

    assert decision.state is ProjectResolutionState.CONFLICT
    assert 0 < len(decision.evidence) <= 16
    keys = [
        (row["source"], row["type"], row["source_version"], row["normalized_value"])
        for row in decision.evidence
    ]
    assert keys == sorted(keys)
    assert decision == again


def test_resolver_exception_preserves_existing_project_and_exposes_only_error_code(
    monkeypatch,
):
    def _boom(self, request, registry):
        raise RuntimeError("sensitive detail /tmp/secret/key material")

    monkeypatch.setattr(ProjectResolver, "_canonical_signals", _boom)

    decision = ProjectResolver().resolve(_request(project_hint="eva"), _registry())

    assert decision.state is ProjectResolutionState.UNRESOLVED
    assert decision.method == "resolver_error"
    assert decision.resolved_project == ""
    assert decision.proposed_project == ""
    assert decision.evidence == ()
    surface = repr(decision) + str(decision)
    assert "sensitive" not in surface
    assert "/tmp" not in surface
