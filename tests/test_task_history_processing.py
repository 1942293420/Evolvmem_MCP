"""Exact owner grants can update one project while global AI stays paused."""
import hashlib
import json
import pytest
from tests.test_lan_sharing import lan
from tests.test_lan_server import http_lan
from tests.test_lan_capture import transcript, upload

TOOL = 'session_archive_process_history'

@pytest.fixture(autouse=True)
def paused(tmp_path, monkeypatch):
    path = tmp_path / 'paused.json'
    path.write_text(json.dumps({'ai_processing_enabled': False, 'legacy_web_enabled': False}))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(path))
    return path

def setup(lan, monkeypatch):
    from evolvmem import auto_organization
    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    service.knowledge().save_project({'project': 'demo'})
    raw = transcript(text='Synthetic task completed an export with verified tests.')
    receipt = upload(adapter, raw)
    calls = []
    def provider(prompt, config, **kwargs):
        calls.append(prompt)
        grant = kwargs['task_history_grant']
        from evolvmem.product_policy import require_ai_processing
        require_ai_processing(task_history_grant=grant)
        return json.dumps({'events': [{'event_type': 'feature', 'title': 'Export verified',
                                     'content': 'Export passed synthetic tests.', 'occurred_at': None}]})
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (object(), provider))
    args = {'device_id': 'windows-main', 'session_id': 'session-1',
            'archive_id': receipt['archive_id'], 'sha256': hashlib.sha256(raw).hexdigest(),
            'source_revision': receipt.get('source_revision', '0'*64), 'project': 'demo',
            'source_format': 'codex-jsonl', 'request_id': 'exact-grant-1'}
    return service, adapter, raw, args, calls

def test_exact_owner_route_only_history_and_replays(lan, monkeypatch, paused):
    service, adapter, raw, args, calls = setup(lan, monkeypatch)
    result = adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert result.get('state') == 'completed', result
    assert result['source_revision'] == args['source_revision']
    assert result['source_sha256'] == args['sha256']
    assert result['source_format'] == 'codex-jsonl'
    assert len(calls) == 1
    assert adapter.call_tool('jiangli', TOOL, args, owner=True) == result
    duplicate = adapter.call_tool('jiangli', TOOL, {**args, 'request_id': 'same-source-other-id'}, owner=True)
    assert duplicate == result and len(calls) == 1
    conn = service.store._connection()
    assert conn.execute('SELECT COUNT(*) FROM organization_tasks').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM capability_memories').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM context_items').fetchone()[0] == 0
    proof = adapter.call_tool('jiangli', 'session_archive_status', {
        'device_id': args['device_id'], 'session_id': args['session_id'],
        'handoff': {'sha256': args['sha256'], 'project': 'demo'}})['handoff']
    assert proof['state'] == 'ready', proof
    assert json.loads(paused.read_text()) == {'ai_processing_enabled': False, 'legacy_web_enabled': False}
    from evolvmem.product_policy import require_ai_processing, AIProcessingDisabled
    with pytest.raises(AIProcessingDisabled): require_ai_processing()

@pytest.mark.parametrize('user,owner', [('jiangli', False), ('kane', False), ('kane', True)])
def test_owner_endpoint_required(lan, monkeypatch, user, owner):
    _, adapter, _, args, calls = setup(lan, monkeypatch)
    assert 'error' in adapter.call_tool(user, TOOL, args, owner=owner)
    assert not calls

@pytest.mark.parametrize('field,value', [('archive_id', 999), ('sha256', 'f'*64),
    ('source_revision', 'f'*64), ('project', 'other'), ('source_format', 'dsh-session-jsonl-v1')])
def test_tuple_mismatch_never_calls_model(lan, monkeypatch, field, value):
    _, adapter, _, args, calls = setup(lan, monkeypatch)
    assert 'error' in adapter.call_tool('jiangli', TOOL, {**args, field: value}, owner=True)
    assert not calls

def test_stable_request_id_is_mandatory_and_conflict_is_rejected(lan, monkeypatch):
    _, adapter, _, args, calls = setup(lan, monkeypatch)
    missing = dict(args); missing.pop('request_id')
    assert 'error' in adapter.call_tool('jiangli', TOOL, missing, owner=True)
    assert not calls
    assert adapter.call_tool('jiangli', TOOL, args, owner=True).get('state') == 'completed'
    assert 'error' in adapter.call_tool('jiangli', TOOL, {**args, 'sha256': 'f'*64}, owner=True)
    assert len(calls) == 1

def test_lost_provider_result_blocks_retry_instead_of_calling_again(lan, monkeypatch):
    from evolvmem import auto_organization
    service, adapter, _, args, calls = setup(lan, monkeypatch)
    def lost(*a, **kw):
        calls.append('started'); raise RuntimeError('private provider detail')
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (object(), lost))
    first = adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert first.get('error') == 'request_indeterminate'
    assert adapter.call_tool('jiangli', TOOL, args, owner=True).get('error') == 'request_indeterminate'
    assert adapter.call_tool('jiangli', TOOL, {**args, 'request_id': 'new-id'}, owner=True).get('error') == 'request_indeterminate'
    assert calls == ['started']
    assert service.store._connection().execute("SELECT COUNT(*) FROM organization_tasks WHERE status='pending'").fetchone()[0] == 0

