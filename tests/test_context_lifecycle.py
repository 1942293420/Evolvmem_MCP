"""Behavioral contracts for candidate lifecycle: evidence, confirm, promotion."""

import logging
import math

import pytest

from evolvmem.context_lifecycle import (
    CandidateSummary,
    ContextLifecycle,
    ContextLifecycleError,
    EvidenceReport,
    PlaybookEligibilityReport,
    PromotionReport,
)
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def lifecycle(test_config, store):
    return ContextLifecycle(test_config, store)


def _make_item(
    store,
    identity_key: str,
    *,
    status: ContextStatus = ContextStatus.CANDIDATE,
    content_type: ContextContentType = ContextContentType.EXPERIENCE,
    project: str = "proj",
    scope: ContextScope = ContextScope.PROJECT,
    confidence: float = 0.8,
    l0: str | None = None,
):
    return store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=content_type,
            layers=ContextLayers(
                l0=l0 or f"Summary of {identity_key}",
                l1="Supporting detail for the lifecycle test item.",
                l2="Full source material for the lifecycle test item.",
                generator="test-suite",
            ),
            project=project,
            scope=scope,
            status=status,
            tier=ContextTier.NORMAL,
            tags=("lifecycle",),
            importance=6.0,
            confidence=confidence,
        )
    )


