"""Windows uploads must become complete, private, replayable encrypted archives."""
import base64
import hashlib
import json
import pytest

from tests.test_lan_sharing import lan
from evolvmem.session_archive import SessionArchiver


def transcript(session_id='session-1', text='客户确认保留完整会话归档和任务断点。'):
    rows = [
        {'type': 'session_meta', 'payload': {'id': session_id, 'cwd': r'C:\work\demo'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': text}]}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output',
         'call_id': 'call-1', 'output': '1 passed in 0.10s'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
         'content': [{'type': 'output_text', 'text': '已完成测试，归档仍需单独核验。'}]}},
    ]
    return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n').encode()


def upload(adapter, raw, *, user='jiangli', device='windows-main', session='session-1',
           start=0, end=None, extract=False, **changes):
    digest = hashlib.sha256(raw).hexdigest()
    args = dict(device_id=device, session_id=session, project='demo', sha256=digest,
                total_bytes=len(raw), offset=start,
                content_b64=base64.b64encode(raw[start:end]).decode(), extract=extract,
                request_id=f'capture-{digest[:16]}-{start}-{int(extract)}')
    args.update(changes)
    return adapter.call_tool(user, 'session_archive_upload', args)


def test_authenticated_identity_is_visible_without_accepting_client_user_override(lan):
    _, adapter = lan
    for user in ('jiangli', 'kane'):
        assert adapter.call_tool(user, 'memory_status', {})['authenticated_user'] == user
        result = adapter.call_tool(user, 'context_session_start', {'project': '', 'query': '会话摘要'})
        assert result['authenticated_user'] == user
        assert isinstance(result['block'], str)
    denied = adapter.call_tool('kane', 'memory_status', {'user': 'jiangli'})
    assert denied['error'] == 'identity_override_forbidden'


def test_partial_upload_is_not_a_formal_archive_and_replay_is_safe(lan):
    runtime, adapter = lan
    raw = transcript()
    first = upload(adapter, raw, end=64)
    assert first['status'] == 'receiving'
    assert first['next_offset'] == 64
    assert upload(adapter, raw, end=64) == first
    server = runtime.server_for('jiangli')
    assert server.context_service.store._connection().execute('SELECT COUNT(*) FROM session_archives').fetchone()[0] == 0
    saved = upload(adapter, raw, start=64)
    assert saved['status'] == 'archived'
    assert saved['authenticated_user'] == 'jiangli'
    assert saved['next_offset'] == len(raw)
    assert upload(adapter, raw, start=64)['archive_id'] == saved['archive_id']
    assert server.context_service.store._connection().execute('SELECT COUNT(*) FROM session_archives').fetchone()[0] == 1
    archive = SessionArchiver(server.config, server.context_service.store)
    payload = json.loads(archive.read_payload(saved['archive_id']))
    assert payload['transcript'] == raw.decode()
    assert payload['source'] == 'client_reported'
    for path in server.config.data_dir.rglob('*.bin'):
        assert '客户确认'.encode() not in path.read_bytes()


def test_other_users_cannot_observe_a_private_upload(lan):
    _, adapter = lan
    saved = upload(adapter, transcript())
    assert saved['status'] == 'archived'
    args = {'device_id': 'windows-main', 'session_id': 'session-1'}
    assert adapter.call_tool('kane', 'session_archive_status', args)['status'] == 'not_found'
    own = adapter.call_tool('jiangli', 'session_archive_status', args)
    assert own['archive_id'] == saved['archive_id']
    assert 'transcript' not in own
    assert own['authenticated_user'] == 'jiangli'


def test_corrupt_chunk_and_metadata_do_not_commit_an_archive(lan):
    runtime, adapter = lan
    raw = transcript()
    assert upload(adapter, raw, sha256='0' * 64)['error'] == 'transcript_hash_mismatch'
    assert upload(adapter, raw, content_b64='not base64')['error'] == 'invalid_chunk'
    assert upload(adapter, raw, session='another-session')['error'] == 'session_id_mismatch'
    assert upload(adapter, raw, offset=10)['error'] == 'invalid_chunk'
    store = runtime.server_for('jiangli').context_service.store
    assert store._connection().execute('SELECT COUNT(*) FROM session_archives').fetchone()[0] == 0


