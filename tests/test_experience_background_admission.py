"""Native LAN/SQLite regressions: background admission never replaces evidence."""
import json

import pytest

from tests.test_lan_sharing import lan, add, publish


@pytest.fixture(autouse=True)
def no_cross_request_cache(monkeypatch):
    monkeypatch.setenv('EVOLVMEM_HEALTH_CACHE_SECONDS', '0')


def baseline(runtime, *names):
    health = runtime._background_health
    for name in names:
        health.refresh(name)
        health._next.clear()
        assert health.run_once()
        observed = health.status(name)
        assert observed['hard_gate'] == 'allowed', observed
        assert observed['diagnostic']['last_checked_ready'] is True, observed
    return health


def recall(adapter, space='personal', **extra):
    return adapter.call_tool('kane', 'experience_recall', {
        'project': 'demo', 'query': 'orchard pagination', 'mode': 'hook',
        'space': space, **extra})


def seed(runtime, tmp_path):
    from evolvmem.experience_sources import ExperienceSourceResolver
    core = runtime.server_for('kane').context_service
    service = core.experiences()
    source = tmp_path / 'source'
    source.mkdir()
    text = 'orchard pagination returns 20 verified rows without duplicates'
    transcript = source / 'rollout-2026-task-1.jsonl'
    transcript.write_text(json.dumps({'type': 'response_item', 'payload': {
        'type': 'function_call_output', 'output': text}}) + '\n', encoding='utf-8')
    service.source_resolver = ExperienceSourceResolver(codex_roots=(source,))
    saved = service.record({
        'project': 'demo', 'problem': 'orchard pagination is slow',
        'steps': ['measure orchard pagination', 'fetch only one page'],
        'conditions': {'size': 'large'}, 'transferable': False,
    }, evidence={'task_id': 'task-1', 'event_id': 'verified-1',
        'outcome': 'success', 'level': 'technical', 'source_kind': 'tool_result',
        'source_ref': str(transcript.resolve()) + '#1', 'quote': text,
        'note': text, 'conditions': {'size': 'large'}})
    return service, saved


@pytest.mark.parametrize('space,names', [
    ('personal', ('kane',)), ('public', ('public',)),
    ('both', ('kane', 'public')),
])
def test_no_foreground_health_scan_and_exact_namespace_checks(lan, monkeypatch, space, names):
    runtime, adapter = lan
    # Disable the supported cross-request diagnostic cache. No sleep or fake
    # health/proof is needed to expose the old foreground evaluation path.
    monkeypatch.setenv('EVOLVMEM_HEALTH_CACHE_SECONDS', '0')
    expected = recall(adapter, space)
    assert 'error' not in expected, expected
    assert 'results' in expected if space != 'public' else 'shared_knowledge' in expected
    health = baseline(runtime, *names)
    statuses, scans = [], []
    original_status = health.status
    monkeypatch.setattr(health, 'status', lambda name: (
        statuses.append(name), original_status(name))[1])
    for server in health.servers.values():
        evaluate = server._evaluate_live_status
        monkeypatch.setattr(server, '_evaluate_live_status',
                            lambda evaluate=evaluate: (scans.append(True), evaluate())[1])
    actual = recall(adapter, space)
    assert 'error' not in actual, actual
    assert actual == expected
    assert scans == [], 'experience request still evaluates complete health'
    assert statuses == [*names, *names]


def test_verified_result_and_all_business_evidence_gates_survive_stale_health(lan, tmp_path, monkeypatch):
    runtime, adapter = lan
    service, saved = seed(runtime, tmp_path)
    expected = recall(adapter, constraints={'size': 'large'})
    assert [row['id'] for row in expected['results']] == [saved['id']]
    health = baseline(runtime, 'kane')
    # An unrelated committed generation makes the background observation stale.
    with service.store.transaction():
        service._conn().execute('UPDATE context_health_generation SET generation=generation+1')
    assert health.status('kane')['freshness'] == 'stale'
    monkeypatch.setattr(runtime.server_for('kane'), '_evaluate_live_status',
                        lambda: pytest.fail('foreground complete health scan'))
    assert recall(adapter, constraints={'size': 'large'}) == expected
    assert recall(adapter, constraints={'size': 'small'})['results'] == []
    assert recall(adapter, project='different')['results'] == []
    with service.store.transaction():
        service._conn().execute("UPDATE context_evidence SET outcome='contradicted' WHERE item_id=?", (saved['id'],))
    assert recall(adapter, constraints={'size': 'large'})['results'] == []


