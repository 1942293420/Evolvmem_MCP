"""Behavioral contracts for rebuilding the Context Core L0 vector cache."""

import numpy as np

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextStatus,
)
from evolvmem.context_store import ContextStore
from evolvmem.context_vector_sync import ContextVectorSynchronizer
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
        raise AssertionError("Context L0 rebuild must use encode_document")

    def encode_query(self, _text: str) -> list[float]:
        raise AssertionError("Context L0 rebuild must not use encode_query")


def make_draft(
    identity_key: str,
    *,
    l0: str,
    status: ContextStatus = ContextStatus.ACTIVE,
    expires_at: str | None = None,
    content_type: ContextContentType = ContextContentType.FACT,
) -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=content_type,
        layers=ContextLayers(
            l0=l0,
            l1=f"detail for {l0}",
            l2=f"source for {l0}",
            generator="test-suite",
        ),
        status=status,
        expires_at=expires_at,
    )


def make_synchronizer(test_config, store, engine):
    context_index = VectorIndex(test_config, path=test_config.context_vector_path)
    return ContextVectorSynchronizer(test_config, store, context_index, engine), context_index


def test_rebuild_embeds_only_active_unexpired_l0_documents_at_context_path(test_config):
    """Including a candidate, expired item, or L1/L2 must not contaminate the L0 cache."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"active l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        active = store.create_item(make_draft("active", l0="active l0"))
        store.create_item(make_draft("candidate", l0="candidate l0", status=ContextStatus.CANDIDATE))
        store.create_item(make_draft("expired", l0="expired l0", expires_at="2000-01-01 00:00:00"))
        store.create_item(make_draft(
            "checkpoint", l0="checkpoint l0",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
        ))
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        report = synchronizer.rebuild_active_l0()

        assert report.status == "synchronized"
        assert report.document_count == 1
        assert engine.documents == ["active l0"]
        assert context_index.path == test_config.context_vector_path.resolve()
        assert context_index.count() == 1
        assert context_index.search(np.array([1, 0, 0], dtype=np.float32), k=1) == [
            {"id": active.id, "distance": 0.0}
        ]
        assert test_config.context_vector_path.exists()
        assert not test_config.vector_path.exists()
        context_index.close()


def test_successful_rebuild_clears_only_the_context_dirty_marker(test_config):
    """A successful Context sync must not declare the separate legacy cache synchronized."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"active l0": [1, 0, 0]})
    legacy_index = VectorIndex(test_config)
    legacy_index.initialize(dim=3)
    legacy_index.mark_dirty()
    with ContextStore(test_config) as store:
        store.create_item(make_draft("active", l0="active l0"))
        synchronizer, context_index = make_synchronizer(test_config, store, engine)
        context_index.mark_dirty()

        report = synchronizer.rebuild_active_l0()

        assert report.status == "synchronized"
        assert context_index.is_dirty() is False
        assert legacy_index.is_dirty() is True
        context_index.close()
    legacy_index.close()


def test_encoding_failure_preserves_context_dirty_marker_and_sqlite_items(test_config):
    """An encoding failure must leave source records and the retry signal intact."""
    test_config.embedding_dim = 3

    class FailingEngine:
        is_loaded = True

        def encode_document(self, text: str) -> list[float]:
            raise RuntimeError(f"cannot encode {text}")

    with ContextStore(test_config) as store:
        item = store.create_item(make_draft("sensitive", l0="do not leak this L0"))
        synchronizer, context_index = make_synchronizer(test_config, store, FailingEngine())

        report = synchronizer.rebuild_active_l0()

        assert report.status == "failed"
        assert report.document_count == 1
        assert report.detail == "RuntimeError"
        assert "do not leak this L0" not in report.detail
        assert context_index.is_dirty() is True
        persisted = store.get_item(item.id)
        assert persisted is not None
        assert persisted.layers is not None
        assert persisted.layers.l0 == "do not leak this L0"
        context_index.close()


