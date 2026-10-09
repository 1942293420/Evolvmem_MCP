"""Real-cache regressions for clean-marker drift and truthful vector readiness."""
import numpy as np
import pytest

from evolvmem.context_models import ContextMatchType, ContextMode, ContextSearchRequest
from evolvmem.recall_recovery import ContextVectorRecoveryLoop, recover_context_vector
from evolvmem.unit_derivations import record_derivation
from tests.test_history_qa_memory import service as base_service
from tests.test_recall_recovery import _add_task, _add_unit, _make_store_item


def test_partial_search_expands_past_unusable_nearest_neighbor(service):
    from evolvmem import vector_provenance
    kept, changing = seed(service)
    vector = np.zeros(service.config.embedding_dim, dtype=np.float32)
    vector[:2] = [0.6, 0.8]
    service.vector_index.remove(kept.id)
    service.vector_index.add(kept.id, vector)
    service.vector_index.save()
    vector_provenance.record(service.config, kept.id, 'kept original constraint', vector)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_items SET expires_at='2000-01-01 00:00:00' WHERE id=?", (changing.id,))
    hits = service.retriever._vector_candidates('unrelated paraphrase', 1)
    assert [h['id'] for h in hits] == [kept.id]


def test_dirty_cache_without_provenance_cannot_offer_partial_vectors(service):
    from evolvmem import vector_provenance
    seed(service)
    vector_provenance.path(service.config).unlink()
    service.vector_index.mark_dirty()
    assert service.status().context_vector_ready is False
    assert service.retriever._vector_candidates('unrelated paraphrase', 5) == []


@pytest.mark.parametrize('drift', ['missing', 'changed', 'unproved_vector'])
def test_pending_updates_keep_only_proven_unchanged_vectors_searchable(service, drift):
    kept, changing = seed(service)
    if drift == 'missing':
        service.vector_index.remove(changing.id)
        service.vector_index.save()
    elif drift == 'changed':
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE context_layers SET content='changed text' WHERE item_id=? AND layer='l0'", (changing.id,))
    else:
        service.vector_index.remove(changing.id)
        vector = np.zeros(service.config.embedding_dim, dtype=np.float32)
        vector[1] = 1.0
        service.vector_index.add(changing.id, vector)
        service.vector_index.save()
    service.vector_index.mark_dirty()
    service.vector_index.preserve_dirty()

    assert service.status().context_vector_ready is True
    assert service.status().context_vector_dirty is True
    assert 'context_vector_partial' in service.status().reason_codes
    hits = service.search(ContextSearchRequest(query='unrelated paraphrase', project='evo'))
    assert [h.id for h in hits] == [kept.id]
    assert hits[0].match_types == (ContextMatchType.VECTOR,)
    assert ContextVectorRecoveryLoop().tick([service])[0].status == 'recovered'
    assert service.status().context_vector_dirty is False
    assert 'context_vector_partial' not in service.status().reason_codes


