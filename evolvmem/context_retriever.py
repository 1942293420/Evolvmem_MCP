"""Deterministic, thresholded retrieval over layered Context Core items."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from evolvmem.request_trace import stage
import re

import numpy as np

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextLayer,
    ContextMatchType,
    ContextRetrievalRecord,
    ContextScope,
    ContextScoreComponents,
    ContextSearchHit,
    ContextSearchRequest,
    ContextSearchResult,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_store import ContextStore
from evolvmem.context_temporal import (
    applicability_preference,
    successor_takes_precedence,
    temporal_rank,
    window_contains,
)
from evolvmem.embedding import EmbeddingEngine
from evolvmem.vector_index import VectorIndex


_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Plain global items stay visible across projects only for task-compatible
# types; global facts, decisions, and session summaries would leak across
# project boundaries.
_APPLICABLE_GLOBAL_TYPES = frozenset(
    (
        ContextContentType.WORKFLOW_POLICY,
        ContextContentType.CONSTRAINT,
        ContextContentType.PREFERENCE,
        ContextContentType.USER_PROFILE,
        ContextContentType.PLAYBOOK,
    )
)

# Frozen type-priority bases from the cutover design; pinned adds a capped bonus.
_TYPE_PRIORITY_BASE = {
    ContextContentType.WORKFLOW_POLICY: 0.8,
    ContextContentType.CONSTRAINT: 0.8,
    ContextContentType.PREFERENCE: 0.6,
    ContextContentType.USER_PROFILE: 0.6,
    ContextContentType.PLAYBOOK: 0.6,
    ContextContentType.DECISION: 0.5,
    ContextContentType.FACT: 0.4,
    ContextContentType.EXPERIENCE: 0.4,
    ContextContentType.SESSION_SUMMARY: 0.4,
    ContextContentType.PROJECT_SUMMARY: 0.4,
    ContextContentType.REFERENCE: 0.2,
    ContextContentType.WORKSTREAM_CHECKPOINT: 0.0,
}
_PINNED_TYPE_BONUS = 0.2

# Candidate pools deliberately over-fetch so threshold and metadata filtering
# never starve the final top_k truncation.
_CANDIDATE_POOL_MULTIPLIER = 5


@dataclass(slots=True)
class _Candidate:
    """One merged search candidate before metadata filtering and scoring."""

    item_id: int
    match_layers: tuple[ContextLayer, ...]
    lexical_raw: float | None = None
    distance: float | None = None


class ContextRetriever:
    """Thresholded hybrid orchestrator over Context Core L0 candidates.

    Query flow:
    1. Active-only L0 FTS5/trigram candidates plus an expanded L0 vector pool.
    2. Over-fetch a bounded pool when optional reranking is available.
    3. Merge match types/layers by context ID and fetch narrow metadata + L0.
    4. Apply status, expiry, scope, content-type, confidence, and tier filters.
    5. Apply the original acceptance gate or independent reranking confirmation.
    6. Rank relevance, keep applicable identity winners, deduplicate full content.

    Optional reranking uses bounded L1 evidence; output remains L0-only.
    The retriever never loads L2 text and never mutates access counts.
    """

    def __init__(
        self,
        config: Config,
        store: ContextStore,
        vector_index: VectorIndex,
        embedding_engine: EmbeddingEngine | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.vector_index = vector_index
        self.embedding_engine = embedding_engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @stage('retrieval')
    def search(self, request: ContextSearchRequest, *, eligible_ids=None) -> tuple[ContextSearchResult, ...]:
        """Return deterministic L0-only results without access-count side effects."""
        reranking = bool(getattr(self.embedding_engine, 'reranker_loaded', False))
        pool_size = max(50, request.top_k * _CANDIDATE_POOL_MULTIPLIER) if reranking else request.top_k * _CANDIDATE_POOL_MULTIPLIER
        moment = self._moment(request)
        # Superseded rows are always candidate material: an explicit ``as_of``
        # may read them, and a *default* current read must still see a
        # predecessor whose known successor is only scheduled for the future.
        # The metadata filter below decides which of them is actually eligible.
        statuses = (ContextStatus.ACTIVE, ContextStatus.SUPERSEDED)
        fts_options = dict(
            top_k=pool_size,
            statuses=statuses,
            layers=(ContextLayer.L0, ContextLayer.L1),
            project=None if request.cross_project else request.project,
            now=moment,
            content_types=request.content_types,
            min_confidence=self.config.context_min_confidence,
            applicable_global_types=None if request.cross_project else tuple(_APPLICABLE_GLOBAL_TYPES),
            exclude_reference=ContextContentType.REFERENCE not in request.content_types,
            exclude_checkpoints=ContextContentType.WORKSTREAM_CHECKPOINT not in request.content_types,
            item_ids=None if eligible_ids is None else tuple(eligible_ids),
        )
        with stage('lexical'):
            fts_hits = self.store.search_fts(request.query, **fts_options)
        # Exact error codes and code identifiers survive a natural-language
        # wrapper. Keep all scope/status gates on these additional queries.
        identifiers = re.findall(r'(?<!\d)\d{4,}(?!\d)|[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_-]+', request.query)
        for identifier in dict.fromkeys(identifiers[:8]):
            fts_hits.extend(self.store.search_fts(identifier, **fts_options))
        candidates = self._merge_candidates(
            fts_hits, self._scoped_vector_candidates(request, pool_size, eligible_ids),
            min_similarity=min(.6, self.config.context_vector_min_similarity) if reranking else None,
        )
        if eligible_ids is not None:
            allowed = set(eligible_ids)
            candidates = [c for c in candidates if c.item_id in allowed]
        if not candidates:
            return ()
        records = {
            record.item.id: record
            for record in self.store.get_retrieval_records(
                [candidate.item_id for candidate in candidates]
            )
        }
        # Successor chain → implicit end for superseded records: a record whose
        # successor became effective at T is not eligible at/after T unless it
        # carries an explicit effective_until (which stays authoritative). The
        # same map is needed for a default current read, where a successor whose
        # boundary is still in the future must not erase the predecessor.
        implicit_ends = self.store.successor_ranks(list(records))
        max_lexical_raw = max(
            (
                candidate.lexical_raw
                for candidate in candidates
                if candidate.lexical_raw is not None
            ),
            default=0.0,
        )
        now = self._now()
        scored: list[tuple[float, ContextSearchResult]] = []
        eligible_candidates = []
        for candidate in candidates:
            record = records.get(candidate.item_id)
            if record is None or not self._eligible(
                record.item,
                request,
                now,
                implicit_end=implicit_ends.get(candidate.item_id),
            ):
                continue
            eligible_candidates.append(candidate)
        rank_scores = None
        rank_documents = {}
        if reranking and eligible_candidates:
            try:
                with stage('rerank'):
                    texts = self._rerank_documents(eligible_candidates, records)
                    rank_documents = dict(zip((c.item_id for c in eligible_candidates), texts))
                    values = self.embedding_engine.rerank(request.query, texts)
                if len(values) != len(eligible_candidates) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                    raise ValueError('invalid_reranking_response')
                rank_scores = dict(zip((c.item_id for c in eligible_candidates), values))
            except Exception:
                # Optional reranking failure restores the original acceptance
                # gate; weak over-fetched neighbors cannot leak on fallback.
                pass
        for candidate in eligible_candidates:
            record = records[candidate.item_id]
            similarity = 0 if candidate.distance is None else 1 - candidate.distance / 2
            rank_score = None if rank_scores is None else rank_scores[candidate.item_id]
            if (candidate.lexical_raw is None and similarity < self.config.context_vector_min_similarity
                    and (rank_score is None or rank_score < self.config.context_reranker_min_score)):
                continue
            if candidate.lexical_raw is None and similarity < self.config.context_vector_min_similarity:
                from evolvmem.query_anchors import supports_named_subject
                if not supports_named_subject(request.query, rank_documents.get(candidate.item_id, '') + '\n' + record.item.project):
                    continue
            components = self._score_components(
                record.item, candidate, request, now, max_lexical_raw
            )
            score = min(1.0, max(0.0, self._total_score(components)))
            result = self._build_result(record, candidate, components, score)
            if rank_score is not None:
                result = replace(result, score=rank_score, rerank_score=rank_score)
            scored.append((score, result))
        # Query/document relevance decides first; existing metadata only breaks
        # equal cross-encoder scores instead of drowning out the query intent.
        scored.sort(key=lambda entry: (-entry[1].score, -entry[0], entry[1].id))
        ranked = self._latest_applicable_per_identity(
            [result for _, result in scored], request, now
        )
        return tuple(self._deduplicate(ranked)[: request.top_k])

    def _rerank_documents(self, candidates, records):
        ids = [c.item_id for c in candidates]
        marks = ','.join('?' for _ in ids)
        details = {r['item_id']: r['detail'] for r in self.store._connection().execute(
            f"SELECT item_id,substr(content,1,1200) AS detail FROM context_layers WHERE layer='l1' AND item_id IN ({marks})", ids)}
        return [records[i].l0[:200] + '\n' + details.get(i, '') for i in ids]

    def _deduplicate(self, results):
        kept, seen = [], {}
        fingerprints = self.store.retrieval_fingerprints([result.id for result in results])
        for result in results:
            fingerprint = fingerprints.get(result.id)
            if fingerprint is None:
                kept.append(result)
                continue
            # A shared short L0 does not prove duplicates. Require complete
            # content, scope, temporal applicability and case conditions alike.
            signature = (result.project, result.scope, result.content_type, result.status, fingerprint)
            if signature in seen:
                position = seen[signature]
                canonical = kept[position]
                kept[position] = replace(canonical, duplicate_ids=canonical.duplicate_ids + (result.id,))
            else:
                seen[signature] = len(kept)
                kept.append(result)
        return kept

    def _latest_applicable_per_identity(
        self,
        results: list[ContextSearchResult],
        request: ContextSearchRequest,
        now: datetime,
    ) -> list[ContextSearchResult]:
        """Keep a hit only when it is the applicable winner of its whole family.

        Supersession is a same-identity precedence rule, not a mere storage flag.
        The winner is resolved over the *actual* family rows (same identity key,
        project and scope) with the same eligibility gates as retrieval, so it
        does not depend on which rows happened to match the query text or fit
        ``top_k``. A matching row that is only stale history for its family is
        dropped (never revived); a matching row that is the family winner is
        returned. Eligibility keeps unknown superseded ranks and superseded
        non-decisions out, and still lets a scheduled future successor leave the
        current predecessor in place.
        """
        families: dict[tuple, list[ContextSearchResult]] = {}
        order: list[tuple] = []
        for result in results:
            key = (result.identity_key, result.project, result.scope)
            if key not in families:
                families[key] = []
                order.append(key)
            families[key].append(result)
        kept: list[ContextSearchResult] = []
        for key in order:
            winner_id = self._family_winner_id(key, request, now)
            if winner_id is None:
                continue
            kept.extend(
                result for result in families[key] if result.id == winner_id
            )
        return kept

    def _family_winner_id(
        self, key: tuple, request: ContextSearchRequest, now: datetime
    ) -> int | None:
        """Applicable winner id for one identity family, or None when none."""
        identity_key, project, scope = key
        family = self.store.get_by_identity(
            identity_key, project=project, scope=scope, include_layers=False
        )
        if not family:
            return None
        implicit_ends = self.store.successor_ranks([item.id for item in family])
        eligible = [
            item
            for item in family
            if self._eligible(
                item,
                request,
                now,
                implicit_end=implicit_ends.get(item.id),
            )
        ]
        if not eligible:
            return None
        winner = max(
            eligible,
            key=lambda item: applicability_preference(
                active=item.status is ContextStatus.ACTIVE,
                effective_from=item.effective_from,
                occurred_at=item.occurred_at,
                item_id=item.id,
            ),
        )
        return winner.id

    # ---- candidate generation ----

    @stage('vector')
    def _scoped_vector_candidates(self, request, pool_size, eligible_ids=None):
        if (getattr(self.embedding_engine, 'reranker_loaded', False)
                and request.project and not request.cross_project):
            # Build the pool within the eligible business scope. Global policies
            # retain their own bounded lane, rather than exhausting the project's
            # candidates before the reranker can see their detailed evidence.
            allowed = None if eligible_ids is None else set(eligible_ids)
            seen, valid = set(), {}
            def select(hits):
                new_ids = [h['id'] for h in hits if h['id'] not in seen]
                seen.update(new_ids)
                records = self.store.get_retrieval_records(new_ids)
                valid.update({r.item.id: r.item for r in records
                              if (allowed is None or r.item.id in allowed) and self._eligible(r.item, request, self._now())})
                scoped = [h for h in hits if h['id'] in valid]
                if request.project and not request.cross_project:
                    local = [h for h in scoped if request.project in (valid[h['id']].projects or (valid[h['id']].project,))]
                    global_hits = [h for h in scoped if request.project not in (valid[h['id']].projects or (valid[h['id']].project,))]
                    return local[:pool_size] + global_hits[:pool_size], len(local) >= pool_size
                return scoped[:pool_size], len(scoped) >= pool_size
            return self._vector_candidates(request.query, pool_size, select=select)
        hits = self._vector_candidates(request.query, pool_size)
        while len(hits) == pool_size:
            records = self.store.get_retrieval_records([hit["id"] for hit in hits])
            if sum(self._eligible(r.item, request, self._now()) and
                   (eligible_ids is None or r.item.id in eligible_ids) for r in records) >= request.top_k:
                break
            try:
                total = self.vector_index.count()
            except Exception:
                break
            if pool_size >= total:
                break
            pool_size = min(total, pool_size * 2)
            hits = self._vector_candidates(request.query, pool_size)
        return hits

    def _vector_candidates(self, query: str, pool_size: int, *, select=None) -> list[dict]:
        """Return raw ANN neighbors, degrading to none on any vector failure."""
        from evolvmem.context_vector_health import inspect_context_vector
        state = inspect_context_vector(self.config, self.store, self.vector_index)
        if not state.available(self.embedding_engine):
            return []
        try:
            with stage('embedding'):
                embedding = np.asarray(self.embedding_engine.encode_query(query), dtype=np.float32)
            limit = pool_size
            while True:
                hits = self.vector_index.search(embedding, limit)
                if state.verified_ids is None and select is None:
                    return hits
                valid = hits if state.verified_ids is None else [h for h in hits if int(h['id']) in state.verified_ids]
                if select is not None:
                    valid, filled = select(valid)
                    if filled or len(hits) < limit or limit >= state.count:
                        return valid
                    limit = min(state.count, limit * 2)
                    continue
                if len(valid) >= pool_size or len(hits) < limit or limit >= state.count:
                    return valid[:pool_size]
                limit = min(state.count, limit * 2)
        except Exception:
            return []  # gracefully degrade to FTS-only on encoding/index failure

    def _vector_available(self) -> bool:
        from evolvmem.context_vector_health import inspect_context_vector
        state = inspect_context_vector(self.config, self.store, self.vector_index)
        return state.available(self.embedding_engine)

    def _merge_candidates(
        self,
        fts_hits: list[ContextSearchHit],
        vector_hits: list[dict],
        *, min_similarity=None,
    ) -> list[_Candidate]:
        """Fuse both channels by context ID, dropping weak vector-only neighbors."""
        by_id: dict[int, _Candidate] = {}
        for hit in fts_hits:
            existing = by_id.get(hit.item_id)
            if existing is not None:
                existing.lexical_raw = max(existing.lexical_raw, hit.score)
                existing.match_layers = tuple(layer for layer in ContextLayer if layer in set(existing.match_layers) | set(hit.match_layers))
                continue
            by_id[hit.item_id] = _Candidate(
                item_id=hit.item_id,
                match_layers=hit.match_layers,
                lexical_raw=hit.score,
            )
        if min_similarity is None:
            min_similarity = self.config.context_vector_min_similarity
        for hit in vector_hits:
            item_id = int(hit["id"])
            distance = float(hit["distance"])
            existing = by_id.get(item_id)
            if existing is not None:
                existing.distance = distance
                continue
            similarity = max(0.0, 1.0 - distance / 2.0)
            if similarity < min_similarity:
                continue  # sub-threshold vector-only neighbors never fill top_k
            by_id[item_id] = _Candidate(
                item_id=item_id,
                match_layers=(ContextLayer.L0,),
                distance=distance,
            )
        return list(by_id.values())

    # ---- metadata filters ----

    def _eligible(
        self,
        item: ContextItem,
        request: ContextSearchRequest,
        now: datetime,
        *,
        implicit_end: str | None = None,
    ) -> bool:
        from evolvmem.memory_eligibility import eligible
        if not eligible(self.store, item.id):
            return False
        moment = request.as_of or now.strftime(_TIMESTAMP_FORMAT)
        if request.as_of is None:
            if item.status is ContextStatus.ACTIVE:
                pass
            elif (
                item.status is ContextStatus.SUPERSEDED
                and item.content_type is ContextContentType.DECISION
                and temporal_rank(item.effective_from, item.occurred_at) is not None
                and not successor_takes_precedence(implicit_end, moment)
                and implicit_end is not None
            ):
                # 继任者已排定但尚未生效：当前有效的仍是前任决定，默认读取
                # 不能出现空档。继任者到期后（或继任者时间未知时）前任不再
                # 算当前，未知日期与 superseded 非 decision 一律不进入默认面。
                pass
            else:
                return False
        else:
            # Explicit historical read: a superseded *decision* is readable when
            # it has a known rank; a superseded non-decision, or one whose dates
            # are UNKNOWN, is honestly not retrievable at that instant.
            if item.status is ContextStatus.ACTIVE:
                pass
            elif (
                item.status is ContextStatus.SUPERSEDED
                and item.content_type is ContextContentType.DECISION
                and temporal_rank(item.effective_from, item.occurred_at) is not None
            ):
                pass
            else:
                return False
        if (
            item.expires_at is not None
            and item.expires_at <= moment
        ):
            return False
        # 已知决策窗口：未来生效或已过期的记录默认不可召回；显式 as_of
        # 只在窗口内部可读（until 为排他边界）。UNKNOWN 窗口不设限。
        # 被取代的记录没有显式 until 时，以继任者生效时刻作为隐式结束。
        end = item.effective_until
        if end is None and item.status is ContextStatus.SUPERSEDED:
            end = implicit_end
        if not window_contains(item.effective_from, end, moment):
            return False
        if not self._scope_allows(item, request):
            return False
        if request.content_types and item.content_type not in request.content_types:
            return False
        if item.confidence < self.config.context_min_confidence:
            return False
        if (
            item.tier is ContextTier.REFERENCE
            and ContextContentType.REFERENCE not in request.content_types
        ):
            return False  # reference tier requires an explicit content-type opt-in
        if (
            item.content_type is ContextContentType.WORKSTREAM_CHECKPOINT
            and ContextContentType.WORKSTREAM_CHECKPOINT not in request.content_types
        ):
            return False  # checkpoints require an explicit content-type opt-in
        return True

    @staticmethod
    def _scope_allows(item: ContextItem, request: ContextSearchRequest) -> bool:
        if request.cross_project:
            return True
        if item.scope is ContextScope.GLOBAL:
            return item.personal_general or item.content_type in _APPLICABLE_GLOBAL_TYPES
        return bool(request.project) and request.project in (item.projects or (item.project,))

    # ---- scoring ----

    def _score_components(
        self,
        item: ContextItem,
        candidate: _Candidate,
        request: ContextSearchRequest,
        now: datetime,
        max_lexical_raw: float,
    ) -> ContextScoreComponents:
        if candidate.lexical_raw is None:
            lexical = 0.0
        elif max_lexical_raw > 0.0:
            lexical = candidate.lexical_raw / max_lexical_raw
        else:
            lexical = 1.0  # all-zero LIKE/CJK fallback hits remain full matches
        if candidate.distance is None:
            vector = 0.0
        else:
            vector = min(1.0, max(0.0, 1.0 - candidate.distance / 2.0))
        relevance = (
            self.config.context_fts_weight * lexical
            + self.config.context_vector_weight * vector
        )
        type_priority = min(
            _TYPE_PRIORITY_BASE[item.content_type]
            + (_PINNED_TYPE_BONUS if item.tier is ContextTier.PINNED else 0.0),
            1.0,
        )
        evidence = (item.success_count + 1.0) / (
            item.success_count + item.failure_count + 2.0
        )
        frequency = min(
            math.log1p(item.access_count)
            / math.log1p(self.config.context_frequency_cap),
            1.0,
        )
        return ContextScoreComponents(
            relevance=relevance,
            project=self._project_component(item, request),
            type_priority=type_priority,
            confidence=item.confidence,
            importance=item.importance / 10.0,
            evidence=evidence,
            recency=self._recency(item, now),
            frequency=frequency,
        )

    @staticmethod
    def _project_component(item: ContextItem, request: ContextSearchRequest) -> float:
        if item.scope is ContextScope.GLOBAL:
            return 0.5
        if request.project and request.project in (item.projects or (item.project,)):
            return 1.0
        return 0.0  # only reachable through explicit cross-project search

    def _recency(self, item: ContextItem, now: datetime) -> float:
        updated = datetime.strptime(item.updated_at, _TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        age_days = max((now - updated).total_seconds() / 86400.0, 0.0)
        return math.exp(-age_days / self.config.context_recency_tau_days)

    def _total_score(self, components: ContextScoreComponents) -> float:
        return (
            self.config.context_score_relevance_weight * components.relevance
            + self.config.context_score_project_weight * components.project
            + self.config.context_score_type_weight * components.type_priority
            + self.config.context_score_confidence_weight * components.confidence
            + self.config.context_score_importance_weight * components.importance
            + self.config.context_score_evidence_weight * components.evidence
            + self.config.context_score_recency_weight * components.recency
            + self.config.context_score_frequency_weight * components.frequency
        )

    # ---- result construction ----

    @staticmethod
    def _build_result(
        record: ContextRetrievalRecord,
        candidate: _Candidate,
        components: ContextScoreComponents,
        score: float,
    ) -> ContextSearchResult:
        item = record.item
        match_types = tuple(
            match_type
            for match_type, present in (
                (ContextMatchType.LEXICAL, candidate.lexical_raw is not None),
                (ContextMatchType.VECTOR, candidate.distance is not None),
            )
            if present
        )
        return ContextSearchResult(
            id=item.id,
            identity_key=item.identity_key,
            l0=record.l0,
            content_type=item.content_type,
            scope=item.scope,
            project=item.project,
            projects=item.projects,
            status=item.status,
            tier=item.tier,
            confidence=item.confidence,
            importance=item.importance,
            score=score,
            score_components=components,
            match_types=match_types,
            match_layers=candidate.match_layers,
            available_layers=record.available_layers,
            effective_from=item.effective_from,
            effective_until=item.effective_until,
            occurred_at=item.occurred_at,
            mentioned_at=item.mentioned_at,
        )

    def _moment(self, request: ContextSearchRequest) -> str:
        """Eligibility instant: the explicit as_of, else the current UTC clock."""
        if request.as_of is not None:
            return request.as_of
        return self._now().strftime(_TIMESTAMP_FORMAT)

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
