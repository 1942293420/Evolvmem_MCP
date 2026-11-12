"""The existing /mcp transport can host isolated libraries without SQLite."""
import http.client
import json
import threading

import pytest

from evolvmem.library_foundation import FoundationError, RequestIdentity
from tests.test_lan_server import request, rpc


PRINCIPAL = '00000000-0000-0000-0000-000000000001'
CREDENTIAL = '00000000-0000-0000-0000-000000000002'
DEVICE = '00000000-0000-0000-0000-000000000003'


class LibraryHost:
    def __init__(self):
        self.identity = RequestIdentity(PRINCIPAL, CREDENTIAL, DEVICE, 1)
        self.calls = []
        self.auth_error = None
        self.after_publish_error = False

    def authenticate(self, token):
        if self.auth_error:
            raise self.auth_error
        return self.identity if token == 'synthetic-library-token' else None

    def serve_rpc(self, identity, request, *, publish):
        assert identity is self.identity
        self.calls.append(request)
        publish({'jsonrpc': '2.0', 'id': request['id'], 'result': {'library_transport': True}})
        if self.after_publish_error:
            raise RuntimeError('PRIVATE failure after transport publication')


@pytest.fixture
def library_http(monkeypatch):
    from evolvmem import lan_server
    from evolvmem.lan_tools import LanTools
    host = LibraryHost()
    monkeypatch.setattr(LanTools, '__init__',
                        lambda *a, **k: pytest.fail('PG-only transport must not initialize SQLite tools'))
    server = lan_server.make_http_server(None, host='127.0.0.1', port=0, library_host=host)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, host
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_new_identity_uses_existing_mcp_endpoint_without_legacy_bootstrap(library_http):
    server, host = library_http
    for method in ('initialize', 'ping', 'tools/list'):
        assert rpc(server, method, token='synthetic-library-token')['result'] == {'library_transport': True}
    assert len(host.calls) == 3
    assert request(server, {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'},
                   token='synthetic-library-token',
                   headers={'X-EvolvMem-Expected-User': PRINCIPAL})[0] == 200


@pytest.mark.parametrize('path', ['/owner/mcp', '/embedding', '/embedding/status', '/rerank'])
def test_library_identity_cannot_enter_legacy_owner_or_shared_model_endpoints(library_http, path):
    server, host = library_http
    client = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        client.request('POST', path, body='{}', headers={'Content-Type': 'application/json',
                       'Authorization': 'Bearer synthetic-library-token'})
        response = client.getresponse()
        assert response.status == 403
        assert json.loads(response.read()) == {'error': 'library_endpoint_forbidden'}
    finally:
        client.close()
    assert not host.calls


def test_new_route_keeps_http_auth_origin_expected_identity_and_json_checks(library_http):
    server, host = library_http
    assert request(server, {}, token='bad')[0] == 401
    assert request(server, {}, token='synthetic-library-token', headers={'Origin': 'https://example.invalid'})[0] == 403
    assert request(server, {}, token='synthetic-library-token', headers={'X-EvolvMem-Expected-User': 'jiangli'})[0] == 403
    assert request(server, raw='{', token='synthetic-library-token')[1]['error']['code'] == -32700
    assert not host.calls


def test_catalog_outage_is_not_silently_treated_as_a_bad_token(library_http):
    server, host = library_http
    host.auth_error = FoundationError('catalog_unavailable')
    status, body = request(server, {}, token='synthetic-library-token')
    assert status == 503 and body == {'error': 'library_authentication_unavailable'}
    assert not host.calls


def test_host_cannot_return_a_legacy_user_string_as_a_library_identity(library_http):
    server, host = library_http
    host.identity = 'jiangli'
    status, body = request(server, {}, token='synthetic-library-token')
    assert status == 503 and body == {'error': 'library_authentication_unavailable'}
    assert not host.calls


def test_no_second_http_reply_after_library_publication(library_http, monkeypatch):
    from http.server import BaseHTTPRequestHandler
    server, host = library_http
    emitted = []
    original = BaseHTTPRequestHandler.send_response
    def record(handler, status, message=None):
        emitted.append(status)
        return original(handler, status, message)
    monkeypatch.setattr(BaseHTTPRequestHandler, 'send_response', record)
    host.after_publish_error = True
    assert rpc(server, 'ping', token='synthetic-library-token')['result'] == {'library_transport': True}
    assert emitted == [200]


def test_library_only_host_requires_explicit_bind_address_and_port():
    from evolvmem.lan_server import make_http_server
    with pytest.raises(ValueError):
        make_http_server(None, library_host=LibraryHost())


def test_existing_runtime_and_library_identity_dispatch_remain_separate(tmp_path):
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_server import make_http_server
    from tests.test_lan_sharing import settings_for
    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    host = LibraryHost()
    server = make_http_server(runtime, host='127.0.0.1', port=0, library_host=host)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert rpc(server, 'ping', token='jiangli-token')['result'] == {}
        assert rpc(server, 'ping', token='synthetic-library-token')['result'] == {'library_transport': True}
        assert len(host.calls) == 1
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
        runtime.close()
