"""Behavioral contracts for the deterministic, thresholded ContextRetriever."""

from datetime import datetime, timedelta, timezone
import math

import numpy as np
import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextMatchType,
    ContextScope,
    ContextSearchRequest,
    ContextSearchResult,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_retriever import ContextRetriever
from evolvmem.context_store import ContextStore
from evolvmem.vector_index import VectorIndex


NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
NOW_STAMP = NOW.strftime("%Y-%m-%d %H:%M:%S")

TYPE_PRIORITY_BASE = {
    ContextContentType.WORKFLOW_POLICY: 0.8,
    ContextContentType.CONSTRAINT: 0.8,
    ContextContentType.PREFERENCE: 0.6,
    ContextContentType.USER_PROFILE: 0.6,
    ContextContentType.PLAYBOOK: 0.6,
    ContextContentType.DECISION: 0.5,
    ContextContentType.FACT: 0.4,
    ContextContentType.EXPERIENCE: 0.4,
    ContextContentType.SESSION_SUMMARY: 0.4,
    ContextContentType.REFERENCE: 0.2,
}


def fixed_clock() -> datetime:
    return NOW


class QueryEmbeddingEngine:
    """Deterministic query embeddings that reject the document encoding API."""

    is_loaded = True

    def __init__(self, vector: list[float]):
        self.vector = vector
        self.queries: list[str] = []

    def encode_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(self.vector)

    def encode_document(self, text: str) -> list[float]:
        raise AssertionError("ContextRetriever must never encode documents")

    def encode(self, text: str) -> list[float]:
        raise AssertionError("ContextRetriever must use encode_query")


class UnloadedQueryEngine(QueryEmbeddingEngine):
    is_loaded = False


class FailingQueryEngine:
    is_loaded = True

    def __init__(self):
        self.queries: list[str] = []

    def encode_query(self, text: str) -> list[float]:
        self.queries.append(text)
        raise RuntimeError("embedding backend unavailable")


class FakeVectorIndex:
    """Canned nearest neighbors with the same duck-typed surface as VectorIndex."""

    def __init__(self, config, results=(), *, dirty: bool = False, path=None):
        self.path = (path or config.context_vector_path).resolve()
        self._results = list(results)
        self._dirty = dirty
        self.search_calls: list[int] = []

    def is_dirty(self) -> bool:
        return self._dirty

    def count(self) -> int:
        return len(self._results)

    def search(self, embedding, k: int) -> list[dict]:
        self.search_calls.append(k)
        return [dict(result) for result in self._results[:k]]


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


def make_draft(
    identity_key: str,
    *,
    l0: str = "zebra fact summary",
    l1: str | None = None,
    l2: str | None = None,
    content_type: ContextContentType = ContextContentType.FACT,
    project: str = "proj",
    scope: ContextScope = ContextScope.PROJECT,
    status: ContextStatus = ContextStatus.ACTIVE,
    tier: ContextTier = ContextTier.NORMAL,
    importance: float = 5.0,
    confidence: float = 0.9,
    expires_at: str | None = None,
) -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=content_type,
        layers=ContextLayers(
            l0=l0,
            l1=l1 or f"detail for {identity_key}",
            l2=l2 or f"source for {identity_key}",
            generator="test-suite",
        ),
        project=project,
        scope=scope,
        status=status,
        tier=tier,
        importance=importance,
        confidence=confidence,
        expires_at=expires_at,
    )


def add_item(store, identity_key: str, **overrides) -> ContextItem:
    return store.create_item(make_draft(identity_key, **overrides))


def make_retriever(config, store, vector_index=None, engine=None) -> ContextRetriever:
    if vector_index is None:
        vector_index = FakeVectorIndex(config)
    return ContextRetriever(config, store, vector_index, engine, clock=fixed_clock)


def request_for(query: str = "zebra", **overrides) -> ContextSearchRequest:
    return ContextSearchRequest(
        query=query, **{"project": "proj", **overrides}
    )


