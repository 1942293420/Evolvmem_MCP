"""Real SQLite tests of the remote identity and curated-public boundary."""
import hashlib
import json

import pytest

from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime


def settings_for(tmp_path):
    return LanSettings(data_dir=tmp_path / 'lan', owner_data_dir=tmp_path / 'owner',
                       token_hashes={u: hashlib.sha256((u + '-token').encode()).hexdigest()
                                     for u in ('jiangli', 'kane')}, embedding_enabled=False)


@pytest.fixture
def lan(tmp_path):
    from evolvmem.lan_tools import LanTools
    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    yield runtime, LanTools(runtime)
    runtime.close()


def add(adapter, user='jiangli', value='private unique orchard marker', key='project:lan:fact:orchard', rid='add1'):
    result = adapter.call_tool(user, 'memory_add', dict(key=key, value=value, request_id=rid))
    assert 'error' not in result, result
    return result


def publish(adapter, source, user='jiangli', rid='pub1', title='shared orchard procedure', summary='curated shared orchard summary'):
    result = adapter.call_tool(user, 'memory_publish', dict(source_context_id=source['context_id'],
                              title=title, summary=summary, request_id=rid))
    assert 'error' not in result, result
    return result


def test_private_isolation_exact_ids_and_identity_overrides(lan):
    _, adapter = lan
    j = add(adapter)
    k = add(adapter, 'kane', 'private banana marker for Kane')
    assert j['context_id'] == k['context_id']
    assert adapter.call_tool('kane', 'context_read', {'id': j['context_id']})['content'] == 'private banana marker for Kane'
    assert adapter.call_tool('kane', 'memory_search', {'query': 'orchard'})['count'] == 0
    for name in ('user', 'user_id', 'owner', 'data_dir'):
        assert adapter.call_tool('kane', 'memory_search', {'query': 'orchard', name: 'jiangli'})['error'] == 'identity_override_forbidden'
    assert adapter.call_tool('kane', 'context_read', {'id': 1, 'space': 'jiangli'})['error'] == 'invalid_space'


def test_publish_read_update_withdraw_and_ownership(lan):
    _, adapter = lan
    source = add(adapter, value='PRIVATE SOURCE /server/private.log must never escape')
    public = publish(adapter, source)
    for layer in ('l1', 'l2'):
        read = adapter.call_tool('kane', 'context_read', {'ref': public['ref'], 'layer': layer})
        assert 'PRIVATE' not in json.dumps(read)
        assert 'source' not in json.dumps(read)
        assert read['space'] == 'public'
    denied = adapter.call_tool('kane', 'memory_update_public', dict(id=public['id'], title='changed', summary='curated replacement', request_id='upd'))
    assert denied['error'] == 'publication_forbidden'
    updated = adapter.call_tool('jiangli', 'memory_update_public', dict(id=public['id'], title='revised orchard', summary='revised curated content', request_id='upd'))
    assert updated['id'] == public['id']
    assert adapter.call_tool('kane', 'context_read', {'id': public['id'], 'space': 'public'})['content'] == 'revised curated content'
    withdrawn = adapter.call_tool('jiangli', 'memory_unpublish', {'id': public['id'], 'request_id': 'withdraw'})
    assert withdrawn['active'] is False
    assert adapter.call_tool('kane', 'context_read', {'ref': public['ref']})['error'] == 'not_found'
    assert adapter.call_tool('kane', 'memory_search', {'query': 'orchard'})['count'] == 0
    assert not adapter.call_tool('kane', 'context_session_start', {'project': '', 'query': 'orchard'})['shared_knowledge']


def test_shared_limits_metadata_filter_and_no_cross_key_merge(lan):
    runtime, adapter = lan
    source = add(adapter)
    k = add(adapter, 'kane')
    p1 = publish(adapter, source)
    p2 = publish(adapter, k, 'kane')
    assert p1['id'] != p2['id']
    rows = adapter.call_tool('kane', 'memory_search', {'query': 'orchard', 'top_k': 2})['results']
    assert len(rows) == 2
    assert all(r['space'] in ('personal', 'public') and r['ref'] for r in rows)
    full = adapter.call_tool('kane', 'context_search', {'query': 'orchard', 'space': 'public'})
    assert full['count'] == 2
    assert len({r['ref'] for r in full['results']}) == 2
    # A public core row without publication metadata must never be remotely visible.
    raw = runtime.server_for('jiangli', 'public').handle_tool_call('memory_add', {'key': 'project:lan:fact:orphan', 'value': 'orphan orchard secret without publication'})
    assert 'error' not in raw
    assert adapter.call_tool('kane', 'context_read', {'id': raw['context_id'], 'space': 'public'})['error'] == 'not_found'
    assert adapter.call_tool('kane', 'memory_search', {'query': 'orphan', 'space': 'public'})['count'] == 0
    assert adapter.call_tool('jiangli', 'memory_unpublish', {'id': p2['id'], 'request_id': 'maintainer'})['active'] is False


