"""Behavioral contracts for the pure context domain models."""

import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)


def test_context_enums_expose_the_persisted_values():
    """A missing enum value would prevent persisted context data from loading."""
    assert {member.value for member in ContextContentType} == {
        "decision", "fact", "experience", "playbook", "workflow_policy",
        "constraint", "preference", "user_profile", "reference", "session_summary",
    }
    assert {member.value for member in ContextStatus} == {
        "candidate", "active", "superseded", "archived", "deleted",
    }
    assert {member.value for member in ContextScope} == {"global", "project"}
    assert {member.value for member in ContextTier} == {"pinned", "normal", "reference"}
    assert {member.value for member in ContextLayer} == {"l0", "l1", "l2"}


def test_draft_normalizes_identity_and_tags_into_stable_values():
    """Equivalent user spelling must not create unstable identity or tag values."""
    draft = ContextItemDraft(
        identity_key="  project: hermes  ",
        content_type=ContextContentType.FACT,
        layers=ContextLayers(l0="fact", l1="A fact.", l2="The full fact.", generator="user"),
        tags=["  python", "", "python", " testing "],  # type: ignore[arg-type]
    )

    assert draft.identity_key == "project: hermes"
    assert draft.tags == ("python", "testing")


def test_draft_retains_canonical_normalized_layer_content():
    """Persisting raw layer whitespace would make equivalent context records differ."""
    draft = ContextItemDraft(
        identity_key="project:hermes",
        content_type=ContextContentType.FACT,
        layers=ContextLayers(
            l0=" \r\n summary \t",
            l1="\t description\r\nwith detail \r\n",
            l2=" \r\n full source\r\nwith evidence \t",
            generator="user",
        ),
    )

    assert draft.layers == ContextLayers(
        l0="summary",
        l1="description\nwith detail",
        l2="full source\nwith evidence",
        generator="user",
    )


@pytest.mark.parametrize("identity_key", ["", " \t\n "])
def test_draft_rejects_empty_identity_keys(identity_key):
    """An empty canonical key would make context replacement ambiguous."""
    with pytest.raises(ContextValidationError, match="identity_key"):
        ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.FACT,
            layers=ContextLayers(l0="fact", l1="A fact.", l2="The full fact.", generator="user"),
        )


@pytest.mark.parametrize(
    "layers",
    [
        ContextLayers(l0="", l1="description", l2="full source", generator="user"),
        ContextLayers(l0="summary", l1="", l2="full source", generator="user"),
        ContextLayers(l0="summary", l1="description", l2="", generator="user"),
    ],
)
def test_draft_requires_all_three_nonempty_layers(layers):
    """Dropping any layer would break the L0/L1/L2 retrieval contract."""
    with pytest.raises(ContextValidationError, match="l[012]"):
        ContextItemDraft(
            identity_key="project:hermes",
            content_type=ContextContentType.FACT,
            layers=layers,
        )


@pytest.mark.parametrize("field, value", [("importance", 0), ("importance", 10.1), ("confidence", -0.1), ("confidence", 1.1)])
def test_draft_rejects_scores_outside_their_allowed_ranges(field, value):
    """Out-of-range scores would make ranking semantics inconsistent."""
    kwargs = {field: value}

    with pytest.raises(ContextValidationError, match=field):
        ContextItemDraft(
            identity_key="project:hermes",
            content_type=ContextContentType.FACT,
            layers=ContextLayers(l0="fact", l1="A fact.", l2="The full fact.", generator="user"),
            **kwargs,
        )
