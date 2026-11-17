"""Bounded capability-only recovery, with durable proofs and competing writers."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore
from evolvmem.vector_index import VectorIndex


class Engine:
    is_loaded = True

    def __init__(self):
        self.documents = []
        self.fail = set()

    def encode_document(self, text):
        self.documents.append(text)
        if text in self.fail:
            raise RuntimeError("embedding failed")
        return np.asarray([1., float(len(text)), 0.5], dtype=np.float32)

    def encode_query(self, text):
        return np.asarray([1., 1., .5], dtype=np.float32)


@pytest.fixture
def config(test_config):
    test_config.embedding_dim = 3
    test_config.embedding_model_filename = "test-model.gguf"
    return test_config


def add(store, title):
    return CapabilityStore(store).upsert(CapabilityMemoryDraft(
        category="backend", title=title, content="body"))


def update(store, memory_id, content):
    with store.transaction():
        store._connection().execute(
            "UPDATE capability_memories SET content=?,revision=revision+1 WHERE memory_id=?",
            (content, memory_id))


def reopen_ids(config):
    index = VectorIndex(config, path=config.capability_vector_path)
    index.initialize(config.embedding_dim)
    try:
        return index.ids()
    finally:
        index.close()


def test_single_sync_never_erases_preexisting_backlog_marker(config):
    with ContextStore(config) as store:
        first, second = add(store, "first"), add(store, "second")
        index = CapabilityIndex(store, config, Engine())
        index.vector_index.mark_dirty()
        assert index.sync(first, "caller text is not authoritative") == "synchronized"
        assert index.vector_index.is_dirty()
        assert reopen_ids(config) == [first]
        assert second not in reopen_ids(config)


def test_bounded_recovery_resumes_without_reencoding_durable_proofs(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(5)]
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        report = index.reconcile(limit=2)
        assert (report["status"], report["updated"], report["remaining"]) == ("partial", 2, 3)
        assert index.vector_index.is_dirty()
        restarted = CapabilityIndex(store, config, engine)
        report = restarted.reconcile(limit=2)
        assert (report["updated"], report["remaining"]) == (2, 1)
        report = restarted.reconcile(limit=2)
        assert (report["status"], report["updated"], report["remaining"]) == ("ready", 1, 0)
        assert not restarted.vector_index.is_dirty()
        assert reopen_ids(config) == ids
        assert len(engine.documents) == 5
        assert restarted.reconcile()["updated"] == 0
        assert len(engine.documents) == 5
        assert not config.context_vector_path.exists()
        assert not config.context_vector_path.with_suffix(".provenance.json").exists()


def test_only_missing_and_changed_content_are_encoded_and_ineligible_removed(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(3)]
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "ready"
        engine.documents.clear()
        update(store, ids[0], "corrected")
        CapabilityStore(store).set_status(ids[1], "archived")
        fresh = add(store, "fresh")
        result = index.reconcile()
        assert result["status"] == "ready"
        assert result["updated"] == 2
        assert result["removed"] == 1
        assert sorted(engine.documents) == ["0\ncorrected", "fresh\nbody"]
        assert reopen_ids(config) == [ids[0], ids[2], fresh]
        assert CapabilityStore(store).get(ids[1])["status"] == "archived"


def test_eligibility_predicate_applies_to_recovery_and_search_without_deleting_rows(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(2)]
        index = CapabilityIndex(store, config, Engine())
        assert index.reconcile()["status"] == "ready"
        guarded = CapabilityIndex(store, config, Engine(), eligible=lambda row: row["memory_id"] == ids[1])
        assert [r["memory_id"] for r in guarded.search("query")] == [ids[1]]
        assert guarded.reconcile()["removed"] == 1
        assert reopen_ids(config) == [ids[1]]
        assert CapabilityStore(store).get(ids[0])["status"] == "active"


def test_failed_item_does_not_hide_other_progress_and_retry_is_bounded(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(3)]
        engine = Engine()
        engine.fail.add("0\nbody")
        index = CapabilityIndex(store, config, engine)
        report = index.reconcile(limit=2)
        assert (report["failed"], report["updated"], report["remaining"]) == (1, 1, 2)
        assert index.vector_index.is_dirty()
        report = index.reconcile(limit=1)
        assert (report["updated"], report["remaining"]) == (1, 1)
        assert reopen_ids(config) == ids[1:]
        engine.fail.clear()
        assert index.reconcile(limit=1)["status"] == "ready"
        assert reopen_ids(config) == ids


def test_absent_proofs_are_rebuilt_in_batches_without_dropping_legacy_index(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(3)]
        legacy = VectorIndex(config, path=config.capability_vector_path)
        legacy.initialize(3)
        for ident in ids:
            legacy.add(ident, np.array([1., 1., .5]))
        legacy.save()
        index = CapabilityIndex(store, config, Engine())
        assert len(index.search("query", top_k=3)) == 3
        result = index.reconcile(limit=1)
        assert result["remaining"] == 2
        assert reopen_ids(config) == ids
        assert len(index.search("query", top_k=3)) == 3


def test_same_dimension_model_change_is_rejected_without_rewriting_existing_file(config):
    with ContextStore(config) as store:
        add(store, "first")
        assert CapabilityIndex(store, config, Engine()).reconcile()["status"] == "ready"
        before = config.capability_vector_path.read_bytes()
        config.embedding_model_filename = "different-model.gguf"
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "incompatible"
        assert index.search("query") == []
        assert engine.documents == []
        assert config.capability_vector_path.read_bytes() == before
        assert index.vector_index.is_dirty()


def test_wrong_dimension_engine_or_restored_index_cannot_claim_ready(config):
    with ContextStore(config) as store:
        add(store, "first")
        legacy = VectorIndex(config, path=config.capability_vector_path)
        legacy.initialize(2)
        legacy.add(99, np.array([1., 0.]))
        legacy.save()
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "incompatible"
        assert engine.documents == []
        assert index.vector_index.is_dirty()


def test_actual_vector_drift_invalidates_old_content_proof(config):
    with ContextStore(config) as store:
        ident = add(store, "first")
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "ready"
        engine.documents.clear()
        raw = VectorIndex(config, path=config.capability_vector_path)
        raw.initialize(3)
        raw.remove(ident)
        raw.add(ident, np.array([0., 0., 1.]))
        raw.save()
        assert index.reconcile()["updated"] == 1
        assert engine.documents == ["first\nbody"]


def test_unavailable_encoder_preserves_existing_vectors_and_pending_marker(config):
    with ContextStore(config) as store:
        first = add(store, "first")
        index = CapabilityIndex(store, config, Engine())
        assert index.reconcile()["status"] == "ready"
        second = add(store, "second")
        offline = CapabilityIndex(store, config)
        result = offline.reconcile()
        assert result["status"] == "unavailable"
        assert result["remaining"] == 1
        assert reopen_ids(config) == [first]
        assert offline.vector_index.is_dirty()
        assert CapabilityStore(store).get(second) is not None


def test_two_resident_writers_encode_concurrently_and_merge_durable_results(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(2)]
    barrier = threading.Barrier(2)
    class ConcurrentEngine(Engine):
        def encode_document(self, text):
            barrier.wait(timeout=5)
            return super().encode_document(text)
    def worker(ident):
        with ContextStore(config) as store:
            index = CapabilityIndex(store, config, ConcurrentEngine())
            index._ensure_index()
            return index.sync(ident, "ignored")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ids))
    assert results == ["synchronized", "synchronized"]
    assert reopen_ids(config) == ids
    with ContextStore(config) as store:
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "ready"
        assert engine.documents == []


@pytest.mark.parametrize("change", ["content", "archive"])
def test_edit_or_withdrawal_during_encoding_cannot_commit_stale_result(config, change):
    with ContextStore(config) as store:
        ident = add(store, "first")
    entered, release = threading.Event(), threading.Event()
    class BlockingEngine(Engine):
        def encode_document(self, text):
            entered.set()
            assert release.wait(timeout=5)
            return super().encode_document(text)
    def worker():
        with ContextStore(config) as store:
            return CapabilityIndex(store, config, BlockingEngine()).sync(ident, "ignored")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(timeout=5)
        with ContextStore(config) as store:
            if change == "content":
                update(store, ident, "new content")
            else:
                CapabilityStore(store).set_status(ident, "archived")
            assert CapabilityIndex(store, config, Engine()).reconcile()["status"] == "ready"
        release.set()
        assert future.result(timeout=5) == "stale"
    with ContextStore(config) as store:
        index = CapabilityIndex(store, config, Engine())
        assert index.reconcile()["status"] == "ready"
        assert index.embedding_engine.documents == []
        assert reopen_ids(config) == ([ident] if change == "content" else [])



def test_final_audit_observes_ids_merged_by_a_competing_save(config, monkeypatch):
    with ContextStore(config) as store:
        ident = add(store, "first")
        index = CapabilityIndex(store, config, Engine())
        assert index.reconcile()["status"] == "ready"
        competitor = CapabilityIndex(store, config, Engine())
        competitor._ensure_index()
        original_save = index.vector_index.save
        raced = False
        def save_with_competitor():
            nonlocal raced
            if not raced:
                raced = True
                competitor.vector_index.add(999, np.array([0., 1., 0.]))
                competitor.vector_index.preserve_dirty()
                competitor.vector_index.save()
            original_save()
        monkeypatch.setattr(index.vector_index, "save", save_with_competitor)
        result = index.reconcile()
        assert result["status"] == "partial"
        assert result["remaining"] == 1
        assert result["dirty"]
        assert reopen_ids(config) == [ident, 999]
        assert index.reconcile()["status"] == "ready"
        assert reopen_ids(config) == [ident]


def test_proof_write_failure_stays_dirty_and_retry_does_not_trust_unproved_vector(config, monkeypatch):
    with ContextStore(config) as store:
        ident = add(store, "first")
        index = CapabilityIndex(store, config, Engine())
        real_save = index._save_proofs
        def fail_save(proof):
            raise OSError("disk full")
        monkeypatch.setattr(index, "_save_proofs", fail_save)
        assert index.reconcile()["status"] == "failed"
        assert index.vector_index.is_dirty()
        assert reopen_ids(config) == [ident]
        monkeypatch.setattr(index, "_save_proofs", real_save)
        assert index.reconcile()["status"] == "ready"
        assert len(index.embedding_engine.documents) == 2


def test_wrong_embedding_shape_cannot_save_or_clear_dirty(config):
    class WrongEngine(Engine):
        def encode_document(self, text):
            return np.array([1., 0.])
    with ContextStore(config) as store:
        add(store, "first")
        index = CapabilityIndex(store, config, WrongEngine())
        result = index.reconcile()
        assert result["status"] == "failed"
        assert result["remaining"] == 1
        assert index.vector_index.is_dirty()
        assert index.vector_index.ids() == []


def test_failed_replacement_keeps_previous_file_and_other_searchable_vectors(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(2)]
        engine = Engine()
        index = CapabilityIndex(store, config, engine)
        assert index.reconcile()["status"] == "ready"
        previous = config.capability_vector_path.read_bytes()
        update(store, ids[0], "new body")
        engine.fail.add("0\nnew body")
        result = index.reconcile()
        assert result["status"] == "failed"
        assert result["remaining"] == 1
        assert config.capability_vector_path.read_bytes() == previous
        assert [row["memory_id"] for row in index.search("query")] == [ids[1]]
        engine.fail.clear()
        assert index.reconcile()["updated"] == 1


def test_parallel_delete_and_new_vectors_do_not_resurrect_or_overwrite_ids(config):
    with ContextStore(config) as store:
        retired = add(store, "retired")
        assert CapabilityIndex(store, config, Engine()).reconcile()["status"] == "ready"
        CapabilityStore(store).set_status(retired, "ignored")
        fresh = [add(store, "new1"), add(store, "new2")]
    begin = threading.Barrier(3)
    encode = threading.Barrier(2)
    class ConcurrentEngine(Engine):
        def encode_document(self, text):
            encode.wait(timeout=5)
            return super().encode_document(text)
    def worker(ident):
        with ContextStore(config) as store:
            index = CapabilityIndex(store, config, ConcurrentEngine())
            index._ensure_index()
            begin.wait(timeout=5)
            return index.sync(ident, "ignored")
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(worker, [retired, *fresh])) == ["synchronized"] * 3
    assert reopen_ids(config) == fresh
    with ContextStore(config) as store:
        engine = Engine()
        assert CapabilityIndex(store, config, engine).reconcile()["status"] == "ready"
        assert engine.documents == []


def test_non_posix_clients_can_import_and_keep_lexical_fallback(config, monkeypatch):
    import builtins
    import runpy
    import inspect
    real_import = builtins.__import__
    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("no POSIX file locks")
        return real_import(name, *args, **kwargs)
    with ContextStore(config) as store:
        ident = add(store, "first")
        monkeypatch.setattr(builtins, "__import__", without_fcntl)
        namespace = runpy.run_path(inspect.getfile(CapabilityIndex), run_name="capability_portability_check")
        engine = Engine()
        index = namespace["CapabilityIndex"](store, config, engine)
        assert index.sync(ident, "ignored") == "synchronized"
        assert index.reconcile()["status"] == "unsupported_platform"
        assert [row["memory_id"] for row in index.search("first")] == [ident]
        assert engine.documents == ["first\nbody"]
        assert index.vector_index.is_dirty()
        assert CapabilityStore(store).search("first")


def test_batch_eligibility_audits_once_per_snapshot_and_rechecks_only_committing_ids(config):
    with ContextStore(config) as store:
        ids = [add(store, str(i)) for i in range(5)]
        calls = []
        def eligibility(candidates):
            calls.append(list(candidates))
            return set(candidates) - {ids[0]}
        index = CapabilityIndex(store, config, Engine(), eligible_ids=eligibility)
        assert index.reconcile()["status"] == "ready"
        assert reopen_ids(config) == ids[1:]
        assert sum(set(call) == set(ids) for call in calls) == 2
        assert all(len(call) in {1, 5} for call in calls)
        calls.clear()
        assert len(index.search("query")) == 4
        assert len(calls) == 1
        assert set(calls[0]) == set(ids[1:])


def test_source_eligibility_change_during_encode_is_rechecked_without_memory_edit(config):
    with ContextStore(config) as store:
        ident = add(store, "first")
        allowed = {ident}
        class WithdrawalEngine(Engine):
            def encode_document(self, text):
                allowed.clear()
                return super().encode_document(text)
        index = CapabilityIndex(store, config, WithdrawalEngine(),
                                eligible_ids=lambda ids: set(ids) & allowed)
        assert index.sync(ident, "ignored") == "stale"
        assert index.vector_index.ids() == []
        assert CapabilityStore(store).get(ident)["status"] == "active"
