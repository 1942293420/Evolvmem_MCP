"""Real stdio process, actual loopback HTTP, no client database/model."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from tests.test_lan_server import http_lan
from tests.test_lan_context import begin, SNAP


def owner_request(server, method, args=None, token='jiangli-token'):
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    conn.request('POST', '/owner/mcp', json.dumps(dict(jsonrpc='2.0', id=7, method=method, params=args or {})),
                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    res = conn.getresponse()
    data = json.loads(res.read())
    conn.close()
    return res.status, data


def test_owner_route_preserves_local_identity_after_remote_calls(http_lan, tmp_path):
    http, runtime = http_lan
    from evolvmem.lan_tools import LanTools
    adapter = LanTools(runtime)
    local = runtime.server_for('jiangli')
    workspace = tmp_path / 'localrepo'
    workspace.mkdir()
    original = local.handle_tool_call('continuity_begin', dict(workspace_path=str(workspace), project='demo', objective='original local work'))
    assert 'error' not in original
    assert 'error' not in begin(adapter, 'jiangli', repo_snapshot=SNAP)
    status, response = owner_request(http, 'tools/call', dict(name='continuity_resume', arguments=dict(workspace_path=str(workspace), project_hint='demo')))
    assert status == 200, response
    result = json.loads(response['result']['content'][0]['text'])
    assert result['workstream_id'] == original['workstream_id']
    assert 'repo_source' not in result
    assert owner_request(http, 'initialize', token='kane-token')[0] == 403
    assert owner_request(http, 'initialize', token='wrong')[0] == 401
    # Actual TCP peer check is also applied independently of user / forwarded headers.
    from evolvmem.lan_server import owner_peer_allowed
    assert not owner_peer_allowed('192.168.1.42', 'jiangli')
    assert not owner_peer_allowed('127.0.0.1', 'kane')
    assert owner_peer_allowed('127.0.0.1', 'jiangli')


def client_config(tmp_path, url):
    token = tmp_path / 'token'
    token.write_text('jiangli-token')
    token.chmod(0o600)
    client = tmp_path / 'client.json'
    client.write_text(json.dumps(dict(url=url, token_file=str(token))))
    data = tmp_path / 'client-data'
    data.mkdir()
    (data / 'config.json').write_text(json.dumps(dict(lan_mcp_client_config=str(client))))
    return data


def test_existing_mcp_entry_forwards_tools_writes_public_and_eof(http_lan, tmp_path):
    http, runtime = http_lan
    data = client_config(tmp_path, f'http://127.0.0.1:{http.server_port}/owner/mcp')
    requests = [dict(jsonrpc='2.0', id=i, method=method, params=params) for i, method, params in [
        (1, 'initialize', {}), (2, 'tools/list', {}),
        (3, 'tools/call', dict(name='memory_status', arguments={})),
        (4, 'tools/call', dict(name='memory_add', arguments=dict(key='project:demo:fact:stdio', value='stdio public demonstration marker'))),
        (5, 'tools/call', dict(name='memory_publish', arguments=dict(source_context_id=1, title='stdio public demonstration', summary='curated stdio demonstration marker'))),
    ]]
    # If main constructs MemoryMCPServer at all this process fails before forwarding.
    script = "from evolvmem import mcp_server; mcp_server.MemoryMCPServer = lambda *a, **k: (_ for _ in ()).throw(AssertionError('local model/database constructed')); mcp_server.main()"
    proc = subprocess.run([sys.executable, '-c', script], input='\n'.join(map(json.dumps, requests))+'\n'+json.dumps(dict(jsonrpc='2.0', method='notifications/initialized'))+'\n',
        text=True, capture_output=True, timeout=15, env={**os.environ, 'EVOLVMEM_DATA_DIR': str(data)})
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r['id'] for r in responses] == [1, 2, 3, 4, 5]
    specs = {s['name']: s for s in responses[1]['result']['tools']}
    assert 'memory_publish' in specs
    assert 'request_id' not in specs['memory_add']['inputSchema'].get('required', [])
    for result in responses[2:]:
        assert not result['result'].get('isError'), result
    assert not (data / 'memory.db').exists()
    assert not (data / 'models').exists()


def test_forwarder_outage_is_truthful_and_credential_free(tmp_path):
    data = client_config(tmp_path, 'http://127.0.0.1:1/owner/mcp')
    proc = subprocess.run([sys.executable, '-m', 'evolvmem.mcp_server'], input=json.dumps(dict(jsonrpc='2.0', id='outage', method='initialize'))+'\n', text=True,
        capture_output=True, timeout=15, env={**os.environ, 'EVOLVMEM_DATA_DIR': str(data)})
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['id'] == 'outage' and result['error']['message'] == 'LAN MCP unavailable'
    assert 'jiangli-token' not in proc.stdout + proc.stderr


def test_nonloopback_owner_rejected_even_with_forwarded_headers(tmp_path):
    import socket
    import threading
    from tests.test_lan_sharing import settings_for
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_server import make_http_server
    import fcntl, struct
    addresses = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _, interface in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', interface.encode()[:15]))
                addresses.append(socket.inet_ntoa(raw[20:24]))
            except OSError:
                pass
    address = next((a for a in addresses if not a.startswith('127.')), None)
    if address is None:
        pytest.skip('host has no nonloopback local address')
    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    server = make_http_server(runtime, host='0.0.0.0', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(address, server.server_port, timeout=5)
        conn.request('POST', '/owner/mcp', '{}', {'Content-Type': 'application/json', 'Authorization': 'Bearer jiangli-token', 'Host': '127.0.0.1', 'X-Forwarded-For': '127.0.0.1'})
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.close()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
        runtime.close()


def test_remote_continuity_over_http_keeps_users_and_devices_isolated(http_lan):
    from tests.test_lan_server import call
    from tests.test_lan_context import PATH, SNAP
    server, runtime = http_lan
    ids = []
    for user, device in [('jiangli', 'pc'), ('kane', 'pc'), ('kane', 'other')]:
        args = dict(workspace_path=PATH, device_id=device, project='demo', objective='HTTP workspace test', repo_snapshot=SNAP, request_id='begin-' + device)
        result, error = call(server, 'continuity_begin', args, token=user+'-token')
        assert not error, result
        ids.append(result['workstream_id'])
        resumed, error = call(server, 'continuity_resume', dict(workspace_path=PATH, device_id=device, project_hint='demo'), token=user+'-token')
        assert not error and resumed['workstream_id'] == result['workstream_id']
        assert resumed['staleness'] == 'unknown'
    assert len(set(ids)) == 3