def results_by_id(retriever, request) -> dict[int, ContextSearchResult]:
    return {result.id: result for result in retriever.search(request)}


def update_metadata(store, item_id: int, **columns) -> None:
    assignments = ", ".join(f"{name}=?" for name in columns)
    store._conn.execute(
        f"UPDATE context_items SET {assignments} WHERE id=?",
        (*columns.values(), item_id),
    )


# ---- candidate generation: lexical channel ----


def test_l0_fts_candidate_returns_an_l0_only_result(store, test_config):
    """A plain L0 FTS hit must surface with lexical match metadata and no L1/L2."""
    item = add_item(store, "fts-basic", l0="zebra striped animal")

    results = make_retriever(test_config, store).search(request_for())

    assert [result.id for result in results] == [item.id]
    result = results[0]
    assert result.l0 == "zebra striped animal"
    assert result.match_types == (ContextMatchType.LEXICAL,)
    assert result.match_layers == (ContextLayer.L0,)
    assert result.available_layers == (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)
    assert result.status is ContextStatus.ACTIVE
    assert result.project == "proj"
    assert 0.0 < result.score <= 1.0


def test_cjk_trigram_hit_generates_a_candidate(store, test_config):
    """CJK queries reach candidates through the trigram index like the store does."""
    if not store._has_trigram:
        pytest.skip("trigram FTS support unavailable")
    item = add_item(store, "cjk-trigram", l0="破损商品退款规则")

    results = make_retriever(test_config, store).search(request_for("破损商品"))

    assert [result.id for result in results] == [item.id]
    assert results[0].match_types == (ContextMatchType.LEXICAL,)


def test_cjk_like_fallback_counts_as_a_full_lexical_match(store, test_config):
    """All-zero LIKE raw scores still normalize to a full lexical hit of 1.0."""
    item = add_item(store, "cjk-like", l0="破损商品退款规则")
    store._has_trigram = False

    results = make_retriever(test_config, store).search(request_for("退款"))

    assert [result.id for result in results] == [item.id]
    assert results[0].score_components.relevance == pytest.approx(0.6)


def test_l1_term_returns_summary_without_detail_body(store, test_config):
    """Detail keywords recall the item while returning only its short summary."""
    add_item(store, "l1-only", l0="ordinary summary", l1="zebra hidden detail")

    results = make_retriever(test_config, store).search(request_for())

    assert len(results) == 1
    assert results[0].l0 == "ordinary summary"
    assert results[0].match_layers == (ContextLayer.L1,)


# ---- candidate generation: vector channel, fusion, and thresholds ----


def test_vector_only_candidate_above_threshold_is_returned(store, test_config):
    """A lexically invisible item can still surface through the vector channel."""
    item = add_item(store, "vector-only", l0="plain unrelated summary")
    index = FakeVectorIndex(test_config, [{"id": item.id, "distance": 0.4}])
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = make_retriever(test_config, store, index, engine).search(request_for())

    assert [result.id for result in results] == [item.id]
    result = results[0]
    assert result.match_types == (ContextMatchType.VECTOR,)
    assert result.match_layers == (ContextLayer.L0,)
    # similarity = 1 - 0.4 / 2 = 0.80; relevance = 0.60 * 0 + 0.40 * 0.80
    assert result.score_components.relevance == pytest.approx(0.4 * 0.8)
    assert engine.queries == ["zebra"]


def test_fused_candidate_is_returned_once_with_both_match_types(store, test_config):
    """Lexical and vector hits for one ID merge into a single fused candidate."""
    fused = add_item(store, "fused", l0="zebra fused summary")
    lexical_only = add_item(store, "lexical-only", l0="zebra other summary")
    index = FakeVectorIndex(test_config, [{"id": fused.id, "distance": 1.0}])

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert [result.id for result in results].count(fused.id) == 1
    by_id = {result.id: result for result in results}
    assert set(by_id) == {fused.id, lexical_only.id}
    assert by_id[fused.id].match_types == (
        ContextMatchType.LEXICAL,
        ContextMatchType.VECTOR,
    )
    # identical-length L0 texts normalize to lexical 1.0; vector = 1 - 1.0 / 2 = 0.5
    assert by_id[fused.id].score_components.relevance == pytest.approx(0.6 + 0.4 * 0.5)
    assert by_id[lexical_only.id].match_types == (ContextMatchType.LEXICAL,)
    assert by_id[lexical_only.id].score_components.relevance == pytest.approx(0.6)


