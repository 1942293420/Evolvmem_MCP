"""Actual Handler methods with in-memory HTTP streams, not a live server test.

Only unrelated LAN/model imports and the listening Server constructor are
replaced. Socket shutdown/timeout use a real local socketpair. Root runs the
complete real HTTP/PG assembly later.
"""
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import RequestIdentity


class LanTools:
    def __init__(self, runtime): pass
    def handle_request(self, user, request, **kwargs): return {'legacy':True}
    @staticmethod
    def _rpc_error(*args): return {'error':'internal'}
class Server:
    def __init__(self, address, handler): self.RequestHandlerClass=handler


def load_transport():
    stubs={
        'evolvmem.embedding_client':NS(valid_vector=lambda value,dimension:True),
        'evolvmem.reranking':NS(valid_scores=lambda value,length:True),
        'evolvmem.lan_config':NS(LanSettings=object),
        'evolvmem.lan_runtime':NS(LanRuntime=object),
        'evolvmem.lan_tools':NS(LanTools=LanTools,PROTOCOLS={'2025-03-26'}),
    }
    path=Path(__file__).resolve().parents[2]/'evolvmem/lan_server.py'
    spec=importlib.util.spec_from_file_location('deadline_transport_candidate',path)
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,stubs): spec.loader.exec_module(module)
    module.ThreadingHTTPServer=Server
    return module


class Host:
    def __init__(self, guard):
        self.deadline_guard=guard; self.calls=0; self.failed=False
        self.auth_delay=0; self.after_publish_error=False; self.budgets=[]
    def authenticate(self, token):
        self.budgets.append(self.deadline_guard.remaining_seconds())
        time.sleep(self.auth_delay)
        return RequestIdentity('00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000003',1)
    def serve_rpc(self, identity, request, *, publish):
        self.calls+=1
        with self.deadline_guard.enforce():
            self.budgets.append(self.deadline_guard.remaining_seconds())
            try: publish({'result':'synthetic'})
            except Exception: self.failed=True; raise
            if self.after_publish_error: raise RuntimeError('private post-publication detail')


class BodyFailure(io.BytesIO):
    def __init__(self): super().__init__(); self.writes=0
    def write(self, body):
        self.writes+=1
        if self.writes==2: raise BrokenPipeError('synthetic closed peer')
        return super().write(body)


class HttpTests(unittest.TestCase):
    def setUp(self): self.transport=load_transport()
    def handler(self, guard, host, *, runtime=None, token='synthetic'):
        self.assertIn('deadline_guard',__import__('inspect').signature(self.transport.make_http_server).parameters,
                      'HTTP handler has no concrete deadline integration')
        with patch.dict(sys.modules, {'evolvmem.lan_tools':NS(LanTools=LanTools)}):
            server=self.transport.make_http_server(runtime,'127.0.0.1',0,library_host=host,deadline_guard=guard)
        handler=server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.connection,peer=socket.socketpair()
        self.addCleanup(handler.connection.close);self.addCleanup(peer.close)
        handler.path='/mcp';handler.command='POST';handler.request_version='HTTP/1.1'
        handler.requestline='POST /mcp HTTP/1.1';handler.client_address=('127.0.0.1',1)
        handler.headers=Message();handler.headers['Authorization']='Bearer '+token
        handler.headers['Content-Type']='application/json';handler.headers['Content-Length']='2'
        handler.rfile=io.BytesIO(b'{}');handler.wfile=io.BytesIO()
        return handler
    def test_authentication_and_publication_share_original_deadline(self):
        guard=SyncRequestDeadlineGuard(.5);host=Host(guard);host.auth_delay=.03
        handler=self.handler(guard,host);handler.do_POST()
        self.assertEqual(1,host.calls)
        self.assertLess(host.budgets[1],host.budgets[0]-.015)
        self.assertIn(b'200 OK',handler.wfile.getvalue())
        self.assertIn(b'"synthetic"',handler.wfile.getvalue())
    def test_late_authentication_never_starts_rpc_or_success_response(self):
        guard=SyncRequestDeadlineGuard(.03);host=Host(guard);host.auth_delay=.07
        handler=self.handler(guard,host);handler.do_POST()
        self.assertEqual(0,host.calls)
        self.assertEqual(b'',handler.wfile.getvalue())
    def test_late_body_cannot_start_business_work(self):
        guard=SyncRequestDeadlineGuard(.04);host=Host(guard)
        handler=self.handler(guard,host)
        test=self
        class SlowBody:
            def read(self,length):
                test.assertLessEqual(handler.connection.gettimeout(),.04)
                time.sleep(.07)
                return b'{}'
        handler.rfile=SlowBody();handler.do_POST()
        self.assertEqual(0,host.calls);self.assertEqual(b'',handler.wfile.getvalue())
    def test_broken_pipe_propagates_inside_publication_without_second_reply(self):
        guard=SyncRequestDeadlineGuard(.5);host=Host(guard)
        handler=self.handler(guard,host);handler.wfile=BodyFailure();handler.do_POST()
        self.assertTrue(host.failed)
        self.assertEqual(2,handler.wfile.writes)
        self.assertEqual(1,handler.wfile.getvalue().count(b'HTTP/1.1'))
    def test_error_after_complete_response_does_not_send_another_response(self):
        guard=SyncRequestDeadlineGuard(.5);host=Host(guard);host.after_publish_error=True
        handler=self.handler(guard,host);handler.do_POST()
        self.assertEqual(1,handler.wfile.getvalue().count(b'HTTP/1.1'))
        self.assertNotIn(b'private',handler.wfile.getvalue())
    def test_legacy_authentication_keeps_original_route(self):
        guard=SyncRequestDeadlineGuard(.5);host=Host(guard)
        guard._unhealthy.set() # entering this guard would fail the legacy request
        runtime=NS(settings=NS(authenticate=lambda token:'legacy-user'),start_capture_worker=lambda adapter:None)
        handler=self.handler(guard,host,runtime=runtime,token='legacy');handler.do_POST()
        self.assertEqual([],host.budgets);self.assertEqual(0,host.calls)
        self.assertIn(b'"legacy": true',handler.wfile.getvalue())
    def test_unrelated_guard_is_rejected_at_http_assembly(self):
        guard=SyncRequestDeadlineGuard(.5);host=Host(SyncRequestDeadlineGuard(.5))
        self.assertIn('deadline_guard',__import__('inspect').signature(self.transport.make_http_server).parameters)
        with self.assertRaises(ValueError):
            self.transport.make_http_server(None,'127.0.0.1',0,library_host=host,deadline_guard=guard)
