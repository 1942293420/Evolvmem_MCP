"""Rebuild the disposable Context Core L0 vector cache from SQLite truth."""

from dataclasses import dataclass

import numpy as np

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.embedding import EmbeddingEngine
from evolvmem.vector_index import VectorIndex


@dataclass(frozen=True, slots=True)
class ContextVectorSyncReport:
    status: str
    document_count: int
    detail: str = ""


class ContextVectorSynchronizer:
    """Synchronize active ContextItem L0 values into the separate vector cache."""

    def __init__(
        self,
        config: Config,
        store: ContextStore,
        vector_index: VectorIndex,
        embedding_engine: EmbeddingEngine | None,
    ):
        self.config = config
        self.store = store
        self.vector_index = vector_index
        self.embedding_engine = embedding_engine

    def rebuild_active_l0(self) -> ContextVectorSyncReport:
        """Rebuild from active L0 documents, keeping SQLite untouched on failure."""
        try:
            self._mark_context_dirty()
        except Exception as exc:
            return ContextVectorSyncReport("failed", 0, exc.__class__.__name__)

        if self.vector_index.path != self.config.context_vector_path.resolve():
            return ContextVectorSyncReport("failed", 0, "ValueError")

        if self.embedding_engine is None or not self.embedding_engine.is_loaded:
            return ContextVectorSyncReport("unavailable", 0, "embedding engine unavailable")

        document_count = 0
        try:
            documents = self.store.list_vector_documents()
            document_count = len(documents)
            self.vector_index.initialize(dim=self.config.embedding_dim)
            embeddings = [
                np.asarray(
                    self.embedding_engine.encode_document(document.l0), dtype=np.float32
                )
                for document in documents
            ]
            for embedding in embeddings:
                if embedding.ndim != 1 or embedding.shape[0] != self.config.embedding_dim:
                    raise ValueError("embedding dimension mismatch")
            self.vector_index.rebuild(
                [document.item_id for document in documents], embeddings
            )
        except Exception as exc:
            self.vector_index.preserve_dirty()
            return ContextVectorSyncReport(
                "failed", document_count, exc.__class__.__name__
            )
        return ContextVectorSyncReport("synchronized", document_count)

    def upsert_active_l0(self, item_id: int, l0: str) -> ContextVectorSyncReport:
        """Insert or replace one active item's L0 vector after a SQLite commit.

        Per-item synchronization never rebuilds the index; a pre-existing
        dirty marker survives even a successful update, because earlier
        failures may still be unsynchronized.
        """
        try:
            was_dirty = bool(self.vector_index.is_dirty())
        except Exception:
            was_dirty = True
        try:
            self._mark_context_dirty()
        except Exception as exc:
            return ContextVectorSyncReport("failed", 0, exc.__class__.__name__)
        if self.vector_index.path != self.config.context_vector_path.resolve():
            return ContextVectorSyncReport("failed", 0, "ValueError")
        try:
            self._ensure_index_ready()
            self.vector_index.remove(item_id)
        except Exception as exc:
            self.vector_index.preserve_dirty()
            return ContextVectorSyncReport("failed", 0, exc.__class__.__name__)
        if self.embedding_engine is None or not self.embedding_engine.is_loaded:
            self.vector_index.preserve_dirty()
            try:
                self.vector_index.save()
            except Exception:
                pass  # the preserved marker already records the stale state
            return ContextVectorSyncReport(
                "unavailable", 0, "embedding engine unavailable"
            )
        try:
            embedding = np.asarray(
                self.embedding_engine.encode_document(l0), dtype=np.float32
            )
            if embedding.ndim != 1 or embedding.shape[0] != self.config.embedding_dim:
                raise ValueError("embedding dimension mismatch")
            self.vector_index.add(item_id, embedding)
            self.vector_index.save()
        except Exception as exc:
            self.vector_index.preserve_dirty()
            return ContextVectorSyncReport("failed", 1, exc.__class__.__name__)
        if not was_dirty:
            self.vector_index.clear_dirty()
        return ContextVectorSyncReport("synchronized", 1)

    def remove_l0(self, item_id: int) -> ContextVectorSyncReport:
        """Remove one item's L0 vector after a SQLite commit; engine-independent."""
        try:
            was_dirty = bool(self.vector_index.is_dirty())
        except Exception:
            was_dirty = True
        try:
            self._mark_context_dirty()
        except Exception as exc:
            return ContextVectorSyncReport("failed", 0, exc.__class__.__name__)
        if self.vector_index.path != self.config.context_vector_path.resolve():
            return ContextVectorSyncReport("failed", 0, "ValueError")
        try:
            self._ensure_index_ready()
            self.vector_index.remove(item_id)
            self.vector_index.save()
        except Exception as exc:
            self.vector_index.preserve_dirty()
            return ContextVectorSyncReport("failed", 0, exc.__class__.__name__)
        if not was_dirty:
            self.vector_index.clear_dirty()
        return ContextVectorSyncReport("synchronized", 0)

    def _ensure_index_ready(self) -> None:
        """Open the existing cache or start an empty one for per-item updates."""
        try:
            self.vector_index.count()
        except Exception:
            self.vector_index.initialize(dim=self.config.embedding_dim)

    def _mark_context_dirty(self) -> None:
        """Leave a durable retry marker without ever marking the legacy cache."""
        marker_index = self.vector_index
        if marker_index.path != self.config.context_vector_path.resolve():
            marker_index = VectorIndex(self.config, path=self.config.context_vector_path)
        marker_index.mark_dirty()
        marker_index.preserve_dirty()
