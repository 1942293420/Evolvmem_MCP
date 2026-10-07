"""Atomically stage the Context Core L0 vector index during cutover.

The staged index is built into a unique same-directory temporary file from
``store.list_vector_documents()`` alone, verified by exact ID set, count,
and dimension, then atomically swapped over the formal cache. Any failure
keeps the previous formal bytes untouched and preserves the durable
Context dirty marker, because the migrated SQLite truth is newer than any
vector cache on disk. An explicit FTS-only approval records a degraded
reason; it never reports vector health and never clears the marker. The
real CLI may pass ``allow_fts_only`` only behind a separate human-approved
flag.
"""

import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ClassVar

import numpy as np

from evolvmem.config import Config
from evolvmem.context_models import ContextValidationError
from evolvmem.context_store import ContextStore
from evolvmem.cutover_models import validate_public_summary
from evolvmem.embedding import EmbeddingEngine
from evolvmem.vector_index import VectorIndex

_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_MAX_DETAIL_CHARS = 128
_STAGE_STATUSES = ("staged", "fts_only", "failed")


class _SourceChangedDuringRebuild(Exception):
    """Internal signal: the guard refused the staged snapshot. Never escapes."""


@dataclass(frozen=True, slots=True)
class ContextVectorStageReport:
    """Privacy-safe outcome of one atomic Context vector staging attempt."""

    SCHEMA: ClassVar[str] = "evolvmem.context_vector_stage"
    VERSION: ClassVar[int] = 1

    status: str
    document_count: int
    vector_ready: bool
    fts_only: bool
    dirty_cleared: bool
    reason_codes: tuple[str, ...]
    detail: str = ""
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in _STAGE_STATUSES:
            raise ContextValidationError("status must be staged, fts_only, or failed")
        for name in ("vector_ready", "fts_only", "dirty_cleared"):
            if type(getattr(self, name)) is not bool:
                raise ContextValidationError(f"{name} must be a boolean")
        if type(self.document_count) is not int or self.document_count < 0:
            raise ContextValidationError("document_count must be a non-negative integer")
        if (
            type(self.duration_ms) not in (int, float)
            or not math.isfinite(self.duration_ms)
            or self.duration_ms < 0
        ):
            raise ContextValidationError(
                "duration_ms must be a non-negative finite number"
            )
        object.__setattr__(self, "duration_ms", float(self.duration_ms))
        object.__setattr__(self, "reason_codes", self._require_reason_codes())
        self._require_safe_detail()
        if self.status == "staged" and (
            not self.vector_ready or not self.dirty_cleared or self.fts_only
        ):
            raise ContextValidationError(
                "a staged report must clear the marker and cannot be fts_only"
            )
        if self.status == "fts_only" and (
            not self.fts_only
            or self.vector_ready
            or self.dirty_cleared
            or not self.reason_codes
        ):
            raise ContextValidationError(
                "fts_only must record a reason and never claim vector health"
            )
        if self.status == "failed" and (self.vector_ready or self.dirty_cleared):
            raise ContextValidationError("a failed stage cannot claim vector health")
        if self.fts_only and self.status != "fts_only":
            raise ContextValidationError("fts_only requires the fts_only status")

    def _require_reason_codes(self) -> tuple[str, ...]:
        try:
            codes = tuple(self.reason_codes)
        except TypeError as exc:
            raise ContextValidationError(
                "reason_codes must be an iterable of reason codes"
            ) from exc
        for code in codes:
            if not isinstance(code, str) or not _REASON_CODE_PATTERN.match(code):
                raise ContextValidationError(
                    "reason_codes must contain only lower-snake reason codes"
                )
        return codes

    def _require_safe_detail(self) -> None:
        if not isinstance(self.detail, str):
            raise ContextValidationError("detail must be a diagnostic string")
        if "\n" in self.detail or "\r" in self.detail:
            raise ContextValidationError("detail must be a single-line string")
        if "/" in self.detail or "\\" in self.detail:
            raise ContextValidationError("detail must not contain a path separator")
        if len(self.detail) > _MAX_DETAIL_CHARS:
            raise ContextValidationError("detail exceeds the diagnostic budget")

    def public_dict(self) -> dict:
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "document_count": self.document_count,
            "vector_ready": self.vector_ready,
            "fts_only": self.fts_only,
            "dirty_cleared": self.dirty_cleared,
            "reason_codes": list(self.reason_codes),
            "detail": self.detail,
        }
        validate_public_summary(public)
        return public

    def digest(self) -> str:
        """SHA-256 over the canonical JSON of ``public_dict()``."""
        payload = json.dumps(
            self.public_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rebuild_context_vector_atomically(
    config: Config,
    store: ContextStore,
    embedding_engine: EmbeddingEngine | None,
    *,
    allow_fts_only: bool = False,
    commit_guard: Callable[[], bool] | None = None,
) -> ContextVectorStageReport:
    """Rebuild the Context L0 vector cache into a temp file and swap it in.

    The staged image is built and verified outside any lock. The guard, the
    swap, and the marker commit then happen inside one short SQLite write
    transaction on the store's own connection: the guard re-reads the truth
    first, so a write that landed during encoding keeps the old formal bytes
    and the durable dirty marker, and a new write can never slip between the
    guard and the clear. No lock is held while models encode.

    ``allow_fts_only`` records an explicit degraded reason instead of stopping
    the cutover. ``commit_guard`` is the caller's re-read of SQLite truth; it
    requires the write transaction, because a guard outside one cannot be
    trusted to represent committed state.
    """
    started = time.monotonic()
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if type(allow_fts_only) is not bool:
        raise ContextValidationError("allow_fts_only must be a boolean")
    if commit_guard is not None and not callable(commit_guard):
        raise ContextValidationError("commit_guard must be callable")

    target = config.context_vector_path.resolve()
    formal_marker = VectorIndex(config, path=config.context_vector_path)

    if embedding_engine is None or not embedding_engine.is_loaded:
        _preserve_formal_dirty(formal_marker)
        if allow_fts_only:
            return ContextVectorStageReport(
                status="fts_only",
                document_count=0,
                vector_ready=False,
                fts_only=True,
                dirty_cleared=False,
                reason_codes=("engine_unavailable", "approved_fts_only"),
                detail="embedding engine unavailable",
                duration_ms=_elapsed_ms(started),
            )
        return ContextVectorStageReport(
            status="failed",
            document_count=0,
            vector_ready=False,
            fts_only=False,
            dirty_cleared=False,
            reason_codes=("engine_unavailable",),
            detail="embedding engine unavailable",
            duration_ms=_elapsed_ms(started),
        )

    document_count = 0
    staged_ids: list[int] = []
    swapped = False
    temp_index: VectorIndex | None = None
    temp_path = _unique_temp_path(target)
    try:
        # ---- unlocked: read truth, encode, build and verify the staged image ----
        documents = store.list_vector_documents()
        document_count = len(documents)
        staged_ids = [int(document.item_id) for document in documents]
        embeddings = [
            np.asarray(embedding_engine.encode_document(document.l0), dtype=np.float32)
            for document in documents
        ]
        for embedding in embeddings:
            if embedding.ndim != 1 or embedding.shape[0] != config.embedding_dim:
                raise ValueError("embedding dimension mismatch")
        temp_index = VectorIndex(config, path=temp_path)
        temp_index.initialize(dim=config.embedding_dim)
        temp_index.rebuild(staged_ids, embeddings)
        temp_index.close()
        temp_index = None
        _fsync_file(temp_path)
        _verify_index(config, temp_path, staged_ids)

        # ---- locked: guard, swap, marker commit ----
        # The staged bytes are ours alone and already verified, so the write
        # lock only covers the guard re-read, the atomic swap, and the marker.
        with store.transaction():
            if commit_guard is not None and not commit_guard():
                raise _SourceChangedDuringRebuild()
            os.replace(temp_path, target)
            swapped = True
            _fsync_dir(target.parent)
            _verify_index(config, target, staged_ids)
            formal_marker.clear_dirty()
    except _SourceChangedDuringRebuild:
        _remove_temp_artifacts(temp_path)
        _preserve_formal_dirty(formal_marker)
        return ContextVectorStageReport(
            status="failed",
            document_count=document_count,
            vector_ready=False,
            fts_only=False,
            dirty_cleared=False,
            reason_codes=("source_changed_during_rebuild",),
            detail="ContextSourceChanged",
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        if temp_index is not None:
            try:
                temp_index.close()
            except Exception:
                pass  # the original failure is the one that matters
        if not swapped:
            _remove_temp_artifacts(temp_path)
        _preserve_formal_dirty(formal_marker)
        if allow_fts_only:
            return ContextVectorStageReport(
                status="fts_only",
                document_count=document_count,
                vector_ready=False,
                fts_only=True,
                dirty_cleared=False,
                reason_codes=("stage_failed", "approved_fts_only"),
                detail=exc.__class__.__name__,
                duration_ms=_elapsed_ms(started),
            )
        return ContextVectorStageReport(
            status="failed",
            document_count=document_count,
            vector_ready=False,
            fts_only=False,
            dirty_cleared=False,
            reason_codes=("stage_failed",),
            detail=exc.__class__.__name__,
            duration_ms=_elapsed_ms(started),
        )

    return ContextVectorStageReport(
        status="staged",
        document_count=document_count,
        vector_ready=True,
        fts_only=False,
        dirty_cleared=True,
        reason_codes=(),
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _preserve_formal_dirty(marker: VectorIndex) -> None:
    """Guarantee the durable retry signal survives any staging failure."""
    marker.mark_dirty()
    marker.preserve_dirty()


def _unique_temp_path(target: Path) -> Path:
    """A collision-free sibling so ``os.replace`` stays on one filesystem."""
    for _ in range(100):
        candidate = target.parent / f"{target.name}.stage-{uuid.uuid4().hex}.usearch"
        if not candidate.exists():
            return candidate
    raise ContextValidationError("could not allocate a unique staging path")


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_temp_artifacts(temp_path: Path) -> None:
    for artifact in (temp_path, temp_path.with_suffix(f"{temp_path.suffix}.dirty")):
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort cleanup; the dirty marker records the retry


def _verify_index(config: Config, path: Path, expected_ids: list[int]) -> None:
    """Reopen an index and require the exact ID set, count, and dimension."""
    index = VectorIndex(config, path=path)
    index.initialize(dim=config.embedding_dim)
    try:
        metadata = index.inspect_metadata()
        if metadata.dimension != config.embedding_dim:
            raise ValueError("staged index dimension mismatch")
        if metadata.count != len(expected_ids):
            raise ValueError("staged index count mismatch")
        if index.ids() != sorted(expected_ids):
            raise ValueError("staged index id mismatch")
    finally:
        index.close()