def test_session_budget_and_shared_unverified_recall(lan):
    _, adapter = lan
    publish(adapter, add(adapter))
    result = adapter.call_tool('kane', 'context_session_start', {'project': '', 'query': 'orchard', 'max_chars': 220})
    assert len(result['block']) <= 220
    assert result['used_chars'] == len(result['block'])
    assert 'public:context:' in result['block']
    assert result['shared_knowledge'][0]['verification'] == 'unverified_shared_summary'
    recall = adapter.call_tool('kane', 'experience_recall', {'query': 'orchard'})
    assert recall['shared_knowledge'][0]['ref'].startswith('public:context:')
    assert not recall.get('cases')


def test_durable_retries_and_payload_mismatch(lan):
    runtime, adapter = lan
    args = dict(key='project:lan:fact:retry', value='durable retry marker text', request_id='retry')
    first = adapter.call_tool('kane', 'memory_add', args)
    assert first == adapter.call_tool('kane', 'memory_add', args)
    settings = runtime.settings
    runtime.close()
    from evolvmem.lan_tools import LanTools
    restarted = LanRuntime(settings)
    restarted.initialize()
    try:
        new = LanTools(restarted)
        assert new.call_tool('kane', 'memory_add', args) == first
        assert new.call_tool('kane', 'memory_search', {'query': 'durable'})['count'] == 1
        assert new.call_tool('kane', 'memory_add', {**args, 'value': 'different retry content'})['error'] == 'request_id_conflict'
    finally:
        restarted.close()


@pytest.mark.parametrize('rid', ['', 'a' * 129, 'Bearer private-token', 'line\nbreak'])
def test_request_id_required_and_bounded(lan, rid):
    _, adapter = lan
    assert adapter.call_tool('kane', 'memory_add', {'key': 'a', 'value': 'valid long content', 'request_id': rid})['error'] == 'invalid_request_id'


def test_remote_paths_evidence_and_public_writes_require_valid_scope(lan):
    _, adapter = lan
    for name in ('continuity_begin', 'continuity_resume', 'context_session_start', 'continuity_find'):
        assert adapter.call_tool('kane', name, {'workspace_path': '/etc', 'request_id': 'blocked'})['error'] == 'invalid_device_id'
    for name, args in [('experience_record', {'case': {'project': 'lan', 'problem': '需核验实际来源', 'steps': ['核对会话事件']}, 'evidence': {'task_id': 'x'}}), ('context_record_outcome', {'id': 1, 'outcome': 'success', 'task_id': 'x'})]:
        assert adapter.call_tool('kane', name, {**args, 'request_id': 'evidence'})['error'] == 'remote_evidence_unavailable'
    assert adapter.call_tool('kane', 'memory_add', {'space': 'public', 'request_id': 'public'})['error'] == 'public_write_forbidden'
    assert adapter.call_tool('kane', 'memory_add', {'key': 'a', 'value': 'long enough content'})['error'] == 'invalid_request_id'


def test_public_sensitive_and_path_material_rejected(lan):
    _, adapter = lan
    source = add(adapter)
    for summary in ('password=veryprivate', 'read /home/jiangli/private.log', 'https://private.example/session', r'C:\Users\private\file'):
        result = adapter.call_tool('jiangli', 'memory_publish', dict(source_context_id=source['context_id'], title='clean title', summary=summary, request_id=hashlib.sha256(summary.encode()).hexdigest()))
        assert result['error'] == 'unsafe_public_content'


def test_rejected_public_write_replay_preserves_original_error(lan):
    _, adapter = lan
    item = publish(adapter, add(adapter))
    args = {'id': item['id'], 'request_id': 'denied'}
    first = adapter.call_tool('kane', 'memory_unpublish', args)
    assert first['error'] == 'publication_forbidden'
    assert adapter.call_tool('kane', 'memory_unpublish', args) == first


