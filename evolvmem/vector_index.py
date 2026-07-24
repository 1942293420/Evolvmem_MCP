"""USearch HNSW vector index — stores only (id, embedding), acts as a read cache for SQLite."""

import numpy as np
from pathlib import Path
from usearch.index import Index, MetricKind, ScalarKind

from evolvmem.config import Config


class VectorIndex:
    """USearch HNSW vector index wrapper.

    Stores only (id, embedding) pairs. id maps to the rowid in MemoryStore's memories table.
    Index file is loaded via mmap for zero-copy startup.
    """

    def __init__(self, config: Config):
        self.config = config
        self._index: Index | None = None
        self._dim: int | None = None
        self._view_mode: bool = False

    # ---- lifecycle ----

    def initialize(self, dim: int = 512) -> None:
        """Create or load index. Uses mmap if file exists, creates new otherwise."""
        self._dim = dim
        path = str(self.config.vector_path)
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

    def close(self) -> None:
        if self._index is not None:
            self._index = None
        self._view_mode = False

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()

    # ---- write ----

    def add(self, mem_id: int, embedding: np.ndarray) -> None:
        """Add a single vector."""
        self._ensure_initialized()
        vec = embedding.astype(np.float32)
        if vec.ndim != 1 or len(vec) != self._dim:
            raise ValueError(
                f"Expected {self._dim}-dim vector, got shape={vec.shape}"
            )
        self._index.add(mem_id, vec)

    def add_batch(self, ids: list[int],
                  embeddings: list[np.ndarray]) -> None:
        """Batch add vectors."""
        for mid, emb in zip(ids, embeddings):
            self.add(mid, emb)

    def rebuild(self, ids: list[int],
                embeddings: list[np.ndarray]) -> None:
        """Full index rebuild (SQLite as source, for crash recovery)."""
        if self._dim is None:
            raise RuntimeError("VectorIndex not initialized, call initialize() first")
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
        self.add_batch(ids, embeddings)
        self.save()

    def save(self) -> None:
        """Persist to disk."""
        self._ensure_initialized()
        if self._view_mode:
            raise RuntimeError(
                "cannot save a view-mode index; use rebuild() instead"
            )
        path = str(self.config.vector_path)
        self._index.save(path)

    # ---- query ----

    def search(self, embedding: np.ndarray, k: int = 20) -> list[dict]:
        """HNSW approximate nearest neighbor search. Returns [{id, distance}, ...] by distance ascending."""
        self._ensure_initialized()
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
        return len(self._index)

    def check_consistency(self, expected_count: int) -> bool:
        """Check if USearch index entry count matches SQLite.

        Note: only compares counts, does not verify ID match.
        If counts match but IDs differ (e.g., after crash recovery key drift),
        semantic search will return wrong results.
        The caller should log a warning after the count check passes, advising
        users to delete vectors.usearch and force a rebuild if results are wrong.
        """
        return self.count() == expected_count

    def _ensure_initialized(self):
        if self._index is None:
            raise RuntimeError("VectorIndex not initialized, call initialize() first")