def _make_archive(store, external_id: str, *, project: str = "proj") -> int:
    with store.transaction():
        return store.upsert_session_archive(
            project,
            "codex",
            external_id,
            payload_path=f"session_archives/{external_id}.bin",
            payload_sha256="a" * 64,
            expires_at="2026-12-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )


def _link_archive(store, item_id: int, archive_id: int) -> int:
    with store.transaction():
        return store.record_session_source(
            item_id, archive_id, extraction_version="test-v1"
        )


def _success_from_new_archive(lifecycle, store, item_id: int, external_id: str) -> None:
    archive_id = _make_archive(store, external_id)
    source_id = _link_archive(store, item_id, archive_id)
    lifecycle.record_outcome(item_id, "success", source_id=source_id)


def _eligible_experience(
    lifecycle,
    store,
    identity_key: str,
    *,
    project: str = "proj",
    scope: ContextScope = ContextScope.PROJECT,
    l0: str,
    successes: int = 2,
) -> int:
    item = _make_item(
        store,
        identity_key,
        status=ContextStatus.ACTIVE,
        project=project,
        scope=scope,
        l0=l0,
    )
    for _ in range(successes):
        lifecycle.record_outcome(item.id, "success")
    return item.id


class _FakeEngine:
    """Deterministic stand-in: maps exact L0 texts to fixed vectors."""

    def __init__(self, vectors: dict[str, list[float]], *, loaded: bool = True):
        self._vectors = vectors
        self.is_loaded = loaded

    def encode_document(self, text: str) -> list[float]:
        return list(self._vectors[text])


class _RaisingEngine:
    is_loaded = True

    def encode_document(self, text: str) -> list[float]:
        raise RuntimeError("backend exploded at /home/alice/secret.gguf")


# Frozen boundary vectors: against [1.0, 0.0] the module's normalized cosine
# (1+cos)/2 is exactly 0.95 for _BOUNDARY and 0.9499 for _BELOW.
_BOUNDARY = [0.9, math.sqrt(1.0 - 0.9**2)]
_BELOW = [0.8998, math.sqrt(1.0 - 0.8998**2)]
_UNIT_X = [1.0, 0.0]


# ---- record_outcome: validation and typed errors ----


def test_record_outcome_rejects_unknown_outcome(lifecycle, store):
    item = _make_item(store, "experience:test:outcome-validation")

    with pytest.raises(ContextValidationError, match="outcome"):
        lifecycle.record_outcome(item.id, "win")
    with pytest.raises(ContextValidationError, match="outcome"):
        lifecycle.record_outcome(item.id, 42)


def test_record_outcome_validates_ids_and_note_types(lifecycle, store):
    item = _make_item(store, "experience:test:id-validation")

    with pytest.raises(ContextValidationError, match="item_id"):
        lifecycle.record_outcome(0, "success")
    with pytest.raises(ContextValidationError, match="item_id"):
        lifecycle.record_outcome(True, "success")
    with pytest.raises(ContextValidationError, match="note"):
        lifecycle.record_outcome(item.id, "success", note=b"bytes")
    with pytest.raises(ContextValidationError, match="source_id"):
        lifecycle.record_outcome(item.id, "success", source_id=True)


def test_record_outcome_item_not_found_is_typed(lifecycle):
    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.record_outcome(999, "success")

    assert excinfo.value.code == "item_not_found"


def test_record_outcome_rejects_sensitive_note_without_storing_anything(
        lifecycle, store):
    item = _make_item(store, "experience:test:sensitive-note", status=ContextStatus.ACTIVE)
    secret_note = "api_key=sk-live-secret-123"

    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.record_outcome(item.id, "success", note=secret_note)

    assert excinfo.value.code == "sensitive_note"
    assert "sk-live-secret-123" not in str(excinfo.value)
    assert store.list_evidence(item.id) == []
    reloaded = store.get_item(item.id)
    assert reloaded.success_count == 0
    assert reloaded.confidence == 0.8


def test_record_outcome_stores_plain_note(lifecycle, store):
    item = _make_item(store, "experience:test:plain-note", status=ContextStatus.ACTIVE)

    report = lifecycle.record_outcome(item.id, "success", note="复用该方法再次验证成功")

    rows = store.list_evidence(item.id)
    assert [row["note"] for row in rows] == ["复用该方法再次验证成功"]
    assert rows[0]["id"] == report.evidence_id
    assert rows[0]["outcome"] == "success"


def test_record_outcome_rejects_non_active_or_candidate_items(lifecycle, store):
    archived = _make_item(store, "experience:test:archived-target")
    with store.transaction():
        store.set_item_status(archived.id, ContextStatus.ARCHIVED)
    superseded = _make_item(store, "experience:test:superseded-target")
    with store.transaction():
        store.set_item_status(superseded.id, ContextStatus.SUPERSEDED)

    for target in (archived, superseded):
        for outcome in ("success", "failure", "confirmed", "contradicted"):
            with pytest.raises(ContextLifecycleError) as excinfo:
                lifecycle.record_outcome(target.id, outcome)
            assert excinfo.value.code == "invalid_item_state"


def test_record_outcome_source_must_belong_to_the_item(lifecycle, store):
    item = _make_item(store, "experience:test:source-owner", status=ContextStatus.ACTIVE)
    other = _make_item(store, "experience:test:source-other", status=ContextStatus.ACTIVE)
    archive_id = _make_archive(store, "sess-owner")
    foreign_source_id = _link_archive(store, other.id, archive_id)

    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.record_outcome(item.id, "success", source_id=999)
    assert excinfo.value.code == "invalid_source"

    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.record_outcome(item.id, "success", source_id=foreign_source_id)
    assert excinfo.value.code == "invalid_source"
    assert store.list_evidence(item.id) == []


# ---- record_outcome: counters, confidence, timestamps ----


def test_outcome_counters_track_success_and_failure_only(lifecycle, store):
    item = _make_item(
        store,
        "experience:test:counters",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
    )

    lifecycle.record_outcome(item.id, "success")
    lifecycle.record_outcome(item.id, "failure")
    lifecycle.record_outcome(item.id, "confirmed")
    lifecycle.record_outcome(item.id, "contradicted")

    reloaded = store.get_item(item.id)
    assert reloaded.success_count == 1
    assert reloaded.failure_count == 1
    outcomes = [row["outcome"] for row in store.list_evidence(item.id)]
    assert outcomes == ["success", "failure", "confirmed", "contradicted"]


def test_confidence_penalty_is_deterministic_and_floored(lifecycle, store):
    """Frozen formula: failure/contradicted -> confidence = max(0.0, c - 0.1)."""
    item = _make_item(
        store,
        "experience:test:confidence",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
        confidence=0.8,
    )

    report = lifecycle.record_outcome(item.id, "failure")
    assert report.confidence == pytest.approx(0.7)
    report = lifecycle.record_outcome(item.id, "contradicted")
    assert report.confidence == pytest.approx(0.6)
    # success and confirmed never move confidence.
    report = lifecycle.record_outcome(item.id, "success")
    assert report.confidence == pytest.approx(0.6)
    report = lifecycle.record_outcome(item.id, "confirmed")
    assert report.confidence == pytest.approx(0.6)

    low = _make_item(
        store,
        "experience:test:confidence-floor",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
        confidence=0.05,
    )
    report = lifecycle.record_outcome(low.id, "failure")
    assert report.confidence == 0.0
    report = lifecycle.record_outcome(low.id, "contradicted")
    assert report.confidence == 0.0
    assert store.get_item(low.id).confidence == 0.0


def test_verified_timestamp_set_on_success_and_confirmed_only(lifecycle, store):
    item = _make_item(
        store,
        "experience:test:verified-at",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
    )
    assert store.get_item(item.id).last_verified_at is None

    lifecycle.record_outcome(item.id, "failure")
    assert store.get_item(item.id).last_verified_at is None

    lifecycle.record_outcome(item.id, "success")
    verified = store.get_item(item.id).last_verified_at
    assert verified is not None

    lifecycle.record_outcome(item.id, "contradicted")
    assert store.get_item(item.id).last_verified_at == verified

    lifecycle.record_outcome(item.id, "confirmed")
    assert store.get_item(item.id).last_verified_at is not None


def test_record_outcome_rolls_back_when_bookkeeping_fails(
        lifecycle, store, monkeypatch):
    item = _make_item(store, "experience:test:rollback", status=ContextStatus.ACTIVE)

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic bookkeeping failure")

    monkeypatch.setattr(store, "update_outcome_stats", boom)

    with pytest.raises(RuntimeError, match="synthetic bookkeeping failure"):
        lifecycle.record_outcome(item.id, "success")

    assert store.list_evidence(item.id) == []
    reloaded = store.get_item(item.id)
    assert reloaded.success_count == 0
    assert reloaded.confidence == 0.8


# ---- failure-driven archive and playbook demotion ----


def test_active_experience_archived_at_equal_failure_boundary(lifecycle, store):
    item = _make_item(store, "experience:test:archive-equal", status=ContextStatus.ACTIVE)
    lifecycle.record_outcome(item.id, "success")

    report = lifecycle.record_outcome(item.id, "failure")

    assert report.archived is True
    assert report.status is ContextStatus.ARCHIVED
    assert store.get_item(item.id).status is ContextStatus.ARCHIVED


def test_active_experience_archived_with_single_failure(lifecycle, store):
    item = _make_item(store, "experience:test:archive-first", status=ContextStatus.ACTIVE)

    report = lifecycle.record_outcome(item.id, "failure")

    assert report.archived is True
    assert store.get_item(item.id).failure_count == 1
    assert store.get_item(item.id).success_count == 0


def test_active_experience_survives_below_failure_boundary(lifecycle, store):
    item = _make_item(store, "experience:test:archive-below", status=ContextStatus.ACTIVE)
    lifecycle.record_outcome(item.id, "success")
    lifecycle.record_outcome(item.id, "success")

    report = lifecycle.record_outcome(item.id, "failure")

    assert report.archived is False
    assert report.status is ContextStatus.ACTIVE
    assert store.get_item(item.id).status is ContextStatus.ACTIVE


def test_failure_never_archives_candidate_experience_or_other_types(
        lifecycle, store):
    candidate = _make_item(store, "experience:test:candidate-stays")
    report = lifecycle.record_outcome(candidate.id, "failure")
    assert report.archived is False
    assert store.get_item(candidate.id).status is ContextStatus.CANDIDATE

    fact = _make_item(
        store,
        "experience:test:fact-stays",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
    )
    report = lifecycle.record_outcome(fact.id, "failure")
    assert report.archived is False
    assert store.get_item(fact.id).status is ContextStatus.ACTIVE


def test_archiving_experience_demotes_dependent_playbooks(lifecycle, store):
    experience = _make_item(
        store, "experience:test:demote-source", status=ContextStatus.ACTIVE
    )
    dependent = _make_item(
        store,
        "playbook:test:dependent",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.PLAYBOOK,
    )
    unrelated = _make_item(
        store,
        "playbook:test:unrelated",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.PLAYBOOK,
    )
    candidate_playbook = _make_item(
        store,
        "playbook:test:candidate-dependent",
        content_type=ContextContentType.PLAYBOOK,
    )
    with store.transaction():
        store.record_experience_source(
            dependent.id, experience.id, extraction_version="test-v1"
        )
        store.record_experience_source(
            candidate_playbook.id, experience.id, extraction_version="test-v1"
        )

    report = lifecycle.record_outcome(experience.id, "failure")

    assert report.archived is True
    assert report.demoted_playbook_ids == (dependent.id,)
    assert store.get_item(dependent.id).status is ContextStatus.CANDIDATE
    assert store.get_item(unrelated.id).status is ContextStatus.ACTIVE
    assert store.get_item(candidate_playbook.id).status is ContextStatus.CANDIDATE


# ---- confirm ----


def test_confirm_promotes_candidate_and_writes_confirmed_evidence(
        lifecycle, store):
    item = _make_item(store, "experience:test:confirm-happy")

    report = lifecycle.confirm(item.id)

    assert isinstance(report, EvidenceReport)
    assert report.outcome == "confirmed"
    assert report.status is ContextStatus.ACTIVE
    reloaded = store.get_item(item.id)
    assert reloaded.status is ContextStatus.ACTIVE
    assert reloaded.success_count == 0
    assert reloaded.failure_count == 0
    assert reloaded.confidence == 0.8
    assert reloaded.last_verified_at is not None
    rows = store.list_evidence(item.id)
    assert [row["outcome"] for row in rows] == ["confirmed"]


def test_confirm_rejects_non_candidate_states(lifecycle, store):
    active = _make_item(store, "experience:test:confirm-active", status=ContextStatus.ACTIVE)
    archived = _make_item(store, "experience:test:confirm-archived")
    with store.transaction():
        store.set_item_status(archived.id, ContextStatus.ARCHIVED)

    for target in (active, archived):
        with pytest.raises(ContextLifecycleError) as excinfo:
            lifecycle.confirm(target.id)
        assert excinfo.value.code == "invalid_item_state"
        assert store.list_evidence(target.id) == []

    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.confirm(999)
    assert excinfo.value.code == "item_not_found"


def test_confirm_rejects_identity_conflict_with_active_item(lifecycle, store):
    _make_item(store, "experience:test:confirm-conflict", status=ContextStatus.ACTIVE)
    candidate = _make_item(store, "experience:test:confirm-conflict")

    with pytest.raises(ContextLifecycleError) as excinfo:
        lifecycle.confirm(candidate.id)

    assert excinfo.value.code == "identity_conflict"
    assert store.get_item(candidate.id).status is ContextStatus.CANDIDATE
    assert store.list_evidence(candidate.id) == []


# ---- evaluate_promotions ----


def test_promotion_requires_two_distinct_archives(lifecycle, store):
    item = _make_item(store, "experience:test:promote-dedup")
    archive_id = _make_archive(store, "sess-same")
    source_id = _link_archive(store, item.id, archive_id)
    # Two successes backed by the SAME archive count as one distinct source.
    lifecycle.record_outcome(item.id, "success", source_id=source_id)
    lifecycle.record_outcome(item.id, "success", source_id=source_id)

    report = lifecycle.evaluate_promotions()

    assert isinstance(report, PromotionReport)
    assert report.promoted_ids == ()
    assert [(skip.item_id, skip.reason) for skip in report.skipped] == [
        (item.id, "insufficient_distinct_archives")
    ]
    assert store.get_item(item.id).status is ContextStatus.CANDIDATE


def test_promotion_ignores_successes_without_archive_sources(lifecycle, store):
    item = _make_item(store, "experience:test:promote-no-archive")
    _success_from_new_archive(lifecycle, store, item.id, "sess-one")
    # Source-less successes and non-archive sources never join the dedup set.
    lifecycle.record_outcome(item.id, "success")
    with store.transaction():
        plain_source = store.record_experience_source(
            item.id, item.id, extraction_version="test-v1"
        )
    lifecycle.record_outcome(item.id, "success", source_id=plain_source)

    report = lifecycle.evaluate_promotions()

    assert report.promoted_ids == ()
    assert [(skip.item_id, skip.reason) for skip in report.skipped] == [
        (item.id, "insufficient_distinct_archives")
    ]


def test_promotion_activates_experience_with_two_archive_successes(
        lifecycle, store):
    item = _make_item(store, "experience:test:promote-happy")
    _success_from_new_archive(lifecycle, store, item.id, "sess-a")
    _success_from_new_archive(lifecycle, store, item.id, "sess-b")

    report = lifecycle.evaluate_promotions()

    assert report.promoted_ids == (item.id,)
    assert report.skipped == ()
    assert store.get_item(item.id).status is ContextStatus.ACTIVE


def test_promotion_blocked_by_any_failure(lifecycle, store):
    item = _make_item(store, "experience:test:promote-failure")
    _success_from_new_archive(lifecycle, store, item.id, "sess-fa")
    _success_from_new_archive(lifecycle, store, item.id, "sess-fb")
    lifecycle.record_outcome(item.id, "failure")

    report = lifecycle.evaluate_promotions()

    assert report.promoted_ids == ()
    assert [(skip.item_id, skip.reason) for skip in report.skipped] == [
        (item.id, "has_failures")
    ]
    assert store.get_item(item.id).status is ContextStatus.CANDIDATE


def test_promotion_only_considers_candidate_experiences(lifecycle, store):
    fact = _make_item(
        store, "experience:test:promote-fact", content_type=ContextContentType.FACT
    )
    _success_from_new_archive(lifecycle, store, fact.id, "sess-fact-a")
    _success_from_new_archive(lifecycle, store, fact.id, "sess-fact-b")

    report = lifecycle.evaluate_promotions()

    assert report.promoted_ids == ()
    assert report.skipped == ()
    assert store.get_item(fact.id).status is ContextStatus.CANDIDATE


def test_promotion_skips_identity_conflict_instead_of_raising(lifecycle, store):
    _make_item(store, "experience:test:promote-conflict", status=ContextStatus.ACTIVE)
    candidate = _make_item(store, "experience:test:promote-conflict")
    _success_from_new_archive(lifecycle, store, candidate.id, "sess-ca")
    _success_from_new_archive(lifecycle, store, candidate.id, "sess-cb")

    report = lifecycle.evaluate_promotions()

    assert report.promoted_ids == ()
    assert [(skip.item_id, skip.reason) for skip in report.skipped] == [
        (candidate.id, "identity_conflict")
    ]
    assert store.get_item(candidate.id).status is ContextStatus.CANDIDATE


def test_promotion_batch_is_atomic(lifecycle, store, monkeypatch):
    first = _make_item(store, "experience:test:promote-atomic-a")
    second = _make_item(store, "experience:test:promote-atomic-b")
    for item, external in ((first, "sess-a1"), (second, "sess-b1")):
        _success_from_new_archive(lifecycle, store, item.id, external)
        _success_from_new_archive(lifecycle, store, item.id, external + "x")

    calls = []
    real_set_status = store.set_item_status

    def fail_on_second(item_id, status):
        calls.append(item_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic promotion failure")
        return real_set_status(item_id, status)

    monkeypatch.setattr(store, "set_item_status", fail_on_second)

    with pytest.raises(RuntimeError, match="synthetic promotion failure"):
        lifecycle.evaluate_promotions()

    assert store.get_item(first.id).status is ContextStatus.CANDIDATE
    assert store.get_item(second.id).status is ContextStatus.CANDIDATE


def test_promotion_report_carries_no_content(lifecycle, store):
    item = _make_item(
        store,
        "experience:test:distinctive-identity",
        l0="绝密正文绝不出现在晋升报告里",
    )

    report = lifecycle.evaluate_promotions()
    rendered = repr(report)

    assert "distinctive-identity" not in rendered
    assert "绝密正文" not in rendered
    assert [(skip.item_id, skip.reason) for skip in report.skipped] == [
        (item.id, "insufficient_distinct_archives")
    ]


# ---- evaluate_playbook_eligibility ----


def test_playbook_eligibility_clusters_similar_experiences(lifecycle, store):
    vectors = {}
    for name in ("alpha", "beta", "gamma"):
        l0 = f"l0-{name}"
        vectors[l0] = list(_UNIT_X)
        _eligible_experience(lifecycle, store, f"experience:test:pb-{name}", l0=l0)

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )

    assert isinstance(report, PlaybookEligibilityReport)
    assert report.reason == "ok"
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster.scope is ContextScope.PROJECT
    assert cluster.project == "proj"
    assert len(cluster.item_ids) == 3
    assert cluster.min_similarity == pytest.approx(1.0)


def test_playbook_cluster_boundary_similarity_qualifies(lifecycle, store):
    """Frozen boundary: normalized similarity exactly 0.95 is eligible."""
    vectors = {"l0-alpha": list(_UNIT_X), "l0-beta": list(_BOUNDARY), "l0-gamma": list(_BOUNDARY)}
    for name in ("alpha", "beta", "gamma"):
        _eligible_experience(lifecycle, store, f"experience:test:pb-b-{name}", l0=f"l0-{name}")

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )

    assert report.reason == "ok"
    assert len(report.clusters) == 1
    assert report.clusters[0].min_similarity == 0.95