def test_old_snapshot_cannot_replace_newer_saved_transcript(lan):
    runtime, adapter = lan
    raw = transcript()
    newest = raw + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    assert len(raw) == 556 and len(newest) == 610
    saved = upload(adapter, newest)
    old = upload(adapter, raw)
    assert old['status'] == 'stale'
    assert old['submitted_sha256'] == hashlib.sha256(raw).hexdigest()
    assert old['submitted_total_bytes'] == len(raw)
    assert old['source_sha256'] == hashlib.sha256(newest).hexdigest()
    assert old['total_bytes'] == len(newest)
    assert old['next_offset'] == len(newest)
    server = runtime.server_for('jiangli')
    payload = SessionArchiver(server.config, server.context_service.store).read_payload(saved['archive_id'])
    assert json.loads(payload)['transcript'] == newest.decode()


def test_incomplete_jsonl_never_becomes_successful_archive(lan):
    _, adapter = lan
    raw = transcript() + b'{"type":'
    assert upload(adapter, raw)['error'] == 'invalid_transcript'


@pytest.mark.parametrize('separator', ['\u0085', '\u2028', '\u2029'])
def test_json_string_line_separators_survive_archive_and_extraction(lan, separator):
    from evolvmem.codex_transcript import parse_transcript
    from evolvmem.experience_sources import ExperienceSourceResolver

    runtime, adapter = lan
    text = '项目决定' + separator + '保留完整会话'
    raw = transcript(text=text)
    saved = upload(adapter, raw)
    assert saved.get('status') == 'archived', saved
    server = runtime.server_for('jiangli')
    archiver = SessionArchiver(server.config, server.context_service.store)
    payload = archiver.read_payload(saved['archive_id'])
    assert json.loads(payload)['transcript'] == raw.decode()
    rows, messages = parse_transcript(raw, 'session-1')
    assert len(rows) == 4
    assert messages[0] == {'role': 'user', 'content': text}
    resolver = ExperienceSourceResolver(archiver=archiver)
    evidence = resolver.resolve(source_kind='tool_result',
        source_ref=f"archive:{saved['archive_id']}#3", task_id='session-1', quote='1 passed in 0.10s')
    assert evidence.source_ref == f"archive:{saved['archive_id']}#3"
    user = resolver.resolve(source_kind='user_confirmation',
        source_ref=f"archive:{saved['archive_id']}#2", task_id='session-1', quote=text)
    assert user.source_ref == f"archive:{saved['archive_id']}#2"


def test_restart_resumes_encrypted_partial_upload_and_rejects_fork(lan):
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_tools import LanTools
    runtime, adapter = lan
    raw = transcript()
    assert upload(adapter, raw, end=77)['next_offset'] == 77
    settings = runtime.settings
    runtime.close()
    restarted = LanRuntime(settings)
    restarted.initialize()
    try:
        other = LanTools(restarted)
        saved = upload(other, raw, start=77)
        assert saved['status'] == 'archived'
        fork = transcript(text='这是一份与已经归档会话不一致的更长分叉内容，不能覆盖旧会话。')
        assert upload(other, fork)['error'] == 'transcript_fork'
    finally:
        restarted.close()


def test_new_snapshot_preserves_old_archive_evidence_payload(lan):
    runtime, adapter = lan
    raw = transcript()
    first = upload(adapter, raw)
    newer = raw + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    second = upload(adapter, newer)
    assert first['archive_id'] != second['archive_id']
    archiver = SessionArchiver(runtime.server_for('jiangli').config,
                               runtime.server_for('jiangli').context_service.store)
    assert json.loads(archiver.read_payload(first['archive_id']))['transcript'] == raw.decode()


def test_purged_latest_snapshot_still_allows_verified_larger_append(lan):
    runtime, adapter = lan
    raw = transcript()
    first = upload(adapter, raw)
    archiver = SessionArchiver(
        runtime.server_for('jiangli').config,
        runtime.server_for('jiangli').context_service.store,
    )
    assert archiver.purge_project('demo').purged_archive_ids == (first['archive_id'],)
    newer = raw + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'

    saved = upload(adapter, newer)

    assert saved['status'] == 'archived'
    assert saved['source_sha256'] == hashlib.sha256(newer).hexdigest()
    assert json.loads(archiver.read_payload(saved['archive_id']))['transcript'] == newer.decode()


