import threading
import pytest
from tests.test_lan_sharing import lan, add


def test_health_detail_is_independent_of_business_lock(lan, monkeypatch):
    runtime, adapter = lan
    server = runtime.server_for('kane')
    monkeypatch.setattr(server, '_contract_view', lambda: pytest.fail('foreground health scan'))
    done = threading.Event()
    results = []
    with adapter.lock:
        thread = threading.Thread(target=lambda: (results.append(adapter.call_tool('kane', 'memory_status', {'detail': 'health'})), done.set()))
        thread.start()
        assert done.wait(2)
    thread.join()
    assert results[0]['detail'] == 'health'
    assert 'diagnostic' in results[0]
    assert 'ready' not in results[0]


def test_health_snapshot_copy_and_uncommitted_generation(lan):
    runtime, adapter = lan
    manager = runtime._background_health
    manager.close()
    manager = type(manager)(manager.servers, start=False)
    try:
        manager.refresh('kane')
        manager.run_once()
        first = manager.status('kane')
        assert first['hard_gate'] == 'allowed'
        assert first['freshness'] == 'current'
        first['diagnostic']['codes'].append('poison')
        assert 'poison' not in manager.status('kane')['diagnostic']['codes']
        store = runtime.server_for('kane').context_service.store
        with pytest.raises(RuntimeError):
            with store.transaction():
                store._connection().execute('UPDATE context_health_generation SET generation=generation+1')
                assert manager.status('kane')['observed_generation'] == first['observed_generation']
                raise RuntimeError('rollback')
        add(adapter, 'kane')
        stale = manager.status('kane')
        assert stale['hard_gate'] == 'allowed'
        assert stale['freshness'] == 'stale'
        manager._next.clear()  # Advance the deterministic per-namespace retry deadline.
        manager.run_once()
        assert manager.status('kane')['freshness'] == 'current'
    finally:
        manager.close()


def test_blocked_worker_coalesces_and_shutdown_does_not_publish(lan, monkeypatch):
    runtime, adapter = lan
    prior = runtime._background_health
    prior.close()
    manager = type(prior)(prior.servers, start=False)
    entered, release = threading.Event(), threading.Event()
    original = manager._evaluate
    scans = []
    def blocked(name):
        scans.append(name)
        entered.set()
        assert release.wait(3)
        return original(name)
    monkeypatch.setattr(manager, '_evaluate', blocked)
    manager.refresh('kane')
    thread = threading.Thread(target=manager.run_once)
    thread.start()
    assert entered.wait(2)
    try:
        for _ in range(20):
            manager.refresh('kane')
            assert manager.status('kane')['refresh_pending']
        assert adapter.call_tool('kane', 'memory_status', {'detail': 'revision'})['detail'] == 'revision'
        manager.close()
    finally:
        release.set()
        thread.join(3)
    assert scans == ['kane']
    assert manager.status('kane')['hard_gate'] == 'health_closed'
    assert manager.status('kane')['diagnostic'] is None


@pytest.mark.parametrize('change', ['schema', 'config', 'physical'])
def test_hard_boundary_never_repairs_on_request(lan, monkeypatch, change):
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    manager = type(prior)(prior.servers, start=False)
    try:
        manager.refresh('kane')
        manager.run_once()
        assert manager.status('kane')['hard_gate'] == 'allowed'
        service = runtime.server_for('kane').context_service
        if change == 'schema':
            service.store._connection().execute('CREATE TABLE extra_health_test(id)')
        elif change == 'config':
            service.config.embedding_dim += 1
        else:
            manager._snapshots['kane']['physical_codes'] = ('quick_check_failed',)
        monkeypatch.setattr(manager, '_evaluate', lambda *_: pytest.fail('request repair scan'))
        assert manager.status('kane')['hard_gate'] != 'allowed'
    finally:
        manager.close()

def capability_setup(runtime, adapter):
    from evolvmem.capability_store import CapabilityStore, CapabilityMemoryDraft, CapabilitySourceDraft
    from evolvmem.capability_provenance import context_revision
    result = add(adapter, 'kane')
    store = runtime.server_for('kane').context_service.store
    ident = result['context_id']
    CapabilityStore(store).upsert(CapabilityMemoryDraft(category='backend', title='orchard', content='orchard procedure'),
        source=CapabilitySourceDraft(source_kind='context', source_key=f'context:{ident}', source_ref=f'context_item:{ident}',
            context_item_id=ident, source_revision=context_revision(store._connection(), ident)))
    old = runtime._background_health
    old.close()
    runtime._background_health = type(old)(old.servers, start=False)
    runtime._background_health.refresh('kane')
    runtime._background_health.run_once()
    return store, ident


