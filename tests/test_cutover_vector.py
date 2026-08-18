"""Behavioral contracts for atomic Context vector staging at cutover."""

import os
from pathlib import Path

import numpy as np
import pytest

from evolvmem import cutover_vector
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextStatus,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore
from evolvmem.cutover_vector import (
    ContextVectorStageReport,
    rebuild_context_vector_atomically,
)
from evolvmem.vector_index import VectorIndex


class DocumentEmbeddingEngine:
    """A deterministic embedding engine that rejects the wrong encoding API."""

    is_loaded = True

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.documents: list[str] = []

    def encode_document(self, text: str) -> list[float]:
        self.documents.append(text)
        return self.vectors[text]

    def encode(self, _text: str) -> list[float]:
        raise AssertionError("Context L0 staging must use encode_document")

    def encode_query(self, _text: str) -> list[float]:
        raise AssertionError("Context L0 staging must not use encode_query")


def make_draft(
    identity_key: str,
    *,
    l0: str,
    status: ContextStatus = ContextStatus.ACTIVE,
    expires_at: str | None = None,
) -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.FACT,
        layers=ContextLayers(
            l0=l0,
            l1=f"detail for {l0}",
            l2=f"source for {l0}",
            generator="test-suite",
        ),
        status=status,
        expires_at=expires_at,
    )


def make_formal_index(test_config, *, dim: int = 3, sentinel_id: int = 999) -> bytes:
    """Create a plausible pre-existing formal Context cache and return its bytes."""
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    index.initialize(dim=dim)
    vector = np.zeros(dim, dtype=np.float32)
    vector[0] = 1.0
    index.add(sentinel_id, vector)
    index.save()
    index.close()
    return test_config.context_vector_path.read_bytes()


def mark_formal_dirty(test_config) -> None:
    """Simulate migrated SQLite truth being newer than the vector cache."""
    marker = VectorIndex(test_config, path=test_config.context_vector_path)
    marker.mark_dirty()
    marker.preserve_dirty()


def formal_dirty_path(test_config) -> Path:
    path = test_config.context_vector_path
    return path.with_suffix(f"{path.suffix}.dirty")


def stage_leftovers(test_config) -> list[Path]:
    return sorted(test_config.data_dir.glob("context_vectors.usearch.stage-*"))


def assert_formal_state_preserved(test_config, report, formal_bytes: bytes) -> None:
    assert report.status == "failed"
    assert report.vector_ready is False
    assert report.dirty_cleared is False
    assert test_config.context_vector_path.read_bytes() == formal_bytes
    assert formal_dirty_path(test_config).exists()
    assert stage_leftovers(test_config) == []


# ---- VectorIndex read-only identity inspection ----


def test_ids_returns_sorted_integer_ids_without_exposing_vectors(test_config):
    index = VectorIndex(test_config)
    index.initialize(dim=3)
    index.add(9, np.array([0, 0, 1], dtype=np.float32))
    index.add(2, np.array([0, 1, 0], dtype=np.float32))
    index.add(5, np.array([1, 0, 0], dtype=np.float32))

    ids = index.ids()

    assert ids == [2, 5, 9]
    assert all(type(member) is int for member in ids)
    index.close()


def test_ids_and_metadata_on_an_empty_index(test_config):
    index = VectorIndex(test_config)
    index.initialize(dim=3)

    assert index.ids() == []
    metadata = index.inspect_metadata()
    assert metadata.count == 0
    assert metadata.dimension == 3
    assert metadata.dirty is False
    index.close()


def test_inspection_requires_an_initialized_index(test_config):
    index = VectorIndex(test_config)
    with pytest.raises(RuntimeError):
        index.ids()
    with pytest.raises(RuntimeError):
        index.inspect_metadata()


def test_ids_are_bounded_by_the_inspection_limit(test_config):
    index = VectorIndex(test_config)
    index.initialize(dim=3)
    index.add(1, np.array([1, 0, 0], dtype=np.float32))
    index.add(2, np.array([0, 1, 0], dtype=np.float32))

    with pytest.raises(ValueError):
        index.ids(limit=1)
    assert index.ids(limit=2) == [1, 2]
    index.close()


