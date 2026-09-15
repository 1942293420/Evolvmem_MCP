"""Stateless JSON Streamable HTTP transport for trusted LAN CLI clients."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_tools import LanTools, PROTOCOLS

MAX_BODY_BYTES = 1024 * 1024


def make_http_server(runtime, host=None, port=None):
    adapter = LanTools(runtime)

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
            if 'Origin' in self.headers:
                self._reply(403, {'error': 'origin_forbidden'})
                return None
            if self.path != '/mcp':
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
                result = adapter.handle_request(user, request)
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
