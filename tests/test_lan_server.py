"""Actual loopback Streamable HTTP acceptance, without a model or live data."""
import http.client
import json
import threading

import pytest

from tests.test_lan_sharing import settings_for
from evolvmem.lan_runtime import LanRuntime


@pytest.fixture
def http_lan(tmp_path):
    from evolvmem.lan_server import make_http_server
    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    server = make_http_server(runtime, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, runtime
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()
    runtime.close()


def request(server, payload=None, token='jiangli-token', method='POST', headers=None, raw=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    hdr = {'Content-Type': 'application/json'}
    if token is not None:
        hdr['Authorization'] = 'Bearer ' + token
    hdr.update(headers or {})
    body = raw if raw is not None else json.dumps(payload)
    connection.request(method, '/mcp', body=body, headers=hdr)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, json.loads(data) if data else None


def rpc(server, method, params=None, token='jiangli-token', id=1):
    return request(server, {'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or {}}, token)[1]


def call(server, name, args, token='jiangli-token'):
    result = rpc(server, 'tools/call', {'name': name, 'arguments': args}, token)['result']
    return json.loads(result['content'][0]['text']), result.get('isError', False)


def test_http_auth_protocol_negotiation_registry_and_invalid_requests(http_lan):
    server, runtime = http_lan
    for token in (None, '', 'wrong'):
        status, body = request(server, {'method': 'tools/list'}, token)
        assert status == 401
        assert str(runtime.settings.data_dir) not in json.dumps(body)
    for version in ('2025-11-25', '2025-03-26'):
        assert rpc(server, 'initialize', {'protocolVersion': version})['result']['protocolVersion'] == version
    assert rpc(server, 'initialize', {'protocolVersion': '2099-01-01'})['result']['protocolVersion'] in ('2025-11-25', '2025-03-26')
    specs = rpc(server, 'tools/list')['result']['tools']
    assert 'request_id' in next(s['inputSchema']['required'] for s in specs if s['name'] == 'memory_add')
    core = runtime.server_for('jiangli')._handle_request({'id': 1, 'method': 'tools/list'})
    assert 'request_id' not in next(s['inputSchema']['properties'] for s in core['result']['tools'] if s['name'] == 'memory_add')
    assert request(server, raw='{')[1]['error']['code'] == -32700
    assert request(server, payload=[])[1]['error']['code'] == -32600
    assert rpc(server, 'unknown')['error']['code'] == -32601
    assert rpc(server, 'ping')['result'] == {}
    assert request(server, method='GET')[0] == 405
    assert request(server, {}, headers={'Origin': 'http://localhost'})[0] == 403
    assert request(server, {}, headers={'Content-Type': 'text/plain'})[0] == 415
    assert request(server, {}, headers={'MCP-Protocol-Version': '2099-01-01'})[0] == 400
    assert request(server, raw='x' * (1024 * 1024 + 1))[0] == 413


def test_http_two_clients_public_lifecycle_and_notifications_never_write(http_lan):
    server, _ = http_lan
    ids = {}
    for user, marker in (('jiangli', 'orchard'), ('kane', 'banana')):
        args = {'key': f'project:lan:fact:{marker}', 'value': f'{user} private {marker} marker', 'request_id': 'add'}
        first, error = call(server, 'memory_add', args, user + '-token')
        assert not error
        assert call(server, 'memory_add', args, user + '-token')[0] == first
        ids[user] = first['context_id']
    assert ids['jiangli'] == ids['kane']
    assert call(server, 'memory_search', {'query': 'orchard'}, 'kane-token')[0]['count'] == 0
    published, error = call(server, 'memory_publish', {'source_context_id': ids['jiangli'], 'title': 'public orchard', 'summary': 'curated public orchard lesson', 'request_id': 'pub'})
    assert not error
    assert call(server, 'context_read', {'ref': published['ref']}, 'kane-token')[0]['content'] == 'curated public orchard lesson'
    assert call(server, 'memory_unpublish', {'id': published['id'], 'request_id': 'withdraw'}, 'kane-token')[1]
    assert not call(server, 'memory_unpublish', {'id': published['id'], 'request_id': 'withdraw'})[1]
    assert call(server, 'context_read', {'ref': published['ref']}, 'kane-token')[1]
    status, body = request(server, {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'memory_add', 'arguments': {'key': 'note', 'value': 'notification mutation forbidden', 'request_id': 'note'}}})
    assert status == 202 and body is None
    assert call(server, 'memory_search', {'query': 'notification'})[0]['count'] == 0


def test_http_parallel_retries_write_once(http_lan):
    from concurrent.futures import ThreadPoolExecutor
    server, _ = http_lan
    args = {'key': 'project:lan:fact:parallel', 'value': 'parallel retry single effect marker', 'request_id': 'parallel'}
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: call(server, 'memory_add', args, 'kane-token'), range(6)))
    assert all(not error for _, error in responses)
    assert all(result == responses[0][0] for result, _ in responses)
    assert call(server, 'memory_search', {'query': 'parallel'}, 'kane-token')[0]['count'] == 1