def test_corrupt_index_file_is_diagnosed_at_initialize(test_config):
    path = test_config.context_vector_path
    path.write_bytes(b"definitely not a usearch index")
    index = VectorIndex(test_config, path=path)

    with pytest.raises(ValueError):
        index.initialize(dim=3)


def test_inspect_metadata_reveals_a_wrong_dimension_index(test_config):
    """initialize() keeps legacy behavior; metadata exposes the real dimension."""
    path = test_config.context_vector_path
    wrong = VectorIndex(test_config, path=path)
    wrong.initialize(dim=4)
    wrong.add(7, np.array([1, 0, 0, 0], dtype=np.float32))
    wrong.save()
    wrong.close()

    index = VectorIndex(test_config, path=path)
    index.initialize(dim=3)  # restore does not re-validate the dimension
    metadata = index.inspect_metadata()
    assert metadata.dimension == 4
    assert metadata.count == 1
    assert index.ids() == [7]
    index.close()


# ---- atomic staging: success path ----


def test_stage_rebuilds_only_the_exact_active_unexpired_l0_id_set(test_config):
    """Candidate, expired, and L1/L2 content must not contaminate the staged cache."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"first l0": [1, 0, 0], "second l0": [0, 1, 0]})
    with ContextStore(test_config) as store:
        first = store.create_item(make_draft("first", l0="first l0"))
        second = store.create_item(make_draft("second", l0="second l0"))
        store.create_item(
            make_draft("candidate", l0="candidate l0", status=ContextStatus.CANDIDATE)
        )
        store.create_item(
            make_draft("expired", l0="expired l0", expires_at="2000-01-01 00:00:00")
        )

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "staged"
        assert report.document_count == 2
        assert report.vector_ready is True
        assert report.fts_only is False
        assert report.dirty_cleared is True
        assert engine.documents == ["first l0", "second l0"]

        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == sorted([first.id, second.id])
        metadata = reopened.inspect_metadata()
        assert metadata.count == 2
        assert metadata.dimension == 3
        assert metadata.dirty is False
        reopened.close()
        assert not formal_dirty_path(test_config).exists()
        assert not test_config.vector_path.exists()
        assert stage_leftovers(test_config) == []


def test_successful_stage_fsyncs_replaces_and_clears_dirty_in_order(
    test_config, monkeypatch
):
    """The swap is durable and the marker is cleared only after reopen verification."""
    test_config.embedding_dim = 3
    decoy = test_config.data_dir / "context_vectors.usearch.stage-deadbeef.usearch"
    decoy.write_bytes(b"stale decoy")
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    events: list[tuple[str, str]] = []

    real_fsync_file = cutover_vector._fsync_file
    real_fsync_dir = cutover_vector._fsync_dir
    real_replace = os.replace
    real_clear_dirty = VectorIndex.clear_dirty

    def recording_fsync_file(path):
        events.append(("fsync_file", Path(path).name))
        return real_fsync_file(path)

    def recording_fsync_dir(path):
        events.append(("fsync_dir", Path(path).name))
        return real_fsync_dir(path)

    def recording_replace(src, dst):
        events.append(("replace", f"{Path(src).name}->{Path(dst).name}"))
        return real_replace(src, dst)

    def recording_clear_dirty(self):
        events.append(("clear_dirty", self.path.name))
        return real_clear_dirty(self)

    monkeypatch.setattr(cutover_vector, "_fsync_file", recording_fsync_file)
    monkeypatch.setattr(cutover_vector, "_fsync_dir", recording_fsync_dir)
    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(VectorIndex, "clear_dirty", recording_clear_dirty)

    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))
        mark_formal_dirty(test_config)

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "staged"
        replace_events = [event for event in events if event[0] == "replace"]
        assert len(replace_events) == 1
        src_name, dst_name = replace_events[0][1].split("->")
        assert dst_name == "context_vectors.usearch"
        assert src_name.startswith("context_vectors.usearch.stage-")
        fsync_file_at = events.index(("fsync_file", src_name))
        replace_at = events.index(replace_events[0])
        fsync_dir_at = next(i for i, event in enumerate(events) if event[0] == "fsync_dir")
        formal_clear_at = events.index(("clear_dirty", "context_vectors.usearch"))
        assert fsync_file_at < replace_at < fsync_dir_at < formal_clear_at
        assert events[-1] == ("clear_dirty", "context_vectors.usearch")
        assert not formal_dirty_path(test_config).exists()
        assert decoy.read_bytes() == b"stale decoy"
        assert stage_leftovers(test_config) == [decoy]


def test_empty_active_set_stages_a_valid_empty_index(test_config):
    """An empty SQLite truth set is a valid staged index, not a failure."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({})
    with ContextStore(test_config) as store:
        store.create_item(
            make_draft("candidate", l0="candidate l0", status=ContextStatus.CANDIDATE)
        )

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "staged"
        assert report.document_count == 0
        assert engine.documents == []
        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == []
        metadata = reopened.inspect_metadata()
        assert metadata.count == 0
        assert metadata.dimension == 3
        reopened.close()
        assert not formal_dirty_path(test_config).exists()