def test_capability_stale_semantics_keeps_candidate_and_never_global_scans(lan, monkeypatch):
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    with store.transaction():
        store._connection().execute('UPDATE context_items SET access_count=access_count+1 WHERE id=?', (ident,))
    monkeypatch.setattr(runtime.server_for('kane'), '_contract_view', lambda: pytest.fail('foreground scan'))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result['count'] == 1, result
    assert result['health']['freshness'] == 'stale'


@pytest.mark.parametrize('sql', [
    "DELETE FROM context_layers WHERE item_id=? AND layer='l0'",
    "DELETE FROM context_layers WHERE item_id=? AND layer='l1'",
    "DELETE FROM context_layers WHERE item_id=? AND layer='l2'",
    "UPDATE context_layers SET generator='corrupt' WHERE item_id=? AND layer='l1'",
    "UPDATE context_items SET tags='{}' WHERE id=?",
    "UPDATE context_items SET expires_at='2000-01-01' WHERE id=?",
    "UPDATE context_items SET status='archived' WHERE id=?",
])
def test_capability_old_good_snapshot_never_authorizes_bad_source(lan, sql):
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    with store.transaction():
        store._connection().execute(sql, (ident,))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result.get('error') or result['count'] == 0, result


def test_capability_withdrawal_after_candidate_scan_is_checked(lan, monkeypatch):
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    server = runtime.server_for('kane')
    original = server.handle_tool_call
    def withdrawn(name, args):
        result = original(name, args)
        if name == 'capability_search':
            assert result['count'] == 1
            with store.transaction():
                store._connection().execute("UPDATE context_items SET status='archived' WHERE id=?", (ident,))
        return result
    monkeypatch.setattr(server, 'handle_tool_call', withdrawn)
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result.get('error') or result['count'] == 0, result

def test_worker_failure_reports_error_and_never_fake_ready(lan, monkeypatch):
    runtime, _ = lan
    old = runtime._background_health
    old.close()
    manager = type(old)(old.servers, start=False)
    try:
        manager.refresh('kane')
        monkeypatch.setattr(manager, '_evaluate', lambda *_: (_ for _ in ()).throw(RuntimeError('PRIVATE BODY /secret')))
        manager.run_once()
        result = manager.status('kane')
        assert result['error'] == 'health_evaluation_failed'
        assert result['diagnostic'] is None
        assert result['hard_gate'] != 'allowed'
        assert 'PRIVATE' not in str(result)
    finally:
        manager.close()


def test_known_resident_physical_failure_revokes_old_good_baseline(lan):
    runtime, adapter = lan
    capability_setup(runtime, adapter)
    service = runtime.server_for('kane').context_service
    assert service._integrity_diagnostics() == ()  # Establish the current schema key.
    old = service._integrity_snapshot
    assert old
    service._integrity_snapshot = (old[0], old[1], ('quick_check_failed',))
    assert runtime._background_health.status('kane')['hard_gate'] == 'health_physical_failed'


def test_model_file_change_revokes_old_good_baseline(lan):
    runtime, adapter = lan
    capability_setup(runtime, adapter)
    service = runtime.server_for('kane').context_service
    service.config.model_path.parent.mkdir(parents=True, exist_ok=True)
    service.config.model_path.write_bytes(b'changed-model')
    assert runtime._background_health.status('kane')['hard_gate'] != 'allowed'


@pytest.mark.parametrize('args', [{'detail': 'health', 'extra': 1}, {'detail': 'health', 'user': 'jiangli'},
                                 {'detail': 'health', 'space': 'both'}, {'detail': 'health', 'ref': 'public:context:1'}])
def test_health_preserves_validation(lan, args):
    _, adapter = lan
    assert 'error' in adapter.call_tool('kane', 'memory_status', args)


def test_health_preserves_user_owner_boundaries(lan):
    _, adapter = lan
    assert adapter.call_tool('other', 'memory_status', {'detail': 'health'})['error'] == 'unauthorized'
    assert adapter.call_tool('kane', 'memory_status', {'detail': 'health'}, owner=True)['error'] == 'owner_forbidden'