def test_vector_threshold_boundary_and_no_top_k_backfill(store, test_config):
    """0.80 passes and 0.7999 is dropped without backfilling the requested top_k."""
    test_config.context_vector_min_similarity = 0.8
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    accepted = add_item(store, "accepted", l0="plain accepted note")
    rejected = add_item(store, "rejected", l0="plain rejected note")
    index = FakeVectorIndex(
        test_config,
        [
            {"id": accepted.id, "distance": 0.4},    # similarity exactly 0.80
            {"id": rejected.id, "distance": 0.4002},  # similarity 0.7999
        ],
    )

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for(top_k=3))

    assert {result.id for result in results} == {fts_item.id, accepted.id}
    assert len(results) == 2  # the rejected neighbor never backfills top_k


def test_vector_similarity_floor_reads_config(store, test_config):
    """The vector floor comes from context_vector_min_similarity, not a literal."""
    test_config.context_vector_min_similarity = 0.5
    item = add_item(store, "mid-similarity", l0="plain unrelated note")
    index = FakeVectorIndex(test_config, [{"id": item.id, "distance": 1.0}])

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert [result.id for result in results] == [item.id]


def test_real_vector_index_supplies_thresholded_candidates(store, test_config):
    """End-to-end: a clean on-disk context index feeds the vector channel."""
    fts_item = add_item(store, "fts-hit", l0="zebra striped animal")
    near = add_item(store, "near", l0="completely unrelated text")
    far = add_item(store, "far", l0="another unrelated note")
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    index.initialize(dim=3)
    index.add(near.id, np.array([1.0, 0.0, 0.0], dtype=np.float32))
    index.add(far.id, np.array([0.0, 1.0, 0.0], dtype=np.float32))
    index.save()  # persists and clears the dirty marker
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = ContextRetriever(
        test_config, store, index, engine, clock=fixed_clock
    ).search(request_for())

    by_id = {result.id: result for result in results}
    assert set(by_id) == {fts_item.id, near.id}  # far: similarity 0.5 < 0.80
    assert by_id[near.id].match_types == (ContextMatchType.VECTOR,)
    assert by_id[near.id].score_components.relevance == pytest.approx(0.4, abs=1e-6)
    assert engine.queries == ["zebra"]
    index.close()


# ---- vector degradation: every failure mode falls back to FTS-only ----


def test_wrong_vector_path_degrades_to_fts_only(store, test_config):
    """An index wired to the legacy vector path must never serve context search."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    vector_item = add_item(store, "vector-hit", l0="plain unrelated note")
    index = FakeVectorIndex(
        test_config,
        [{"id": vector_item.id, "distance": 0.0}],
        path=test_config.vector_path,
    )
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = make_retriever(test_config, store, index, engine).search(request_for())

    assert [result.id for result in results] == [fts_item.id]
    assert engine.queries == []
    assert index.search_calls == []


def test_dirty_vector_index_degrades_to_fts_only(store, test_config):
    """A dirty sync marker means the vector cache is untrusted until rebuilt."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    vector_item = add_item(store, "vector-hit", l0="plain unrelated note")
    index = FakeVectorIndex(
        test_config, [{"id": vector_item.id, "distance": 0.0}], dirty=True
    )
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = make_retriever(test_config, store, index, engine).search(request_for())

    assert [result.id for result in results] == [fts_item.id]
    assert index.search_calls == []


