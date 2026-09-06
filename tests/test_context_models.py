"""Behavioral contracts for the pure context domain models."""

import dataclasses

import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextExclusionCount,
    ContextItem,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextMatchType,
    ContextMode,
    ContextReadRequest,
    ContextReadResult,
    ContextRetrievalRecord,
    ContextScope,
    ContextScoreComponents,
    ContextSearchRequest,
    ContextSearchResult,
    ContextSelectionReason,
    ContextServiceStatus,
    ContextSessionStartRequest,
    ContextSessionStartResult,
    ContextStatus,
    ContextTier,
    ContextValidationError,
    parse_context_mode,
)


def test_context_enums_expose_the_persisted_values():
    """A missing enum value would prevent persisted context data from loading."""
    assert {member.value for member in ContextContentType} == {
        "decision", "fact", "experience", "playbook", "workflow_policy",
        "constraint", "preference", "user_profile", "reference", "session_summary",
        "project_summary", "workstream_checkpoint",
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


def test_mode_match_and_selection_enums_expose_the_persisted_values():
    """Mode/match/selection values are protocol-visible; renaming breaks adapters."""
    assert {member.value for member in ContextMode} == {
        "legacy", "compat", "shadow", "primary",
    }
    assert {member.value for member in ContextMatchType} == {
        "lexical", "vector", "pinned_policy",
    }
    assert {member.value for member in ContextSelectionReason} == {
        "pinned_policy", "lexical", "vector",
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("legacy", ContextMode.LEGACY),
        ("compat", ContextMode.COMPAT),
        ("shadow", ContextMode.SHADOW),
        ("primary", ContextMode.PRIMARY),
        (ContextMode.PRIMARY, ContextMode.PRIMARY),
    ],
)
def test_parse_context_mode_accepts_exact_known_values(raw, expected):
    assert parse_context_mode(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "primray",   # 评审复现的 typo：不得抛 ValueError 崩溃调用方
        "turbo",
        "",
        " primary",  # 不修剪、不大小写折叠：未知即 None
        "Primary",
        None,
        0,
        b"legacy",
        ["legacy"],
    ],
)
def test_parse_context_mode_fails_closed_to_none(raw):
    """Unknown or non-string modes yield None; adapters never coerce to primary."""
    assert parse_context_mode(raw) is None


def _score_components(**overrides):
    values = dict(
        relevance=0.9,
        project=1.0,
        type_priority=0.8,
        confidence=0.7,
        importance=0.6,
        evidence=0.5,
        recency=0.4,
        frequency=0.3,
    )
    values.update(overrides)
    return ContextScoreComponents(**values)


def _item(**overrides):
    values = dict(
        id=1,
        identity_key="project:test:fact:sample",
        content_type=ContextContentType.FACT,
        project="test",
        scope=ContextScope.PROJECT,
        status=ContextStatus.ACTIVE,
        tier=ContextTier.NORMAL,
        tags=(),
        importance=7.5,
        confidence=0.8,
        source_state="none",
        source_count=0,
        success_count=0,
        failure_count=0,
        access_count=0,
        last_accessed=None,
        last_verified_at=None,
        expires_at=None,
        supersedes=None,
        superseded_by=None,
        created_at="2026-08-18 00:00:00",
        updated_at="2026-08-18 00:00:00",
        layers=None,
    )
    values.update(overrides)
    return ContextItem(**values)


def _search_result(**overrides):
    values = dict(
        id=7,
        identity_key="project:test:fact:sample",
        l0="summary",
        content_type=ContextContentType.FACT,
        scope=ContextScope.PROJECT,
        project="test",
        status=ContextStatus.ACTIVE,
        tier=ContextTier.NORMAL,
        confidence=0.8,
        importance=7.5,
        score=0.66,
        score_components=_score_components(),
        match_types=(ContextMatchType.LEXICAL,),
        match_layers=(ContextLayer.L0,),
        available_layers=(ContextLayer.L0, ContextLayer.L1, ContextLayer.L2),
    )
    values.update(overrides)
    return ContextSearchResult(**values)