def test_evaluator_owns_readonly_snapshot_and_native_handles(lan, monkeypatch):
    from evolvmem.context_service import ContextService
    import sqlite3
    runtime, adapter = lan
    old = runtime._background_health
    old.close()
    manager = type(old)(old.servers, start=False)
    resident = runtime.server_for('kane').context_service
    entered, release = threading.Event(), threading.Event()
    observations = []
    original = ContextService.evaluated_status
    def inspect(service):
        assert service is not resident
        assert service.store._connection() is not resident.store._connection()
        assert service.vector_index is not resident.vector_index
        with pytest.raises(sqlite3.OperationalError):
            service.store._connection().execute('UPDATE context_health_generation SET generation=generation+1')
        generation = service.store._connection().execute('SELECT generation FROM context_health_generation').fetchone()[0]
        entered.set()
        assert release.wait(3)
        assert service.store._connection().execute('SELECT generation FROM context_health_generation').fetchone()[0] == generation
        observations.append(service)
        return original(service)
    monkeypatch.setattr(ContextService, 'evaluated_status', inspect)
    manager.refresh('kane')
    thread = threading.Thread(target=manager.run_once)
    thread.start()
    assert entered.wait(2)
    try:
        with resident.store.transaction():
            resident.store._connection().execute('UPDATE context_health_generation SET generation=generation+1')
        assert manager.status('kane')['diagnostic'] is None
    finally:
        release.set()
        thread.join(3)
    try:
        assert observations
        assert observations[0].vector_index._index is None
        assert manager.status('kane')['freshness'] == 'stale'
        monkeypatch.setattr(ContextService, 'evaluated_status', original)
        manager._next.clear()  # Advance the deterministic per-namespace retry deadline.
        manager.run_once()
        assert manager.status('kane')['freshness'] == 'current'
    finally:
        manager.close()


def test_integrity_expiry_and_database_replacement_fail_closed(lan):
    runtime, adapter = lan
    capability_setup(runtime, adapter)
    manager = runtime._background_health
    manager._snapshots['kane']['physical_checked'] -= 10000
    assert manager.status('kane')['hard_gate'] == 'health_physical_expired'
    path = runtime.server_for('kane').config.db_path
    path.rename(path.with_suffix('.retained'))
    path.write_bytes(b'changed-file')
    assert manager.status('kane')['hard_gate'] == 'health_boundary_unavailable'

def test_direct_adapter_without_hosting_keeps_original_capability_gate(lan, monkeypatch):
    runtime, adapter = lan
    assert not runtime._background_health.active
    seen = []
    server = runtime.server_for('kane')
    original = server._contract_view
    def observed():
        seen.append(True)
        return original()
    monkeypatch.setattr(server, '_contract_view', observed)
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert seen and result['count'] == 0
    assert 'health' not in result


def test_production_http_host_starts_and_closes_diagnostic_worker(lan):
    from evolvmem.lan_server import make_http_server
    runtime, adapter = lan
    manager = runtime._background_health
    assert not manager.active
    server = make_http_server(runtime, host='127.0.0.1', port=0)
    try:
        assert manager.active and manager._worker.is_alive()
        with manager._condition:
            assert manager._condition.wait_for(lambda: 'kane' in manager._snapshots, timeout=5)
        assert adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})['health']['hard_gate'] == 'allowed'
    finally:
        server.server_close()
        runtime.close()
    assert not manager._worker.is_alive()
    assert manager.status('kane')['hard_gate'] == 'health_closed'

def test_health_detail_is_advertised_only_by_lan(lan):
    from evolvmem.mcp_contract import tool_specs
    runtime, adapter = lan
    server = runtime.server_for('kane')
    standalone = next(s for s in tool_specs(adapter=server.adapter, mode=server.context_mode, health=None, catalog=True) if s.name == 'memory_status')
    assert standalone.input_schema['properties']['detail']['enum'] == ['full', 'revision']
    assert adapter._catalog_specs[('kane', False)]['memory_status']['inputSchema']['properties']['detail']['enum'] == ['full', 'revision', 'health']


def test_refresh_requests_cannot_bypass_retry_cadence(lan):
    runtime, _ = lan
    old = runtime._background_health
    old.close()
    manager = type(old)(old.servers, start=False)
    try:
        manager.refresh('kane')
        assert manager.run_once()
        checked = manager.status('kane')['completion_age_seconds']
        for _ in range(20):
            manager.refresh('kane')
            assert manager.run_once() is False
        assert manager.status('kane')['completion_age_seconds'] >= checked
    finally:
        manager.close()