def test_empty_vector_index_degrades_to_fts_only(store, test_config):
    """An initialized but empty index has no neighbors to offer."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    add_item(store, "vector-hit", l0="plain unrelated note")
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    index.initialize(dim=3)
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = ContextRetriever(
        test_config, store, index, engine, clock=fixed_clock
    ).search(request_for())

    assert [result.id for result in results] == [fts_item.id]
    assert engine.queries == []
    index.close()


def test_uninitialized_vector_index_degrades_to_fts_only(store, test_config):
    """A never-initialized index must degrade cleanly instead of raising."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    add_item(store, "vector-hit", l0="plain unrelated note")
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    engine = QueryEmbeddingEngine([1.0, 0.0, 0.0])

    results = ContextRetriever(
        test_config, store, index, engine, clock=fixed_clock
    ).search(request_for())

    assert [result.id for result in results] == [fts_item.id]
    assert engine.queries == []


def test_missing_or_unloaded_engine_degrades_to_fts_only(store, test_config):
    """Without a loaded embedding engine the vector channel stays dark."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    vector_item = add_item(store, "vector-hit", l0="plain unrelated note")

    for engine in (None, UnloadedQueryEngine([1.0, 0.0, 0.0])):
        index = FakeVectorIndex(test_config, [{"id": vector_item.id, "distance": 0.0}])

        results = make_retriever(test_config, store, index, engine).search(
            request_for()
        )

        assert [result.id for result in results] == [fts_item.id]
        assert index.search_calls == []


def test_encode_failure_degrades_to_fts_only(store, test_config):
    """An encoding crash must not escape or poison the lexical channel."""
    fts_item = add_item(store, "fts-hit", l0="zebra summary")
    vector_item = add_item(store, "vector-hit", l0="plain unrelated note")
    index = FakeVectorIndex(test_config, [{"id": vector_item.id, "distance": 0.0}])
    engine = FailingQueryEngine()

    results = make_retriever(test_config, store, index, engine).search(request_for())

    assert [result.id for result in results] == [fts_item.id]
    assert engine.queries == ["zebra"]  # attempted, failed, degraded


# ---- metadata filters ----


def test_non_active_items_never_reach_results(store, test_config):
    """Candidate, superseded, and archived items are excluded, even via a stale index."""
    active = add_item(store, "active", l0="zebra active summary")
    add_item(store, "candidate", l0="zebra candidate summary",
             status=ContextStatus.CANDIDATE)
    add_item(store, "superseded", l0="zebra superseded summary",
             status=ContextStatus.SUPERSEDED)
    stale = add_item(store, "stale", l0="plain stale note",
                     status=ContextStatus.ARCHIVED)
    index = FakeVectorIndex(test_config, [{"id": stale.id, "distance": 0.0}])

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert [result.id for result in results] == [active.id]


def test_expired_items_are_excluded_at_the_fixed_clock(store, test_config):
    """Expiry compares against the injected clock; an exact boundary is expired."""
    fresh = add_item(store, "fresh", l0="zebra fresh summary",
                     expires_at="2026-08-18 12:00:01")
    no_expiry = add_item(store, "no-expiry", l0="zebra plain summary")
    add_item(store, "expired", l0="zebra expired summary",
             expires_at="2026-08-18 11:59:59")
    add_item(store, "boundary", l0="zebra boundary summary", expires_at=NOW_STAMP)

    results = make_retriever(test_config, store).search(request_for())

    assert {result.id for result in results} == {fresh.id, no_expiry.id}


def test_default_scope_keeps_exact_project_and_applicable_globals(store, test_config):
    """Other projects and non-applicable global types are filtered before ranking."""
    exact = add_item(store, "exact", l0="zebra exact summary")
    global_policy = add_item(
        store, "global-policy", l0="zebra policy summary",
        project="", scope=ContextScope.GLOBAL,
        content_type=ContextContentType.WORKFLOW_POLICY,
    )
    add_item(store, "other-project", l0="zebra other summary", project="other")
    add_item(store, "global-fact", l0="zebra leaked fact",
             project="", scope=ContextScope.GLOBAL)

    results = make_retriever(test_config, store).search(request_for())

    assert {result.id for result in results} == {exact.id, global_policy.id}


def test_cross_project_search_lifts_the_scope_gate(store, test_config):
    """Explicit cross-project search admits other projects and any global type."""
    exact = add_item(store, "exact", l0="zebra exact summary")
    other = add_item(store, "other-project", l0="zebra other summary", project="other")
    global_fact = add_item(store, "global-fact", l0="zebra leaked fact",
                           project="", scope=ContextScope.GLOBAL)
    retriever = make_retriever(test_config, store)

    default_results = retriever.search(request_for())
    wide = results_by_id(retriever, request_for(cross_project=True))

    assert [result.id for result in default_results] == [exact.id]
    assert set(wide) == {exact.id, other.id, global_fact.id}
    assert wide[exact.id].score_components.project == 1.0
    assert wide[global_fact.id].score_components.project == 0.5
    assert wide[other.id].score_components.project == 0.0


def test_empty_project_search_is_global_only(store, test_config):
    """An empty project must not mix every project's content into the results."""
    global_policy = add_item(
        store, "global-policy", l0="zebra policy summary",
        project="", scope=ContextScope.GLOBAL,
        content_type=ContextContentType.WORKFLOW_POLICY,
    )
    add_item(store, "project-item", l0="zebra project summary", project="proj")

    results = make_retriever(test_config, store).search(request_for(project=""))

    assert [result.id for result in results] == [global_policy.id]