@pytest.mark.parametrize('namespace,space', [('kane','personal'), ('public','public'), ('public','both')])
def test_actual_background_failure_rejects_before_dispatch(lan, monkeypatch, namespace, space):
    runtime, adapter = lan
    names = ('kane','public') if space == 'both' else (namespace,)
    health = baseline(runtime, *names)
    # Change a real observed config boundary, never inject a fake status.
    health.servers[namespace].config.context_vectors_required = True
    monkeypatch.setattr(adapter, '_dispatch', lambda *a: pytest.fail('denied dispatch'))
    assert recall(adapter, space)['error'] == 'health_boundary_changed'


def test_last_checked_not_ready_rejects_without_foreground_repair(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, 'kane')
    service = runtime.server_for('kane').context_service
    with service.store.transaction():
        service.store._connection().execute("DELETE FROM context_layers WHERE item_id=? AND layer='l1'", (item['context_id'],))
    health = runtime._background_health
    health.refresh('kane')
    assert health.run_once()
    status = health.status('kane')
    assert status['hard_gate'] == 'allowed'
    assert status['diagnostic']['last_checked_ready'] is False
    monkeypatch.setattr(adapter, '_dispatch', lambda *a: pytest.fail('not-ready dispatch'))
    assert recall(adapter)['error'] == 'degraded_legacy'


def test_exit_boundary_change_discards_already_computed_results(lan, monkeypatch):
    runtime, adapter = lan
    baseline(runtime, 'kane', 'public')
    original = adapter._dispatch
    def changed(*args):
        result = original(*args)
        runtime.server_for('jiangli', 'public').config.context_vectors_required = True
        return result
    monkeypatch.setattr(adapter, '_dispatch', changed)
    result = recall(adapter, 'both')
    assert result['error'] == 'health_boundary_changed'
    assert 'results' not in result and 'shared_knowledge' not in result


def test_scoped_readiness_does_not_mutate_resident_or_enable_other_calls(lan, monkeypatch):
    runtime, adapter = lan
    baseline(runtime, 'kane')
    server = runtime.server_for('kane')
    server.context_service._ready = False
    assert 'error' not in recall(adapter)
    assert server.context_service._ready is False
    # Standalone reads still evaluate their original gate after LAN scope exit.
    scans = []
    monkeypatch.setattr(server, '_evaluate_live_status', lambda: scans.append(True))
    assert server.handle_tool_call('experience_recall', {'query':'orchard'})['error'] == 'not_initialized'
    assert scans


def test_active_worker_without_baseline_fails_closed(lan, monkeypatch):
    runtime, adapter = lan
    runtime._background_health.active = True
    monkeypatch.setattr(adapter, '_dispatch', lambda *a: pytest.fail('no-baseline dispatch'))
    assert recall(adapter)['error'] == 'health_baseline_pending'


def test_identity_and_schema_rejected_before_background_admission(lan, monkeypatch):
    runtime, adapter = lan
    health = baseline(runtime, 'kane')
    monkeypatch.setattr(health, 'status', lambda *a: pytest.fail('invalid request checked health'))
    assert recall(adapter, user='jiangli')['error'] == 'identity_override_forbidden'
    assert recall(adapter, mode='invalid')['error'] == 'invalid_arguments'


def test_nonexperience_and_writes_keep_original_health_gate(lan, monkeypatch):
    runtime, adapter = lan
    baseline(runtime, 'kane')
    assert 'error' not in recall(adapter)
    server = runtime.server_for('kane')
    original, scans = server._evaluate_live_status, []
    monkeypatch.setattr(server, '_evaluate_live_status', lambda: (scans.append(True), original())[1])
    assert 'error' not in adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'space': 'personal'})
    assert scans
    scans.clear()
    add(adapter, 'kane')
    assert scans


def test_no_background_mode_preserves_original_foreground_path(lan, monkeypatch):
    runtime, adapter = lan
    assert runtime._background_health.active is False
    server = runtime.server_for('kane')
    original, calls = server._evaluate_live_status, []
    monkeypatch.setattr(server, '_evaluate_live_status', lambda: (calls.append(True), original())[1])
    assert 'error' not in recall(adapter)
    assert calls
