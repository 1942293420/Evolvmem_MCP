"""Real file locks, native vectors and WAL snapshots under competing work."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
import fcntl
import json
import logging
import threading

import numpy as np
import pytest

from evolvmem.capability_index import CapabilityIndex
from evolvmem.context_store import ContextStore
from tests.test_capability_audit_lock import prepared, external_write
from tests.test_capability_index_recovery import Engine, config, add, update, reopen_ids


def lock_available(path):
    with path.open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


@pytest.mark.parametrize('operation,expected_calls', [('reconcile', 2), ('sync', 2), ('search', 1)])
def test_heavy_proofs_hold_neither_file_lock_nor_sqlite_writer(prepared, operation, expected_calls):
    store, identity = prepared
    observations = []
    path = store.config.capability_vector_path
    def gate(ids):
        observations.append((lock_available(path.with_suffix('.recovery.lock')),
                             lock_available(path.with_suffix(path.suffix + '.lock')),
                             store._transaction_depth,
                             store._connection().in_transaction,
                             store._connection().execute('PRAGMA query_only').fetchone()[0]))
        return set(ids)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    if operation == 'reconcile':
        assert index.reconcile()['status'] == 'ready'
    elif operation == 'sync':
        assert index.sync(identity, 'ignored') == 'synchronized'
    else:
        assert [r['memory_id'] for r in index.search('query')] == [identity]
    assert observations == [(True, True, 0, True, 1)] * expected_calls


@pytest.mark.parametrize('pause_at', [1, 2])
def test_query_finishes_while_maintenance_proof_is_still_blocked(config, pause_at):
    with ContextStore(config) as store:
        identity = add(store, 'first')
        assert CapabilityIndex(store, config, Engine()).reconcile()['status'] == 'ready'
    initialized = threading.Barrier(3)
    entered, release, queried = threading.Event(), threading.Event(), threading.Event()
    def maintenance():
        with ContextStore(config) as store:
            calls = 0
            def gate(ids):
                nonlocal calls
                calls += 1
                if calls == pause_at:
                    entered.set()
                    assert release.wait(5)
                return set(ids)
            index = CapabilityIndex(store, config, Engine(), eligible_ids=gate)
            index._ensure_index()
            initialized.wait(5)
            return index.reconcile()
    def reader():
        with ContextStore(config) as store:
            index = CapabilityIndex(store, config, Engine())
            index._ensure_index()
            initialized.wait(5)
            assert entered.wait(5)
            result = index.search('query')
            queried.set()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        maint, query = pool.submit(maintenance), pool.submit(reader)
        initialized.wait(5)
        assert entered.wait(5)
        try:
            completed_before_release = queried.wait(2)
        finally:
            release.set()
        rows, report = query.result(5), maint.result(5)
    assert completed_before_release, 'query waited for the maintenance proof'
    assert [row['memory_id'] for row in rows] == [identity]
    assert report['status'] == 'ready'


@pytest.mark.parametrize('change', ['vector', 'proof'])
def test_final_audit_cannot_certify_replaced_files(prepared, change):
    store, identity = prepared
    calls = 0
    competitor = CapabilityIndex(store, store.config, Engine())
    competitor._ensure_index()
    def gate(ids):
        nonlocal calls
        calls += 1
        if calls == 2:
            # This writer changes only index files, never SQLite. A DB-only
            # fence cannot detect this race. It must finish before we return.
            if change == 'vector':
                competitor.vector_index.remove(identity)
                competitor.vector_index.add(identity, np.array([0., 0., 1.]))
                competitor.vector_index.preserve_dirty()
                competitor.vector_index.save()
            else:
                proof = json.loads(competitor._proof_path.read_text())
                proof['generation'] += 1
                competitor._proof_path.write_text(json.dumps(proof))
        return set(ids)
    # Avoid deadlocking the old implementation's nested shared-cache lock.
    if change == 'vector':
        observations = []
        original_gate = gate
        def gate(ids):
            if not lock_available(store.config.capability_vector_path.with_suffix('.usearch.lock')):
                observations.append('blocked')
                return set(ids)
            return original_gate(ids)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    report = index.reconcile()
    assert report['status'] == 'partial' and report['dirty']
    assert report['stale'] == 1


@pytest.mark.parametrize('own_connection', [False, True])
def test_sync_does_not_publish_after_source_change_between_proof_and_writer(prepared, monkeypatch, own_connection):
    store, identity = prepared
    calls = []
    def gate(ids):
        calls.append(1)
        return set(ids) if store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] else set()
    original = store.transaction
    @contextmanager
    def change_before_writer():
        if len(calls) == 2:
            if own_connection:
                store._connection().execute('UPDATE source_gate SET allowed=0')
                store._connection().rollback()  # Same-value rollback still invalidates old proof.
            else:
                external_write(store, 'UPDATE source_gate SET allowed=0')
        with original():
            yield store
    monkeypatch.setattr(store, 'transaction', change_before_writer)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    before = store.config.capability_vector_path.read_bytes()
    assert index.sync(identity, 'ignored') == 'stale'
    assert store.config.capability_vector_path.read_bytes() == before
    assert index.vector_index.is_dirty()


def test_search_keeps_frozen_vectors_and_proofs_during_a_competing_save(prepared):
    store, identity = prepared
    competitor = CapabilityIndex(store, store.config, Engine())
    competitor._ensure_index()
    def gate(ids):
        competitor.vector_index.remove(identity)
        competitor.vector_index.add(identity, np.array([0., 0., 1.]))
        competitor.vector_index.preserve_dirty()
        competitor.vector_index.save()  # Deliberately leave old proof on disk.
        return set(ids)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    rows = index.search('query')
    assert [row['memory_id'] for row in rows] == [identity]
    # A later query sees the changed image and refuses its mismatched proof.
    assert CapabilityIndex(store, store.config, Engine()).search('query') == []


def test_query_proof_cannot_write_and_failure_restores_transaction_and_locks(prepared):
    store, _ = prepared
    def gate(ids):
        store._connection().execute('UPDATE source_gate SET allowed=0')
        return set(ids)
    index = CapabilityIndex(store, store.config, Engine(), eligible_ids=gate)
    assert index.search('query') == []
    assert store._connection().execute('SELECT allowed FROM source_gate').fetchone()[0] == 1
    assert not store._connection().in_transaction
    assert store._connection().execute('PRAGMA query_only').fetchone()[0] == 0
    assert lock_available(index.vector_index.path.with_suffix('.recovery.lock'))
    assert lock_available(index.vector_index.path.with_suffix('.usearch.lock'))


def test_background_recovery_emits_content_free_audit_and_lock_durations(prepared, caplog, monkeypatch):
    from evolvmem import request_trace
    store, _ = prepared
    monkeypatch.setattr(request_trace, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        report = CapabilityIndex(store, store.config, Engine()).reconcile()
    assert report['status'] == 'ready'
    rows = [json.loads(row.message.split('request_trace ', 1)[1]) for row in caplog.records
            if 'request_trace ' in row.message]
    assert len(rows) == 1 and rows[0]['tool'] == 'capability_reconcile'
    assert {'capability_recovery_initial_audit', 'capability_recovery_final_audit',
            'capability_lock_wait', 'capability_lock_held'} <= rows[0]['stages_ms'].keys()
    assert str(store.config.data_dir) not in caplog.text


@pytest.mark.parametrize('operation', ['sync', 'reconcile'])
def test_borrowed_writer_is_rejected_before_waiting_for_file_gate(prepared, operation):
    source, identity = prepared
    initialized, begin = threading.Event(), threading.Event()
    def worker():
        with ContextStore(source.config) as store:
            index = CapabilityIndex(store, store.config, Engine())
            initialized.set()
            assert begin.wait(5)
            with store.transaction():
                result = index.sync(identity, 'ignored') if operation == 'sync' else index.reconcile()
                assert store._connection().in_transaction
                assert store._transaction_depth == 1
            return result
    blocked = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker)
        assert initialized.wait(5)
        with source.config.capability_vector_path.with_suffix('.recovery.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            begin.set()
            try:
                task.result(1)
            except TimeoutError:
                blocked = True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        result = task.result(5)
    assert not blocked, 'borrowed SQLite writer waited for recovery gate'
    assert (result if operation == 'sync' else result['status']) == 'failed'