def test_content_types_filter_restricts_candidates(store, test_config):
    """An explicit content-type set admits only those types."""
    add_item(store, "fact", l0="zebra fact summary")
    decision = add_item(store, "decision", l0="zebra decision summary",
                        content_type=ContextContentType.DECISION)

    results = make_retriever(test_config, store).search(
        request_for(content_types=(ContextContentType.DECISION,))
    )

    assert [result.id for result in results] == [decision.id]


def test_confidence_floor_filters_and_reads_config(store, test_config):
    """The confidence floor is context_min_confidence, inclusive and configurable."""
    add_item(store, "low", l0="zebra low summary", confidence=0.5)
    edge = add_item(store, "edge", l0="zebra edge summary", confidence=0.55)
    high = add_item(store, "high", l0="zebra high summary", confidence=0.9)
    retriever = make_retriever(test_config, store)

    assert {result.id for result in retriever.search(request_for())} == {
        edge.id,
        high.id,
    }

    test_config.context_min_confidence = 0.9
    assert [result.id for result in retriever.search(request_for())] == [high.id]


def test_reference_tier_requires_an_explicit_content_type_opt_in(store, test_config):
    """Reference-tier items stay hidden unless the caller names the reference type."""
    reference = add_item(store, "reference", l0="zebra reference summary",
                         content_type=ContextContentType.REFERENCE,
                         tier=ContextTier.REFERENCE)
    normal = add_item(store, "normal", l0="zebra normal summary")
    retriever = make_retriever(test_config, store)

    assert [result.id for result in retriever.search(request_for())] == [normal.id]

    explicit = retriever.search(
        request_for(content_types=(ContextContentType.REFERENCE,))
    )
    assert [result.id for result in explicit] == [reference.id]


# ---- side-effect freedom ----


def test_search_never_updates_access_counts(store, test_config, monkeypatch):
    """Neither returned nor filtered candidates may gain access telemetry."""
    returned = add_item(store, "returned")
    filtered = add_item(store, "filtered", confidence=0.5)
    vector_only = add_item(store, "vector-only", l0="plain note")
    index = FakeVectorIndex(test_config, [{"id": vector_only.id, "distance": 0.0}])

    def forbidden_update(*args, **kwargs):
        raise AssertionError("ContextRetriever must not update access counts")

    monkeypatch.setattr(store, "update_access", forbidden_update)
    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert {result.id for result in results} == {returned.id, vector_only.id}
    for item_id in (returned.id, filtered.id, vector_only.id):
        assert store.get_item(item_id).access_count == 0