def test_effect_before_result_failure_is_indeterminate_without_reexecution(lan):
    runtime, adapter = lan
    ledger = runtime.server_for('kane').context_service.store._connection()
    ledger.execute("CREATE TRIGGER fail_result BEFORE UPDATE OF result ON lan_write_requests BEGIN SELECT RAISE(ABORT, 'simulated result persistence failure'); END")
    ledger.commit()
    args = dict(key='project:lan:fact:crash', value='crash window durable effect marker', request_id='crash')
    assert adapter.call_tool('kane', 'memory_add', args)['error'] == 'request_indeterminate'
    assert adapter.call_tool('kane', 'memory_search', {'query': 'crash'})['count'] == 1
    ledger.execute('DROP TRIGGER fail_result')
    ledger.commit()
    settings = runtime.settings
    runtime.close()
    from evolvmem.lan_tools import LanTools
    restarted = LanRuntime(settings)
    restarted.initialize()
    try:
        other = LanTools(restarted)
        assert other.call_tool('kane', 'memory_add', args)['error'] == 'request_indeterminate'
        assert other.call_tool('kane', 'memory_search', {'query': 'crash'})['count'] == 1
    finally:
        restarted.close()


def test_publication_failure_rolls_back_item_and_layers(lan):
    _, adapter = lan
    source = add(adapter)
    conn = adapter.sharing.conn
    conn.execute("CREATE TRIGGER fail_publication BEFORE INSERT ON lan_publications BEGIN SELECT RAISE(ABORT, 'simulated publication failure'); END")
    conn.commit()
    args = dict(source_context_id=source['context_id'], title='rollback orchard', summary='curated rollback content', request_id='rollback')
    assert adapter.call_tool('jiangli', 'memory_publish', args)['error'] == 'request_indeterminate'
    assert conn.execute('SELECT COUNT(*) FROM context_items').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM context_layers').fetchone()[0] == 0
    assert adapter.call_tool('kane', 'memory_search', {'query': 'rollback'})['count'] == 0


@pytest.mark.parametrize('tool', ['memory_update_public', 'memory_unpublish'])
def test_failed_public_change_preserves_previous_visibility(lan, tool):
    _, adapter = lan
    item = publish(adapter, add(adapter))
    conn = adapter.sharing.conn
    conn.execute("CREATE TRIGGER fail_public_change BEFORE UPDATE ON lan_publications BEGIN SELECT RAISE(ABORT, 'simulated metadata update failure'); END")
    conn.commit()
    args = {'id': item['id'], 'request_id': 'rollback'}
    if tool == 'memory_update_public':
        args.update(title='replacement title', summary='replacement text')
    assert adapter.call_tool('jiangli', tool, args)['error'] == 'request_indeterminate'
    assert adapter.call_tool('kane', 'context_read', {'ref': item['ref']})['content'] == 'curated shared orchard summary'
    assert adapter.call_tool('kane', 'memory_search', {'query': 'orchard'})['count'] == 1


def test_candidate_record_qualified_refs_and_personal_archive_maintenance(lan):
    _, adapter = lan
    candidate = adapter.call_tool('kane', 'experience_record', {'case': {'project': 'lan', 'problem': 'real bounded candidate mechanism', 'steps': ['Inspect the current SQLite namespace']}, 'request_id': 'candidate'})
    assert candidate['status'] == 'candidate'
    assert candidate['validation_level'] == 'unverified'
    assert candidate['ref'] == f"personal:context:{candidate['id']}"
    derived = adapter.call_tool('kane', 'experience_record', {'case': {'project': 'lan', 'problem': 'derived candidate mechanism', 'steps': ['Inspect the alternate condition'], 'parent_experience_id': candidate['id']}, 'request_id': 'derived'})
    assert derived['parent_experience_ref'] == candidate['ref']
    assert 'error' not in adapter.call_tool('kane', 'context_archive_project', {'request_id': 'archive', 'project': 'lan'})
    assert 'error' not in adapter.call_tool('kane', 'context_sweep', {'request_id': 'sweep'})


def test_session_shared_excerpt_does_not_return_full_summary_outside_budget(lan):
    _, adapter = lan
    publish(adapter, add(adapter), summary='curated orchard ' * 70)
    result = adapter.call_tool('kane', 'context_session_start', {'project': '', 'query': 'orchard', 'max_chars': 180})
    assert len(result['block']) <= 180
    assert len(result['shared_knowledge'][0]['summary']) < 180
    assert 'value' not in result['shared_knowledge'][0]