def test_playbook_cluster_below_boundary_is_rejected(lifecycle, store):
    """Frozen boundary: normalized similarity 0.9499 is not eligible."""
    vectors = {"l0-alpha": list(_UNIT_X), "l0-beta": list(_BELOW), "l0-gamma": list(_BELOW)}
    for name in ("alpha", "beta", "gamma"):
        _eligible_experience(lifecycle, store, f"experience:test:pb-c-{name}", l0=f"l0-{name}")

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )

    assert report.reason == "ok"
    assert report.clusters == ()


def test_playbook_eligibility_requires_min_successes_per_experience(
        lifecycle, store, test_config):
    vectors = {}
    for name in ("alpha", "beta", "gamma"):
        vectors[f"l0-{name}"] = list(_UNIT_X)
    _eligible_experience(lifecycle, store, "experience:test:pb-min-a", l0="l0-alpha")
    _eligible_experience(lifecycle, store, "experience:test:pb-min-b", l0="l0-beta")
    thin = _make_item(store, "experience:test:pb-min-c", status=ContextStatus.ACTIVE, l0="l0-gamma")
    lifecycle.record_outcome(thin.id, "success")  # only one success

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )

    assert report.clusters == ()

    # The threshold is configuration-driven: lowering it admits the thin item.
    test_config.context_promotion_min_successes = 1
    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    assert len(report.clusters) == 1
    assert thin.id in report.clusters[0].item_ids