def test_search_never_loads_l1_or_l2_text(store, test_config, monkeypatch):
    """Results are built from metadata plus L0; layer reads are forbidden."""
    item = add_item(store, "layered", l0="zebra layered summary")

    def forbidden_layer_read(*args, **kwargs):
        raise AssertionError("ContextRetriever must not read layer text")

    monkeypatch.setattr(store, "get_layer", forbidden_layer_read)
    monkeypatch.setattr(store, "get_item", forbidden_layer_read)

    results = make_retriever(test_config, store).search(request_for())

    assert [result.id for result in results] == [item.id]
    assert results[0].l0 == "zebra layered summary"
    assert not hasattr(results[0], "l1")
    assert not hasattr(results[0], "l2")


# ---- frozen score components ----


def test_lexical_component_normalizes_against_batch_best(store, test_config):
    """lexical = raw / max_positive_raw inside the current candidate batch."""
    strong = add_item(store, "strong", l0="zebra zebra zebra")
    weak = add_item(store, "weak", l0="zebra padding padding padding padding")

    results = results_by_id(make_retriever(test_config, store), request_for())

    raws = {
        hit.item_id: hit.score
        for hit in store.search_fts(
            "zebra",
            top_k=10,
            statuses=(ContextStatus.ACTIVE,),
            layers=(ContextLayer.L0,),
        )
    }
    best = max(raws.values())
    assert raws[strong.id] == best
    assert raws[weak.id] < best
    assert results[strong.id].score_components.relevance == pytest.approx(0.6)
    assert results[weak.id].score_components.relevance == pytest.approx(
        0.6 * raws[weak.id] / best
    )


def test_missing_channels_score_zero_without_rescaling(store, test_config):
    """A missing channel contributes 0; the other channel is never renormalized."""
    lexical_only = add_item(store, "lexical-only", l0="zebra lexical summary")
    vector_only = add_item(store, "vector-only", l0="plain vector note")
    index = FakeVectorIndex(test_config, [{"id": vector_only.id, "distance": 0.0}])

    results = results_by_id(
        make_retriever(test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])),
        request_for(),
    )

    assert results[lexical_only.id].score_components.relevance == pytest.approx(0.6)
    assert results[vector_only.id].score_components.relevance == pytest.approx(0.4)


def test_project_component_scores_exact_above_applicable_global(store, test_config):
    """Exact project scores 1.0; an applicable global scores 0.5."""
    exact = add_item(store, "exact")
    global_policy = add_item(
        store, "global-policy", project="", scope=ContextScope.GLOBAL,
        content_type=ContextContentType.WORKFLOW_POLICY,
    )

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert results[exact.id].score_components.project == 1.0
    assert results[global_policy.id].score_components.project == 0.5


def test_type_priority_base_values_follow_the_frozen_table(store, test_config):
    """Every content type maps to its frozen base before any pinned bonus."""
    ids = {}
    for content_type, base in TYPE_PRIORITY_BASE.items():
        item = add_item(store, f"type-{content_type.value}", content_type=content_type)
        ids[item.id] = base

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert len(results) == len(TYPE_PRIORITY_BASE)
    for item_id, base in ids.items():
        assert results[item_id].score_components.type_priority == pytest.approx(base)


def test_pinned_tier_adds_a_capped_bonus(store, test_config):
    """Pinned adds 0.2 to the type base, truncated at 1.0."""
    pinned_policy = add_item(store, "pinned-policy", tier=ContextTier.PINNED,
                             content_type=ContextContentType.WORKFLOW_POLICY)
    pinned_fact = add_item(store, "pinned-fact", tier=ContextTier.PINNED)
    plain_fact = add_item(store, "plain-fact")

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert results[pinned_policy.id].score_components.type_priority == pytest.approx(1.0)
    assert results[pinned_fact.id].score_components.type_priority == pytest.approx(0.6)
    assert results[plain_fact.id].score_components.type_priority == pytest.approx(0.4)