def _service_status(**overrides):
    values = dict(
        mode=ContextMode.PRIMARY,
        adapter="codex",
        ready=True,
        status_counts={"active": 4, "candidate": 1},
        mapping_count=9,
        projection_lag=0,
        context_vector_ready=True,
        context_vector_dirty=False,
        legacy_vector_ready=True,
        legacy_vector_dirty=False,
        diagnostics=("fts_only",),
    )
    values.update(overrides)
    return ContextServiceStatus(**values)


def test_search_request_normalizes_the_query_and_keeps_documented_defaults():
    """The MCP contract defaults must stay stable for adapters compiled against them."""
    request = ContextSearchRequest(query="  refund policy  ")

    assert request.query == "refund policy"
    assert request.project == ""
    assert request.top_k == 10
    assert request.content_types == ()
    assert request.cross_project is False


@pytest.mark.parametrize("query", ["", " \t\n "])
def test_search_request_rejects_blank_queries(query):
    """A blank query would make every candidate generation path meaningless."""
    with pytest.raises(ContextValidationError, match="query"):
        ContextSearchRequest(query=query)


@pytest.mark.parametrize("top_k", [0, 21, -1, True, 1.5, "10"])
def test_search_request_rejects_top_k_outside_1_to_20(top_k):
    """The public contract caps top_k at 1..20 and booleans are not integers."""
    with pytest.raises(ContextValidationError, match="top_k"):
        ContextSearchRequest(query="q", top_k=top_k)


def test_search_request_validates_content_types_and_cross_project():
    """MCP dictionaries must not leak untyped values into the domain boundary."""
    request = ContextSearchRequest(
        query="q",
        content_types=[ContextContentType.FACT, ContextContentType.DECISION],  # type: ignore[arg-type]
    )
    assert request.content_types == (ContextContentType.FACT, ContextContentType.DECISION)

    with pytest.raises(ContextValidationError, match="content_types"):
        ContextSearchRequest(query="q", content_types=("fact",))  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="cross_project"):
        ContextSearchRequest(query="q", cross_project="yes")  # type: ignore[arg-type]


def test_read_request_defaults_to_l1_and_rejects_unsupported_layers():
    """Exact disclosure is L1/L2 only; L0 already travels with search results."""
    assert ContextReadRequest(id=1).layer is ContextLayer.L1

    with pytest.raises(ContextValidationError, match="layer"):
        ContextReadRequest(id=1, layer=ContextLayer.L0)
    with pytest.raises(ContextValidationError, match="layer"):
        ContextReadRequest(id=1, layer="l1")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", [0, -3, True, 1.5, "1"])
def test_read_request_rejects_non_positive_or_boolean_ids(bad_id):
    """A boolean-as-number or non-integer id must never reach exact-layer SQL."""
    with pytest.raises(ContextValidationError, match="id"):
        ContextReadRequest(id=bad_id)


def test_session_start_request_validates_query_and_max_chars():
    """Session start needs a real task query and only a positive budget override."""
    request = ContextSessionStartRequest(project="evolvmem", query="first task")
    assert request.max_chars is None

    with pytest.raises(ContextValidationError, match="query"):
        ContextSessionStartRequest(project="evolvmem", query="  ")
    for bad in (0, -5, True, 1.5):
        with pytest.raises(ContextValidationError, match="max_chars"):
            ContextSessionStartRequest(
                project="evolvmem", query="task", max_chars=bad  # type: ignore[arg-type]
            )


def test_score_components_are_normalized_numbers_and_reject_booleans():
    """Each ranking component must stay inside the documented 0..1 normalization."""
    components = _score_components()
    assert components.relevance == 0.9

    with pytest.raises(ContextValidationError, match="relevance"):
        _score_components(relevance=True)
    with pytest.raises(ContextValidationError, match="recency"):
        _score_components(recency=1.01)
    with pytest.raises(ContextValidationError, match="frequency"):
        _score_components(frequency=-0.01)