def test_playbook_eligibility_requires_min_cluster_size(
        lifecycle, store, test_config):
    vectors = {"l0-alpha": list(_UNIT_X), "l0-beta": list(_UNIT_X)}
    _eligible_experience(lifecycle, store, "experience:test:pb-small-a", l0="l0-alpha")
    _eligible_experience(lifecycle, store, "experience:test:pb-small-b", l0="l0-beta")

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    assert report.clusters == ()

    test_config.context_playbook_min_experiences = 2
    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    assert len(report.clusters) == 1


def test_playbook_eligibility_groups_by_project_and_global_scope(
        lifecycle, store):
    vectors = {}
    for index in range(2):
        l0 = f"l0-proj-a-{index}"
        vectors[l0] = list(_UNIT_X)
        _eligible_experience(
            lifecycle, store, f"experience:test:pb-iso-a-{index}", project="alpha", l0=l0
        )
    for index in range(2):
        l0 = f"l0-proj-b-{index}"
        vectors[l0] = list(_UNIT_X)
        _eligible_experience(
            lifecycle, store, f"experience:test:pb-iso-b-{index}", project="beta", l0=l0
        )

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    # Two eligible experiences per project never merge across projects.
    assert report.clusters == ()

    global_vectors = {}
    for index in range(3):
        l0 = f"l0-global-{index}"
        global_vectors[l0] = list(_UNIT_X)
        _eligible_experience(
            lifecycle,
            store,
            f"experience:test:pb-global-{index}",
            project="",
            scope=ContextScope.GLOBAL,
            l0=l0,
        )
    global_vectors.update(vectors)
    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(global_vectors)
    )
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster.scope is ContextScope.GLOBAL
    assert cluster.project == ""
    assert len(cluster.item_ids) == 3