def test_confidence_component_passes_through(store, test_config):
    """The confidence component is the stored value, already normalized."""
    low = add_item(store, "low", confidence=0.6)
    high = add_item(store, "high", confidence=0.9)

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert results[low.id].score_components.confidence == pytest.approx(0.6)
    assert results[high.id].score_components.confidence == pytest.approx(0.9)


def test_importance_component_scales_by_ten(store, test_config):
    """importance lives on a 1..10 scale and normalizes by division by 10."""
    low = add_item(store, "low", importance=1.0)
    mid = add_item(store, "mid", importance=5.0)
    high = add_item(store, "high", importance=10.0)

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert results[low.id].score_components.importance == pytest.approx(0.1)
    assert results[mid.id].score_components.importance == pytest.approx(0.5)
    assert results[high.id].score_components.importance == pytest.approx(1.0)


def test_evidence_component_uses_laplace_smoothing(store, test_config):
    """evidence = (success + 1) / (success + failure + 2)."""
    unknown = add_item(store, "unknown")
    proven = add_item(store, "proven")
    failing = add_item(store, "failing")
    update_metadata(store, proven.id, success_count=3, failure_count=1)
    update_metadata(store, failing.id, success_count=0, failure_count=2)

    results = results_by_id(make_retriever(test_config, store), request_for())

    assert results[unknown.id].score_components.evidence == pytest.approx(0.5)
    assert results[proven.id].score_components.evidence == pytest.approx(4 / 6)
    assert results[failing.id].score_components.evidence == pytest.approx(0.25)


