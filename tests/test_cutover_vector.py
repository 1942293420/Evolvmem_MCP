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


# ---- source-truth guard: encoding a snapshot that then moved is not health ----


def test_commit_guard_failure_keeps_the_old_bytes_and_the_dirty_marker(test_config):
    """A guard that re-reads newer truth must not swap or clear anything."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=lambda: False
        )

        assert report.status == "failed"
        assert report.vector_ready is False
        assert report.dirty_cleared is False
        assert report.reason_codes == ("source_changed_during_rebuild",)
        assert report.document_count == 1
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_commit_guard_exception_keeps_the_old_bytes_and_preserves_dirty(test_config):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})

    def broken_guard():
        raise RuntimeError("cannot re-read source truth")

    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=broken_guard
        )

        assert report.status == "failed"
        assert report.detail == "RuntimeError"
        assert "source truth" not in report.detail
        assert_formal_state_preserved(test_config, report, formal_bytes)


def test_staged_image_never_overwrites_truth_written_during_encoding(test_config):
    """The exact reported defect: a write mid-build must not be declared clean."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"early l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("early", l0="early l0"))

        def truth_moved_during_encoding():
            # The writer commits a new item after the staged image was built.
            store.create_item(make_draft("late", l0="late l0"))
            return False

        report = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=truth_moved_during_encoding
        )

        assert report.status == "failed"
        assert report.reason_codes == ("source_changed_during_rebuild",)
        assert_formal_state_preserved(test_config, report, formal_bytes)

        # A later attempt on stable truth does swap and clear.
        stable = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=lambda: True
        )
        assert stable.status == "staged"
        assert stable.dirty_cleared is True
        assert not formal_dirty_path(test_config).exists()
        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == [doc.item_id for doc in store.list_vector_documents()]
        reopened.close()


def test_commit_guard_runs_inside_a_write_transaction(test_config):
    """The guard reads committed truth under the same lock as the swap."""
    from evolvmem.context_store import ContextStore as Store

    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    seen = []

    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        def observing_guard():
            seen.append(store._transaction_depth > 0)
            return True

        report = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=observing_guard
        )

        assert seen == [True]
        assert report.status == "staged"
        assert store._transaction_depth == 0


def test_commit_guard_passing_still_stages_and_clears(test_config):
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    seen = []

    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, engine,
            commit_guard=lambda: (seen.append(True), True)[1],
        )

        assert seen == [True]
        assert report.status == "staged"
        assert report.dirty_cleared is True
        assert not formal_dirty_path(test_config).exists()


def test_commit_guard_must_be_callable(test_config):
    with ContextStore(test_config) as store:
        with pytest.raises(ContextValidationError):
            rebuild_context_vector_atomically(
                test_config, store, None, commit_guard="not callable"
            )


def test_source_truth_written_during_encoding_never_replaces_the_old_file(test_config):
    """The reported defect, end to end: a mid-build write keeps the old bytes.

    The writer commits through the same store while the engine is still
    encoding, which is exactly the sequence that previously let a superseded
    snapshot be swapped in and reported as synchronized.
    """
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    written = []
    store_ref = {}

    class CommittingEngine:
        is_loaded = True

        def encode_document(self, text: str) -> list[float]:
            if not written:
                # A real second write lands after this first encode.
                store_ref['store'].create_item(make_draft("late", l0="late l0"))
                written.append(text)
            return [1.0, 0.0, 0.0]

    with ContextStore(test_config) as store:
        store_ref['store'] = store
        store.create_item(make_draft("early", l0="early l0"))
        first_snapshot = [document.item_id for document in store.list_vector_documents()]

        def guard():
            current = [document.item_id for document in store.list_vector_documents()]
            return current == first_snapshot

        report = rebuild_context_vector_atomically(
            test_config, store, CommittingEngine(), commit_guard=guard
        )

        assert report.status == "failed"
        assert report.reason_codes == ("source_changed_during_rebuild",)
        assert test_config.context_vector_path.read_bytes() == formal_bytes
        assert formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []
        # The database truth still holds both records.
        assert len(store.list_vector_documents()) == 2