def test_playbook_eligibility_excludes_unresolved_contradictions(
        lifecycle, store):
    vectors = {}
    for name in ("alpha", "beta", "gamma"):
        vectors[f"l0-{name}"] = list(_UNIT_X)
    _eligible_experience(lifecycle, store, "experience:test:pb-con-a", l0="l0-alpha")
    _eligible_experience(lifecycle, store, "experience:test:pb-con-b", l0="l0-beta")
    contradicted = _eligible_experience(
        lifecycle, store, "experience:test:pb-con-c", l0="l0-gamma"
    )
    # A contradicted evidence with no newer success/confirmed stays unresolved.
    lifecycle.record_outcome(contradicted, "contradicted")

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    assert report.clusters == ()

    # A newer success resolves the contradiction and restores eligibility.
    lifecycle.record_outcome(contradicted, "success")
    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )
    assert len(report.clusters) == 1
    assert contradicted in report.clusters[0].item_ids


def test_playbook_eligibility_only_considers_active_experiences(lifecycle, store):
    vectors = {}
    for name in ("alpha", "beta"):
        vectors[f"l0-{name}"] = list(_UNIT_X)
        _eligible_experience(lifecycle, store, f"experience:test:pb-act-{name}", l0=f"l0-{name}")
    candidate = _make_item(store, "experience:test:pb-act-c", l0="l0-gamma")
    vectors["l0-gamma"] = list(_UNIT_X)
    for _ in range(2):
        lifecycle.record_outcome(candidate.id, "success")
    fact = _make_item(
        store,
        "experience:test:pb-act-d",
        status=ContextStatus.ACTIVE,
        content_type=ContextContentType.FACT,
        l0="l0-delta",
    )
    vectors["l0-delta"] = list(_UNIT_X)
    for _ in range(2):
        lifecycle.record_outcome(fact.id, "success")

    report = lifecycle.evaluate_playbook_eligibility(
        embedding_engine=_FakeEngine(vectors)
    )

    assert report.clusters == ()