def test_recency_component_decays_with_the_configured_tau(store, test_config):
    """recency = exp(-age_days / context_recency_tau_days) from updated_at."""
    fresh = add_item(store, "fresh")
    aged = add_item(store, "aged")
    update_metadata(store, fresh.id, updated_at=NOW_STAMP)
    update_metadata(
        store, aged.id,
        updated_at=(NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    retriever = make_retriever(test_config, store)

    results = results_by_id(retriever, request_for())
    assert results[fresh.id].score_components.recency == pytest.approx(1.0)
    assert results[aged.id].score_components.recency == pytest.approx(math.exp(-1.0))

    test_config.context_recency_tau_days = 10.0
    reran = results_by_id(retriever, request_for())
    assert reran[aged.id].score_components.recency == pytest.approx(math.exp(-3.0))


def test_frequency_component_is_logarithmic_with_a_configured_cap(store, test_config):
    """frequency = min(log1p(access_count) / log1p(context_frequency_cap), 1)."""
    idle = add_item(store, "idle")
    mid = add_item(store, "mid")
    popular = add_item(store, "popular")
    update_metadata(store, mid.id, access_count=5)
    update_metadata(store, popular.id, access_count=20)
    retriever = make_retriever(test_config, store)

    results = results_by_id(retriever, request_for())
    assert results[idle.id].score_components.frequency == 0.0
    assert results[mid.id].score_components.frequency == pytest.approx(
        math.log1p(5) / math.log1p(20)
    )
    assert results[popular.id].score_components.frequency == pytest.approx(1.0)

    test_config.context_frequency_cap = 100
    reran = results_by_id(retriever, request_for())
    assert reran[popular.id].score_components.frequency == pytest.approx(
        math.log1p(20) / math.log1p(100)
    )


def test_total_score_is_the_configured_weighted_sum(store, test_config):
    """The final score is the eight components' weighted sum, sorted descending."""
    fused = add_item(store, "fused", l0="zebra fused summary")
    lexical = add_item(store, "lexical", l0="zebra plain summary")
    vector = add_item(store, "vector", l0="plain vector note")
    index = FakeVectorIndex(
        test_config,
        [{"id": fused.id, "distance": 0.5}, {"id": vector.id, "distance": 0.0}],
    )

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert [result.id for result in results] == [fused.id, lexical.id, vector.id]
    for result in results:
        c = result.score_components
        expected = (
            test_config.context_score_relevance_weight * c.relevance
            + test_config.context_score_project_weight * c.project
            + test_config.context_score_type_weight * c.type_priority
            + test_config.context_score_confidence_weight * c.confidence
            + test_config.context_score_importance_weight * c.importance
            + test_config.context_score_evidence_weight * c.evidence
            + test_config.context_score_recency_weight * c.recency
            + test_config.context_score_frequency_weight * c.frequency
        )
        assert result.score == pytest.approx(expected)


def test_total_score_reads_weights_from_config(store, test_config):
    """Score weights come from context_score_* config, never from literals."""
    test_config.context_score_relevance_weight = 1.0
    for name in ("project", "type", "confidence", "importance", "evidence",
                 "recency", "frequency"):
        setattr(test_config, f"context_score_{name}_weight", 0.0)
    add_item(store, "weighted")

    result = make_retriever(test_config, store).search(request_for())[0]

    assert result.score == pytest.approx(result.score_components.relevance)


def test_equal_scores_tie_break_by_ascending_id(store, test_config):
    """Fully identical candidates keep ascending-ID order for stable output."""
    first = add_item(store, "first", l0="zebra tied summary")
    second = add_item(store, "second", l0="zebra tied summary")
    update_metadata(store, first.id, updated_at=NOW_STAMP)
    update_metadata(store, second.id, updated_at=NOW_STAMP)

    results = make_retriever(test_config, store).search(request_for())

    assert [result.id for result in results] == [first.id, second.id]
    assert results[0].score == results[1].score


def test_every_component_and_total_stays_within_unit_interval(store, test_config):
    """All eight components and the weighted total remain inside 0..1."""
    pinned = add_item(store, "pinned", content_type=ContextContentType.WORKFLOW_POLICY,
                      tier=ContextTier.PINNED, importance=10.0, confidence=1.0)
    fused = add_item(store, "fused")
    vector_only = add_item(store, "vector-only", l0="plain vector note")
    update_metadata(store, fused.id, success_count=7, failure_count=2, access_count=50)
    index = FakeVectorIndex(
        test_config,
        [{"id": pinned.id, "distance": 0.0}, {"id": vector_only.id, "distance": 0.2}],
    )

    results = make_retriever(
        test_config, store, index, QueryEmbeddingEngine([1.0, 0.0, 0.0])
    ).search(request_for())

    assert len(results) == 3
    for result in results:
        c = result.score_components
        values = (
            c.relevance, c.project, c.type_priority, c.confidence, c.importance,
            c.evidence, c.recency, c.frequency, result.score,
        )
        for value in values:
            assert 0.0 <= value <= 1.0


def test_target_project_not_starved_by_other_project_candidates(test_config, store):
    for i in range(12):
        add_item(store, f'other:{i}', l0='zebra database', project='other')
    target = add_item(store, 'target', l0='zebra database', project='proj')
    result = make_retriever(test_config, store).search(request_for('zebra', top_k=1))
    assert [r.id for r in result] == [target.id]


def test_explicit_search_can_find_detail_only_keyword(test_config, store):
    target = add_item(store, 'detail', l0='Project status', l1='zebra database migration')
    result = make_retriever(test_config, store).search(request_for('zebra'))
    assert [r.id for r in result] == [target.id]
    assert result[0].l0 == 'Project status'
    assert ContextLayer.L1 in result[0].match_layers


def test_target_type_not_starved_by_irrelevant_types(test_config, store):
    for n in range(15):
        add_item(store, f'noise-{n}', l0='zebra striped animal',
                 content_type=ContextContentType.FACT)
    target = add_item(store, 'experience-target', l0='zebra striped animal',
                      content_type=ContextContentType.EXPERIENCE)
    results = make_retriever(test_config, store).search(request_for(
        top_k=1, content_types=(ContextContentType.EXPERIENCE,)))
    assert [r.id for r in results] == [target.id]
