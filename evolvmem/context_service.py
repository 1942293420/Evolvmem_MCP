"""Typed Context Core read boundary.

ContextService is the single coordination point between adapters and the
Context Core read path: it owns the service lifecycle, gates operations by
context mode, normalizes workspace identifiers, orchestrates
Retriever/Renderer/Store for session start and search, performs exact
L1/L2 reads, and reports a content-free status snapshot.

Slice-1 scope: ``initialize()`` opens the existing Context schema only. It
never runs ``LegacyMemoryMigrator``, never rebuilds a vector index, never
edits config, and never routes a production writer. Context operations in
legacy/compat mode fail closed with ``context_not_enabled``; shadow mode
serves explicit reads; primary mode first re-validates the currently
testable schema/layer/vector invariants and reports ``degraded_legacy``
when any of them fail (the full preflight evaluator arrives with the
cutover gates).

Privacy contract: workspace paths are normalized to basename/alias before
use, and the service neither stores nor logs absolute paths, queries, or
content — it owns no logger at all.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import PurePosixPath

from evolvmem.config import Config
from evolvmem.context_models import (
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
from evolvmem.context_renderer import ContextRenderCandidate, ContextRenderer
from evolvmem.context_retriever import ContextRetriever
from evolvmem.context_store import ContextStore
from evolvmem.embedding import EmbeddingEngine
from evolvmem.vector_index import VectorIndex


_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Diagnostics stay bounded and content-free: a short prefix of stable codes
# and config validation messages, each truncated to a fixed length.
_MAX_DIAGNOSTICS = 8
_MAX_DIAGNOSTIC_CHARS = 160


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
                self.embedding_engine,
            ):
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
        finally:
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
            legacy_rows = self.store.iter_legacy_rows()
            mapping_count = sum(
                1 for row in legacy_rows if row["context_item_id"] is not None
            )
            # Currently testable lag class: legacy rows without a mapping.
            # The full mismatch evaluator arrives with the cutover gates.
            projection_lag = sum(
                1 for row in legacy_rows if row["context_item_id"] is None
            )
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
        """Revalidate the currently testable primary invariants, content-free."""
        diagnostics: list[str] = list(self.config.validate_runtime())
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
        diagnostics.extend(self._vector_diagnostics(documents))
        return tuple(
            message[:_MAX_DIAGNOSTIC_CHARS]
            for message in diagnostics[:_MAX_DIAGNOSTICS]
        )

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
            self.store.update_access([result.id for result in results])
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
            self.store.update_access(list(rendered.selected_ids))
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
