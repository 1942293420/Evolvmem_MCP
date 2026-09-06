"""Candidate lifecycle: evidence recording, confirmation, and promotion rules.

Frozen deterministic rules (pinned by tests, adjustable only via config):

- Confidence penalty: a ``failure`` or ``contradicted`` outcome applies
  ``confidence = max(0.0, confidence - 0.1)``; ``success``/``confirmed`` never
  move confidence. ``last_verified_at`` is set by ``success``/``confirmed``.
- Archive rule: an active ``experience`` whose ``failure_count >=
  success_count`` (with ``failure_count > 0``) becomes ``archived``; every
  active ``playbook`` whose ``context_sources`` chain references it (a
  ``source_kind='experience'`` row with ``source_ref=str(experience_id)``)
  drops back to ``candidate`` review. Only ``failure`` evidence can trigger
  the rule, because only ``failure`` moves ``failure_count``.
- Auto-promotion: a candidate ``experience`` needs at least
  ``context_promotion_min_successes`` ``success`` evidence rows backed by
  *distinct* ``session_archives`` (via ``evidence.source_id`` →
  ``context_sources.archive_id``; source-less or archive-less successes never
  join the dedup set) and ``failure_count == 0``.
- Unresolved contradiction: a ``contradicted`` evidence row is unresolved when
  no newer ``success``/``confirmed`` evidence exists for the same item.
- Playbook eligibility: within one project (or within ``scope='global'``), at
  least ``context_playbook_min_experiences`` active experiences each carrying
  enough successes and no unresolved contradiction form a qualification
  cluster when every pairwise L0 normalized cosine ``(1+cos)/2`` reaches
  ``context_promotion_similarity_threshold``. Evaluation never generates
  playbooks (that is P3), and a missing/broken embedding engine yields an
  empty result with an explicit reason — never a faked qualification.

All multi-write operations join one outer ``ContextStore.transaction()``.
Logs and reports carry ids and reason codes only — never note text, L0/L1/L2
content, or exception messages from the embedding backend.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextScope,
    ContextStatus,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore
from evolvmem.extraction_policy import contains_sensitive_text

logger = logging.getLogger(__name__)

_OUTCOME_SUCCESS = "success"
_OUTCOME_FAILURE = "failure"
_OUTCOME_CONFIRMED = "confirmed"
_OUTCOME_CONTRADICTED = "contradicted"
_OUTCOMES = frozenset(
    {_OUTCOME_SUCCESS, _OUTCOME_FAILURE, _OUTCOME_CONFIRMED, _OUTCOME_CONTRADICTED}
)

# Frozen confidence rule: deterministic penalty with a hard lower bound.
_CONFIDENCE_PENALTY = 0.1

_LIFECYCLE_ERROR_CODES = frozenset(
    {
        "item_not_found",
        "invalid_item_state",
        "invalid_source",
        "sensitive_note",
        "identity_conflict",
    }
)

_REASON_HAS_FAILURES = "has_failures"
_REASON_INSUFFICIENT_ARCHIVES = "insufficient_distinct_archives"
_REASON_IDENTITY_CONFLICT = "identity_conflict"
_PROMOTION_SKIP_REASONS = frozenset(
    {_REASON_HAS_FAILURES, _REASON_INSUFFICIENT_ARCHIVES, _REASON_IDENTITY_CONFLICT}
)

_REASON_OK = "ok"
_REASON_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
_ELIGIBILITY_REASONS = frozenset({_REASON_OK, _REASON_EMBEDDING_UNAVAILABLE})

_EVIDENCE_RECORDABLE_STATUSES = frozenset(
    {ContextStatus.ACTIVE, ContextStatus.CANDIDATE}
)


class ContextLifecycleError(RuntimeError):
    """Typed lifecycle failure with a stable, content-free reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in _LIFECYCLE_ERROR_CODES:
            raise ContextValidationError(
                "code must be one of "
                + ", ".join(sorted(_LIFECYCLE_ERROR_CODES))
            )
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """Result of one recorded outcome: the evidence id plus post-write state."""

    evidence_id: int
    item_id: int
    outcome: str
    confidence: float
    status: ContextStatus
    archived: bool = False
    demoted_playbook_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _validate_positive_int(self.evidence_id, "evidence_id")
        _validate_positive_int(self.item_id, "item_id")
        if self.outcome not in _OUTCOMES:
            raise ContextValidationError(
                "outcome must be one of " + ", ".join(sorted(_OUTCOMES))
            )
        _validate_unit_interval(self.confidence, "confidence")
        if not isinstance(self.status, ContextStatus):
            raise ContextValidationError("status must be a ContextStatus")
        if type(self.archived) is not bool:
            raise ContextValidationError("archived must be a boolean")
        object.__setattr__(
            self,
            "demoted_playbook_ids",
            _normalize_id_tuple(self.demoted_playbook_ids, "demoted_playbook_ids"),
        )


