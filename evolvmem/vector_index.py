"""USearch HNSW vector index — stores only (id, embedding), acts as a read cache for SQLite."""

from dataclasses import dataclass

import numpy as np
from pathlib import Path
from usearch.index import Index, MetricKind, ScalarKind

from evolvmem.config import Config

IDS_INSPECTION_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class VectorIndexMetadata:
    """Read-only diagnostics for an initialized index; never vector payloads."""

    count: int
    dimension: int
    dirty: bool


class VectorIndex:
    """USearch HNSW vector index wrapper.

    Stores only (id, embedding) pairs. id maps to the rowid in MemoryStore's memories table.
    Index file is loaded via mmap for zero-copy startup.
    """

    def __init__(self, config: Config, *, path: Path | None = None):
        self.config = config
        self.path = (path or config.vector_path).resolve()
        self._index: Index | None = None
        self._dim: int | None = None
        self._view_mode: bool = False
        self._owns_dirty_marker: bool = False
        self._shared_cache = None

    @property
    def _dirty_path(self) -> Path:
        return self.path.with_suffix(f"{self.path.suffix}.dirty")

    # ---- lifecycle ----

    def initialize(self, dim: int = 512) -> None:
        """Create or load index. Uses mmap if file exists, creates new otherwise."""
        self._dim = dim
        self._owns_dirty_marker = False
        path = str(self.path)
        if Path(path).exists():
            self._index = Index.restore(path, view=False)
            self._view_mode = False
        else:
            self._index = Index(
                ndim=dim,
                metric=MetricKind.Cos,
                dtype=ScalarKind.F32,
            )
            self._view_mode = False

        if self.config.lan_shared_vector_cache or self.config.embedding_http_url or self.config.lan_mcp_client_config:
            from evolvmem.lan_vector_cache import SharedVectorCache
            self._shared_cache = SharedVectorCache(self)

    def close(self) -> None:
        if self._index is not None:
            self._index = None
        self._view_mode = False
        self._shared_cache = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()

    # ---- write ----

    def add(self, mem_id: int, embedding: np.ndarray) -> None:
        """Add a single vector."""
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        self._add_without_refresh(mem_id, embedding)

    def _add_without_refresh(self, mem_id: int, embedding: np.ndarray) -> None:
        """Stage a vector without replacing the image being built."""
        vec = embedding.astype(np.float32)
        if vec.ndim != 1 or len(vec) != self._dim:
            raise ValueError(
                f"Expected {self._dim}-dim vector, got shape={vec.shape}"
            )
        self.mark_dirty()
        try:
            self._index.add(mem_id, vec)
            if self._shared_cache is not None:
                self._shared_cache.pending[mem_id] = vec.copy()
        except Exception:
            self.preserve_dirty()
            raise

    def add_batch(self, ids: list[int],
                  embeddings: list[np.ndarray]) -> None:
        """Batch add vectors."""
        for mid, emb in zip(ids, embeddings):
            self.add(mid, emb)

    def remove(self, mem_id: int) -> bool:
        """Remove a single vector. Returns True if removed, False if absent."""
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        try:
            if mem_id not in self._index:
                return False
            self.mark_dirty()
            self._index.remove(mem_id)
            if self._shared_cache is not None:
                self._shared_cache.pending[mem_id] = None
            return True
        except Exception:
            self.preserve_dirty()
            return False

    def rebuild(self, ids: list[int],
                embeddings: list[np.ndarray]) -> None:
        """Full index rebuild (SQLite as source, for crash recovery)."""
        if self._dim is None:
            raise RuntimeError("VectorIndex not initialized, call initialize() first")
        if self._shared_cache is not None:
            # Delete only IDs from this loaded snapshot. A later disk image can
            # include another writer's new rows, absent from our rebuild input.
            self._shared_cache.pending.update({int(key): None for key in self._index.keys})
        self.mark_dirty()
        # Explicitly release old mmap index to avoid resource leak
        if self._index is not None:
            self._index = None
        self._view_mode = False
        # Create a fresh in-memory index (no path to avoid loading old data with duplicate keys)
        self._index = Index(
            ndim=self._dim,
            metric=MetricKind.Cos,
            dtype=ScalarKind.F32,
        )
        # Build the replacement image before merging with the latest disk
        # cache. add_batch would refresh between inserts and reintroduce IDs
        # that may also be in this rebuild input.
        for mem_id, embedding in zip(ids, embeddings):
            self._add_without_refresh(mem_id, embedding)
        self.save()
        self.clear_dirty()

    def save(self) -> None:
        """Persist to disk."""
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        if self._view_mode:
            raise RuntimeError(
                "cannot save a view-mode index; use rebuild() instead"
            )
        path = str(self.path)
        if self._shared_cache is not None:
            self._shared_cache.save()
        else:
            self._index.save(path)
        if self._owns_dirty_marker:
            self.clear_dirty()

    def mark_dirty(self) -> None:
        """Persist that SQLite/vector synchronization is not yet durable."""
        self._dirty_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._dirty_path.exists():
            self._dirty_path.touch()
            self._owns_dirty_marker = True

    def preserve_dirty(self) -> None:
        """Prevent a later unrelated save from clearing a failure marker."""
        self._owns_dirty_marker = False

    def clear_dirty(self) -> None:
        """Clear the durable marker only after a successful index save."""
        self._dirty_path.unlink(missing_ok=True)
        self._owns_dirty_marker = False

    def is_dirty(self) -> bool:
        """Return whether a previous synchronization may be incomplete."""
        return self._dirty_path.exists()

    # ---- query ----

    def search(self, embedding: np.ndarray, k: int = 20) -> list[dict]:
        """HNSW approximate nearest neighbor search. Returns [{id, distance}, ...] by distance ascending."""
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        if self.count() == 0:
            return []
        vec = embedding.astype(np.float32)
        if vec.ndim != 1 or len(vec) != self._dim:
            raise ValueError(
                f"Expected {self._dim}-dim vector, got shape={vec.shape}"
            )
        results = self._index.search(vec, min(k, self.count()))
        return [
            {"id": int(match.key), "distance": float(match.distance)}
            for match in results
        ]

    # ---- status ----

    def count(self) -> int:
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        return len(self._index)

    def check_consistency(self, expected_count: int) -> bool:
        """Check count and the durable incomplete-synchronization marker."""
        return not self.is_dirty() and self.count() == expected_count

    # ---- inspection (read-only; valid only after initialization) ----

    def ids(self, *, limit: int = IDS_INSPECTION_LIMIT) -> list[int]:
        """Sorted integer IDs for exact-set verification.

        Read-only and bounded: the inspection never exposes vectors, and an
        index larger than ``limit`` fails loudly instead of being silently
        truncated.
        """
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        if limit < 0:
            raise ValueError("ids inspection limit must be non-negative")
        keys = sorted(int(key) for key in self._index.keys)
        if len(keys) > limit:
            raise ValueError(
                f"index holds {len(keys)} ids, above the inspection limit of {limit}"
            )
        return keys

    def inspect_metadata(self) -> VectorIndexMetadata:
        """Count/dimension/dirty diagnostics for a live, initialized index.

        The dimension comes from the underlying index itself, so a restored
        file whose dimension differs from the initialize() argument is
        diagnosable.
        """
        self._ensure_initialized()
        if self._shared_cache is not None:
            self._shared_cache.refresh()
        return VectorIndexMetadata(
            count=len(self._index),
            dimension=int(self._index.ndim),
            dirty=self.is_dirty(),
        )

    def _ensure_initialized(self):
        if self._index is None:
            raise RuntimeError("VectorIndex not initialized, call initialize() first")