def test_purged_latest_snapshot_rejects_larger_fork_and_never_false_acks_old(lan):
    runtime, adapter = lan
    raw = transcript()
    first = upload(adapter, raw)
    archiver = SessionArchiver(
        runtime.server_for('jiangli').config,
        runtime.server_for('jiangli').context_service.store,
    )
    assert archiver.purge_project('demo').purged_archive_ids == (first['archive_id'],)
    fork = transcript(text='分叉内容' * 80)
    assert len(fork) > len(raw)

    assert upload(adapter, fork)['error'] == 'transcript_fork'
    assert upload(adapter, raw)['error'] == 'archive_payload_unavailable'
    assert runtime.server_for('jiangli').context_service.store._connection().execute(
        "SELECT COUNT(*) FROM session_archives WHERE state='available'"
    ).fetchone()[0] == 0


def test_uploaded_session_extracts_through_existing_core_and_is_recalled(lan, monkeypatch):
    from evolvmem import kimi_hooks
    runtime, adapter = lan
    responses = []
    def model_response(prompt, config, **_kwargs):
        responses.append(prompt)
        if 'SESSION_SUMMARY' in prompt:
            return json.dumps({'memories': [
                {'key': 'SESSION_SUMMARY', 'value': '本次确认橙园项目使用统一会话归档，当前正在验证双端自动记忆读取。'},
                {'key': 'project:demo:decision:archive', 'value': '橙园项目统一保留会话加密归档，以便跨设备核验历史决策的依据。',
                 'attribute': 'decision', 'confidence': 0.95}]}, ensure_ascii=False)
        return json.dumps({'l0': '橙园项目采用统一会话归档并支持跨设备续接。',
                           'l1': '橙园项目统一保留会话加密归档，用于核验历史决策并实现双端记忆读取。',
                           'l2': '橙园项目已经确认统一保留会话加密归档，后续需要核验 Windows 与 Linux 的记忆读取和任务续接。'}, ensure_ascii=False)
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model_response)
    credentials = runtime.settings.owner_data_dir / 'llm_credentials.json'
    credentials.write_text(json.dumps({'provider': 'deepseek', 'api_key': 'fixture-only'}))
    saved = upload(adapter, transcript(text='橙园项目统一保留会话加密归档，以便跨设备核验历史决策的依据。'), extract=True)
    assert saved['extraction_status'] == 'pending'
    assert adapter.process_pending() == 1
    status = adapter.call_tool('jiangli', 'session_archive_status', {'device_id': 'windows-main', 'session_id': 'session-1'})
    assert status['extraction_status'] == 'extracted', status
    assert status['extraction']['summary_context_id'] > 0
    loaded = adapter.call_tool('jiangli', 'context_session_start', {'project': 'demo', 'query': '橙园归档'})
    assert '橙园' in loaded['block']
    count = len(responses)
    assert upload(adapter, transcript(text='橙园项目统一保留会话加密归档，以便跨设备核验历史决策的依据。'), extract=True)['extraction_status'] == 'extracted'
    assert adapter.process_pending() == 0
    assert len(responses) == count


def test_extraction_provider_failure_is_distinct_from_archive_success(lan):
    _, adapter = lan
    upload(adapter, transcript(), extract=True)
    assert adapter.process_pending() == 1
    status = adapter.call_tool('jiangli', 'session_archive_status', {'device_id': 'windows-main', 'session_id': 'session-1'})
    assert status['status'] == 'archived'
    assert status['extraction_status'] == 'failed'
    assert status['processing_error'] == 'extraction_provider_unavailable'


def test_memory_revision_changes_on_native_write_but_not_repeated_reads(lan):
    runtime, adapter = lan
    before = adapter.call_tool('jiangli', 'memory_status', {})['memory_revision']
    assert adapter.call_tool('jiangli', 'memory_status', {})['memory_revision'] == before
    runtime.server_for('jiangli').handle_tool_call('memory_add', {
        'key': 'project:demo:decision:version', 'value': '项目每次读取应能发现另一设备已经提交的新记忆。'})
    after = adapter.call_tool('jiangli', 'memory_status', {})['memory_revision']
    assert after != before
    adapter.call_tool('jiangli', 'context_session_start', {'project': 'demo', 'query': '项目记忆'})
    assert adapter.call_tool('jiangli', 'memory_status', {})['memory_revision'] == after