def test_replace_failure_after_the_guard_keeps_the_old_bytes(test_config, monkeypatch):
    """The swap happens iff the guard passed, and a failed swap keeps the bytes."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    engine = DocumentEmbeddingEngine({"early l0": [1, 0, 0], "late l0": [0, 1, 0]})
    index_overwrite_attempts = []

    def failing_replace(src, dst):
        # The at-risk instant: os.replace is about to overwrite the live file.
        index_overwrite_attempts.append(Path(dst).read_bytes() == formal_bytes)
        raise OSError("injected replace failure")

    monkeypatch.setattr(cutover_vector.os, "replace", failing_replace)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("early", l0="early l0"))
        store.create_item(make_draft("late", l0="late l0"))

        report = rebuild_context_vector_atomically(
            test_config, store, engine, commit_guard=lambda: True
        )

        assert report.status == "failed"
        assert report.detail == "OSError"
        assert index_overwrite_attempts == [True], "the old file was still in place"
        assert test_config.context_vector_path.read_bytes() == formal_bytes
        assert formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []


# ---- bounded catch-up: small writes during encoding are reconciled --------
#
# A full re-encode of a large L0 library takes minutes; continuous capture keeps
# writing. The explicit bounded catch-up mode re-reads the eligible truth after
# the first pass and encodes only the delta, for a bounded number of rounds. The
# caller's snapshot guard is retired inside this mode: correctness comes from the
# exact ID/L0 re-read that happens in the same short write transaction as the
# swap, so the index is published only when it is byte-equivalent to committed
# truth. Over budget, a model error, or an unstable transaction boundary keeps
# the previous formal bytes and the durable dirty marker.


class MutatingEngine:
    """Deterministic encoder that commits one synthetic write on first use."""

    is_loaded = True

    def __init__(self, store, mutate):
        self.store = store
        self.mutate = mutate
        self.documents: list[str] = []
        self.mutated = False

    def encode_document(self, text: str) -> list[float]:
        self.documents.append(text)
        if not self.mutated:
            self.mutated = True
            with self.store.transaction():
                self.mutate(self.store)
        vector = np.zeros(3, dtype=np.float32)
        vector[sum(text.encode("utf-8")) % 3] = 1.0
        return vector


def _set_l0(store, item_id: int, l0: str) -> None:
    with store.transaction():
        store._connection().execute(
            "UPDATE context_layers SET content=? WHERE item_id=? AND layer='l0'",
            (l0, item_id),
        )


def _reopen_ids(test_config) -> list[int]:
    reopened = VectorIndex(test_config, path=test_config.context_vector_path)
    reopened.initialize(dim=test_config.embedding_dim)
    try:
        return reopened.ids()
    finally:
        reopened.close()


def test_catch_up_encodes_an_item_added_during_the_first_encoding_pass(test_config):
    """The reported defect: one late write no longer discards the whole rebuild."""
    test_config.embedding_dim = 3

    def add_late(store):
        store.create_item(make_draft("late", l0="late l0"))

    with ContextStore(test_config) as store:
        early = store.create_item(make_draft("early", l0="early l0"))
        engine = MutatingEngine(store, add_late)

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=3
        )

        late_id = next(
            int(document.item_id) for document in store.list_vector_documents()
            if int(document.item_id) != early.id
        )
        assert report.status == "staged"
        assert report.dirty_cleared is True
        assert report.document_count == 2
        assert sorted(engine.documents) == ["early l0", "late l0"]
        assert engine.documents.count("early l0") == 1, "unchanged text is never re-encoded"
        assert _reopen_ids(test_config) == sorted([early.id, late_id])
        assert not formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []


def test_catch_up_applies_a_changed_l0_and_drops_a_newly_archived_item(test_config):
    """Changed text is re-encoded; an item that left eligibility is removed."""
    test_config.embedding_dim = 3

    def change_and_archive(store):
        changed = store.get_by_identity("changed")[0]
        archived = store.get_by_identity("archived")[0]
        _set_l0(store, changed.id, "changed l0 v2")
        store.set_item_status(archived.id, ContextStatus.ARCHIVED)

    with ContextStore(test_config) as store:
        changed = store.create_item(make_draft("changed", l0="changed l0 v1"))
        store.create_item(make_draft("archived", l0="archived l0"))
        engine = MutatingEngine(store, change_and_archive)

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=3
        )

        assert report.status == "staged"
        assert report.document_count == 1
        assert engine.documents == ["changed l0 v1", "archived l0", "changed l0 v2"]
        assert engine.documents.count("changed l0 v1") == 1, "unchanged rows are not re-encoded"
        assert _reopen_ids(test_config) == [changed.id]
        assert not formal_dirty_path(test_config).exists()


def test_catch_up_reuses_unchanged_vectors_across_two_writes(test_config):
    """Two separate late writes cost two extra encodes, not two full rebuilds."""
    test_config.embedding_dim = 3
    writes = 0

    class TwoWriteEngine:
        is_loaded = True

        def __init__(self, store):
            self.store = store
            self.documents: list[str] = []

        def encode_document(self, text: str) -> list[float]:
            nonlocal writes
            self.documents.append(text)
            if writes < 2:
                writes += 1
                with self.store.transaction():
                    self.store.create_item(make_draft(
                        f"late{writes}", l0=f"late l0 {writes}"))
            vector = np.zeros(3, dtype=np.float32)
            vector[sum(text.encode("utf-8")) % 3] = 1.0
            return vector

    with ContextStore(test_config) as store:
        store.create_item(make_draft("stable", l0="stable l0"))
        engine = TwoWriteEngine(store)

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=5
        )

        assert report.status == "staged"
        assert report.document_count == 3
        assert engine.documents.count("stable l0") == 1
        assert sorted(engine.documents) == ["late l0 1", "late l0 2", "stable l0"]
        assert _reopen_ids(test_config) == sorted(
            int(document.item_id) for document in store.list_vector_documents()
        )


@pytest.mark.parametrize("drop_all", [False, True])
@pytest.mark.parametrize("shared_cache", [False, True])
def test_catch_up_removes_archived_rows_without_any_new_encoding(test_config, drop_all, shared_cache):
    test_config.embedding_dim = 3
    test_config.lan_shared_vector_cache = shared_cache
    mark_formal_dirty(test_config)
    with ContextStore(test_config) as store:
        keep = store.create_item(make_draft("keep", l0="keep l0"))
        drop = store.create_item(make_draft("drop", l0="drop l0"))

        def archive_only(current_store):
            current_store.set_item_status(drop.id, ContextStatus.ARCHIVED)
            if drop_all:
                current_store.set_item_status(keep.id, ContextStatus.ARCHIVED)

        engine = MutatingEngine(store, archive_only)
        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=1
        )
        assert report.status == "staged"
        assert _reopen_ids(test_config) == ([] if drop_all else [keep.id])
        assert len(engine.documents) == 2
        assert report.document_count == (0 if drop_all else 1)
        assert not formal_dirty_path(test_config).exists()


@pytest.mark.parametrize("empty", [False, True])
def test_bounded_single_pass_accepts_stable_or_empty_truth(test_config, empty):
    test_config.embedding_dim = 3
    with ContextStore(test_config) as store:
        expected = [] if empty else [store.create_item(make_draft("one", l0="one")).id]
        engine = DocumentEmbeddingEngine({"one": [1, 0, 0]})
        report = rebuild_context_vector_atomically(test_config, store, engine, catch_up_rounds=1)
        assert report.status == "staged"
        assert _reopen_ids(test_config) == expected


@pytest.mark.parametrize("failure", ["budget", "model"])
def test_bounded_failure_releases_the_temporary_index(test_config, monkeypatch, failure):
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    mark_formal_dirty(test_config)
    created = []
    real_index = VectorIndex

    class TrackedIndex(real_index):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if ".stage-" in str(self.path):
                created.append(self)

    monkeypatch.setattr(cutover_vector, "VectorIndex", TrackedIndex)
    with ContextStore(test_config) as store:
        store.create_item(make_draft("first", l0="first"))

        class FailingEngine:
            is_loaded = True
            calls = 0

            def encode_document(self, text):
                self.calls += 1
                if self.calls == 2 and failure == "model":
                    raise RuntimeError("model unavailable")
                store.create_item(make_draft(f"late-{self.calls}", l0=f"late {self.calls}"))
                return [1, 0, 0]

        report = rebuild_context_vector_atomically(test_config, store, FailingEngine(), catch_up_rounds=2)
        assert_formal_state_preserved(test_config, report, formal_bytes)
        assert created
        assert all(index._index is None for index in created)


def test_bounded_mode_retires_the_caller_guard_and_verifies_in_the_transaction(
    test_config, monkeypatch
):
    """The outer snapshot guard is replaced by the in-transaction exact check.

    The bounded mode never accepts a caller guard, and the swap still runs
    inside one write transaction, so the exact re-read is what licenses the
    marker clear.
    """
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    mark_formal_dirty(test_config)
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    transaction_depths: list[int] = []

    with ContextStore(test_config) as store:
        item = store.create_item(make_draft("active", l0="l0"))
        real_mismatch = cutover_vector._current_staged_mismatch

        def observing_mismatch(inner_store, staged, *, expected_count=None):
            transaction_depths.append(inner_store._transaction_depth)
            return real_mismatch(inner_store, staged, expected_count=expected_count)

        monkeypatch.setattr(
            cutover_vector, "_current_staged_mismatch", observing_mismatch
        )

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=1
        )

        assert report.status == "staged"
        assert report.dirty_cleared is True
        assert transaction_depths[-1] > 0, "the exact check runs inside the transaction"
        assert _reopen_ids(test_config) == [item.id]
        assert test_config.context_vector_path.read_bytes() != formal_bytes
        assert not formal_dirty_path(test_config).exists()


def test_bounded_mode_refuses_to_keep_a_caller_snapshot_guard(test_config):
    """A guard pinned to the first snapshot can only refuse; the modes are exclusive."""
    with ContextStore(test_config) as store:
        with pytest.raises(ContextValidationError):
            rebuild_context_vector_atomically(
                test_config,
                store,
                None,
                commit_guard=lambda: True,
                catch_up_rounds=3,
            )


def test_bounded_mode_rejects_a_non_positive_round_budget(test_config):
    with ContextStore(test_config) as store:
        with pytest.raises(ContextValidationError):
            rebuild_context_vector_atomically(
                test_config, store, None, catch_up_rounds=0
            )
        with pytest.raises(ContextValidationError):
            rebuild_context_vector_atomically(
                test_config, store, None, catch_up_rounds=-1
            )
        with pytest.raises(ContextValidationError):
            rebuild_context_vector_atomically(
                test_config, store, None, catch_up_rounds=True
            )


def test_unstable_catch_up_budget_keeps_the_old_file_and_dirty(test_config):
    """Every round sees a new write: bounded failure, never a stale publish."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    mark_formal_dirty(test_config)

    class AlwaysWritingEngine:
        is_loaded = True

        def __init__(self, store):
            self.store = store
            self.rounds = 0
            self.documents: list[str] = []

        def encode_document(self, text: str) -> list[float]:
            self.documents.append(text)
            self.rounds += 1
            with self.store.transaction():
                self.store.create_item(make_draft(f"churn{self.rounds}", l0=f"churn l0 {self.rounds}"))
            return [1.0, 0.0, 0.0]

    with ContextStore(test_config) as store:
        store.create_item(make_draft("early", l0="early l0"))
        engine = AlwaysWritingEngine(store)

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=3
        )

        assert report.status == "failed"
        assert report.reason_codes == ("catch_up_rounds_exhausted",)
        assert report.vector_ready is False
        assert report.dirty_cleared is False
        assert test_config.context_vector_path.read_bytes() == formal_bytes
        assert formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []
        assert len(engine.documents) == 3, "the budget bounds the encode rounds"