def test_successful_stage_clears_only_the_context_dirty_marker(test_config):
    """Staging must never declare the separate legacy cache synchronized."""
    test_config.embedding_dim = 3
    legacy_index = VectorIndex(test_config)
    legacy_index.initialize(dim=3)
    legacy_index.add(999, np.array([0, 1, 0], dtype=np.float32))
    legacy_index.save()
    legacy_index.mark_dirty()
    legacy_index.preserve_dirty()
    legacy_bytes = test_config.vector_path.read_bytes()
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))
        mark_formal_dirty(test_config)

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "staged"
        assert report.dirty_cleared is True
        assert not formal_dirty_path(test_config).exists()
        assert legacy_index.is_dirty() is True
        assert test_config.vector_path.read_bytes() == legacy_bytes
    legacy_index.close()


def test_stage_reads_only_list_vector_documents_from_the_store(test_config, monkeypatch):
    """The staging builder must not probe items through any other store API."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        def forbidden(*args, **kwargs):
            raise AssertionError("staging must use list_vector_documents only")

        monkeypatch.setattr(ContextStore, "get_item", forbidden)
        monkeypatch.setattr(ContextStore, "search_fts", forbidden)
        monkeypatch.setattr(ContextStore, "get_retrieval_records", forbidden)

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "staged"
        assert report.document_count == 1


# ---- atomic staging: injected failures keep formal bytes and the dirty marker ----


def test_encode_failure_keeps_formal_bytes_and_preserves_dirty_marker(test_config):
    """The migrated SQLite truth is newer, so the retry marker must survive."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)

    class FailingEngine:
        is_loaded = True

        def encode_document(self, text: str) -> list[float]:
            raise RuntimeError(f"cannot encode {text}")

    with ContextStore(test_config) as store:
        item = store.create_item(make_draft("sensitive", l0="sensitive l0 body"))

        report = rebuild_context_vector_atomically(test_config, store, FailingEngine())

        assert report.detail == "RuntimeError"
        assert "sensitive l0 body" not in report.detail
        assert report.document_count == 1
        assert_formal_state_preserved(test_config, report, formal_bytes)
        persisted = store.get_item(item.id)
        assert persisted is not None and persisted.layers is not None
        assert persisted.layers.l0 == "sensitive l0 body"


def test_rebuild_failure_keeps_formal_bytes_and_preserves_dirty_marker(
    test_config, monkeypatch
):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def failing_rebuild(self, ids, embeddings):
        raise RuntimeError("injected rebuild failure")

    monkeypatch.setattr(VectorIndex, "rebuild", failing_rebuild)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.detail == "RuntimeError"
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_validation_failure_keeps_formal_bytes_and_preserves_dirty_marker(
    test_config, monkeypatch
):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def failing_ids(self, **kwargs):
        raise RuntimeError("injected validation failure")

    monkeypatch.setattr(VectorIndex, "ids", failing_ids)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.detail == "RuntimeError"
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_close_failure_keeps_formal_bytes_and_preserves_dirty_marker(
    test_config, monkeypatch
):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def failing_close(self):
        raise OSError("injected close failure")

    monkeypatch.setattr(VectorIndex, "close", failing_close)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.detail == "OSError"
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_fsync_failure_keeps_formal_bytes_and_preserves_dirty_marker(
    test_config, monkeypatch
):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def failing_fsync_file(path):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(cutover_vector, "_fsync_file", failing_fsync_file)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.detail == "OSError"
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_replace_failure_keeps_formal_bytes_and_preserves_dirty_marker(
    test_config, monkeypatch
):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    mark_formal_dirty(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def failing_replace(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.detail == "OSError"
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_wrong_embedding_dimension_fails_before_touching_the_formal_index(test_config):
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0]})  # violates the dim=3 contract
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, engine)

        assert report.status == "failed"
        assert report.detail == "ValueError"
        assert report.document_count == 1
        assert not test_config.context_vector_path.exists()
        assert formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []


# ---- FTS-only degrade: explicit, journaled, never vector-healthy ----


def test_missing_engine_without_approval_fails_and_keeps_the_marker(test_config):
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(test_config, store, None)

        assert report.status == "failed"
        assert report.vector_ready is False
        assert report.dirty_cleared is False
        assert "engine_unavailable" in report.reason_codes
        assert formal_dirty_path(test_config).exists()
        assert not test_config.context_vector_path.exists()


def test_allow_fts_only_records_an_explicit_reason_and_preserves_the_marker(test_config):
    """FTS-only degrade is an explicit, recorded state — never vector health."""
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, None, allow_fts_only=True
        )

        assert report.status == "fts_only"
        assert report.fts_only is True
        assert report.vector_ready is False
        assert report.dirty_cleared is False
        assert "engine_unavailable" in report.reason_codes
        assert "approved_fts_only" in report.reason_codes
        assert formal_dirty_path(test_config).exists()
        assert not test_config.context_vector_path.exists()
        public = report.public_dict()
        assert public["status"] == "fts_only"
        assert len(report.digest()) == 64


def test_allow_fts_only_marks_a_staging_failure_as_degraded(test_config):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)

    class FailingEngine:
        is_loaded = True

        def encode_document(self, text: str) -> list[float]:
            raise RuntimeError(f"cannot encode {text}")

    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, FailingEngine(), allow_fts_only=True
        )

        assert report.status == "fts_only"
        assert report.detail == "RuntimeError"
        assert report.vector_ready is False
        assert report.dirty_cleared is False
        assert test_config.context_vector_path.read_bytes() == formal_bytes
        assert formal_dirty_path(test_config).exists()


def test_allow_fts_only_does_not_skip_a_healthy_rebuild(test_config):
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, engine, allow_fts_only=True
        )

        assert report.status == "staged"
        assert report.vector_ready is True
        assert report.fts_only is False
        assert not formal_dirty_path(test_config).exists()


# ---- report model contracts ----


def make_report(**overrides) -> ContextVectorStageReport:
    fields = dict(
        status="failed",
        document_count=0,
        vector_ready=False,
        fts_only=False,
        dirty_cleared=False,
        reason_codes=("stage_failed",),
    )
    fields.update(overrides)
    return ContextVectorStageReport(**fields)


def test_stage_report_rejects_an_fts_only_claim_of_vector_health():
    with pytest.raises(ContextValidationError):
        make_report(
            status="fts_only",
            fts_only=True,
            vector_ready=True,
            reason_codes=("engine_unavailable", "approved_fts_only"),
        )


def test_stage_report_rejects_staged_without_a_dirty_clear():
    with pytest.raises(ContextValidationError):
        make_report(status="staged", vector_ready=True, reason_codes=())


def test_stage_report_rejects_an_unknown_status():
    with pytest.raises(ContextValidationError):
        make_report(status="healthy")


def test_stage_report_rejects_content_like_detail():
    with pytest.raises(ContextValidationError):
        make_report(detail="leaked /absolute/path detail")


def test_stage_report_public_dict_and_digest_are_stable_and_privacy_safe():
    report = make_report(detail="RuntimeError", document_count=3, duration_ms=1.5)
    public = report.public_dict()
    assert public["schema"] == "evolvmem.context_vector_stage"
    assert public["version"] == 1
    assert public["detail"] == "RuntimeError"
    assert report.digest() == report.digest()
    assert len(report.digest()) == 64


def test_stage_requires_a_real_config_instance():
    with pytest.raises(ContextValidationError):
        rebuild_context_vector_atomically(object(), None, None)