@dataclass(frozen=True, slots=True)
class PromotionSkip:
    """One candidate left untouched, with the stable reason code."""

    item_id: int
    reason: str

    def __post_init__(self) -> None:
        _validate_positive_int(self.item_id, "item_id")
        if self.reason not in _PROMOTION_SKIP_REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_PROMOTION_SKIP_REASONS))
            )


@dataclass(frozen=True, slots=True)
class PromotionReport:
    """Batch promotion outcome: promoted ids plus per-candidate skip reasons."""

    promoted_ids: tuple[int, ...]
    skipped: tuple[PromotionSkip, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "promoted_ids", _normalize_id_tuple(self.promoted_ids, "promoted_ids")
        )
        try:
            skipped = tuple(self.skipped)
        except TypeError as exc:
            raise ContextValidationError(
                "skipped must be an iterable of PromotionSkip"
            ) from exc
        if any(not isinstance(skip, PromotionSkip) for skip in skipped):
            raise ContextValidationError(
                "skipped must be an iterable of PromotionSkip"
            )
        object.__setattr__(self, "skipped", skipped)


@dataclass(frozen=True, slots=True)
class PlaybookCluster:
    """One eligible experience cluster; evaluation only, never a generation."""

    scope: ContextScope
    project: str
    item_ids: tuple[int, ...]
    min_similarity: float

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ContextScope):
            raise ContextValidationError("scope must be a ContextScope")
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        object.__setattr__(
            self, "item_ids", _normalize_id_tuple(self.item_ids, "item_ids")
        )
        _validate_unit_interval(self.min_similarity, "min_similarity")


