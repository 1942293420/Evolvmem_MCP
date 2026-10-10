"""One request may share health checks only while observed state is unchanged."""
import sqlite3

from tests.test_lan_sharing import lan, add


def test_read_request_checks_health_once_and_next_request_checks_again(lan, monkeypatch):
    runtime, adapter = lan
    add(adapter, 'kane')
    service = runtime.server_for('kane').context_service
    refresh = service._refresh_health
    evaluations = []

    def observed_refresh():
        evaluations.append(True)
        return refresh()

    monkeypatch.setattr(service, '_refresh_health', observed_refresh)
    first = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'project': 'lan', 'space': 'personal'})
    assert first['count'] == 1
    assert len(evaluations) == 1
    second = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'project': 'lan', 'space': 'personal'})
    assert second['results'][0]['id'] == first['results'][0]['id']
    assert len(evaluations) == 2


def test_external_database_change_between_preflight_and_dispatch_is_rechecked(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, 'kane')
    server = runtime.server_for('kane')
    dispatch = adapter._dispatch

    def changed_dispatch(*args):
        with sqlite3.connect(server.config.db_path) as writer:
            writer.execute("DELETE FROM context_layers WHERE item_id=? AND layer='l1'", (item['context_id'],))
        return dispatch(*args)

    monkeypatch.setattr(adapter, '_dispatch', changed_dispatch)
    result = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'space': 'personal'})
    assert result.get('error') == 'degraded_legacy'
    assert not result.get('results')


def test_same_connection_change_between_preflight_and_dispatch_is_rechecked(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, 'kane')
    server = runtime.server_for('kane')
    dispatch = adapter._dispatch

    def changed_dispatch(*args):
        with server.context_service.store.transaction():
            server.context_service.store._connection().execute(
                "DELETE FROM context_layers WHERE item_id=? AND layer='l0'", (item['context_id'],))
        return dispatch(*args)

    monkeypatch.setattr(adapter, '_dispatch', changed_dispatch)
    result = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'space': 'personal'})
    assert result.get('error') == 'degraded_legacy'
    assert not result.get('results')


def test_config_change_does_not_reuse_successful_preflight_health(lan, monkeypatch):
    runtime, adapter = lan
    add(adapter, 'kane')
    server = runtime.server_for('kane')
    dispatch = adapter._dispatch

    def changed_dispatch(*args):
        server.config.context_vectors_required = True
        return dispatch(*args)

    monkeypatch.setattr(adapter, '_dispatch', changed_dispatch)
    result = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'space': 'personal'})
    assert result.get('error') == 'degraded_legacy'
    assert not result.get('results')
