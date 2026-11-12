"""A full capability audit must never monopolize SQLite's only writer."""
from contextlib import contextmanager
import sqlite3

import pytest

from evolvmem.capability_index import CapabilityIndex
from evolvmem.context_store import ContextStore
from tests.test_capability_index_recovery import Engine, add
from tests.test_capability_provenance import provenance_db, add_memory, unit_source


@pytest.fixture
def prepared(test_config):
    test_config.embedding_dim = 3
    with ContextStore(test_config) as store:
        identity = add(store, 'first')
        conn = store._connection()
        with store.transaction():
            conn.execute('CREATE TABLE source_gate (allowed INTEGER NOT NULL)')
            conn.execute('INSERT INTO source_gate VALUES(1)')
        index = CapabilityIndex(store, test_config, Engine())
        assert index.reconcile()['status'] == 'ready'
        yield store, identity


def external_write(store, sql):
    other = sqlite3.connect(store.config.db_path, timeout=.1)
    try:
        other.execute(sql)
        other.commit()
    finally:
        other.close()


def test_both_whole_library_audits_use_read_snapshot_without_write_transaction(prepared):
    store, identity = prepared
    seen = []
    def eligible(ids):
        seen.append((store._connection().in_transaction, store._transaction_depth,
                     store._connection().execute('PRAGMA query_only').fetchone()[0]))
        return set(ids)
    report = CapabilityIndex(store, store.config, Engine(), eligible_ids=eligible).reconcile()
    assert report['status'] == 'ready'
    assert seen == [(True, 0, 1), (True, 0, 1)]
    assert not store._connection().in_transaction
    assert store._connection().execute('PRAGMA query_only').fetchone()[0] == 0


def test_source_writer_can_commit_during_final_audit_and_snapshot_is_consistent(prepared):
    store, identity = prepared
    calls, observations = [], []
    def eligible(ids):
        calls.append(1)
        if len(calls) == 2:
            external_write(store, 'UPDATE source_gate SET allowed=0')
            observations.append(store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0])
        return set(ids) if store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] else set()
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=eligible)
    report = index.reconcile()
    assert observations == [1]  # The audit retains its original WAL snapshot.
    assert store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] == 0
    assert report['status'] == 'partial'
    assert report['stale'] == 1 and report['remaining'] is None and report['dirty']
    assert index.reconcile()['status'] == 'ready'
    assert index.vector_index.ids() == []


@pytest.mark.parametrize('same_connection', [False, True])
def test_source_change_after_snapshot_before_writer_fence_keeps_dirty(prepared, monkeypatch, same_connection):
    store, identity = prepared
    original = store.transaction
    calls = []
    def eligible(ids):
        calls.append(1)
        return set(ids) if store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] else set()
    @contextmanager
    def changing_transaction():
        if len(calls) == 2:
            if same_connection:
                store._connection().execute('UPDATE source_gate SET allowed=0')
                store._connection().commit()
            else:
                external_write(store, 'UPDATE source_gate SET allowed=0')
        with original():
            yield store
    monkeypatch.setattr(store, 'transaction', changing_transaction)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=eligible)
    report = index.reconcile()
    assert report['status'] == 'partial'
    assert report['stale'] == 1 and report['remaining'] is None and report['dirty']
    assert store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] == 0


def test_eligibility_callback_cannot_write_inside_read_snapshot(prepared):
    store, identity = prepared
    calls = []
    def eligible(ids):
        calls.append(1)
        if len(calls) == 2:
            store._connection().execute('UPDATE source_gate SET allowed=0')
        return set(ids)
    report = CapabilityIndex(store, store.config, Engine(), eligible_ids=eligible).reconcile()
    assert report['status'] == 'failed' and report['dirty']
    assert store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] == 1
    assert not store._connection().in_transaction
    assert store._connection().execute('PRAGMA query_only').fetchone()[0] == 0


def test_existing_caller_transaction_is_not_certified_or_committed(prepared):
    store, identity = prepared
    with store.transaction():
        store._connection().execute('UPDATE source_gate SET allowed=0')
        report = CapabilityIndex(store, store.config, Engine()).reconcile()
        assert report['status'] == 'failed' and report['dirty']
        assert store._connection().in_transaction and store._transaction_depth == 1


def test_whole_library_eligibility_is_not_repeated_in_publication_transaction(prepared):
    store, identity = prepared
    calls = []
    def eligible(ids):
        assert store._transaction_depth == 0
        calls.append(tuple(ids))
        return set(ids)
    report = CapabilityIndex(store, store.config, Engine(), eligible_ids=eligible).reconcile()
    assert report['status'] == 'ready'
    assert calls == [(identity,), (identity,)]


def test_embedding_contract_changed_after_snapshot_cannot_clear_dirty(prepared, monkeypatch):
    store, identity = prepared
    original = store.transaction
    @contextmanager
    def changing_transaction():
        store.config.embedding_doc_prefix += 'changed'
        with original():
            yield store
    monkeypatch.setattr(store, 'transaction', changing_transaction)
    report = CapabilityIndex(store, store.config, Engine()).reconcile()
    assert report['status'] != 'ready' and report['dirty']