def test_shared_model_semantic_results_stay_in_allowed_namespaces(tmp_path):
    from dataclasses import replace
    from evolvmem.lan_tools import LanTools

    class Engine:
        is_loaded = False
        initialized = 0

        def initialize(self):
            self.is_loaded = True
            self.initialized += 1

        def encode_document(self, _text):
            return [1.0] + [0.0] * 767

        encode_query = encode_document

        def close(self):
            pass

    engine = Engine()
    runtime = LanRuntime(replace(settings_for(tmp_path), embedding_enabled=True), engine)
    runtime.initialize()
    try:
        adapter = LanTools(runtime)
        j = add(adapter, value='jiangli private semantic material')
        k = add(adapter, 'kane', value='kane private semantic material')
        publish(adapter, j)
        publish(adapter, k, 'kane')
        results = adapter.call_tool('kane', 'memory_search', {'query': 'unrelatedsemanticquery'})
        assert results['count'] == 3
        assert {r['space'] for r in results['results']} == {'personal', 'public'}
        assert all('jiangli private' not in json.dumps(r) for r in results['results'])
        assert engine.initialized == 1
        assert adapter.call_tool('kane', 'context_status', {'space': 'public'})['active_memories'] == 2
    finally:
        runtime.close()


def test_credentials_cannot_be_used_as_request_ids(lan):
    _, adapter = lan
    assert adapter.call_tool('kane', 'memory_add', {'key': 'a', 'value': 'valid content marker', 'request_id': 'kane-token'})['error'] == 'invalid_request_id'


def test_public_status_and_personal_diagnostics_do_not_leak_paths(lan):
    runtime, adapter = lan
    add(adapter)
    assert adapter.call_tool('kane', 'memory_status', {})['active_memories'] == 0
    for space in ('personal', 'public'):
        for tool in ('memory_status', 'context_status'):
            result = adapter.call_tool('kane', tool, {'space': space})
            assert result['space'] == space
            assert str(runtime.settings.data_dir) not in json.dumps(result)
            assert 'diagnostics' not in result


def test_publish_requires_exact_own_readable_source(lan):
    _, adapter = lan
    source = add(adapter)
    result = adapter.call_tool('kane', 'memory_publish', {'source_context_id': source['context_id'], 'title': 'unowned source', 'summary': 'cannot publish another private source', 'request_id': 'notmine'})
    assert result['error'] == 'source_not_readable'
    assert adapter.call_tool('kane', 'memory_status', {'space': 'public'})['active_memories'] == 0


def test_public_search_indexes_curated_summary_as_well_as_title(lan):
    _, adapter = lan
    publish(adapter, add(adapter), title='general procedure', summary='Orchidguard separates reusable lessons from private transcripts')
    assert adapter.call_tool('kane', 'memory_search', {'query': 'Orchidguard'})['count'] == 1


def test_remote_memory_refs_remain_correct_when_context_and_legacy_ids_differ(lan):
    _, adapter = lan
    adapter.call_tool('kane', 'experience_record', {'case': {'project': 'lan', 'problem': 'native context occupies first id', 'steps': ['Preserve separate ID domains']}, 'request_id': 'native'})
    memory = add(adapter, 'kane')
    assert memory['id'] != memory['context_id']
    assert memory['ref'] == f"personal:context:{memory['context_id']}"
    assert memory['memory_ref'] == f"personal:memory:{memory['id']}"
    removed = adapter.call_tool('kane', 'memory_remove', {'ref': memory['memory_ref'], 'request_id': 'remove'})
    assert removed['ref'] == memory['ref']
    assert removed['memory_ref'] == memory['memory_ref']
    assert adapter.call_tool('kane', 'memory_search', {'query': 'orchard'})['count'] == 0


def test_context_status_counts_stay_numeric(lan):
    _, adapter = lan
    add(adapter, 'kane')
    status = adapter.call_tool('kane', 'context_status', {})
    assert all(type(count) is int for count in status['status_counts'].values())


def test_retry_results_with_private_payload_stay_in_personal_sqlite(lan):
    _, adapter = lan
    marker = 'PRIVATECANDIDATERETRYBODY'
    result = adapter.call_tool('kane', 'experience_record', {'case': {'project': 'lan', 'problem': marker, 'steps': ['Store private retry result in its owner namespace']}, 'request_id': 'private-retry'})
    assert result['problem'] == marker
    assert marker not in '\n'.join(adapter.sharing.conn.iterdump())
