"""Remote archive proof and project operations reuse the real Linux core."""
import json

from tests.test_lan_sharing import lan
from tests.test_lan_capture import transcript, upload
from tests.test_experience_service import case
from tests.test_lan_context import begin, PATH, SNAP


def evidence(archive_id, **changes):
    return dict(task_id='session-1', event_id='verification-1', outcome='success',
                level='technical', source_kind='tool_result',
                source_ref=f'archive:{archive_id}#3', quote='1 passed in 0.10s',
                note='实际工具事件表明相关测试通过',
                conditions={'data_size': 'large', 'freshness': 'realtime'}, **changes)


def test_remote_verified_experience_uses_immutable_archive_and_deduplicates(lan):
    runtime, adapter = lan
    archived = upload(adapter, transcript())
    proof = evidence(archived['archive_id'])
    saved = adapter.call_tool('jiangli', 'experience_record', {'case': case(), 'evidence': proof, 'request_id': 'verified'})
    assert saved.get('status') == 'active', saved
    assert saved['success_count'] == 1
    conn = runtime.server_for('jiangli').context_service.store._connection()
    assert conn.execute('SELECT archive_id FROM context_sources WHERE item_id=?', (saved['id'],)).fetchone()[0] == archived['archive_id']
    upload(adapter, transcript() + b'{"type":"event_msg","payload":{"type":"token_count"}}\n')
    replay = adapter.call_tool('jiangli', 'context_record_outcome', {'id': saved['id'], **proof, 'request_id': 'replay-event'})
    assert replay['success_count'] == 1
    hits = adapter.call_tool('jiangli', 'experience_recall', {'project': 'demo', 'query': '列表打开缓慢'})
    assert hits['results'][0]['id'] == saved['id']
    denied = adapter.call_tool('kane', 'experience_record', {'case': case(), 'evidence': proof, 'request_id': 'wrong-owner'})
    assert 'error' in denied


def test_remote_assistant_claim_wrong_task_and_server_path_are_rejected(lan):
    _, adapter = lan
    archived = upload(adapter, transcript())
    for index, changes in enumerate(({'task_id': 'wrong-task'},
            {'source_ref': f"archive:{archived['archive_id']}#4", 'quote': '已完成测试'},
            {'source_ref': '/home/jiangli/.codex/sessions/rollout.jsonl#3'})):
        proof = {**evidence(archived['archive_id']), **changes}
        result = adapter.call_tool('jiangli', 'experience_record', {'case': case(), 'evidence': proof, 'request_id': f'bad-proof-{index}'})
        assert 'error' in result
    assert adapter.call_tool('jiangli', 'experience_recall', {'project': 'demo', 'query': '列表打开缓慢'})['results'] == []


def test_remote_maintenance_uses_only_authenticated_store(lan):
    _, adapter = lan
    for user in ('jiangli', 'kane'):
        assert 'error' not in adapter.call_tool(user, 'memory_add', {'key': 'project:demo:fact:private', 'value': '项目的私有记忆须保持属于当前认证用户。', 'request_id': 'private'})
    result = adapter.call_tool('kane', 'context_archive_project', {'project': 'demo', 'request_id': 'archive'})
    assert 'error' not in result, result
    assert adapter.call_tool('jiangli', 'memory_search', {'query': '私有记忆', 'space': 'personal'})['count'] == 1
    swept = adapter.call_tool('kane', 'context_sweep', {'request_id': 'sweep'})
    assert 'error' not in swept, swept
    # This fixture intentionally disables vectors; preserve the native diagnostic.
    assert adapter.call_tool('kane', 'memory_consolidate', {'request_id': 'consolidate'})['error'] == 'embedding engine not loaded'


def test_remote_board_uses_current_device_and_does_not_borrow_owner_environment(lan, monkeypatch, tmp_path):
    runtime, adapter = lan
    inherited = tmp_path / 'owner-board.json'
    inherited.write_text(json.dumps({'base_url': 'http://127.0.0.1:1', 'api_key': 'fixture-only'}))
    monkeypatch.setenv('EVOLVMEM_PROJECT_BOARD_CONFIG', str(inherited))
    # No per-user board config: another user's environment must not activate it.
    first = begin(adapter, repo_snapshot=SNAP)
    assert 'error' not in first
    result = adapter.call_tool('kane', 'project_board_status', {'workspace_path': PATH, 'device_id': 'pc1', 'project_hint': 'demo'})
    assert result.get('status') == 'disabled', result
    server = runtime.server_for('kane')
    from evolvmem.lan_context import remote_context, validate_snapshot
    with remote_context(server, 'pc1', validate_snapshot(SNAP)) as provider:
        board = server._project_board()
        assert board.workspace_identity is provider
        assert board.config_path == server.config.data_dir / 'project_board.json'
    with remote_context(server, 'pc2', validate_snapshot(SNAP)) as provider:
        assert server._project_board().workspace_identity is provider


def test_start_injects_current_checkpoint_even_without_continue_keyword(lan):
    _, adapter = lan
    first = begin(adapter, repo_snapshot=SNAP)
    result = adapter.call_tool('kane', 'context_session_start', {'project': 'demo', 'query': '检查当前代码',
        'workspace_path': PATH, 'device_id': 'pc1', 'repo_snapshot': SNAP})
    assert first['workstream_id'] in json.dumps(result)
    assert 'Finish the remote demonstration' in result['block']