@pytest.mark.parametrize('sql,still_eligible', [
    ("UPDATE session_archives SET state='purged' WHERE id=1", False),
    ("UPDATE conversation_history SET body='changed original' WHERE archive_id=1", False),
    ("UPDATE organization_tasks SET status='superseded' WHERE id=1", False),
    ("UPDATE organization_units SET disposition='set_aside' WHERE task_id=1", False),
    ("UPDATE organization_units SET project='beta',projects='[\"beta\"]',revision=revision+1 WHERE task_id=1", True),
])
def test_real_source_or_manual_change_during_audit_never_certifies_old_view(provenance_db, sql, still_eligible):
    from evolvmem.capability_recall_eligibility import eligible_ids
    from evolvmem.capability_store import CapabilityStore
    store = provenance_db
    store.config.embedding_dim = 3
    identity = add_memory(store, source=unit_source(store))
    assert eligible_ids(store, [identity]) == {identity}
    assert CapabilityIndex(store, store.config, Engine()).reconcile()['status'] == 'ready'
    calls = []
    def gate(ids):
        calls.append(1)
        if len(calls) == 2:
            external_write(store, sql)
        return eligible_ids(store, ids)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    report = index.reconcile()
    assert report['status'] == 'partial' and report['stale'] == 1 and report['dirty']
    assert CapabilityStore(store).get(identity)['status'] == 'active'
    assert index.reconcile()['status'] == 'ready'
    assert index.vector_index.ids() == ([identity] if still_eligible else [])


@pytest.mark.parametrize('field', ['expires_at', 'effective_from', 'effective_until'])
def test_source_time_boundary_after_snapshot_without_db_write_keeps_dirty(provenance_db, monkeypatch, field):
    from evolvmem import context_store
    from evolvmem.capability_recall_eligibility import eligible_ids
    from tests.test_capability_provenance import context_source
    store = provenance_db
    store.config.embedding_dim = 3
    clock = ['2029-01-01T00:00:00+00:00']
    monkeypatch.setattr(context_store, '_now_iso', lambda: clock[0])
    with store.transaction():
        store._connection().execute('UPDATE context_items SET ' + field + "='2030-01-01T00:00:00+00:00' WHERE id=1")
    identity = add_memory(store, source=context_source(store))
    assert CapabilityIndex(store, store.config, Engine(), eligible_ids=lambda ids: eligible_ids(store, ids)).reconcile()['status'] == 'ready'
    calls = []
    def gate(ids):
        calls.append(1)
        return eligible_ids(store, ids)
    original = store.transaction
    @contextmanager
    def cross_boundary():
        if len(calls) == 2:
            clock[0] = '2031-01-01T00:00:00+00:00'
        with original():
            yield store
    monkeypatch.setattr(store, 'transaction', cross_boundary)
    before = store._connection().total_changes
    report = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate).reconcile()
    assert report['status'] == 'partial' and report['stale'] == 1 and report['dirty']
    assert store._connection().total_changes == before



def test_audit_cannot_cross_both_edges_of_short_effective_window(provenance_db, monkeypatch):
    from evolvmem import context_store
    from evolvmem.capability_recall_eligibility import eligible_ids
    from tests.test_capability_provenance import context_source
    store = provenance_db
    store.config.embedding_dim = 3
    clock = ['2029-01-01T00:00:00+00:00']
    monkeypatch.setattr(context_store, '_now_iso', lambda: clock[0])
    with store.transaction():
        store._connection().execute("UPDATE context_items SET effective_from='2030-01-01T00:00:00+00:00',effective_until='2031-01-01T00:00:00+00:00' WHERE id=1")
    identity = add_memory(store, source=context_source(store))
    # Preserve a real matching vector so a middle-window audit could say ready.
    assert CapabilityIndex(store, store.config, Engine()).reconcile()['status'] == 'ready'
    index = CapabilityIndex(store, store.config, Engine())
    index._ensure_index(reload=True)
    def gate(ids):
        clock[0] = '2030-06-01T00:00:00+00:00'
        result = eligible_ids(store, ids)
        assert result == {identity}
        clock[0] = '2032-01-01T00:00:00+00:00'
        return result
    index.eligible_ids = gate
    with pytest.raises(RuntimeError, match='eligibility_snapshot_changed'):
        index._read_audit(index._load_proofs())


def test_initial_snapshot_drift_does_not_starve_safe_individual_progress(prepared):
    store, identity = prepared
    calls = []
    def gate(ids):
        calls.append(1)
        if len(calls) == 1:
            external_write(store, 'UPDATE source_gate SET allowed=0')
        return set(ids) if store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] else set()
    # A mismatching vector is selected from the read snapshot. Its current
    # source is still rechecked individually before any removal/publication.
    with store.transaction():
        store._connection().execute("UPDATE capability_memories SET content='changed',revision=revision+1 WHERE memory_id=?", (identity,))
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    report = index.reconcile()
    assert report['processed'] == 1 and report['removed'] == 1
    assert report['status'] == 'ready' and index.vector_index.ids() == []