def test_a_write_that_lands_at_the_transaction_boundary_is_not_published(
    test_config, monkeypatch
):
    """The final transaction re-reads truth: a new row there aborts the swap."""
    test_config.embedding_dim = 3
    formal_bytes = make_formal_index(test_config)
    mark_formal_dirty(test_config)
    engine = DocumentEmbeddingEngine({"early l0": [1, 0, 0]})
    replace_attempts: list[str] = []

    with ContextStore(test_config) as store:
        store.create_item(make_draft("early", l0="early l0"))
        real_transaction = store.transaction
        injected = [False]

        def boundary_transaction():
            # The narrow window the final transaction must close: a legal write
            # commits after the last unlocked read but before the swap's read.
            if not injected[0]:
                injected[0] = True
                with real_transaction():
                    store.create_item(make_draft("boundary", l0="boundary l0"))
            return real_transaction()

        def recording_replace(src, dst):
            replace_attempts.append(str(dst))
            raise AssertionError("the swap must not run against stale truth")

        monkeypatch.setattr(store, "transaction", boundary_transaction)
        monkeypatch.setattr(cutover_vector.os, "replace", recording_replace)

        report = rebuild_context_vector_atomically(
            test_config, store, engine, catch_up_rounds=3
        )

        assert replace_attempts == [], "a stale image must never reach os.replace"
        assert report.status == "failed"
        assert report.reason_codes == ("source_changed_during_rebuild",)
        assert report.dirty_cleared is False
        assert test_config.context_vector_path.read_bytes() == formal_bytes
        assert formal_dirty_path(test_config).exists()
        assert stage_leftovers(test_config) == []
        # The database truth still holds both records.
        assert len(store.list_vector_documents()) == 2