def test_playbook_eligibility_degrades_without_embedding_engine(
        lifecycle, store, caplog):
    l0 = "绝密正文绝不进入资格评估日志"
    _eligible_experience(lifecycle, store, "experience:test:pb-noeng-a", l0=l0)
    _eligible_experience(lifecycle, store, "experience:test:pb-noeng-b", l0="l0-beta")
    _eligible_experience(lifecycle, store, "experience:test:pb-noeng-c", l0="l0-gamma")

    with caplog.at_level(logging.WARNING):
        report = lifecycle.evaluate_playbook_eligibility(embedding_engine=None)
    assert report.clusters == ()
    assert report.reason == "embedding_unavailable"

    with caplog.at_level(logging.WARNING):
        report = lifecycle.evaluate_playbook_eligibility(
            embedding_engine=_FakeEngine({}, loaded=False)
        )
    assert report.reason == "embedding_unavailable"

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered  # content-free degradation warnings were recorded
    assert l0 not in rendered


def test_playbook_eligibility_degrades_when_encoding_fails(
        lifecycle, store, caplog):
    l0 = "绝密正文绝不进入失败日志"
    _eligible_experience(lifecycle, store, "experience:test:pb-raise-a", l0=l0)
    _eligible_experience(lifecycle, store, "experience:test:pb-raise-b", l0="l0-beta")
    _eligible_experience(lifecycle, store, "experience:test:pb-raise-c", l0="l0-gamma")

    with caplog.at_level(logging.WARNING):
        report = lifecycle.evaluate_playbook_eligibility(
            embedding_engine=_RaisingEngine()
        )

    assert report.clusters == ()
    assert report.reason == "embedding_unavailable"
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "backend exploded" not in rendered
    assert "/home/alice" not in rendered
    assert l0 not in rendered


