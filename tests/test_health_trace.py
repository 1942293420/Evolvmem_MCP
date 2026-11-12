"""Health timing identifies retries and physical baselines without private state."""
import json
import logging

from tests.test_lan_sharing import lan, add
from evolvmem import request_health, request_trace as tracing


def reports(caplog):
    return [json.loads(r.message.split('request_trace ', 1)[1])
            for r in caplog.records if 'request_trace ' in r.message]


def test_health_trace_counts_retries_and_only_names_changed_fields(lan, monkeypatch, caplog):
    runtime, adapter = lan
    item = add(adapter, 'kane', value='PRIVATE_MEMORY_SECRET')
    server = runtime.server_for('kane')
    request_health._entry(server).snapshot = None
    original = server._evaluate_live_status
    written = []
    def evaluate():
        result = original()
        if not written:
            with server.context_service.store.transaction():
                server.context_service.store._connection().execute(
                    'UPDATE context_items SET access_count=access_count+1 WHERE id=?', (item['context_id'],))
            written.append(True)
        return result
    monkeypatch.setattr(server, '_evaluate_live_status', evaluate)
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        with tracing.request_trace('context_status'):
            assert server._live_status().ready
    report = reports(caplog)[0]
    assert report['health_evaluations'] == 2
    assert report['health_passes'][0]['changed'] == ['database_changes']
    assert report['health_passes'][1]['changed'] == []
    assert all(p['ready'] and p['elapsed_ms'] >= 0 for p in report['health_passes'])
    for name in ('health_state', 'health_projection', 'health_vector', 'health_documents'):
        assert report['stages_ms'][name] >= 0
    assert 'PRIVATE_' not in caplog.text
    assert str(server.config.data_dir) not in caplog.text


def test_physical_baseline_reason_and_cache_hits_are_separate(lan, monkeypatch, caplog):
    runtime, _ = lan
    server = runtime.server_for('kane')
    server.context_service._integrity_snapshot = None
    request_health._entry(server).snapshot = None
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        with tracing.request_trace('context_status'):
            assert server._live_status().ready
            assert server._live_status().ready
    report = reports(caplog)[0]
    assert report['health_events']['physical_baseline_initial'] == 1
    assert report['stages_ms']['health_physical'] >= 0
    assert report['health_cache_hits'] == 1


def test_health_trace_is_bounded_and_drops_unknown_labels(monkeypatch, caplog):
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        with tracing.request_trace('context_status'):
            for _ in range(20):
                tracing.health_evaluation(1.5, True, ['config', 'PRIVATE_TOKEN'])
                tracing.health_event('PRIVATE_QUERY')
    report = reports(caplog)[0]
    assert report['health_evaluations'] == 20
    assert len(report['health_passes']) == 8
    assert all(p['changed'] == ['config'] for p in report['health_passes'])
    assert 'PRIVATE_' not in caplog.text