def test_source_change_in_flight_discards_response(lan, monkeypatch):
    from evolvmem import auto_organization
    service, adapter, raw, args, calls = setup(lan, monkeypatch)
    def stale(*a, **kw):
        calls.append('started')
        upload(adapter, raw + b'{"type":"event_msg","payload":{"type":"token_count"}}\n')
        return {'events': [{'event_type': 'feature', 'title': 'Stale', 'content': 'Must not publish'}]}
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (object(), stale))
    result = adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert 'error' in result and len(calls) == 1
    assert service.store._connection().execute('SELECT COUNT(*) FROM project_events').fetchone()[0] == 0

def test_invalid_policy_denies_even_explicit_grant(lan, monkeypatch, paused):
    _, adapter, _, args, calls = setup(lan, monkeypatch)
    paused.write_text('{broken')
    assert 'error' in adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert not calls


def test_unfinished_grant_never_enters_generic_queue_when_enabled(lan, monkeypatch, paused):
    from evolvmem import auto_organization as org, organization_task_upgrade
    service, adapter, _, args, calls = setup(lan, monkeypatch)
    def lost(*a, **kw):
        raise RuntimeError('lost response')
    monkeypatch.setattr(org, '_load_llm', lambda: (object(), lost))
    assert adapter.call_tool('jiangli', TOOL, args, owner=True).get('error') == 'request_indeterminate'
    paused.write_text(json.dumps({'ai_processing_enabled': True, 'legacy_web_enabled': True}))
    worker = org.OrganizationWorker.__new__(org.OrganizationWorker)
    worker._claim_offset = 0
    assert worker._claim(service) is None
    assert organization_task_upgrade.advance(service) == {'created': 0, 'examined': 0}
    receipt = adapter.call_tool('jiangli', 'session_archive_status', {
        'device_id': args['device_id'], 'session_id': args['session_id']})
    assert receipt['task_history']['state'] == 'running'
    assert receipt['task_history']['source_revision'] == args['source_revision']


def test_scoped_http_path_one_attempt_and_no_ambient_bypass(lan, monkeypatch):
    from evolvmem import auto_organization, kimi_hooks as hooks
    from evolvmem.product_policy import require_ai_processing, AIProcessingDisabled
    service, adapter, _, args, calls = setup(lan, monkeypatch)
    response = {'events': [{'event_type': 'feature', 'title': 'Scoped HTTP', 'content': 'Synthetic HTTP result'}]}
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return json.dumps({'choices': [{'message': {'content': json.dumps(response)}}]}).encode()
    class FakeOpener:
        def open(self, request, timeout):
            calls.append('http')
            with pytest.raises(AIProcessingDisabled): require_ai_processing()
            return FakeResponse()
    monkeypatch.setattr(hooks.urllib.request, 'build_opener', lambda *a: FakeOpener())
    config = hooks.LLMConfig('deepseek', 'synthetic-test-key', 'https://example.invalid', 'synthetic')
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (config, hooks._call_llm_with_retry))
    assert adapter.call_tool('jiangli', TOOL, args, owner=True).get('state') == 'completed'
    assert calls == ['http']


def test_scoped_unknown_http_outcome_has_no_retry_or_fallback(lan, monkeypatch):
    from evolvmem import auto_organization, kimi_hooks as hooks
    _, adapter, _, args, calls = setup(lan, monkeypatch)
    class FakeOpener:
        def open(self, request, timeout):
            calls.append('http'); raise TimeoutError('synthetic lost response')
    monkeypatch.setattr(hooks.urllib.request, 'build_opener', lambda *a: FakeOpener())
    backup = hooks.LLMConfig('deepseek', 'synthetic-backup', 'https://backup.invalid', 'backup')
    config = hooks.LLMConfig('deepseek', 'synthetic-test-key', 'https://example.invalid', 'synthetic', backup)
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (config, hooks._call_llm_with_retry))
    assert adapter.call_tool('jiangli', TOOL, args, owner=True).get('error') == 'request_indeterminate'
    assert calls == ['http']