def test_unavailable_engine_reports_unavailable_without_breaking_fts(test_config):
    """Optional vector infrastructure must not make SQLite FTS unavailable."""
    with ContextStore(test_config) as store:
        item = store.create_item(make_draft("fts", l0="searchable context"))
        synchronizer, context_index = make_synchronizer(test_config, store, None)

        report = synchronizer.rebuild_active_l0()

        assert report.status == "unavailable"
        assert report.document_count == 0
        assert context_index.is_dirty() is True
        assert [hit.item_id for hit in store.search_fts("searchable")] == [item.id]
        context_index.close()


def test_wrong_index_path_marks_the_context_cache_dirty_without_touching_legacy(test_config):
    """A wiring error must leave a retry signal on the cache that actually failed."""
    legacy_index = VectorIndex(test_config)
    with ContextStore(test_config) as store:
        synchronizer = ContextVectorSynchronizer(test_config, store, legacy_index, None)

        report = synchronizer.rebuild_active_l0()

        assert report.status == "failed"
        assert report.detail == "ValueError"
        assert test_config.context_vector_path.with_suffix(".usearch.dirty").exists()
        assert legacy_index.is_dirty() is False
    legacy_index.close()


def test_dirty_marker_io_failure_returns_a_safe_failed_report(test_config, monkeypatch):
    """A marker-write failure must not escape or expose context content."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"sensitive L0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("sensitive", l0="sensitive L0"))
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        def fail_to_mark_dirty():
            raise OSError("cannot write marker for sensitive L0")

        monkeypatch.setattr(context_index, "mark_dirty", fail_to_mark_dirty)
        report = synchronizer.rebuild_active_l0()

        assert report.status == "failed"
        assert report.document_count == 0
        assert report.detail == "OSError"
        assert "sensitive L0" not in report.detail
        context_index.close()


def test_empty_active_set_rebuilds_a_valid_empty_context_index(test_config):
    """An empty SQLite truth set is a valid index state, not an unavailable rebuild."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({})
    with ContextStore(test_config) as store:
        store.create_item(make_draft("candidate", l0="candidate", status=ContextStatus.CANDIDATE))
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        report = synchronizer.rebuild_active_l0()

        assert report.status == "synchronized"
        assert report.document_count == 0
        assert context_index.count() == 0
        assert context_index.is_dirty() is False
        assert test_config.context_vector_path.exists()
        context_index.close()


def test_context_rebuild_does_not_overwrite_a_readable_legacy_vector_file(test_config):
    """ContextItem IDs must never overwrite the legacy MemoryStore vector cache."""
    test_config.embedding_dim = 3
    legacy_index = VectorIndex(test_config)
    legacy_index.initialize(dim=3)
    legacy_index.add(999, np.array([0, 1, 0], dtype=np.float32))
    legacy_index.save()
    legacy_index.close()

    engine = DocumentEmbeddingEngine({"context l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        item = store.create_item(make_draft("context", l0="context l0"))
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        report = synchronizer.rebuild_active_l0()

        assert report.status == "synchronized"
        context_index.close()

    reopened_legacy = VectorIndex(test_config)
    reopened_legacy.initialize(dim=3)
    reopened_context = VectorIndex(test_config, path=test_config.context_vector_path)
    reopened_context.initialize(dim=3)
    assert reopened_legacy.search(np.array([0, 1, 0], dtype=np.float32), k=1)[0]["id"] == 999
    assert reopened_context.search(np.array([1, 0, 0], dtype=np.float32), k=1)[0]["id"] == item.id
    reopened_legacy.close()
    reopened_context.close()


# ---- per-item post-commit synchronization ----


def test_upsert_adds_one_item_without_rebuilding_existing_entries(test_config):
    """A single-item sync must not disturb entries already in the cache."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"new l0": [1, 0, 0], "old l0": [0, 1, 0]})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)
        context_index.initialize(dim=3)
        context_index.add(999, np.array([0, 1, 0], dtype=np.float32))
        context_index.save()

        report = synchronizer.upsert_active_l0(7, "new l0")

        assert report.status == "synchronized"
        assert report.document_count == 1
        assert context_index.count() == 2
        assert context_index.is_dirty() is False
        context_index.close()


def test_upsert_twice_keeps_a_single_entry(test_config):
    """Re-syncing the same item replaces its vector instead of duplicating it."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        assert synchronizer.upsert_active_l0(7, "l0").status == "synchronized"
        assert synchronizer.upsert_active_l0(7, "l0").status == "synchronized"

        assert context_index.count() == 1
        assert context_index.is_dirty() is False
        context_index.close()


def test_remove_deletes_only_the_target_entry(test_config):
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"a l0": [1, 0, 0], "b l0": [0, 1, 0]})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)
        assert synchronizer.upsert_active_l0(1, "a l0").status == "synchronized"
        assert synchronizer.upsert_active_l0(2, "b l0").status == "synchronized"

        report = synchronizer.remove_l0(1)

        assert report.status == "synchronized"
        assert context_index.count() == 1
        assert context_index.is_dirty() is False
        context_index.close()