@pytest.mark.parametrize('disposition', ['history_only', 'keep'])
def test_history_provenance_does_not_leave_ineligible_vectors_disabling_recall(service, disposition):
    from evolvmem.unit_extraction import write_history
    kept, changing = seed(service)
    task_id = _add_task(service, status='completed')
    _add_unit(service, task_id, None, ordinal=0)
    text = '页面必须保留查询条件和未保存的草稿，不能切换页面后丢失。'
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE organization_units SET disposition=?,text=?,cleaned_text=? WHERE task_id=?',
            (disposition, text, text, task_id))
    task = dict(service.store._connection().execute('SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone())
    unit = dict(service.store._connection().execute('SELECT * FROM organization_units WHERE task_id=?', (task_id,)).fetchone())

    item_id = write_history(service, task, unit, [{'role': 'user', 'content': text}])

    assert item_id is not None
    assert service.store.get_item(item_id).status.value == 'active', 'retain the original history'
    expected = {kept.id, changing.id} | ({item_id} if disposition == 'keep' else set())
    assert set(service.vector_index.ids()) == expected
    assert service.status().context_vector_ready is True
    assert service.retriever._vector_available() is True


def test_one_new_vector_cannot_certify_remaining_vectors_from_an_old_contract(service):
    import json
    from evolvmem import vector_provenance
    kept, _ = seed(service)
    proof_path = vector_provenance.path(service.config)
    payload = json.loads(proof_path.read_text())
    payload['contract'] = payload['contract'][:3]
    proof_path.write_text(json.dumps(payload))
    vector_provenance.record(service.config, kept.id, 'kept original constraint',
                             service.embedding_engine.encode_query('kept'))
    assert service.status().context_vector_ready is False
    assert vector_provenance.load(service.config) == {}


@pytest.mark.parametrize('fail', [False, True])
def test_old_encoder_contract_is_unavailable_and_reencoded(service, monkeypatch, fail):
    import json
    from evolvmem import vector_provenance
    kept, changing = seed(service)
    proof_path = vector_provenance.path(service.config)
    payload = json.loads(proof_path.read_text())
    payload['contract'] = [service.config.embedding_model_filename,
                           service.config.embedding_dim, service.config.embedding_doc_prefix]
    proof_path.write_text(json.dumps(payload))
    before = service.config.context_vector_path.read_bytes()
    assert not service.vector_index.is_dirty()
    assert service.status().context_vector_ready is False
    assert service.retriever._vector_available() is False
    if fail:
        def unavailable(text):
            raise RuntimeError('synthetic encoder failure')
        monkeypatch.setattr(service.embedding_engine, 'encode_document', unavailable)
    reports = ContextVectorRecoveryLoop().tick([service])
    assert [r.status for r in reports] == [('failed' if fail else 'recovered')]
    if fail:
        assert service.config.context_vector_path.read_bytes() == before
        assert service.vector_index.is_dirty()
        assert not service.status().context_vector_ready
    else:
        assert sorted(service.embedding_engine.documents) == ['changing source constraint', 'kept original constraint']
        assert service.status().context_vector_ready
        assert vector_provenance.load(service.config)


class Engine:
    is_loaded = True

    def __init__(self, dim):
        self.dim = dim
        self.documents = []

    def encode_document(self, text):
        self.documents.append(text)
        return self.encode_query(text)

    def encode_query(self, text):
        return np.array([1.0] + [0.0] * (self.dim - 1), dtype=np.float32)


@pytest.fixture
def service(base_service):
    base_service.config.context_vectors_required = False
    engine = Engine(base_service.config.embedding_dim)
    base_service.embedding_engine = engine
    base_service.retriever.embedding_engine = engine
    base_service.vector_index.initialize(dim=engine.dim)
    base_service._mode = ContextMode.PRIMARY
    base_service._refresh_health()
    return base_service


def seed(service):
    kept = _make_store_item(service.config, service.store, identity_key='kept',
                            l0='kept original constraint', project='evo')
    changing = _make_store_item(service.config, service.store, identity_key='changing',
                                l0='changing source constraint', project='evo')
    report = recover_context_vector(service.config, service.store, service.vector_index,
                                    service.embedding_engine, force=True)
    assert report.status == 'recovered'
    service.embedding_engine.documents.clear()
    return kept, changing


@pytest.mark.parametrize('cause', ['expired', 'source_withdrawn'])
def test_clean_extra_is_reported_then_automatically_removed_without_reencoding(service, cause):
    kept, changing = seed(service)
    with service.store.transaction():
        conn = service.store._connection()
        if cause == 'expired':
            conn.execute("UPDATE context_items SET expires_at='2000-01-01 00:00:00' WHERE id=?",
                         (changing.id,))
        else:
            task_id = _add_task(service, status='superseded')
            _add_unit(service, task_id, changing.id, ordinal=0)
            record_derivation(service, task_id, 'synthetic-digest', changing.id, project='evo')

    assert service.vector_index.is_dirty() is False
    assert service.status().context_vector_ready is True
    assert 'context_vector_partial' in service.status().reason_codes
    assert service.status().ready is True, 'optional vectors must not disable lexical reads'
    assert service.retriever._vector_available() is True
    assert [h.id for h in service.search(ContextSearchRequest(query='unrelated paraphrase', project='evo'))] == [kept.id]
    assert kept.id in {r.id for r in service.search(ContextSearchRequest(query='kept', project='evo'))}

    reports = ContextVectorRecoveryLoop().tick([service])

    assert [r.status for r in reports] == ['recovered']
    assert service.vector_index.ids() == [kept.id]
    assert service.embedding_engine.documents == [], 'unchanged vectors must be reused'
    assert service.status().context_vector_ready is True
    hits = service.search(ContextSearchRequest(query='unrelated paraphrase', project='evo'))
    assert [r.id for r in hits] == [kept.id]
    assert hits[0].match_types == (ContextMatchType.VECTOR,)
    assert service.store.get_item(changing.id).status.value == 'active', 'repair changes only the cache'


def test_equal_counts_do_not_hide_a_missing_id_and_an_extra_id(service):
    kept, changing = seed(service)
    index = service.vector_index
    index.remove(changing.id)
    index.add(999999, service.embedding_engine.encode_query('extra'))
    index.save()
    assert index.count() == 2 and not index.is_dirty()

    assert service.retriever._vector_available() is True
    assert service.status().context_vector_ready is True
    assert 'context_vector_partial' in service.status().reason_codes
    assert [h.id for h in service.search(ContextSearchRequest(query='unrelated paraphrase', project='evo'))] == [kept.id]
    reports = ContextVectorRecoveryLoop().tick([service])

    assert [r.status for r in reports] == ['recovered']
    assert index.ids() == [kept.id, changing.id]
    assert service.embedding_engine.documents == ['changing source constraint']
    assert service.status().context_vector_ready is True


def test_clean_l0_change_is_detected_and_only_changed_document_is_encoded(service):
    kept, changing = seed(service)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_layers SET content='updated constraint' WHERE item_id=? AND layer='l0'",
            (changing.id,))

    assert not service.vector_index.is_dirty()
    assert service.retriever._vector_available() is True
    assert service.status().context_vector_ready is True
    assert 'context_vector_partial' in service.status().reason_codes
    assert [h.id for h in service.search(ContextSearchRequest(query='unrelated paraphrase', project='evo'))] == [kept.id]
    reports = ContextVectorRecoveryLoop().tick([service])

    assert [r.status for r in reports] == ['recovered']
    assert service.embedding_engine.documents == ['updated constraint']
    assert service.vector_index.ids() == [kept.id, changing.id]
    assert service.status().context_vector_ready is True


def test_unloaded_encoder_cannot_report_vector_ready_but_lexical_still_works(service):
    kept, _ = seed(service)
    service.embedding_engine.is_loaded = False

    assert service.status().context_vector_ready is False
    assert service.retriever._vector_available() is False
    results = service.search(ContextSearchRequest(query='kept', project='evo'))
    assert [r.id for r in results] == [kept.id]
    assert results[0].match_types == (ContextMatchType.LEXICAL,)


def test_failed_recovery_of_clean_drift_keeps_previous_cache_and_retry_marker(service, monkeypatch):
    kept, changing = seed(service)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_layers SET content='updated constraint' WHERE item_id=? AND layer='l0'",
            (changing.id,))
    before = service.config.context_vector_path.read_bytes()

    def unavailable(text):
        raise RuntimeError('synthetic encoder failure')

    monkeypatch.setattr(service.embedding_engine, 'encode_document', unavailable)
    reports = ContextVectorRecoveryLoop().tick([service])

    assert [r.status for r in reports] == ['failed']
    assert service.config.context_vector_path.read_bytes() == before
    assert service.vector_index.is_dirty() is True
    assert service.status().context_vector_ready is True
    assert 'context_vector_partial' in service.status().reason_codes


@pytest.mark.parametrize('fail', [False, True])
def test_staging_releases_its_unique_lock_files(service, monkeypatch, fail):
    service.config.lan_shared_vector_cache = True
    seed(service)
    # Ignore setup's old leak; inspect only artifacts created by this attempt.
    before = set(service.config.data_dir.glob('context_vectors.usearch.stage-*.lock'))
    _make_store_item(service.config, service.store, identity_key='next', l0='next constraint', project='evo')
    if fail:
        def unavailable(text):
            raise RuntimeError('synthetic failure')
        monkeypatch.setattr(service.embedding_engine, 'encode_document', unavailable)
    report = recover_context_vector(service.config, service.store, service.vector_index,
                                    service.embedding_engine, force=True)
    assert report.status == ('failed' if fail else 'recovered')
    assert set(service.config.data_dir.glob('context_vectors.usearch.stage-*.lock')) == before
