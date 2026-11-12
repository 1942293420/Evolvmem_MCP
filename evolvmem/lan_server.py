"""Stateless JSON Streamable HTTP transport for trusted LAN CLI clients."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import getpass
import json
import inspect
import ipaddress
import os
from pathlib import Path
import uuid

from evolvmem.lan_config import LanSettings
from evolvmem.console_access import console_user_gate
from evolvmem.library_startup import (load_server_configuration,build_library_host,
    request_backend,requires_library_backend)
from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_host import LibraryHost, PROTOCOLS, _rpc_error

MAX_BODY_BYTES = 1024 * 1024


def owner_peer_allowed(peer, user, owner_user=None):
    expected_owner = owner_user or (os.environ.get("EVOLVMEM_CONSOLE_OWNER_ID") or "").strip() or getpass.getuser()
    try:
        return user == expected_owner and ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _accepts_kwarg(fn, name):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def make_http_server(runtime, host=None, port=None, *, library_host=None, deadline_guard=None, allow_legacy=True, agent_registry=None, device_resolver=None):
    """Share the real transport, keeping library identities out of SQLite.

    The optional host is trusted application code, never request configuration.
    Its RPC method must publish while its authorization lease is still held.
    """
    if type(allow_legacy) is not bool or (runtime is not None and not allow_legacy):
        raise ValueError('legacy runtime must be absent when legacy endpoints are disabled')
    if library_host is not None and not all(callable(getattr(library_host, name, None))
                                            for name in ('authenticate', 'serve_rpc')):
        raise ValueError('invalid library host')
    if deadline_guard is not None:
        from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
        if (not isinstance(deadline_guard, SyncRequestDeadlineGuard)
                or library_host is None or getattr(library_host, 'deadline_guard', None) is not deadline_guard):
            raise ValueError('HTTP and library host must share the concrete request guard')
    elif getattr(library_host, 'deadline_guard', None) is not None:
        raise ValueError('guarded library host requires explicit HTTP guard wiring')
    if runtime is None and (library_host is None or host is None or port is None):
        raise ValueError('library-only transport requires an explicit bind address and port')
    scoped_host = library_host if isinstance(library_host, LibraryHost) else None
    if scoped_host is not None and deadline_guard is not None:
        raise ValueError('request-owned host cannot borrow a static HTTP guard')
    if runtime is not None:
        from evolvmem.lan_tools import LanTools
        if agent_registry is not None or device_resolver is not None:
            adapter = LanTools(runtime, agent_registry=agent_registry, device_resolver=device_resolver)
        else:
            adapter = LanTools(runtime)
    else:
        adapter = None
    if runtime is not None:
        runtime.start_capture_worker(adapter)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            # Request paths, tokens, bodies and exceptions are never logged.
            pass

        def _reply(self, status, payload=None, headers=None):
            guard = getattr(self, '_deadline_guard', None)
            if guard is None:
                return self._write_reply(status, payload, headers)
            with guard.operation(kind='output'):
                self.connection.settimeout(guard.remaining_seconds())
                self._write_reply(status, payload, headers)

        def _write_reply(self, status, payload=None, headers=None):
            if getattr(self, '_response_started', False):
                raise RuntimeError('response_already_started')
            body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b''
            guard = getattr(self, '_deadline_guard', None)
            if guard is not None:
                guard.check()  # serialization itself may have spent the budget
            self._response_started = True
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
                if guard is not None:
                    self.connection.settimeout(guard.remaining_seconds())
                self.wfile.write(body)
                if guard is not None:
                    self.wfile.flush()
                    guard.check()
            except (BrokenPipeError, ConnectionResetError):
                if guard is not None:
                    raise  # preserve delivery failure while the lease is held
                pass

        def _authenticate(self):
            self._library_request = False
            values = self.headers.get_all('Authorization', [])
            value = values[0] if len(values) == 1 else ''
            token = value[7:] if value.startswith('Bearer ') else None
            try:
                selected=request_backend(self.path,self.headers.get_all('X-EvolvMem-Backend',[]),
                                         library_host is not None,allow_legacy)
            except FoundationError as exc:
                self._reply(503 if exc.code=='postgres_backend_disabled' else 400,{'error':exc.code})
                return None
            # Explicit PG selection is resolved before any legacy-token lookup.
            # A token existing in both catalogs cannot silently choose SQLite.
            user=(runtime.settings.authenticate(token) if selected!='postgres'
                  and runtime is not None and token is not None else None)
            if user is None and selected in ('postgres','auto') and library_host is not None and token is not None:
                try:
                    if scoped_host is not None:
                        self._deadline_guard = self._request_stack.enter_context(
                            scoped_host.request_scope(self.connection))
                    elif deadline_guard is not None:
                        self._deadline_guard = deadline_guard
                        self._request_stack.enter_context(deadline_guard.enforce())
                        deadline_guard.bind_socket(self.connection)
                    user = library_host.authenticate(token)
                    if self._deadline_guard is not None:
                        self._deadline_guard.check()
                    if user is not None and not isinstance(user, RequestIdentity):
                        raise RuntimeError('invalid_library_identity')
                    self._library_request = user is not None
                except FoundationError as exc:
                    if exc.code != 'unauthorized':
                        self._reply(503, {'error': 'library_authentication_unavailable'})
                        return None
                except Exception:
                    self._reply(503, {'error': 'library_authentication_unavailable'})
                    return None
            if user is None:
                self._reply(401, {'error': 'unauthorized'}, {'WWW-Authenticate': 'Bearer'})
                return None
            if not self._library_request and runtime is not None:
                gate = console_user_gate(user, runtime.settings)
                if gate is not None:
                    self._reply(*gate)
                    return None
            expected_users = self.headers.get_all('X-EvolvMem-Expected-User', [])
            expected_identity = user.principal_id if self._library_request else user
            if expected_users and (len(expected_users) != 1 or expected_users[0] != expected_identity):
                self._reply(403, {'error': 'expected_user_mismatch'})
                return None
            if 'Origin' in self.headers:
                self._reply(403, {'error': 'origin_forbidden'})
                return None
            if self._library_request and self.path not in ('/mcp','/library/mcp'):
                self._reply(403, {'error': 'library_endpoint_forbidden'})
                return None
            expected_owner = getattr(runtime, 'owner_user', None) if runtime is not None else None
            if self.path == '/owner/mcp' and not owner_peer_allowed(self.client_address[0], user, owner_user=expected_owner):
                self._reply(403, {'error': 'owner_forbidden'})
                return None
            if self.path not in ('/mcp', '/library/mcp', '/owner/mcp', '/embedding', '/embedding/status', '/rerank'):
                self._reply(404, {'error': 'not_found'})
                return None
            version = self.headers.get('MCP-Protocol-Version')
            if version is not None and version not in PROTOCOLS:
                self._reply(400, {'error': 'unsupported_protocol_version'})
                return None
            return user

        def _run_request(self, operation):
            self._deadline_guard = None
            try:
                with ExitStack() as stack:
                    self._request_stack = stack
                    operation()
            except Exception:
                if self._deadline_guard is None:
                    raise
                # Cancellation/partial publication cannot be replaced with a
                # fresh JSON response after the guard/authority scope exits.
                self.close_connection = True

        def do_POST(self):
            self._run_request(self._post)

        def _post(self):
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
            guard = self._deadline_guard
            self.connection.settimeout(10 if guard is None else min(10, guard.remaining_seconds()))
            try:
                raw = self.rfile.read(length)
                if guard is not None:
                    guard.check()
                if len(raw) != length:
                    self._reply(400, {'error': 'incomplete_body'})
                    return
                def reject_constant(_value):
                    raise ValueError('non-JSON number')
                request = json.loads(raw, parse_constant=reject_constant)
                if guard is not None:
                    guard.check()
            except (ValueError, UnicodeError):
                self._reply(400, _rpc_error(None, -32700, 'Parse error'))
                return
            except OSError:
                self._reply(400, {'error': 'incomplete_body'})
                return
            try:
                if not self._library_request and requires_library_backend(request):
                    # Body selection cannot retroactively switch a legacy
                    # authentication scope or let SQLite ignore PG parameters.
                    self._reply(409, _rpc_error(request.get('id'),-32000,'explicit_postgres_backend_required'))
                    return
                mcp_session_id = (self.headers.get('Mcp-Session-Id') or '').strip() or None
                hdr_agent = (self.headers.get('X-EvolvMem-Agent') or '').strip()
                hdr_model = (self.headers.get('X-EvolvMem-Model') or '').strip()
                client_hint = {}
                if hdr_agent:
                    client_hint['name'] = hdr_agent
                if hdr_model:
                    client_hint['model'] = hdr_model
                if isinstance(request, dict) and request.get('method') == 'initialize' and mcp_session_id is None:
                    mcp_session_id = f'mcp-{uuid.uuid4().hex}'
                reply_headers = {'Mcp-Session-Id': mcp_session_id} if mcp_session_id else None
                if self._library_request:
                    # Do not return a body and publish it after the host's lease
                    # exits. The callback is the actual HTTP response boundary.
                    if _accepts_kwarg(library_host.serve_rpc, 'session_id'):
                        library_host.serve_rpc(
                            user,
                            request,
                            publish=lambda payload: self._reply(202 if payload is None else 200, payload, reply_headers),
                            session_id=mcp_session_id,
                            client_hint=client_hint or None,
                        )
                    else:
                        library_host.serve_rpc(
                            user,
                            request,
                            publish=lambda payload: self._reply(202 if payload is None else 200, payload, reply_headers),
                        )
                    if not getattr(self, '_response_started', False):
                        raise RuntimeError('library_response_not_published')
                    return
                if self.path == '/rerank':
                    from evolvmem.reranking import valid_scores
                    engine = runtime._shared_engine
                    if (not isinstance(request, dict) or set(request) != {'query', 'documents'}
                            or not isinstance(request['query'], str) or len(request['query']) > 4000
                            or not isinstance(request['documents'], list) or len(request['documents']) > 200
                            or any(not isinstance(d, str) or len(d) > 4000 for d in request['documents'])):
                        self._reply(400, {'error': 'invalid_reranking_input'})
                        return
                    if not getattr(engine, 'reranker_loaded', False):
                        self._reply(503, {'error': 'reranker_unavailable'})
                        return
                    scores = engine.rerank(request['query'], request['documents'])
                    if not valid_scores(scores, len(request['documents'])):
                        self._reply(502, {'error': 'invalid_reranking_response'})
                        return
                    result = {'scores': scores}
                elif self.path.startswith('/embedding'):
                    from evolvmem.embedding_client import valid_vector
                    # Model serialization is separate from SQLite dispatch: native
                    # extraction can hold a SQLite transaction while calling here.
                    engine = runtime._shared_engine
                    config = runtime.server_for(user).config
                    available = bool(engine.is_loaded)
                    if self.path == '/embedding/status':
                        result = dict(available=available, dimension=config.embedding_dim,
                                      reranker_available=bool(getattr(engine, 'reranker_loaded', False)),
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
                    if _accepts_kwarg(adapter.handle_request, 'session_id'):
                        result = adapter.handle_request(
                            user,
                            request,
                            owner=self.path == '/owner/mcp',
                            session_id=mcp_session_id,
                            client_hint=client_hint or None,
                        )
                    else:
                        result = adapter.handle_request(user, request, owner=self.path == '/owner/mcp')
            except Exception:
                if not getattr(self, '_response_started', False):
                    self._reply(500, _rpc_error(None, -32603, 'Internal error'))
                return
            self._reply(202 if result is None else 200, result, reply_headers)

        def do_GET(self):
            self._run_request(self._get)

        def _get(self):
            if self._authenticate() is not None:
                self._reply(405, {'error': 'method_not_allowed'}, {'Allow': 'POST'})

        do_DELETE = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_OPTIONS = do_GET

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = True
        # Finite kernel pending-accept queue for short identity bursts.  This
        # is not application concurrency: LibraryHost max_requests and request
        # deadlines still bound admitted work.
        request_queue_size = 64

        def server_close(self):
            if scoped_host is not None:
                scoped_host.close()
            super().server_close()

        def handle_error(self, request, client_address):
            # Base implementation prints exception tracebacks; avoid that path.
            pass

    return Server((runtime.settings.host if host is None else host,
                   runtime.settings.port if port is None else port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='EvolvMem trusted LAN MCP')
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    startup=load_server_configuration(args.config)
    runtime=server=library_host=None
    try:
        # All trusted settings/secrets/capabilities are checked before any
        # listener or optional SQLite/model/capture worker is initialized.
        library_host=build_library_host(startup)
        if startup.allow_legacy:
            from evolvmem.lan_runtime import LanRuntime
            runtime=LanRuntime(startup.settings)
            runtime.initialize()
        server=make_http_server(runtime,host=startup.settings.host,port=startup.settings.port,
            library_host=library_host,allow_legacy=startup.allow_legacy)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server is not None:server.server_close()
        finally:
            if library_host is not None:library_host.close()
            if runtime is not None:runtime.close()


if __name__ == '__main__':
    main()