def test_mixed_workspace_is_archived_but_requires_explicit_attribution(lan):
    runtime, adapter = lan
    raw = transcript() + b'{"type":"turn_context","payload":{"cwd":"D:\\\\other"}}\n'
    result = upload(adapter, raw, extract=True)
    assert result['status'] == 'archived'
    assert result['project'] == ''
    assert result['attribution_reason'] == 'mixed_workspace'
    assert result['extraction_status'] == 'unassigned'
    assert adapter.process_pending() == 0
    assert upload(adapter, raw, extract=True)['archive_id'] == result['archive_id']
    registered = adapter.call_tool('jiangli', 'continuity_begin', {'project': 'demo',
        'workspace_path': r'C:\work\demo', 'device_id': 'windows-main',
        'objective': '登记归档验收项目', 'request_id': 'register-demo'})
    assert 'error' not in registered, registered
    identity = dict(device_id='windows-main', session_id='session-1')
    assigned = adapter.call_tool('jiangli', 'session_archive_assign', {**identity, 'project': 'demo', 'request_id': 'assign-demo'})
    assert assigned['project'] == 'demo', assigned
    assert assigned['extraction_status'] == 'pending'
    assert upload(adapter, raw, extract=True)['archive_id'] == result['archive_id']


def test_retry_preserves_archive_and_requeues_failed_extraction(lan):
    _, adapter = lan
    saved = upload(adapter, transcript(), extract=True)
    adapter.process_pending()
    result = adapter.call_tool('jiangli', 'session_archive_retry', {
        'device_id': 'windows-main', 'session_id': 'session-1', 'request_id': 'retry-extract'})
    assert result['archive_id'] == saved['archive_id']
    assert result['extraction_status'] == 'pending'
    assert 'processing_error' not in result


def test_idle_bound_archive_backfills_without_focus_or_completed_claims(lan):
    import time
    runtime, adapter = lan
    args = dict(workspace_path=r'C:\work\demo', device_id='windows-main', project='demo')
    begun = adapter.call_tool('jiangli', 'continuity_begin', {
        **args, 'objective': '保留人工创建的项目焦点', 'request_id': 'manual-focus'})
    assert 'workstream_id' in begun, begun
    raw = transcript(text='验证中断日志的保守补录')
    raw += b'{"type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"unfinished","arguments":"{}"}}\n'
    upload(adapter, raw)
    assert adapter.process_backfills(now=time.time()) == 0
    assert adapter.process_backfills(now=time.time() + 1900) == 1
    identity = dict(device_id='windows-main', session_id='session-1')
    status = adapter.call_tool('jiangli', 'session_archive_status', identity)
    assert status['backfill']['code'] == 'created', status
    assert status['backfill']['workstream_id'] != begun['workstream_id']
    focus = adapter.call_tool('jiangli', 'continuity_resume', {
        'workspace_path': args['workspace_path'], 'device_id': args['device_id'], 'project_hint': 'demo'})
    assert focus['workstream_id'] == begun['workstream_id']
    store = runtime.server_for('jiangli').context_service.store
    row = store._connection().execute('SELECT * FROM continuity_workstreams WHERE id=?',
        (status['backfill']['workstream_id'],)).fetchone()
    assert row['status'] not in ('completed', 'cancelled')
    assert adapter.process_backfills(now=time.time() + 1900) == 0


def test_unbound_archive_backfill_stays_candidate(lan):
    import time
    _, adapter = lan
    upload(adapter, transcript())
    assert adapter.process_backfills(now=time.time() + 1900) == 1
    status = adapter.call_tool('jiangli', 'session_archive_status', {
        'device_id': 'windows-main', 'session_id': 'session-1'})
    assert status['backfill']['code'] == 'candidate'
    assert status['backfill']['reason'] == 'workspace_unbound'