@dataclass(frozen=True, slots=True)
class PlaybookEligibilityReport:
    """Eligibility clusters plus the evaluation reason; empty when degraded."""

    clusters: tuple[PlaybookCluster, ...]
    reason: str

    def __post_init__(self) -> None:
        try:
            clusters = tuple(self.clusters)
        except TypeError as exc:
            raise ContextValidationError(
                "clusters must be an iterable of PlaybookCluster"
            ) from exc
        if any(not isinstance(cluster, PlaybookCluster) for cluster in clusters):
            raise ContextValidationError(
                "clusters must be an iterable of PlaybookCluster"
            )
        if self.reason not in _ELIGIBILITY_REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_ELIGIBILITY_REASONS))
            )
        if self.reason == _REASON_EMBEDDING_UNAVAILABLE and clusters:
            raise ContextValidationError(
                "embedding_unavailable reports must not claim clusters"
            )
        object.__setattr__(self, "clusters", clusters)


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """Candidate review view: L0 plus metadata; never carries L1/L2 text."""

    id: int
    identity_key: str
    content_type: ContextContentType
    project: str
    scope: ContextScope
    l0: str
    confidence: float
    success_count: int
    failure_count: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _validate_positive_int(self.id, "id")
        if not isinstance(self.identity_key, str) or not self.identity_key.strip():
            raise ContextValidationError("identity_key must not be empty")
        if not isinstance(self.content_type, ContextContentType):
            raise ContextValidationError("content_type must be a ContextContentType")
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        if not isinstance(self.scope, ContextScope):
            raise ContextValidationError("scope must be a ContextScope")
        if not isinstance(self.l0, str):
            raise ContextValidationError("l0 must be a string")
        _validate_unit_interval(self.confidence, "confidence")
        for name in ("success_count", "failure_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ContextValidationError(f"{name} must be a non-negative integer")
        if not isinstance(self.created_at, str) or not isinstance(self.updated_at, str):
            raise ContextValidationError("created_at and updated_at must be strings")


class ContextLifecycle:
    """Owns evidence, confirmation, and the deterministic promotion rules."""

    def __init__(self, config: Config, store: ContextStore) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        self._config = config
        self._store = store

    # ---- evidence recording ----

    def _structured_payload(self, item_id):
        row = self._store._connection().execute(
            'SELECT experience_payload FROM context_items WHERE id=?', (item_id,)
        ).fetchone()
        return row[0] if row else ''

    def record_outcome(
        self,
        item_id: int,
        outcome: str,
        note: str = "",
        source_id: int | None = None,
    ) -> EvidenceReport:
        """Record one outcome and apply counters/confidence in one transaction.

        The note passes the existing sensitive-content policy; a hit rejects
        the call before anything is stored. Raw terminal output is never
        accepted here — only the caller-written, policy-checked note.
        """
        item_id = _validate_positive_int(item_id, "item_id")
        if self._structured_payload(item_id):
            raise ContextLifecycleError("invalid_source")
        outcome = _validate_outcome(outcome)
        note = _normalize_note(note)
        if source_id is not None:
            source_id = _validate_positive_int(source_id, "source_id")
        if note and contains_sensitive_text(note):
            raise ContextLifecycleError("sensitive_note")

        store = self._store
        with store.transaction():
            item = store.get_item(item_id, include_layers=False)
            if item is None:
                raise ContextLifecycleError("item_not_found")
            if item.status not in _EVIDENCE_RECORDABLE_STATUSES:
                raise ContextLifecycleError("invalid_item_state")
            if source_id is not None:
                owned = {int(row["id"]) for row in store.list_item_sources(item_id)}
                if source_id not in owned:
                    raise ContextLifecycleError("invalid_source")

            now = _now_iso()
            evidence_id = store.insert_evidence(item_id, source_id, outcome, note, now)
            confidence = None
            if outcome in (_OUTCOME_FAILURE, _OUTCOME_CONTRADICTED):
                confidence = max(0.0, item.confidence - _CONFIDENCE_PENALTY)
            verified_at = (
                now if outcome in (_OUTCOME_SUCCESS, _OUTCOME_CONFIRMED) else None
            )
            store.update_outcome_stats(
                item_id,
                success_delta=1 if outcome == _OUTCOME_SUCCESS else 0,
                failure_delta=1 if outcome == _OUTCOME_FAILURE else 0,
                confidence=confidence,
                last_verified_at=verified_at,
            )
            archived, demoted = self._apply_failure_transitions(item, outcome)
            reloaded = store.get_item(item_id, include_layers=False)
        if reloaded is None:  # pragma: no cover - the row was just updated
            raise RuntimeError("evidence target item could not be reloaded")
        return EvidenceReport(
            evidence_id=evidence_id,
            item_id=item_id,
            outcome=outcome,
            confidence=reloaded.confidence,
            status=reloaded.status,
            archived=archived,
            demoted_playbook_ids=demoted,
        )

    def _apply_failure_transitions(
        self, item: ContextItem, outcome: str
    ) -> tuple[bool, tuple[int, ...]]:
        """Archive an active experience past the failure boundary, demote dependents."""
        if (
            outcome != _OUTCOME_FAILURE
            or item.content_type is not ContextContentType.EXPERIENCE
            or item.status is not ContextStatus.ACTIVE
        ):
            return False, ()
        # The new failure is already counted by the caller's bookkeeping;
        # failure_count+1 > 0 always holds, so the boundary check is complete.
        if item.failure_count + 1 < item.success_count:
            return False, ()
        store = self._store
        store.set_item_status(item.id, ContextStatus.ARCHIVED)
        demoted = store.list_dependent_playbook_ids(item.id)
        for playbook_id in demoted:
            store.set_item_status(playbook_id, ContextStatus.CANDIDATE)
        return True, tuple(demoted)

    # ---- user confirmation ----

    def confirm(self, item_id: int) -> EvidenceReport:
        """Promote one candidate to active and record a confirmed evidence."""
        item_id = _validate_positive_int(item_id, "item_id")
        if self._structured_payload(item_id):
            raise ContextLifecycleError("invalid_source")
        store = self._store
        with store.transaction():
            item = store.get_item(item_id, include_layers=False)
            if item is None:
                raise ContextLifecycleError("item_not_found")
            if item.status is not ContextStatus.CANDIDATE:
                raise ContextLifecycleError("invalid_item_state")
            if store.active_identity_exists(
                item.identity_key, item.project, item.scope, exclude_id=item.id
            ):
                raise ContextLifecycleError("identity_conflict")
            now = _now_iso()
            evidence_id = store.insert_evidence(
                item_id, None, _OUTCOME_CONFIRMED, "", now
            )
            store.set_item_status(item_id, ContextStatus.ACTIVE)
            store.update_outcome_stats(item_id, last_verified_at=now)
            reloaded = store.get_item(item_id, include_layers=False)
        if reloaded is None:  # pragma: no cover - the row was just updated
            raise RuntimeError("confirmed item could not be reloaded")
        return EvidenceReport(
            evidence_id=evidence_id,
            item_id=item_id,
            outcome=_OUTCOME_CONFIRMED,
            confidence=reloaded.confidence,
            status=reloaded.status,
        )

    # ---- automatic promotion ----

    def evaluate_promotions(self) -> PromotionReport:
        """Promote qualifying candidate experiences in one batch transaction."""
        store = self._store
        promoted: list[int] = []
        skipped: list[PromotionSkip] = []
        with store.transaction():
            candidate_ids = store.list_item_ids(
                status=ContextStatus.CANDIDATE,
                content_type=ContextContentType.EXPERIENCE,
            )
            for item_id in candidate_ids:
                # Structured cases are promoted only by their revisioned,
                # source-bound evidence boundary, never the legacy archive rule.
                if self._structured_payload(item_id):
                    skipped.append(PromotionSkip(item_id=item_id, reason=_REASON_INSUFFICIENT_ARCHIVES))
                    continue
                item = store.get_item(item_id, include_layers=False)
                if item is None:  # pragma: no cover - id came from the same tx
                    continue
                if item.failure_count > 0:
                    skipped.append(
                        PromotionSkip(item_id=item_id, reason=_REASON_HAS_FAILURES)
                    )
                    continue
                if store.active_identity_exists(
                    item.identity_key, item.project, item.scope, exclude_id=item.id
                ):
                    skipped.append(
                        PromotionSkip(item_id=item_id, reason=_REASON_IDENTITY_CONFLICT)
                    )
                    continue
                if (
                    len(self._distinct_success_archives(item_id))
                    < self._config.context_promotion_min_successes
                ):
                    skipped.append(
                        PromotionSkip(
                            item_id=item_id, reason=_REASON_INSUFFICIENT_ARCHIVES
                        )
                    )
                    continue
                store.set_item_status(item_id, ContextStatus.ACTIVE)
                promoted.append(item_id)
        return PromotionReport(promoted_ids=tuple(promoted), skipped=tuple(skipped))

    def _distinct_success_archives(self, item_id: int) -> frozenset:
        """Distinct archive ids backing the item's success evidence."""
        rows = self._store.list_evidence(item_id)
        source_ids = {
            int(row["source_id"])
            for row in rows
            if row["outcome"] == _OUTCOME_SUCCESS and row["source_id"] is not None
        }
        if not source_ids:
            return frozenset()
        archives = set()
        for source in self._store.list_item_sources(item_id):
            if int(source["id"]) in source_ids and source["archive_id"] is not None:
                archives.add(int(source["archive_id"]))
        return frozenset(archives)

    # ---- playbook eligibility (evaluation only; generation is P3) ----

    def evaluate_playbook_eligibility(
        self, *, embedding_engine=None
    ) -> PlaybookEligibilityReport:
        """Cluster eligible active experiences by L0 normalized similarity.

        A missing, unloaded, or failing embedding engine degrades to an empty
        report with the explicit ``embedding_unavailable`` reason; eligibility
        is never claimed without real similarity evidence.
        """
        engine_ready = (
            embedding_engine is not None
            and getattr(embedding_engine, "is_loaded", False) is True
        )
        if not engine_ready:
            logger.warning(
                "playbook eligibility evaluation skipped: embedding engine unavailable"
            )
            return PlaybookEligibilityReport(
                clusters=(), reason=_REASON_EMBEDDING_UNAVAILABLE
            )

        store = self._store
        ids = store.list_item_ids(
            status=ContextStatus.ACTIVE,
            content_type=ContextContentType.EXPERIENCE,
        )
        records = store.get_retrieval_records(ids)
        groups: dict[tuple[ContextScope, str], list] = {}
        for record in records:
            item = record.item
            if not self._is_playbook_seed(item.id):
                continue
            if item.scope is ContextScope.GLOBAL:
                key = (ContextScope.GLOBAL, "")
            else:
                key = (ContextScope.PROJECT, item.project)
            groups.setdefault(key, []).append(record)

        clusters: list[PlaybookCluster] = []
        for (scope, project), members in sorted(
            groups.items(), key=lambda entry: (entry[0][0].value, entry[0][1])
        ):
            if len(members) < self._config.context_playbook_min_experiences:
                continue
            try:
                vectors = [
                    embedding_engine.encode_document(record.l0) for record in members
                ]
            except Exception:
                logger.warning(
                    "playbook eligibility evaluation skipped: embedding encoding failed"
                )
                return PlaybookEligibilityReport(
                    clusters=(), reason=_REASON_EMBEDDING_UNAVAILABLE
                )
            cluster = _best_similarity_cluster(
                [record.item.id for record in members],
                vectors,
                self._config.context_promotion_similarity_threshold,
                self._config.context_playbook_min_experiences,
            )
            if cluster is not None:
                cluster_ids, min_similarity = cluster
                structured = {i:json.loads(self._structured_payload(i)) for i in cluster_ids
                              if self._structured_payload(i)}
                if structured:
                    # A summary may not mix old opaque notes with verified cases.
                    # Similar words alone cannot establish a transferable mechanism.
                    if len(structured) != len(cluster_ids):
                        continue
                    from evolvmem.experience_service import _terms
                    cases = list(structured.values())
                    compatible = True
                    for position, first in enumerate(cases):
                        for second in cases[position+1:]:
                            if any(k in second['conditions'] and second['conditions'][k] != v
                                   for k,v in first['conditions'].items()):
                                compatible = False
                            left = _terms(' '.join(first['steps']) + first['rationale'])
                            right = _terms(' '.join(second['steps']) + second['rationale'])
                            if len(left & right) / max(1, min(len(left),len(right))) < .35:
                                compatible = False
                    tasks = set()
                    for i in cluster_ids:
                        latest = {}
                        for e in store.list_evidence(i):
                            if e['event_key']:
                                latest[e['event_key']] = e
                        tasks.update(e['task_id'] for e in latest.values()
                                     if e['outcome'] in {'success','confirmed'} and e['source_id'])
                    if not compatible or len(tasks) < 2:
                        continue
                clusters.append(
                    PlaybookCluster(
                        scope=scope,
                        project=project,
                        item_ids=cluster_ids,
                        min_similarity=min_similarity,
                    )
                )
        return PlaybookEligibilityReport(
            clusters=tuple(clusters), reason=_REASON_OK
        )

    def _is_playbook_seed(self, item_id: int) -> bool:
        """Enough successes and no unresolved contradicted evidence."""
        if self._structured_payload(item_id):
            item = self._store.get_item(item_id, include_layers=False)
            return item.status is ContextStatus.ACTIVE and item.success_count > 0
        rows = self._store.list_evidence(item_id)
        successes = sum(1 for row in rows if row["outcome"] == _OUTCOME_SUCCESS)
        if successes < self._config.context_promotion_min_successes:
            return False
        return not _has_unresolved_contradiction(rows)

    # ---- candidate review ----

    def list_candidates(self, *, project: str | None = None) -> tuple[CandidateSummary, ...]:
        """Read-only candidate review: L0 plus metadata, no access side effects."""
        if project is not None and not isinstance(project, str):
            raise ContextValidationError("project must be a string or None")
        ids = self._store.list_item_ids(
            status=ContextStatus.CANDIDATE, project=project
        )
        records = self._store.get_retrieval_records(ids)
        return tuple(
            CandidateSummary(
                id=record.item.id,
                identity_key=record.item.identity_key,
                content_type=record.item.content_type,
                project=record.item.project,
                scope=record.item.scope,
                l0=record.l0,
                confidence=record.item.confidence,
                success_count=record.item.success_count,
                failure_count=record.item.failure_count,
                created_at=record.item.created_at,
                updated_at=record.item.updated_at,
            )
            for record in records
        )


def _has_unresolved_contradiction(rows: list[dict]) -> bool:
    """Frozen rule: a contradicted row is unresolved when no newer
    success/confirmed evidence exists for the same item."""
    last_resolving = -1
    last_contradicted = -1
    for row in rows:
        outcome = row["outcome"]
        if outcome in (_OUTCOME_SUCCESS, _OUTCOME_CONFIRMED):
            last_resolving = max(last_resolving, int(row["id"]))
        elif outcome == _OUTCOME_CONTRADICTED:
            last_contradicted = max(last_contradicted, int(row["id"]))
    return last_contradicted > last_resolving


def _normalized_cosine(first: list, second: list) -> float:
    """Normalized cosine similarity ``(1+cos)/2`` clamped to [0, 1]."""
    dot = sum(x * y for x, y in zip(first, second))
    norm_first = math.sqrt(sum(x * x for x in first))
    norm_second = math.sqrt(sum(x * x for x in second))
    if norm_first == 0.0 or norm_second == 0.0:
        return 0.0
    cosine = max(-1.0, min(1.0, dot / (norm_first * norm_second)))
    return (1.0 + cosine) / 2.0


def _best_similarity_cluster(
    item_ids: list[int],
    vectors: list,
    threshold: float,
    min_size: int,
) -> tuple[tuple[int, ...], float] | None:
    """Largest deterministic clique whose pairwise similarity reaches threshold.

    Each seed position (in id order) grows a clique greedily; the largest
    clique wins, earliest seed breaks ties. Returns the cluster's item ids
    (ascending) and its minimum pairwise similarity, or None below min_size.
    """
    best: tuple[int, ...] = ()
    for seed in range(len(item_ids)):
        cluster = [seed]
        for other in range(len(item_ids)):
            if other == seed:
                continue
            if all(
                _normalized_cosine(vectors[other], vectors[member]) >= threshold
                for member in cluster
            ):
                cluster.append(other)
        if len(cluster) > len(best):
            best = tuple(cluster)
    if len(best) < min_size:
        return None
    pair_sims = [
        _normalized_cosine(vectors[left], vectors[right])
        for index, left in enumerate(best)
        for right in best[index + 1 :]
    ]
    return (
        tuple(sorted(item_ids[position] for position in best)),
        min(pair_sims),
    )


def _now_iso() -> str:
    """Match the store's lexical-order UTC timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _validate_outcome(value: object) -> str:
    if not isinstance(value, str) or value not in _OUTCOMES:
        raise ContextValidationError(
            "outcome must be one of " + ", ".join(sorted(_OUTCOMES))
        )
    return value


def _normalize_note(value: object) -> str:
    if not isinstance(value, str):
        raise ContextValidationError("note must be a string")
    return value.replace("\r\n", "\n").strip()


def _validate_positive_int(value: object, field_name: str) -> int:
    """Reject booleans even though Python models them as integers."""
    if type(value) is not int or value <= 0:
        raise ContextValidationError(f"{field_name} must be a positive integer")
    return value


def _validate_unit_interval(value: object, field_name: str) -> None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ContextValidationError(f"{field_name} must be between 0 and 1")


def _normalize_id_tuple(value: object, field_name: str) -> tuple[int, ...]:
    try:
        ids = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            f"{field_name} must be an iterable of positive integers"
        ) from exc
    for item_id in ids:
        _validate_positive_int(item_id, field_name)
    return ids