def test_playbook_eligibility_with_too_few_items_never_calls_engine(
        lifecycle, store):
    _eligible_experience(lifecycle, store, "experience:test:pb-lazy-a", l0="l0-alpha")

    class ExplodingOnCall:
        is_loaded = True
        called = False

        def encode_document(self, text):
            self.called = True
            raise AssertionError("must not be called")

    engine = ExplodingOnCall()
    report = lifecycle.evaluate_playbook_eligibility(embedding_engine=engine)

    assert report.reason == "ok"
    assert report.clusters == ()
    assert engine.called is False


# ---- list_candidates ----


def test_list_candidates_returns_l0_and_metadata_only(lifecycle, store):
    candidate = _make_item(store, "experience:test:review-a", l0="候选摘要甲")
    _make_item(store, "experience:test:review-active", status=ContextStatus.ACTIVE)
    archived = _make_item(store, "experience:test:review-archived")
    with store.transaction():
        store.set_item_status(archived.id, ContextStatus.ARCHIVED)

    summaries = lifecycle.list_candidates()

    assert [summary.id for summary in summaries] == [candidate.id]
    summary = summaries[0]
    assert isinstance(summary, CandidateSummary)
    assert summary.l0 == "候选摘要甲"
    assert summary.identity_key == "experience:test:review-a"
    assert summary.content_type is ContextContentType.EXPERIENCE
    assert summary.scope is ContextScope.PROJECT
    assert summary.confidence == 0.8
    assert summary.success_count == 0
    assert summary.failure_count == 0
    assert not hasattr(summary, "l1")
    assert not hasattr(summary, "l2")


def test_list_candidates_filters_by_project(lifecycle, store):
    _make_item(store, "experience:test:review-proj-a", project="alpha")
    kept = _make_item(store, "experience:test:review-proj-b", project="beta")

    summaries = lifecycle.list_candidates(project="beta")

    assert [summary.id for summary in summaries] == [kept.id]
    assert summaries[0].project == "beta"


def test_list_candidates_has_no_access_side_effects(lifecycle, store):
    candidate = _make_item(store, "experience:test:review-readonly")

    lifecycle.list_candidates()
    lifecycle.list_candidates(project="proj")

    reloaded = store.get_item(candidate.id)
    assert reloaded.access_count == 0
    assert reloaded.last_accessed is None
    assert reloaded.status is ContextStatus.CANDIDATE
