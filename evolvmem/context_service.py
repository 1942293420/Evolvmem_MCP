"""Typed Context Core read/write coordination boundary.

ContextService is the single coordination point between adapters and the
Context Core: it owns the service lifecycle, gates operations by context
mode, normalizes workspace identifiers, orchestrates
Retriever/Renderer/Store for session start and search, performs exact
L1/L2 reads, and reports a content-free status snapshot. It also
coordinates the candidate lifecycle (confirm/outcome review), the
session-archive purge sweeps, and the promotion/playbook consolidation
pass under the same mode gates, shared lock, and transaction discipline.

The service owns every production legacy mutation. In compat/shadow/primary
mode each typed legacy_* call takes the shared cutover lock and writes the
legacy projection row, the ContextItem with its three layers, and their ID
mapping inside one outer ContextStore transaction; in legacy mode the same
typed calls are served by a service-owned legacy backend with no claim that
Core changed. Vector indexes stay derived caches with independent dirty
markers, updated only after the SQLite commit.

``initialize()`` opens the existing Context schema only. It never runs
``LegacyMemoryMigrator.migrate()``, never rebuilds a vector index, and
never edits config. Context reads in legacy/compat mode fail closed with
``context_not_enabled``; shadow mode serves explicit reads; primary mode
re-validates the startup invariants with the same evaluator the formal
cutover gate uses — quick-check, mapping completeness, per-item layers,
the full projection-lag classes, vector health, and config diagnostics —
and reports ``degraded_legacy`` when any of them fail. Readiness is always
computed from that evaluation; the service accepts no caller-supplied
ready claim.

Privacy contract: workspace paths are normalized to basename/alias before
use, and the service neither stores nor logs absolute paths, queries, or
content — it owns no logger at all.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath

import numpy as np

from evolvmem.config import Config
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.context_lifecycle import (
    CandidateSummary,
    ContextLifecycle,
    EvidenceReport,
    PromotionSkip,
)
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextMatchType,
    ContextMode,
    ContextReadRequest,
    ContextReadResult,
    ContextRetrievalRecord,
    ContextScope,
    ContextScoreComponents,
    ContextSearchRequest,
    ContextSearchResult,
    ContextServiceError,
    ContextServiceStatus,
    ContextSessionStartRequest,
    ContextSessionStartResult,
    ContextStatus,
    ContextValidationError,
    ContextVectorDocument,
)
from evolvmem.context_playbook import PlaybookGenerator, PlaybookSkip
from evolvmem.context_renderer import ContextRenderCandidate, ContextRenderer
from evolvmem.context_retriever import ContextRetriever
from evolvmem.context_store import ContextStore
from evolvmem.context_vector_sync import ContextVectorSynchronizer
from evolvmem.cutover_checks import check_projection_lag
from evolvmem.cutover_lock import CutoverLock
from evolvmem.embedding import EmbeddingEngine
from evolvmem.legacy_compat import LegacyCompatibilityFacade
from evolvmem.legacy_models import (
    LegacyAccessRequest,
    LegacyAccessResult,
    LegacyAddRequest,
    LegacyExtractionItem,
    LegacyExtractionRequest,
    LegacyExtractionResult,
    LegacyHardDeleteRequest,
    LegacyMutationResult,
    LegacyRemoveRequest,
    LegacyReplaceRequest,
    LegacyStatusRequest,
    LegacyUpdateRequest,
)
from evolvmem.legacy_projection import (
    LegacyProjectionInsert,
    LegacyProjectionReplace,
    LegacyProjectionUpdate,
)
from evolvmem.memory_store import MemoryStore
from evolvmem.semantic_merge import find_semantic_match
from evolvmem.session_archive import SessionArchiver, SessionPurgeReport
from evolvmem.vector_index import VectorIndex


_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Diagnostics stay bounded and content-free: a short prefix of stable codes
# and config validation messages, each truncated to a fixed length.
_MAX_DIAGNOSTICS = 8
_MAX_DIAGNOSTIC_CHARS = 160

# Every mapped live ContextItem has exactly one L0, L1, and L2.
_ALL_LAYERS = (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)


@dataclass(frozen=True, slots=True)
class _VectorAftermath:
    """Post-commit vector work for one mutation; texts are already persisted."""

    legacy_upserts: tuple[tuple[int, str], ...] = ()
    legacy_removals: tuple[int, ...] = ()
    context_upserts: tuple[tuple[int, str], ...] = ()
    context_removals: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    """Merged maintenance outcome: the promotion batch plus playbook generation.

    Carries ids and reason codes only; a degraded playbook stage reports its
    explicit reason and is never an error.
    """

    promoted_ids: tuple[int, ...]
    promotion_skipped: tuple[PromotionSkip, ...]
    playbook_created_ids: tuple[int, ...]
    playbook_skipped: tuple[PlaybookSkip, ...]
    playbook_reason: str


class ContextService:
    """Mode-gated typed read boundary over the Context Core components."""

    def __init__(
        self,
        config: Config,
        *,
        store: ContextStore | None = None,
        vector_index: VectorIndex | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        retriever: ContextRetriever | None = None,
        renderer: ContextRenderer | None = None,
    ) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        self.config = config
        self.store = store
        self.vector_index = vector_index
        self.embedding_engine = embedding_engine
        self.retriever = retriever
        self.renderer = renderer
        self._legacy_vector: VectorIndex | None = None
        self._legacy_store: MemoryStore | None = None
        self._migrator: LegacyMemoryMigrator | None = None
        self._synchronizer: ContextVectorSynchronizer | None = None
        self._facade: LegacyCompatibilityFacade | None = None
        self._lifecycle: ContextLifecycle | None = None
        self._archiver: SessionArchiver | None = None
        self._generator: PlaybookGenerator | None = None
        self._cutover_lock = CutoverLock(config)
        self._mode: ContextMode | None = None
        self._adapter = ""
        self._ready = False
        self._reason_codes: tuple[str, ...] = ()
        self._diagnostics: tuple[str, ...] = ()

    # ---- lifecycle ----

    def initialize(self, *, mode: ContextMode, adapter: str) -> ContextServiceStatus:
        """Open the Context schema once and evaluate mode-gated readiness.

        Repeat calls with the same mode/adapter are idempotent and never
        re-open the store; a conflicting pair is rejected. Unknown modes
        fail closed before any resource is touched.
        """
        if not isinstance(mode, ContextMode):
            raise ContextServiceError(
                "invalid_mode", "context mode must be a ContextMode"
            )
        if not isinstance(adapter, str):
            raise ContextValidationError("adapter must be a string")
        if self._mode is not None:
            if mode is self._mode and adapter == self._adapter:
                return self.status()
            raise ContextServiceError(
                "initialize_conflict",
                "service is already initialized with a different mode/adapter",
            )
        self._ensure_dependencies()
        self.store.initialize()
        self._mode = mode
        self._adapter = adapter
        self._refresh_health()
        return self.status()

    def close(self) -> None:
        """Close every dependency the lifecycle coordinates; safe to repeat."""
        try:
            for resource in (
                self.store,
                self.vector_index,
                self._legacy_vector,
                self._legacy_store,
                self.embedding_engine,
            ):
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
        finally:
            # 借用 store 的协调器不持有资源；丢弃引用即完成清理
            self._lifecycle = None
            self._archiver = None
            self._generator = None
            self._mode = None
            self._adapter = ""
            self._ready = False
            self._reason_codes = ()
            self._diagnostics = ()

    def status(self) -> ContextServiceStatus:
        """Content-free snapshot: modes, counts, flags, reason codes, diagnostics."""
        if self._mode is None:
            raise ContextServiceError(
                "not_initialized", "service is not initialized"
            )
        try:
            status_counts = self.store.count_by_status()
        except Exception:
            status_counts = {}  # a broken schema must not block diagnostics
        try:
            # The full design-defined lag evaluator: every mismatch class,
            # not just missing mappings. A broken schema must not block
            # diagnostics, so failures degrade to zeros.
            lag = check_projection_lag(self.config, self.store)
            mapping_count = lag.legacy_rows - lag.missing_mapping
            projection_lag = lag.projection_lag
        except Exception:
            mapping_count = 0
            projection_lag = 0
        context_ready, context_dirty = self._vector_flags(self.vector_index)
        legacy_ready, legacy_dirty = self._vector_flags(self._legacy_vector_index())
        return ContextServiceStatus(
            mode=self._mode,
            adapter=self._adapter,
            ready=self._ready,
            status_counts=status_counts,
            mapping_count=mapping_count,
            projection_lag=projection_lag,
            context_vector_ready=context_ready,
            context_vector_dirty=context_dirty,
            legacy_vector_ready=legacy_ready,
            legacy_vector_dirty=legacy_dirty,
            diagnostics=self._diagnostics,
            reason_codes=self._reason_codes,
        )

    def _ensure_dependencies(self) -> None:
        if self.store is None:
            self.store = ContextStore(self.config)
        if self.vector_index is None:
            self.vector_index = VectorIndex(
                self.config, path=self.config.context_vector_path
            )
        if self.retriever is None:
            self.retriever = ContextRetriever(
                self.config, self.store, self.vector_index, self.embedding_engine
            )
        if self.renderer is None:
            self.renderer = ContextRenderer(self.config)

    def _refresh_health(self) -> None:
        if self._mode in (ContextMode.LEGACY, ContextMode.COMPAT):
            self._ready = False
            self._reason_codes = ("context_not_enabled",)
            self._diagnostics = ()
            return
        if self._mode is ContextMode.SHADOW:
            # Shadow serves explicit reads for acceptance; vector stays optional.
            self._ready = True
            self._reason_codes = ()
            self._diagnostics = ()
            return
        diagnostics = self._primary_diagnostics()
        self._ready = not diagnostics
        self._reason_codes = () if self._ready else ("degraded_legacy",)
        self._diagnostics = diagnostics

    def _primary_diagnostics(self) -> tuple[str, ...]:
        """Revalidate the startup primary invariants, content-free.

        Quick-check, mapping completeness, per-item layers, and every
        projection-lag class come from the same cutover_checks evaluator the
        formal primary gate consumes, so startup readiness cannot drift from
        the gate and can never be supplied by the caller.
        """
        diagnostics: list[str] = list(self.config.validate_runtime())
        diagnostics.extend(self._quick_check_diagnostics())
        try:
            self.store.count_by_status()
        except Exception:
            diagnostics.append("schema_invariant_failed")
        documents: list[ContextVectorDocument] | None = None
        try:
            documents = self.store.list_vector_documents()
            for document in documents:
                # Raises unless the serving item has exactly L0/L1/L2.
                self.store.get_item(document.item_id)
        except Exception:
            diagnostics.append("layer_invariant_failed")
            documents = None
        diagnostics.extend(self._projection_invariant_diagnostics())
        diagnostics.extend(self._vector_diagnostics(documents))
        deduped = list(dict.fromkeys(diagnostics))
        return tuple(
            message[:_MAX_DIAGNOSTIC_CHARS]
            for message in deduped[:_MAX_DIAGNOSTICS]
        )

    def _quick_check_diagnostics(self) -> tuple[str, ...]:
        try:
            rows = self.store._connection().execute("PRAGMA quick_check").fetchall()
        except Exception:
            return ("quick_check_failed",)
        if rows and all(str(row[0]).lower() == "ok" for row in rows):
            return ()
        return ("quick_check_failed",)

    def _projection_invariant_diagnostics(self) -> tuple[str, ...]:
        """Map the full lag report onto startup diagnostics, one code per cause."""
        try:
            report = check_projection_lag(self.config, self.store)
        except Exception:
            return ("projection_evaluation_failed",)
        diagnostics: list[str] = []
        if (
            report.missing_mapping
            or report.duplicate_mapping_target
            or report.orphan_mapping
            or report.dangling_item_mapping
        ):
            diagnostics.append("legacy_mapping_incomplete")
        if report.layer_mismatch:
            diagnostics.append("layer_invariant_failed")
        if report.status_mismatch or report.l1_mismatch or report.supersession_mismatch:
            diagnostics.append("projection_lag_nonzero")
        return tuple(diagnostics)

    def _vector_diagnostics(
        self, documents: list[ContextVectorDocument] | None
    ) -> list[str]:
        index = self.vector_index
        if index.path != self.config.context_vector_path.resolve():
            return ["context_vector_path_mismatch"]
        if documents is None:
            return []  # schema/layer failures already cover the count baseline
        try:
            dirty = bool(index.is_dirty())
            count = index.count()
        except Exception:
            return ["context_vector_unavailable"]
        if dirty:
            return ["context_vector_dirty"]
        if count != len(documents):
            return ["context_vector_count_mismatch"]
        return []

    def _legacy_vector_index(self) -> VectorIndex:
        if self._legacy_vector is None:
            self._legacy_vector = VectorIndex(self.config)
        return self._legacy_vector

    @staticmethod
    def _vector_flags(index: VectorIndex) -> tuple[bool, bool]:
        """Read-only (ready, dirty) flags that survive an uninitialized index."""
        try:
            dirty = bool(index.is_dirty())
        except Exception:
            dirty = False
        try:
            ready = not dirty and index.count() > 0
        except Exception:
            ready = False
        return ready, dirty

    def _require_serving(self) -> None:
        if self._mode is None:
            raise ContextServiceError(
                "not_initialized", "service is not initialized"
            )
        if self._mode in (ContextMode.LEGACY, ContextMode.COMPAT):
            raise ContextServiceError(
                "context_not_enabled",
                f"context reads are disabled in {self._mode.value} mode",
            )
        if self._mode is ContextMode.PRIMARY and not self._ready:
            raise ContextServiceError(
                "degraded_legacy",
                "primary invariants failed; context reads report degraded_legacy",
            )

    # ---- reads ----

    def read(self, request: ContextReadRequest) -> ContextReadResult:
        """Return the exact requested layer or a structured failure.

        Never consults the Retriever and never substitutes a similar item.
        """
        if not isinstance(request, ContextReadRequest):
            raise ContextValidationError(
                "request must be a ContextReadRequest instance"
            )
        self._require_serving()
        item = self.store.get_item(request.id, include_layers=False)
        if item is None:
            return self._read_error(request, "not_found")
        if item.status is not ContextStatus.ACTIVE:
            return self._read_error(request, "not_readable")
        now = datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)
        if item.expires_at is not None and item.expires_at <= now:
            return self._read_error(request, "expired")
        content = self.store.get_layer(request.id, request.layer)
        if content is None:
            return self._read_error(request, "invalid_layer")
        return ContextReadResult(
            id=request.id, layer=request.layer, content=content
        )

    @staticmethod
    def _read_error(request: ContextReadRequest, code: str) -> ContextReadResult:
        return ContextReadResult(
            id=request.id, layer=request.layer, content="", error_code=code
        )

    def search(
        self, request: ContextSearchRequest
    ) -> tuple[ContextSearchResult, ...]:
        """Run thresholded retrieval, then batch-update access for served results."""
        if not isinstance(request, ContextSearchRequest):
            raise ContextValidationError(
                "request must be a ContextSearchRequest instance"
            )
        self._require_serving()
        normalized = replace(
            request, project=self._normalize_project(request.project)
        )
        results = self.retriever.search(normalized)
        if results:
            # One batch, only after the final result tuple exists; neighbors
            # dropped by thresholds never reach this update.
            self._record_served_access([result.id for result in results])
        return results

    def session_start(
        self, request: ContextSessionStartRequest
    ) -> ContextSessionStartResult:
        """Render a bounded L1 history block; update access only for rendered IDs."""
        if not isinstance(request, ContextSessionStartRequest):
            raise ContextValidationError(
                "request must be a ContextSessionStartRequest instance"
            )
        self._require_serving()
        project = self._normalize_project(request.project)
        candidates = tuple(
            ContextRenderCandidate(
                result=result,
                l1=self.store.get_layer(result.id, ContextLayer.L1) or "",
            )
            for result in self._session_candidates(project, request.query)
        )
        rendered = self.renderer.render(
            candidates, project=project, max_chars=request.max_chars
        )
        if rendered.selected_ids:
            # A renderer exception or an empty block never reaches this update.
            self._record_served_access(list(rendered.selected_ids))
        return ContextSessionStartResult(
            block=rendered.block,
            selected_ids=rendered.selected_ids,
            used_chars=rendered.used_chars,
            excluded_counts=rendered.excluded_counts,
        )

    def _session_candidates(
        self, project: str, query: str
    ) -> tuple[ContextSearchResult, ...]:
        """Retriever hits plus eligible pinned-policy seeds, de-duplicated by ID."""
        top_k = min(max(self.config.context_inject_max_items, 1), 20)
        results = self.retriever.search(
            ContextSearchRequest(query=query, project=project, top_k=top_k)
        )
        by_id = {result.id: result for result in results}
        seeds = self.store.list_pinned_policy_records(
            project=project, min_confidence=self.config.context_min_confidence
        )
        for record in seeds:
            by_id.setdefault(record.item.id, self._pinned_seed_result(record))
        return tuple(by_id.values())

    @staticmethod
    def _pinned_seed_result(record: ContextRetrievalRecord) -> ContextSearchResult:
        """Wrap a pinned-policy seed for the renderer.

        Seeds enter the pinned pool without a query match by design, so the
        ranking-derived components stay zeroed and the score is a fixed
        force-select marker; session start never surfaces either one.
        """
        item = record.item
        return ContextSearchResult(
            id=item.id,
            identity_key=item.identity_key,
            l0=record.l0,
            content_type=item.content_type,
            scope=item.scope,
            project=item.project,
            status=item.status,
            tier=item.tier,
            confidence=item.confidence,
            importance=item.importance,
            score=1.0,
            score_components=ContextScoreComponents(
                relevance=0.0,
                project=1.0 if item.scope is ContextScope.PROJECT else 0.5,
                type_priority=0.0,
                confidence=item.confidence,
                importance=item.importance / 10.0,
                evidence=0.0,
                recency=0.0,
                frequency=0.0,
            ),
            match_types=(ContextMatchType.PINNED_POLICY,),
            match_layers=(),
            available_layers=record.available_layers,
        )

    def _normalize_project(self, project: str) -> str:
        """Reduce workspace paths to basename, then apply configured aliases."""
        name = PurePosixPath(project.strip().replace("\\", "/")).name
        aliases = self.config.context_project_aliases
        if isinstance(aliases, dict):
            mapped = aliases.get(name)
            if isinstance(mapped, str) and mapped.strip():
                return mapped.strip()
        return name

    def _record_served_access(self, context_ids: list[int]) -> None:
        """Batch access increments for served Core hits under the shared lock.

        One transaction increments each served ContextItem exactly once and
        mirrors the same +1 onto its mapped legacy projection row: the
        forgetting engine reads the legacy counters, so a Codex-served hit
        must refresh both sides or hot items look idle. Unmapped
        (Core-native) items move only their own counter.
        """
        ids = list(dict.fromkeys(context_ids))
        if not ids:
            return
        with self._cutover_lock.shared():
            with self.store.transaction():
                self.store.update_access(ids)
                legacy_ids = self.store.legacy_ids_mapped_to_items(ids)
                if legacy_ids:
                    self.store.legacy_projection().update_access(legacy_ids)

    # ---- candidate lifecycle and session archives ----

    def confirm(self, item_id: int) -> EvidenceReport:
        """Promote one candidate to active through the lifecycle state machine.

        The status flip and its confirmed evidence share one transaction
        under the shared cutover lock; the newly active item's L0 joins the
        context vector cache only after the commit.
        """
        self._require_serving()
        with self._cutover_lock.shared():
            report = self._context_lifecycle().confirm(item_id)
            l0 = self.store.get_layer(report.item_id, ContextLayer.L0) or ""
        self._apply_vector_aftermath(
            _VectorAftermath(context_upserts=((report.item_id, l0),))
        )
        return report

    def record_outcome(
        self,
        item_id: int,
        outcome: str,
        note: str = "",
        source_id: int | None = None,
    ) -> EvidenceReport:
        """Record one outcome and apply the frozen confidence/archive rules.

        The lifecycle policy-checks the note before anything is stored.
        Failure-driven archival and playbook demotion leave the context
        vector cache only after the commit.
        """
        self._require_serving()
        with self._cutover_lock.shared():
            report = self._context_lifecycle().record_outcome(
                item_id, outcome, note=note, source_id=source_id
            )
        removals: list[int] = []
        if report.archived:
            removals.append(report.item_id)
        removals.extend(report.demoted_playbook_ids)
        if removals:
            self._apply_vector_aftermath(
                _VectorAftermath(context_removals=tuple(removals))
            )
        return report

    def sweep_archives(self) -> SessionPurgeReport:
        """Run one TTL purge sweep over expired session archives.

        Purge is irreversible and never faked: rows transition only after
        their payload file is actually gone, and failures stay available
        for the next sweep.
        """
        self._require_serving()
        with self._cutover_lock.shared():
            return self._session_archiver().sweep_expired()

    def archive_project(self, project: str) -> SessionPurgeReport:
        """Immediately purge every available archive of one project.

        The workspace identifier normalizes exactly like session_start
        (basename, then configured aliases); ContextItems are never
        deleted — only their source_state is recomputed.
        """
        if not isinstance(project, str):
            raise ContextValidationError("project must be a string")
        self._require_serving()
        normalized = self._normalize_project(project)
        if not normalized:
            raise ContextValidationError("project must not be empty")
        with self._cutover_lock.shared():
            return self._session_archiver().purge_project(normalized)

    def list_candidates(
        self, project: str | None = None
    ) -> tuple[CandidateSummary, ...]:
        """Read-only candidate review: L0 metadata, no access side effects.

        Candidates are visible only through this explicit review API; they
        never enter retrieval or the session-start block.
        """
        self._require_serving()
        normalized = (
            self._normalize_project(project) if project is not None else None
        )
        return self._context_lifecycle().list_candidates(project=normalized)

    def run_consolidation(self) -> ConsolidationReport:
        """Maintenance pass: automatic promotions, then playbook generation.

        Each stage keeps its own single-transaction guarantee under the
        shared cutover lock. A missing embedding engine or LLM degrades the
        playbook stage to its explicit reason — never an error.
        """
        self._require_serving()
        lifecycle = self._context_lifecycle()
        with self._cutover_lock.shared():
            promotion = lifecycle.evaluate_promotions()
            upserts = tuple(
                (item_id, self.store.get_layer(item_id, ContextLayer.L0) or "")
                for item_id in promotion.promoted_ids
            )
        if upserts:
            self._apply_vector_aftermath(
                _VectorAftermath(context_upserts=upserts)
            )
        with self._cutover_lock.shared():
            generation = self._playbook_generator().generate()
        return ConsolidationReport(
            promoted_ids=promotion.promoted_ids,
            promotion_skipped=promotion.skipped,
            playbook_created_ids=generation.created_ids,
            playbook_skipped=generation.skipped,
            playbook_reason=generation.reason,
        )

    # ---- typed legacy mutations ----

    def legacy_facade(self) -> LegacyCompatibilityFacade:
        """Old-shaped compatibility boundary delegating to this service."""
        if self._facade is None:
            self._facade = LegacyCompatibilityFacade(self)
        return self._facade

    def legacy_add(self, request: LegacyAddRequest) -> LegacyMutationResult:
        """Write the projection row, the ContextItem + layers, and the mapping atomically."""
        self._require_request(request, LegacyAddRequest)
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                legacy_id = self._legacy_backend().add(
                    key=request.key,
                    value=request.value,
                    attribute=request.attribute,
                    tags=list(request.tags) or None,
                    source_session=request.source_session,
                    importance=request.importance,
                    tier=request.tier,
                    expires_at=request.expires_at,
                )
            self._apply_vector_aftermath(
                _VectorAftermath(legacy_upserts=((legacy_id, request.value),))
            )
            return LegacyMutationResult(
                legacy_id=legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=True,
            )
        with self._cutover_lock.shared():
            with self.store.transaction():
                result, aftermath = self._add_dual_in_transaction(request)
        self._apply_vector_aftermath(aftermath)
        return result

    def _add_dual_in_transaction(
        self, request: LegacyAddRequest
    ) -> tuple[LegacyMutationResult, "_VectorAftermath"]:
        """legacy_add write steps; the caller holds the lock and outer transaction."""
        repository = self.store.legacy_projection()
        legacy_id = repository.insert(
            LegacyProjectionInsert(
                key=request.key,
                value=request.value,
                attribute=request.attribute,
                tags=request.tags,
                source_session=request.source_session,
                importance=request.importance,
                tier=request.tier,
                expires_at=request.expires_at,
            )
        )
        row = repository.get_by_id(legacy_id)
        # The Context draft derives from the just-written projection
        # row so projection inheritance and Core metadata cannot diverge.
        draft = self._legacy_migrator().draft_from_projection_row(
            row, confidence=request.confidence
        )
        item = self.store._create_legacy_item(draft)
        self.store.record_legacy_mapping(legacy_id, item.id)
        new_l0 = item.layers.l0 if item.layers is not None else ""
        return (
            LegacyMutationResult(
                legacy_id=legacy_id,
                context_id=item.id,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=_ALL_LAYERS,
                changed=True,
            ),
            _VectorAftermath(
                legacy_upserts=((legacy_id, request.value),),
                context_upserts=((item.id, new_l0),),
            ),
        )

    def legacy_replace(self, request: LegacyReplaceRequest) -> LegacyMutationResult:
        """Supersede the exact mapped predecessor on both sides atomically."""
        self._require_request(request, LegacyReplaceRequest)
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                new_id = self._legacy_backend().replace(
                    key=request.key,
                    new_value=request.new_value,
                    attribute=request.attribute,
                    tags=request.tags,
                    source_session=request.source_session,
                    importance=request.importance,
                    tier=request.tier,
                    expires_at=request.expires_at,
                )
            self._apply_vector_aftermath(
                _VectorAftermath(legacy_upserts=((new_id, request.new_value),))
            )
            return LegacyMutationResult(
                legacy_id=new_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=True,
            )
        with self._cutover_lock.shared():
            with self.store.transaction():
                result, aftermath = self._replace_dual_in_transaction(request)
        self._apply_vector_aftermath(aftermath)
        return result

    def _replace_dual_in_transaction(
        self, request: LegacyReplaceRequest
    ) -> tuple[LegacyMutationResult, "_VectorAftermath"]:
        """legacy_replace write steps; the caller holds the lock and outer transaction."""
        repository = self.store.legacy_projection()
        old_legacy_id, new_id = repository.replace(
            LegacyProjectionReplace(
                key=request.key,
                new_value=request.new_value,
                attribute=request.attribute,
                tags=request.tags,
                source_session=request.source_session,
                importance=request.importance,
                tier=request.tier,
                expires_at=request.expires_at,
            )
        )
        new_row = repository.get_by_id(new_id)
        draft = self._legacy_migrator().draft_from_projection_row(
            new_row, confidence=request.confidence
        )
        old_context_id = None
        if old_legacy_id is None:
            item = self.store._create_legacy_item(draft)
        else:
            old_context_id = self.store.resolve_legacy_mapping(old_legacy_id)
            if old_context_id is None:
                # An unmapped legacy row is migrated inside this same
                # outer transaction before the mutation continues.
                old_context_id = self._legacy_migrator().migrate_projection_row(
                    repository.get_by_id(old_legacy_id)
                )
            item = self.store.supersede_item(old_context_id, draft)
        self.store.record_legacy_mapping(new_id, item.id)
        new_l0 = item.layers.l0 if item.layers is not None else ""
        return (
            LegacyMutationResult(
                legacy_id=new_id,
                context_id=item.id,
                old_legacy_id=old_legacy_id,
                old_context_id=old_context_id,
                available_layers=_ALL_LAYERS,
                changed=True,
            ),
            _VectorAftermath(
                legacy_upserts=((new_id, request.new_value),),
                legacy_removals=(
                    (old_legacy_id,) if old_legacy_id is not None else ()
                ),
                context_upserts=((item.id, new_l0),),
                context_removals=(
                    (old_context_id,) if old_context_id is not None else ()
                ),
            ),
        )

    def legacy_remove(self, request: LegacyRemoveRequest) -> LegacyMutationResult:
        """Soft-delete the projection row and its mapped ContextItem."""
        self._require_request(request, LegacyRemoveRequest)
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                backend = self._legacy_backend()
                existed = backend.get_by_id(request.legacy_id) is not None
                if existed:
                    backend.remove(request.legacy_id)
            if existed:
                self._apply_vector_aftermath(
                    _VectorAftermath(legacy_removals=(request.legacy_id,))
                )
            return LegacyMutationResult(
                legacy_id=request.legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=existed,
            )
        context_id: int | None = None
        with self._cutover_lock.shared():
            with self.store.transaction():
                repository = self.store.legacy_projection()
                row = repository.get_by_id(request.legacy_id)
                changed = row is not None
                if changed:
                    context_id = self.store.resolve_legacy_mapping(request.legacy_id)
                    if context_id is None:
                        context_id = self._legacy_migrator().migrate_projection_row(row)
                    repository.soft_delete(request.legacy_id)
                    if not self.store.set_item_status(context_id, ContextStatus.DELETED):
                        raise ContextServiceError(
                            "degraded_legacy",
                            "legacy mapping points at a missing ContextItem; "
                            "the whole mutation rolled back",
                        )
        if changed:
            self._apply_vector_aftermath(
                _VectorAftermath(
                    legacy_removals=(request.legacy_id,),
                    context_removals=(context_id,),
                )
            )
        return LegacyMutationResult(
            legacy_id=request.legacy_id,
            context_id=context_id,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=_ALL_LAYERS if changed else (),
            changed=changed,
        )

    def legacy_update(self, request: LegacyUpdateRequest) -> LegacyMutationResult:
        """Mirror an in-place metadata edit onto the projection and its ContextItem.

        importance/tier/attribute/tags share one transaction. When attribute
        or tags move, the mapped item's derived fields move with them:
        content_type/scope re-derive from the just-written projection row
        through the migrator's public policy (``content_type_for`` /
        ``scope_for`` / ``tags_for``), so the two sides cannot drift apart
        invisibly.
        """
        self._require_request(request, LegacyUpdateRequest)
        self._require_initialized()
        has_changes = (
            request.importance is not None
            or request.tier is not None
            or request.attribute is not None
            or request.tags is not None
        )
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                backend = self._legacy_backend()
                changed = (
                    has_changes
                    and backend.get_by_id(request.legacy_id) is not None
                )
                if changed:
                    update = LegacyProjectionUpdate(
                        legacy_id=request.legacy_id,
                        importance=request.importance,
                        tier=request.tier,
                    )
                    # 旧行为等价（就地编辑、id/历史不变），但四个字段共享一个
                    # 事务。backend 的窄更新面只携带 importance/tier；投影写经
                    # 服务自有 ContextStore 的借用连接，绝无第二连接第二提交。
                    with self.store.transaction():
                        self.store.legacy_projection().update_metadata(update)
                        self.store.update_legacy_projection_classification(
                            request.legacy_id,
                            attribute=request.attribute,
                            tags=request.tags,
                        )
            return LegacyMutationResult(
                legacy_id=request.legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=changed,
            )
        context_id: int | None = None
        with self._cutover_lock.shared():
            with self.store.transaction():
                repository = self.store.legacy_projection()
                row = repository.get_by_id(request.legacy_id)
                changed = has_changes and row is not None
                if row is not None:
                    context_id = self.store.resolve_legacy_mapping(request.legacy_id)
                if changed:
                    if context_id is None:
                        context_id = self._legacy_migrator().migrate_projection_row(row)
                    update = LegacyProjectionUpdate(
                        legacy_id=request.legacy_id,
                        importance=request.importance,
                        tier=request.tier,
                    )
                    repository.update_metadata(update)
                    self.store.update_legacy_projection_classification(
                        request.legacy_id,
                        attribute=request.attribute,
                        tags=request.tags,
                    )
                    content_type: ContextContentType | None = None
                    scope: ContextScope | None = None
                    tags: tuple[str, ...] | None = None
                    if request.attribute is not None or request.tags is not None:
                        # Core 派生字段取自刚落库的投影行，与
                        # draft_from_projection_row 共用同一套迁移器公开策略
                        updated_row = repository.get_by_id(request.legacy_id)
                        if request.attribute is not None:
                            content_type = self._legacy_migrator().content_type_for(
                                updated_row
                            )
                            scope = LegacyMemoryMigrator.scope_for(content_type)
                        if request.tags is not None:
                            tags = LegacyMemoryMigrator.tags_for(
                                updated_row.get("tags")
                            )
                    if not self.store.update_item_from_legacy(
                        context_id,
                        update,
                        content_type=content_type,
                        scope=scope,
                        tags=tags,
                    ):
                        raise ContextServiceError(
                            "degraded_legacy",
                            "legacy mapping points at a missing ContextItem; "
                            "the whole update rolled back",
                        )
        return LegacyMutationResult(
            legacy_id=request.legacy_id,
            context_id=context_id,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=_ALL_LAYERS if context_id is not None else (),
            changed=changed,
        )

    def legacy_archive(self, request: LegacyStatusRequest) -> LegacyMutationResult:
        """Archive the projection row and its mapped ContextItem."""
        return self._legacy_set_status(
            request,
            legacy_status="archived",
            context_status=ContextStatus.ARCHIVED,
            reactivate=False,
        )

    def legacy_restore(self, request: LegacyStatusRequest) -> LegacyMutationResult:
        """Return the projection row and its mapped ContextItem to active."""
        return self._legacy_set_status(
            request,
            legacy_status="active",
            context_status=ContextStatus.ACTIVE,
            reactivate=True,
        )

    def legacy_hard_delete(
        self, request: LegacyHardDeleteRequest
    ) -> LegacyMutationResult:
        """Physically remove the exact mapping/projection/ContextItem triple.

        Irreversible and never invoked by an automated lifecycle: the mapping
        goes first (it foreign-key references the ContextItem), then the
        projection row, then the ContextItem; any failure rolls back all.
        """
        self._require_request(request, LegacyHardDeleteRequest)
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                backend = self._legacy_backend()
                existed = backend.get_by_id(request.legacy_id) is not None
                if existed:
                    with backend.transaction():
                        backend._projection().hard_delete(request.legacy_id)
            if existed:
                self._apply_vector_aftermath(
                    _VectorAftermath(legacy_removals=(request.legacy_id,))
                )
            return LegacyMutationResult(
                legacy_id=request.legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=existed,
            )
        context_id: int | None = None
        with self._cutover_lock.shared():
            with self.store.transaction():
                repository = self.store.legacy_projection()
                row = repository.get_by_id(request.legacy_id)
                changed = row is not None
                if changed:
                    context_id = self.store.resolve_legacy_mapping(request.legacy_id)
                    if context_id is not None:
                        self.store.delete_legacy_mapping(request.legacy_id)
                    repository.hard_delete(request.legacy_id)
                    if context_id is not None:
                        self.store.hard_delete_item(context_id)
        if changed:
            self._apply_vector_aftermath(
                _VectorAftermath(
                    legacy_removals=(request.legacy_id,),
                    context_removals=(
                        (context_id,) if context_id is not None else ()
                    ),
                )
            )
        return LegacyMutationResult(
            legacy_id=request.legacy_id,
            context_id=context_id,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=_ALL_LAYERS if context_id is not None else (),
            changed=changed,
        )

    def legacy_access(self, request: LegacyAccessRequest) -> LegacyAccessResult:
        """Mirror one batched access increment onto both mapped sides."""
        self._require_request(request, LegacyAccessRequest)
        self._require_initialized()
        if not request.legacy_ids:
            return LegacyAccessResult(updated_legacy_ids=(), updated_context_ids=())
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                backend = self._legacy_backend()
                existing = tuple(
                    sorted(
                        int(row["id"])
                        for row in backend.get_by_ids(list(request.legacy_ids))
                    )
                )
                with backend.transaction():
                    for legacy_id in existing:
                        backend.update_access(legacy_id)
            return LegacyAccessResult(
                updated_legacy_ids=existing, updated_context_ids=()
            )
        with self._cutover_lock.shared():
            with self.store.transaction():
                repository = self.store.legacy_projection()
                rows = repository.get_by_ids(list(request.legacy_ids))
                pairs: list[tuple[int, int]] = []
                for row in sorted(rows, key=lambda item: int(item["id"])):
                    legacy_id = int(row["id"])
                    context_id = self.store.resolve_legacy_mapping(legacy_id)
                    if context_id is None:
                        context_id = self._legacy_migrator().migrate_projection_row(row)
                    pairs.append((legacy_id, context_id))
                updated_legacy_ids = repository.update_access(
                    tuple(legacy_id for legacy_id, _ in pairs)
                )
                context_ids = tuple(context_id for _, context_id in pairs)
                self.store.update_access(list(context_ids))
        return LegacyAccessResult(
            updated_legacy_ids=updated_legacy_ids,
            updated_context_ids=context_ids,
        )

    # ---- extraction batch ----

    def persist_legacy_extraction(
        self, request: LegacyExtractionRequest
    ) -> LegacyExtractionResult:
        """Persist one extraction batch as all-or-nothing under the cutover lock.

        The summary equivalence check/repair and at most ``max_writes``
        actual candidate writes share one outer SQLite transaction; a failure
        on any write rolls the summary and every earlier candidate back on
        both sides. Both derived vector batches start only after the commit.
        """
        self._require_request(request, LegacyExtractionRequest)
        self._require_initialized()
        engine = self.embedding_engine
        engine_ready = engine is not None and getattr(engine, "is_loaded", False)
        if engine_ready:
            try:
                # The semantic-merge search needs an open legacy index; when it
                # cannot be opened the batch degrades to pure SQLite writes.
                self._ensure_vector_index_ready(self._legacy_vector_index())
            except Exception:
                engine_ready = False
        if self._mode is ContextMode.LEGACY:
            return self._persist_extraction_legacy(request, engine_ready=engine_ready)
        return self._persist_extraction_dual(request, engine_ready=engine_ready)

    def _persist_extraction_dual(
        self, request: LegacyExtractionRequest, *, engine_ready: bool
    ) -> LegacyExtractionResult:
        source_session = request.source_session

        def write_add(
            item: LegacyExtractionItem,
        ) -> tuple[LegacyMutationResult, _VectorAftermath]:
            return self._add_dual_in_transaction(
                LegacyAddRequest(
                    key=item.key,
                    value=item.value,
                    attribute=item.attribute,
                    tags=item.tags,
                    source_session=source_session,
                    importance=item.importance,
                    tier=item.tier,
                    expires_at=item.expires_at,
                    confidence=item.confidence,
                )
            )

        def write_replace(
            item: LegacyExtractionItem,
            *,
            key: str | None = None,
            tier: str | None = None,
            repair: bool = False,
        ) -> tuple[LegacyMutationResult, _VectorAftermath]:
            # repair (the summary path) rewrites metadata explicitly; candidate
            # replaces inherit attribute/tags from the superseded row instead.
            return self._replace_dual_in_transaction(
                LegacyReplaceRequest(
                    key=key if key is not None else item.key,
                    new_value=item.value,
                    attribute=item.attribute if repair else None,
                    tags=item.tags if repair else None,
                    source_session=source_session,
                    importance=item.importance,
                    tier=item.tier if tier is None else tier,
                    expires_at=item.expires_at,
                    confidence=item.confidence,
                )
            )

        with self._cutover_lock.shared():
            with self.store.transaction():
                summary_result, candidate_results, aftermath = (
                    self._extraction_batch_writes(
                        request,
                        self.store.legacy_projection(),
                        engine_ready=engine_ready,
                        conflict_as_replace=True,
                        write_add=write_add,
                        write_replace=write_replace,
                    )
                )
        self._apply_vector_aftermath(aftermath)
        return LegacyExtractionResult(
            summary=summary_result,
            candidates=candidate_results,
            persisted=(1 if summary_result is not None else 0)
            + len(candidate_results),
        )

    def _persist_extraction_legacy(
        self, request: LegacyExtractionRequest, *, engine_ready: bool
    ) -> LegacyExtractionResult:
        """Legacy emergency mode: the same policy over the old backend only."""
        source_session = request.source_session
        backend = self._legacy_backend()

        def write_add(
            item: LegacyExtractionItem,
        ) -> tuple[LegacyMutationResult, _VectorAftermath]:
            new_id = backend.add(
                key=item.key,
                value=item.value,
                attribute=item.attribute,
                tags=list(item.tags) or None,
                source_session=source_session,
                importance=item.importance,
                tier=item.tier,
                expires_at=item.expires_at,
            )
            return (
                LegacyMutationResult(
                    legacy_id=new_id,
                    context_id=None,
                    old_legacy_id=None,
                    old_context_id=None,
                    available_layers=(),
                    changed=True,
                ),
                _VectorAftermath(legacy_upserts=((new_id, item.value),)),
            )

        def write_replace(
            item: LegacyExtractionItem,
            *,
            key: str | None = None,
            tier: str | None = None,
            repair: bool = False,
        ) -> tuple[LegacyMutationResult, _VectorAftermath]:
            new_id = backend.replace(
                key=key if key is not None else item.key,
                new_value=item.value,
                attribute=item.attribute if repair else None,
                tags=list(item.tags) if repair else None,
                source_session=source_session,
                importance=item.importance,
                tier=item.tier if tier is None else tier,
                expires_at=item.expires_at,
            )
            return (
                LegacyMutationResult(
                    legacy_id=new_id,
                    context_id=None,
                    old_legacy_id=None,
                    old_context_id=None,
                    available_layers=(),
                    changed=True,
                ),
                _VectorAftermath(legacy_upserts=((new_id, item.value),)),
            )

        with self._cutover_lock.shared():
            with backend.transaction():
                summary_result, candidate_results, aftermath = (
                    self._extraction_batch_writes(
                        request,
                        backend,
                        engine_ready=engine_ready,
                        conflict_as_replace=False,
                        write_add=write_add,
                        write_replace=write_replace,
                    )
                )
        self._apply_vector_aftermath(aftermath)
        return LegacyExtractionResult(
            summary=summary_result,
            candidates=candidate_results,
            persisted=(1 if summary_result is not None else 0)
            + len(candidate_results),
        )

    def _extraction_batch_writes(
        self,
        request: LegacyExtractionRequest,
        reader,
        *,
        engine_ready: bool,
        conflict_as_replace: bool,
        write_add,
        write_replace,
    ) -> tuple[
        LegacyMutationResult | None, tuple[LegacyMutationResult, ...], _VectorAftermath
    ]:
        """Shared extraction policy; the caller owns the lock and transaction.

        ``reader`` is the mode-selected legacy read backend (projection
        repository or MemoryStore); the writers perform one add or replace
        each and return its result plus post-commit vector work.
        ``conflict_as_replace`` is set in dual modes, where the Core's one
        active item per identity makes a legacy-style dual-active add
        impossible; an undecidable same-key conflict converges to a replace.
        """
        detector = ConflictDetector(reader)
        engine = self.embedding_engine if engine_ready else None
        aftermaths: list[_VectorAftermath] = []
        summary_result = self._extraction_summary_write(
            request.summary, reader, write_add, write_replace, aftermaths
        )
        candidate_results: list[LegacyMutationResult] = []
        for item in request.candidates:
            if len(candidate_results) >= request.max_writes:
                break
            decision = detector.check(item.key, item.value)
            if decision.action == "skip":
                continue
            if decision.action == "replace":
                result, aftermath = write_replace(item)
            else:
                # 同 key 无冲突或 conflict → 再做跨 key 语义合并
                # （tier == "reference" 的候选不参与合并：永不 supersede 别人）
                match = None
                if engine is not None and item.tier != "reference":
                    match = find_semantic_match(
                        reader,
                        self._legacy_vector_index(),
                        engine,
                        item.value,
                        self.config.add_merge_threshold,
                    )
                if match:
                    # 合并目标是 pinned 记忆时保留 pinned tier，避免被候选的
                    # 默认 "normal" 静默降级、掉出每会话必注入层
                    merged_tier = (
                        "pinned" if match.get("tier") == "pinned" else item.tier
                    )
                    result, aftermath = write_replace(
                        item, key=match["key"], tier=merged_tier
                    )
                elif decision.action == "conflict" and conflict_as_replace:
                    result, aftermath = write_replace(item)
                else:
                    result, aftermath = write_add(item)
            candidate_results.append(result)
            aftermaths.append(aftermath)
        return (
            summary_result,
            tuple(candidate_results),
            self._merge_aftermaths(tuple(aftermaths)),
        )

    def _extraction_summary_write(
        self,
        summary: LegacyExtractionItem,
        reader,
        write_add,
        write_replace,
        aftermaths: list,
    ) -> LegacyMutationResult | None:
        """Write the summary, or accept an already equivalent active one."""
        active = next(
            (
                record
                for record in reader.get_by_key(summary.key)
                if record["status"] == "active"
            ),
            None,
        )
        if active is not None:
            metadata_equivalent = (
                active["attribute"] == summary.attribute
                and active["tags"] == ",".join(summary.tags)
                and active["importance"] == summary.importance
                and active["tier"] == summary.tier
            )
            if (
                active["value"].strip() == summary.value.strip()
                and metadata_equivalent
            ):
                return None
        if active is None:
            result, aftermath = write_add(summary)
        else:
            result, aftermath = write_replace(summary, repair=True)
        aftermaths.append(aftermath)
        return result

    @staticmethod
    def _merge_aftermaths(aftermaths: tuple) -> "_VectorAftermath":
        """Concatenate per-write vector work, preserving the write order."""
        return _VectorAftermath(
            legacy_upserts=tuple(
                entry for aftermath in aftermaths for entry in aftermath.legacy_upserts
            ),
            legacy_removals=tuple(
                entry
                for aftermath in aftermaths
                for entry in aftermath.legacy_removals
            ),
            context_upserts=tuple(
                entry
                for aftermath in aftermaths
                for entry in aftermath.context_upserts
            ),
            context_removals=tuple(
                entry
                for aftermath in aftermaths
                for entry in aftermath.context_removals
            ),
        )

    def _legacy_set_status(
        self,
        request: LegacyStatusRequest,
        *,
        legacy_status: str,
        context_status: ContextStatus,
        reactivate: bool,
    ) -> LegacyMutationResult:
        self._require_request(request, LegacyStatusRequest)
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            with self._cutover_lock.shared():
                backend = self._legacy_backend()
                row = backend.get_by_id(request.legacy_id)
                if row is not None:
                    with backend.transaction():
                        backend._projection().set_status(
                            request.legacy_id, legacy_status
                        )
            if row is not None:
                self._apply_vector_aftermath(
                    _VectorAftermath(
                        legacy_upserts=((request.legacy_id, row["value"]),),
                    )
                    if reactivate
                    else _VectorAftermath(legacy_removals=(request.legacy_id,))
                )
            return LegacyMutationResult(
                legacy_id=request.legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=row is not None,
            )
        context_id: int | None = None
        row_value = ""
        l0 = ""
        with self._cutover_lock.shared():
            with self.store.transaction():
                repository = self.store.legacy_projection()
                row = repository.get_by_id(request.legacy_id)
                changed = row is not None
                if changed:
                    row_value = row["value"]
                    context_id = self.store.resolve_legacy_mapping(request.legacy_id)
                    if context_id is None:
                        context_id = self._legacy_migrator().migrate_projection_row(row)
                    repository.set_status(request.legacy_id, legacy_status)
                    if not self.store.set_item_status(context_id, context_status):
                        raise ContextServiceError(
                            "degraded_legacy",
                            "legacy mapping points at a missing ContextItem; "
                            "the whole mutation rolled back",
                        )
                    l0 = self.store.get_layer(context_id, ContextLayer.L0) or ""
        if changed:
            self._apply_vector_aftermath(
                _VectorAftermath(
                    legacy_upserts=((request.legacy_id, row_value),),
                    context_upserts=((context_id, l0),),
                )
                if reactivate
                else _VectorAftermath(
                    legacy_removals=(request.legacy_id,),
                    context_removals=(context_id,),
                )
            )
        return LegacyMutationResult(
            legacy_id=request.legacy_id,
            context_id=context_id,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=_ALL_LAYERS if changed else (),
            changed=changed,
        )

    # ---- mutation internals ----

    def _require_initialized(self) -> None:
        if self._mode is None:
            raise ContextServiceError(
                "not_initialized", "service is not initialized"
            )

    @staticmethod
    def _require_request(request: object, request_type: type) -> None:
        if not isinstance(request, request_type):
            raise ContextValidationError(
                f"request must be a {request_type.__name__} instance"
            )

    def _legacy_backend(self) -> MemoryStore:
        """Service-owned legacy backend for the explicit legacy mode."""
        if self._legacy_store is None:
            store = MemoryStore(self.config)
            store.initialize()
            self._legacy_store = store
        return self._legacy_store

    def _legacy_reader(self):
        """Narrow legacy read backend selected by the current mode."""
        self._require_initialized()
        if self._mode is ContextMode.LEGACY:
            return self._legacy_backend()
        return self.store.legacy_projection()

    def _legacy_migrator(self) -> LegacyMemoryMigrator:
        if self._migrator is None:
            self._migrator = LegacyMemoryMigrator(self.store, self.config)
        return self._migrator

    def _context_lifecycle(self) -> ContextLifecycle:
        """Service-owned lifecycle coordinator; construction fails closed."""
        if self._lifecycle is None:
            try:
                self._lifecycle = ContextLifecycle(self.config, self.store)
            except Exception as exc:
                raise ContextServiceError(
                    "degraded_legacy",
                    "context lifecycle could not be constructed; failing closed",
                ) from exc
        return self._lifecycle

    def _session_archiver(self) -> SessionArchiver:
        """Service-owned session archiver; construction fails closed."""
        if self._archiver is None:
            try:
                self._archiver = SessionArchiver(self.config, self.store)
            except Exception as exc:
                raise ContextServiceError(
                    "degraded_legacy",
                    "session archiver could not be constructed; failing closed",
                ) from exc
        return self._archiver

    def _playbook_generator(self) -> PlaybookGenerator:
        """Service-owned playbook generator; construction fails closed.

        The service wires no LLM of its own: generation degrades to the
        explicit ``llm_unavailable`` reason instead of erroring.
        """
        if self._generator is None:
            try:
                self._generator = PlaybookGenerator(
                    self.config,
                    self.store,
                    self._context_lifecycle(),
                    llm=None,
                    embedding_engine=self.embedding_engine,
                )
            except Exception as exc:
                raise ContextServiceError(
                    "degraded_legacy",
                    "playbook generator could not be constructed; failing closed",
                ) from exc
        return self._generator

    def _context_synchronizer(self) -> ContextVectorSynchronizer:
        if self._synchronizer is None:
            self._synchronizer = ContextVectorSynchronizer(
                self.config, self.store, self.vector_index, self.embedding_engine
            )
        return self._synchronizer

    def _apply_vector_aftermath(self, aftermath: _VectorAftermath) -> None:
        """Best-effort post-commit dual-index sync with independent dirty markers.

        Runs strictly after the SQLite commit: vector failures never roll
        back persisted rows, and each index keeps its own retry marker — a
        successful sibling never clears or misreports the other side.
        """
        self._sync_legacy_vector_aftermath(
            aftermath.legacy_upserts, aftermath.legacy_removals
        )
        self._sync_context_vector_aftermath(
            aftermath.context_upserts, aftermath.context_removals
        )

    def _sync_legacy_vector_aftermath(
        self,
        upserts: tuple[tuple[int, str], ...],
        removals: tuple[int, ...],
    ) -> None:
        if not upserts and not removals:
            return
        index = self._legacy_vector_index()
        try:
            was_dirty = bool(index.is_dirty())
        except Exception:
            was_dirty = True
        try:
            index.mark_dirty()
            index.preserve_dirty()
        except Exception:
            return  # even the retry marker is unwritable; nothing safe remains
        try:
            self._ensure_vector_index_ready(index)
            for legacy_id in removals:
                index.remove(legacy_id)
            engine = self.embedding_engine
            if upserts and (engine is None or not getattr(engine, "is_loaded", False)):
                index.preserve_dirty()
                index.save()  # persist the removals; the preserved marker stays
                return
            for legacy_id, text in upserts:
                embedding = np.asarray(
                    engine.encode_document(text), dtype=np.float32
                )
                if embedding.ndim != 1 or embedding.shape[0] != self.config.embedding_dim:
                    raise ValueError("embedding dimension mismatch")
                index.remove(legacy_id)
                index.add(legacy_id, embedding)
            index.save()
            if not was_dirty:
                index.clear_dirty()
        except Exception:
            try:
                index.preserve_dirty()
            except Exception:
                pass

    def _sync_context_vector_aftermath(
        self,
        upserts: tuple[tuple[int, str], ...],
        removals: tuple[int, ...],
    ) -> None:
        if not upserts and not removals:
            return
        try:
            synchronizer = self._context_synchronizer()
            for context_id in removals:
                synchronizer.remove_l0(context_id)
            for context_id, l0 in upserts:
                synchronizer.upsert_active_l0(context_id, l0)
        except Exception:
            # 同步器自身爆炸（区别于单条同步失败——那些同步器内部已捕获并
            # 保留 dirty）：SQLite 早已提交，绝不能向调用方抛错（调用方会
            # retry 造成重复写）；与 legacy 侧对称，如实留下 durable 重试标记
            try:
                self.vector_index.mark_dirty()
                self.vector_index.preserve_dirty()
            except Exception:
                pass  # even the retry marker is unwritable; nothing safe remains

    def _ensure_vector_index_ready(self, index: VectorIndex) -> None:
        """Open the existing cache or start an empty one for per-item updates."""
        try:
            index.count()
        except Exception:
            index.initialize(dim=self.config.embedding_dim)