@pytest.mark.parametrize('empty_events', [False, True])
def test_first_history_call_preserves_startup_health_schema(lan, monkeypatch, empty_events):
    from evolvmem import auto_organization
    from evolvmem.health_generation import observed
    service, adapter, _, args, calls = setup(lan, monkeypatch)
    runtime, _ = lan
    manager = runtime._background_health
    conn = service.store._connection()
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    assert observed(service.store, schema) is not None
    hard_before, _ = manager._observation('jiangli', conn)
    assert hard_before == manager._contracts['jiangli']
    if empty_events:
        def provider(*a, **kwargs):
            calls.append('synthetic empty event result')
            return json.dumps({'events': []})
        monkeypatch.setattr(auto_organization, '_load_llm', lambda: (object(), provider))
    result = adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert result.get('state') == 'completed', result
    assert len(calls) == 1
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    assert observed(service.store, schema) is not None
    assert manager._observation('jiangli', conn)[0] == hard_before
    assert [row[0] for row in conn.execute('PRAGMA quick_check')] == ['ok']
    manager.refresh('jiangli')
    manager.run_once()
    assert manager.status('jiangli')['hard_gate'] == 'allowed'
    recall = adapter.call_tool('jiangli', 'context_project_recall', {'query': 'demo', 'space': 'personal'}, owner=True)
    assert 'error' not in recall, recall
    if empty_events:
        assert result['event_ids'] == [] and result['summary_revision'] == 0
        proof = adapter.call_tool('jiangli', 'session_archive_status', {
            'device_id': args['device_id'], 'session_id': args['session_id'],
            'handoff': {'sha256': args['sha256'], 'project': 'demo'}})['handoff']
        assert proof['state'] != 'ready'


def test_grant_schema_is_prepared_only_for_owner_before_health(lan):
    from evolvmem.health_generation import observed
    runtime, adapter = lan
    for user, space in [('jiangli', 'personal'), ('kane', 'personal'), ('jiangli', 'public')]:
        service = runtime.server_for(user, space).context_service
        conn = service.store._connection()
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        assert observed(service.store, schema) is not None
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_history_grants'").fetchone()
        assert bool(present) == (user == 'jiangli' and space == 'personal')
    assert TOOL in adapter._catalog_specs[('jiangli', True)]
    for role in [('jiangli', False), ('kane', True), ('kane', False)]:
        assert TOOL not in adapter._catalog_specs[role]


def test_request_does_not_recreate_missing_grant_schema(lan, monkeypatch):
    service, adapter, _, args, calls = setup(lan, monkeypatch)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('DROP TABLE IF EXISTS task_history_grants')
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    receipt = adapter.call_tool('jiangli', 'session_archive_status', {
        'device_id': args['device_id'], 'session_id': args['session_id'],
        'handoff': {'sha256': args['sha256'], 'project': 'demo'}})
    assert 'error' not in receipt and receipt.get('task_history') is None
    assert receipt['handoff']['state'] != 'ready'
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    result = adapter.call_tool('jiangli', TOOL, args, owner=True)
    assert result.get('error') == 'task_history_schema_unprepared'
    assert not calls
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_history_grants'").fetchone()


@pytest.mark.parametrize('empty_events', [False, True])
def test_http_first_history_write_keeps_resident_health_and_project_recall(http_lan, monkeypatch, empty_events):
    import base64
    import http.client
    from evolvmem import auto_organization
    from evolvmem.health_generation import observed
    server, runtime = http_lan  # Real production make_http_server/worker/physical-owner startup.
    service = runtime.server_for('jiangli').context_service
    service.knowledge().save_project({'project': 'demo'})
    conn = service.store._connection()
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    assert observed(service.store, schema) is not None
    calls = []
    def provider(*a, **kwargs):
        calls.append('synthetic provider')
        return json.dumps({'events': [] if empty_events else [
            {'event_type': 'feature', 'title': 'HTTP startup verified', 'content': 'Synthetic scoped history verified.'}]})
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (object(), provider))
    def owner_call(name, arguments):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request('POST', '/owner/mcp', body=json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}), headers={
                    'Content-Type': 'application/json', 'Authorization': 'Bearer jiangli-token',
                    'X-EvolvMem-Expected-User': 'jiangli'})
            reply = connection.getresponse()
            body = json.loads(reply.read())
            assert reply.status == 200 and body['id'] == 1
            result = body['result']
            data = json.loads(result['content'][0]['text'])
            assert not result.get('isError') and 'error' not in data, data
            assert data['authenticated_user'] == 'jiangli'
            return data
        finally:
            connection.close()
    raw = transcript(text='Synthetic HTTP startup regression source.')
    sha = hashlib.sha256(raw).hexdigest()
    receipt = owner_call('session_archive_upload', {
        'device_id': 'windows-main', 'session_id': 'session-1', 'project': 'demo',
        'sha256': sha, 'total_bytes': len(raw), 'offset': 0, 'extract': False,
        'content_b64': base64.b64encode(raw).decode(), 'request_id': 'http-startup-upload'})
    result = owner_call(TOOL, {
        'device_id': 'windows-main', 'session_id': 'session-1', 'project': 'demo',
        'archive_id': receipt['archive_id'], 'sha256': sha,
        'source_revision': receipt['source_revision'], 'source_format': 'codex-jsonl',
        'request_id': 'http-startup-history'})
    assert result['state'] == 'completed' and calls == ['synthetic provider']
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    assert observed(service.store, schema) is not None
    health = owner_call('memory_status', {'detail': 'health', 'space': 'personal'})
    assert health['hard_gate'] == 'allowed'
    assert 'error' not in owner_call('context_project_recall', {'query': 'demo', 'space': 'personal'})
    assert [row[0] for row in conn.execute('PRAGMA quick_check')] == ['ok']
    if empty_events:
        assert result['event_ids'] == [] and result['summary_revision'] == 0
