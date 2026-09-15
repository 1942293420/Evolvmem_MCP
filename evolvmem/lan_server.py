"""Stateless JSON Streamable HTTP transport for trusted LAN CLI clients."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
from pathlib import Path

from evolvmem.embedding_client import valid_vector
from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_tools import LanTools, PROTOCOLS

MAX_BODY_BYTES = 1024 * 1024


def owner_peer_allowed(peer, user):
    try:
        return user == "jiangli" and ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def make_http_server(runtime, host=None, port=None):
    adapter = LanTools(runtime)
    runtime.start_capture_worker(adapter)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            # Request paths, tokens, bodies and exceptions are never logged.
            pass

        def _reply(self, status, payload=None, headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b''
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            if body:
                self.send_header('Content-Type', 'application/json')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _authenticate(self):
            values = self.headers.get_all('Authorization', [])
            value = values[0] if len(values) == 1 else ''
            user = runtime.settings.authenticate(value[7:]) if value.startswith('Bearer ') else None
            if user is None:
                self._reply(401, {'error': 'unauthorized'}, {'WWW-Authenticate': 'Bearer'})
                return None
            expected_users = self.headers.get_all('X-EvolvMem-Expected-User', [])
            if expected_users and (len(expected_users) != 1 or expected_users[0] != user):
                self._reply(403, {'error': 'expected_user_mismatch'})
                return None
            if 'Origin' in self.headers:
                self._reply(403, {'error': 'origin_forbidden'})
                return None
            if self.path == '/owner/mcp' and not owner_peer_allowed(self.client_address[0], user):
                self._reply(403, {'error': 'owner_forbidden'})
                return None
            if self.path not in ('/mcp', '/owner/mcp', '/embedding', '/embedding/status'):
                self._reply(404, {'error': 'not_found'})
                return None
            version = self.headers.get('MCP-Protocol-Version')
            if version is not None and version not in PROTOCOLS:
                self._reply(400, {'error': 'unsupported_protocol_version'})
                return None
            return user

        def do_POST(self):
            user = self._authenticate()
            if user is None:
                return
            if self.headers.get_content_type() != 'application/json':
                self._reply(415, {'error': 'unsupported_media_type'})
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or 'Transfer-Encoding' in self.headers:
                self._reply(400, {'error': 'invalid_body_length'})
                return
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if length < 0:
                self._reply(400, {'error': 'invalid_body_length'})
                return
            if length > MAX_BODY_BYTES:
                self._reply(413, {'error': 'body_too_large'})
                return
            self.connection.settimeout(10)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self._reply(400, {'error': 'incomplete_body'})
                    return
                def reject_constant(_value):
                    raise ValueError('non-JSON number')
                request = json.loads(raw, parse_constant=reject_constant)
            except (ValueError, UnicodeError):
                self._reply(400, adapter._rpc_error(None, -32700, 'Parse error'))
                return
            except OSError:
                self._reply(400, {'error': 'incomplete_body'})
                return
            try:
                if self.path.startswith('/embedding'):
                    # Model serialization is separate from SQLite dispatch: native
                    # extraction can hold a SQLite transaction while calling here.
                    engine = runtime._shared_engine
                    config = runtime.server_for(user).config
                    available = bool(engine.is_loaded)
                    if self.path == '/embedding/status':
                        result = dict(available=available, dimension=config.embedding_dim,
                                      query_prefix=config.embedding_query_prefix, document_prefix=config.embedding_doc_prefix)
                    elif not available:
                        self._reply(503, {'error': 'embedding_unavailable'})
                        return
                    elif (not isinstance(request, dict) or set(request) != {'kind', 'text'}
                          or request.get('kind') not in ('query', 'document', 'raw')
                          or not isinstance(request.get('text'), str) or len(request['text']) > 32768):
                        self._reply(400, {'error': 'invalid_embedding_input'})
                        return
                    else:
                        encode = {'query': engine.encode_query, 'document': engine.encode_document, 'raw': engine.encode}[request['kind']]
                        vector = encode(request['text'])
                        if not valid_vector(vector, config.embedding_dim):
                            self._reply(502, {'error': 'invalid_embedding_response'})
                            return
                        result = {'vector': vector}
                else:
                    result = adapter.handle_request(user, request, owner=self.path == '/owner/mcp')
            except Exception:
                self._reply(500, adapter._rpc_error(None, -32603, 'Internal error'))
                return
            self._reply(202 if result is None else 200, result)

        def do_GET(self):
            if self._authenticate() is not None:
                self._reply(405, {'error': 'method_not_allowed'}, {'Allow': 'POST'})

        do_DELETE = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_OPTIONS = do_GET

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = True

        def handle_error(self, request, client_address):
            # Base implementation prints exception tracebacks; avoid that path.
            pass

    return Server((runtime.settings.host if host is None else host,
                   runtime.settings.port if port is None else port), Handler)


def main():
    parser = argparse.ArgumentParser(description='EvolvMem trusted LAN MCP')
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    runtime = LanRuntime(LanSettings.from_file(args.config))
    server = None
    try:
        runtime.initialize()
        server = make_http_server(runtime)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        runtime.close()


if __name__ == '__main__':
    main()