def test_remove_of_an_absent_item_is_a_clean_noop(test_config):
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)

        report = synchronizer.remove_l0(12345)

        assert report.status == "synchronized"
        assert context_index.is_dirty() is False
        context_index.close()


def test_upsert_without_engine_reports_unavailable_and_stays_dirty(test_config):
    """An unavailable engine must leave a truthful retry marker."""
    test_config.embedding_dim = 3
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, None)

        report = synchronizer.upsert_active_l0(7, "new l0")

        assert report.status == "unavailable"
        assert context_index.is_dirty() is True
        assert context_index.count() == 0
        context_index.close()


def test_remove_works_without_an_engine(test_config):
    """Removals need no embeddings and must not be blocked by an absent engine."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)
        assert synchronizer.upsert_active_l0(7, "l0").status == "synchronized"
        synchronizer_no_engine = ContextVectorSynchronizer(
            test_config, store, context_index, None
        )

        report = synchronizer_no_engine.remove_l0(7)

        assert report.status == "synchronized"
        assert context_index.count() == 0
        assert context_index.is_dirty() is False
        context_index.close()


def test_failed_upsert_preserves_the_dirty_marker_and_old_entry(test_config):
    test_config.embedding_dim = 3

    class FailingEngine:
        is_loaded = True

        def encode_document(self, text: str) -> list[float]:
            raise RuntimeError("cannot encode sensitive L0")

    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(
            test_config, store, DocumentEmbeddingEngine({"old l0": [1, 0, 0]})
        )
        assert synchronizer.upsert_active_l0(7, "old l0").status == "synchronized"
        failing = ContextVectorSynchronizer(
            test_config, store, context_index, FailingEngine()
        )

        report = failing.upsert_active_l0(8, "sensitive new l0")

        assert report.status == "failed"
        assert report.detail == "RuntimeError"
        assert context_index.is_dirty() is True
        assert context_index.count() == 1  # the earlier entry survives untouched
        context_index.close()


def test_successful_upsert_preserves_a_preexisting_dirty_marker(test_config):
    """One successful item sync must not declare an earlier failure recovered."""
    test_config.embedding_dim = 3
    engine = DocumentEmbeddingEngine({"l0": [1, 0, 0]})
    with ContextStore(test_config) as store:
        synchronizer, context_index = make_synchronizer(test_config, store, engine)
        context_index.initialize(dim=3)
        context_index.mark_dirty()  # an earlier failure still awaits a rebuild
        context_index.preserve_dirty()

        report = synchronizer.upsert_active_l0(7, "l0")

        assert report.status == "synchronized"
        assert context_index.count() == 1
        assert context_index.is_dirty() is True
        context_index.close()


def test_upsert_on_a_wrong_path_index_marks_the_context_cache_dirty(test_config):
    legacy_index = VectorIndex(test_config)
    with ContextStore(test_config) as store:
        synchronizer = ContextVectorSynchronizer(test_config, store, legacy_index, None)

        report = synchronizer.upsert_active_l0(7, "new l0")

        assert report.status == "failed"
        assert report.detail == "ValueError"
        assert test_config.context_vector_path.with_suffix(".usearch.dirty").exists()
        assert legacy_index.is_dirty() is False
    legacy_index.close()
