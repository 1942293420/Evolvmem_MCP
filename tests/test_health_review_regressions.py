from contextlib import closing
"""Counterexamples from the independent Task 2B review, using synthetic data."""
import hashlib
import http.client
import json
from types import SimpleNamespace
import pytest
from tests.test_lan_sharing import lan, add
from tests.test_lan_server import http_lan
from tests.test_background_health import capability_setup


@pytest.mark.parametrize('layer', ['l0', 'l1', 'l2'])
@pytest.mark.parametrize('rehash,rebind', [(False, False), (True, False), (True, True)])
def test_current_source_body_and_projection_must_match(lan, layer, rehash, rebind):
    from evolvmem.capability_provenance import context_revision
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    body = 'withdrawn and replaced source body'
    with store.transaction():
        store._connection().execute('UPDATE context_layers SET content=? WHERE item_id=? AND layer=?', (body, ident, layer))
        if rehash:
            store._connection().execute('UPDATE context_layers SET content_hash=? WHERE item_id=? AND layer=?',
                (hashlib.sha256(body.encode()).hexdigest(), ident, layer))
        if rebind:
            store._connection().execute('UPDATE capability_sources SET source_revision=? WHERE context_item_id=?',
                (context_revision(store._connection(), ident), ident))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result.get('error') or result.get('count') == 0, result


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'orphan', 'legacy_value', 'legacy_status', 'supersession'])
def test_selected_mapping_and_projection_cannot_borrow_old_good_health(lan, damage):
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    conn = store._connection()
    legacy_id = conn.execute('SELECT legacy_memory_id FROM legacy_memory_migrations WHERE context_item_id=?', (ident,)).fetchone()[0]
    if damage == 'supersession':
        second = add(adapter, 'kane', 'second source', 'project:lan:fact:second', 'second')
        other = conn.execute('SELECT legacy_memory_id FROM legacy_memory_migrations WHERE context_item_id=?', (second['context_id'],)).fetchone()[0]
    with store.transaction():
        if damage == 'missing':
            conn.execute('DELETE FROM legacy_memory_migrations WHERE context_item_id=?', (ident,))
        elif damage == 'duplicate':
            conn.execute("INSERT INTO legacy_memory_migrations VALUES(999,?,'now')", (ident,))
        elif damage == 'orphan':
            conn.execute('UPDATE legacy_memory_migrations SET legacy_memory_id=999 WHERE context_item_id=?', (ident,))
        elif damage == 'legacy_value':
            conn.execute("UPDATE memories SET value='new current source' WHERE id=?", (legacy_id,))
        elif damage == 'legacy_status':
            conn.execute("UPDATE memories SET status='archived' WHERE id=?", (legacy_id,))
        else:
            conn.execute('UPDATE memories SET supersedes=? WHERE id=?', (other, legacy_id))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result.get('error') or result.get('count') == 0, result


def test_slow_successful_scans_do_not_starve_any_namespace(lan, monkeypatch):
    import evolvmem.background_health as module
    runtime, _ = lan
    old = runtime._background_health
    old.close()
    manager = type(old)(old.servers, start=False)
    clock, scans = [100.0], []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def scan(name):
        scans.append(name)
        clock[0] += 40.0
        if len(scans) == 9:
            manager.close()
        return {'completed': clock[0]}
    monkeypatch.setattr(manager, '_evaluate', scan)
    manager._loop()
    assert set(scans[:3]) == set(manager.servers), scans
    assert all(scans.count(name) == 3 for name in manager.servers), scans


@pytest.mark.parametrize('endpoint', ['/mcp', '/owner/mcp'])
@pytest.mark.parametrize('state', ['success', 'pending', 'failed'])
def test_health_http_reports_diagnostics_as_successful_tool_result(http_lan, monkeypatch, endpoint, state):
    server, runtime = http_lan
    old = runtime._background_health
    old.close()
    manager = runtime._background_health = type(old)(old.servers, start=False)
    if state != 'pending':
        if state == 'failed':
            monkeypatch.setattr(manager, '_evaluate', lambda *_: (_ for _ in ()).throw(RuntimeError('private')))
        manager.refresh('jiangli')
        manager.run_once()
    def call(arguments):
        with closing(http.client.HTTPConnection(*server.server_address, timeout=5)) as conn:
            conn.request('POST', endpoint, body=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'memory_status', 'arguments': arguments}}),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer jiangli-token'})
            return json.loads(conn.getresponse().read())['result']
    response = call({'detail': 'health'})
    assert response['isError'] is False, response
    payload = json.loads(response['content'][0]['text'])
    assert payload['diagnostic_error'] == ('health_evaluation_failed' if state == 'failed' else None)
    assert 'error' not in payload
    assert call({'detail': 'health', 'extra': True})['isError'] is True

def test_consistent_body_hash_source_and_projection_update_remains_usable(lan, monkeypatch):
    from evolvmem.context_migration import LegacyMemoryMigrator
    from evolvmem.capability_provenance import context_revision
    runtime, adapter = lan
    store, ident = capability_setup(runtime, adapter)
    conn = store._connection()
    legacy_id = conn.execute('SELECT legacy_memory_id FROM legacy_memory_migrations WHERE context_item_id=?', (ident,)).fetchone()[0]
    new_body = 'updated orchard source'
    with store.transaction():
        conn.execute('UPDATE memories SET value=? WHERE id=?', (new_body, legacy_id))
        legacy = dict(conn.execute('SELECT * FROM memories WHERE id=?', (legacy_id,)).fetchone())
        migrator = LegacyMemoryMigrator(store, runtime.server_for('kane').config)
        layers = migrator.layers_for(new_body, migrator.content_type_for(legacy))
        for layer, body in [('l0', layers.l0), ('l1', layers.l1), ('l2', new_body)]:
            conn.execute('UPDATE context_layers SET content=?,content_hash=? WHERE item_id=? AND layer=?',
                (body, hashlib.sha256(body.encode()).hexdigest(), ident, layer))
        conn.execute('UPDATE capability_sources SET source_revision=? WHERE context_item_id=?',
            (context_revision(conn, ident), ident))
    monkeypatch.setattr(store, 'iter_legacy_rows', lambda: pytest.fail('all-library projection scan'))
    monkeypatch.setattr(runtime.server_for('kane').context_service, 'evaluated_status', lambda: pytest.fail('all-library diagnosis'))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'orchard'})
    assert result['count'] == 1, result


def test_native_context_source_does_not_require_legacy_mapping(lan):
    from tests.test_context_retriever import add_item
    from evolvmem.capability_store import CapabilityStore, CapabilityMemoryDraft, CapabilitySourceDraft
    from evolvmem.capability_provenance import context_revision
    runtime, adapter = lan
    store, _ = capability_setup(runtime, adapter)
    native = add_item(store, 'native-source', project='', l0='native source')
    # Native knowledge edits also use this generator; it is not a mapping proof.
    with store.transaction():
        store._connection().execute("UPDATE context_layers SET generator='migrated' WHERE item_id=?", (native.id,))
    CapabilityStore(store).upsert(CapabilityMemoryDraft(category='backend', title='nativequery', content='nativequery procedure'),
        source=CapabilitySourceDraft(source_kind='context', source_key=f'context:{native.id}', source_ref=f'context_item:{native.id}',
            context_item_id=native.id, source_revision=context_revision(store._connection(), native.id)))
    result = adapter.call_tool('kane', 'capability_search', {'query': 'nativequery'})
    assert result['count'] == 1, result