@pytest.mark.parametrize(
    "field, value",
    [("confidence", 1.5), ("confidence", True), ("importance", 0.5),
     ("importance", True), ("score", -0.1), ("score", 1.1), ("score", True)],
)
def test_search_result_rejects_invalid_confidence_importance_and_score(field, value):
    """Out-of-range metrics would make threshold and ranking semantics ambiguous."""
    with pytest.raises(ContextValidationError, match=field):
        _search_result(**{field: value})


def test_search_result_requires_typed_enums_and_component_containers():
    """Plain strings must not impersonate domain enums inside a typed result."""
    with pytest.raises(ContextValidationError, match="content_type"):
        _search_result(content_type="fact")  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="score_components"):
        _search_result(score_components={"relevance": 1.0})  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="match_types"):
        _search_result(match_types=("lexical",))  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="available_layers"):
        _search_result(available_layers=("l0",))  # type: ignore[arg-type]


def test_read_result_carries_only_structured_error_codes():
    """A failed exact read reports a stable reason instead of a nearby substitute."""
    ok = ContextReadResult(id=3, layer=ContextLayer.L1, content="detail")
    assert ok.error_code is None

    for code in ("not_found", "not_readable", "expired", "invalid_layer"):
        result = ContextReadResult(id=3, layer=ContextLayer.L2, content="", error_code=code)
        assert result.error_code == code

    with pytest.raises(ContextValidationError, match="error_code"):
        ContextReadResult(id=3, layer=ContextLayer.L1, content="", error_code="nearby_item")


def test_session_start_result_and_exclusion_counts_are_typed_and_immutable():
    """Exclusion accounting must stay countable and tamper-proof for status output."""
    result = ContextSessionStartResult(
        block="[history]",
        selected_ids=(2, 5),
        used_chars=9,
        excluded_counts=(ContextExclusionCount(reason="below_confidence", count=3),),
    )
    assert result.selected_ids == (2, 5)
    assert result.excluded_counts[0].reason == "below_confidence"

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.used_chars = 0  # type: ignore[misc]
    with pytest.raises(ContextValidationError, match="count"):
        ContextExclusionCount(reason="expired", count=-1)
    with pytest.raises(ContextValidationError, match="count"):
        ContextExclusionCount(reason="expired", count=True)
    with pytest.raises(ContextValidationError, match="used_chars"):
        ContextSessionStartResult(
            block="", selected_ids=(), used_chars=-1, excluded_counts=()
        )


def test_service_status_is_a_typed_immutable_snapshot():
    """Status snapshots must carry typed mode/flags so adapters cannot misread them."""
    status = _service_status()
    assert status.mode is ContextMode.PRIMARY
    assert status.diagnostics == ("fts_only",)

    with pytest.raises(dataclasses.FrozenInstanceError):
        status.ready = False  # type: ignore[misc]
    with pytest.raises(ContextValidationError, match="mode"):
        _service_status(mode="primary")  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="ready"):
        _service_status(ready="yes")  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="mapping_count"):
        _service_status(mapping_count=-1)


def test_retrieval_record_requires_a_typed_item_and_typed_layers():
    """Retrieval records feed ranking; untyped payloads would hide store bugs."""
    record = ContextRetrievalRecord(
        item=_item(),
        l0="summary",
        available_layers=(ContextLayer.L0, ContextLayer.L1, ContextLayer.L2),
    )
    assert record.l0 == "summary"

    with pytest.raises(ContextValidationError, match="item"):
        ContextRetrievalRecord(item="not-an-item", l0="x", available_layers=())  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError, match="available_layers"):
        ContextRetrievalRecord(item=_item(), l0="x", available_layers=("l0",))  # type: ignore[arg-type]


def test_read_contracts_are_immutable():
    """Request/result objects cross adapter boundaries and must not be mutated."""
    request = ContextSearchRequest(query="q")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.top_k = 5  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _search_result().score = 0.1  # type: ignore[misc]
