"""Real LAN assembly/SQLite/algorithm; run in root's isolated native source.

No AST loaders or model substitutions here; reuse existing synthetic LAN
fixtures and actual final selected proof / snapshot implementations.
"""
import importlib
import importlib.util
import sqlite3
import pytest

from tests.test_lan_sharing import lan, add, publish
from tests.test_experience_background_admission import baseline, seed, recall


def current_inputs(store):
    name = 'evolvmem.context_retrieval_inputs'
    if importlib.util.find_spec(name) is None:
        return None  # Baseline RED measures missing scoped behavior.
    return importlib.import_module(name).current_inputs(store)


def test_native_lan_personal_public_inputs_retire_before_all_final_validation(lan, tmp_path, monkeypatch):
    from evolvmem.experience_service import ExperienceService
    from evolvmem import lan_experience_reads as reads
    from evolvmem.lan_experience_snapshot import ExperienceSnapshot
    runtime, adapter = lan
    seed(runtime, tmp_path)
    publish(adapter, add(adapter), title='orchard pagination', summary='shared orchard pagination instructions')
    baseline(runtime, 'kane', 'public')
    stores, scopes, events = [], [], []
    original_recall, original_share = ExperienceService.recall, adapter.sharing.search
    original_proof, original_validate = reads.SelectedExperienceProof, ExperienceSnapshot.validate

    def observe(store, event):
        active = current_inputs(store)
        assert active is not None, 'initial native retrieval lacks input scope'
        assert store._connection().in_transaction
        stores.append(store); scopes.append(active); events.append(event)

    def personal(service, *args, **kwargs):
        observe(service.store, 'personal')
        return original_recall(service, *args, **kwargs)

    def public(*args, **kwargs):
        observe(kwargs['service'].store, 'public')
        return original_share(*args, **kwargs)

    def no_inputs(event):
        assert len(stores) == 2
        assert all(current_inputs(store) is None for store in stores)
        assert all(not scope.active and not scope.cache for scope in scopes)
        events.append(event)

    def proof(*args, **kwargs):
        no_inputs('selected_proof')
        return original_proof(*args, **kwargs)

    def validate(snapshot, *args, **kwargs):
        no_inputs('final_validate')
        return original_validate(snapshot, *args, **kwargs)

    monkeypatch.setattr(ExperienceService, 'recall', personal)
    monkeypatch.setattr(adapter.sharing, 'search', public)
    monkeypatch.setattr(reads, 'SelectedExperienceProof', proof)
    monkeypatch.setattr(ExperienceSnapshot, 'validate', validate)
    result = recall(adapter, 'both', constraints={'size': 'large'})
    assert 'error' not in result, result
    assert result['results'] and result['shared_knowledge']
    assert scopes[0] is not scopes[1]
    assert events[:2] == ['personal', 'public']
    assert events.count('selected_proof') == 2 and events.count('final_validate') == 4


def test_native_lan_handler_failure_retires_inputs_connections_and_permit(lan, monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime, adapter = lan
    baseline(runtime, 'kane')
    observed = []

    def fail(service, *args, **kwargs):
        active = current_inputs(service.store)
        assert active is not None, 'initial native retrieval lacks input scope'
        observed.append((active, service.store._connection()))
        raise RuntimeError('synthetic_handler_failure')

    monkeypatch.setattr(ExperienceService, 'recall', fail)
    result = recall(adapter)
    assert 'error' in result and 'results' not in result
    assert len(observed) == 1
    scope, conn = observed[0]
    assert not scope.active and scope.cache == {}
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    slots = adapter._experience_reader._slots
    acquired = 0
    try:
        for _ in range(2):
            assert slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            slots.release()
