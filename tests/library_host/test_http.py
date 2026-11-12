import http.client
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from tests.library_host.support import Port,TOKEN,L,platform_shim

platform_shim()


class HttpTests(unittest.TestCase):
    def setUp(self):
        from evolvmem.lan_server import make_http_server
        self.port=Port();self.addCleanup(self.port.close)
        patcher=self.port.driver_patch();patcher.start();self.addCleanup(patcher.stop)
        self.host=self.port.options(enabled=True)
        self.cleaned=threading.Event()
        original_scope=self.host.request_scope
        @contextmanager
        def observed_scope(connection=None):
            self.cleaned.clear()
            try:
                with original_scope(connection) as guard:
                    yield guard
            finally:
                # The peer may read all bytes before the held control lease's
                # final liveness SQL and resource retirement have completed.
                self.cleaned.set()
        self.host.request_scope=observed_scope
        self.server=make_http_server(None,'127.0.0.1',0,library_host=self.host)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start();self.addCleanup(self.finish)

    def finish(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,payload,path='/mcp',token=TOKEN,extra=None):
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token}
        headers.update(extra or {})
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        try:
            conn.request('POST',path,body=json.dumps(payload),headers=headers)
            response=conn.getresponse();body=response.read()
            self.assertTrue(self.cleaned.wait(2),'actual request_scope did not retire')
            return response.status,json.loads(body) if body else None
        finally:conn.close()

    def test_actual_http_request_scope_guard_and_one_publication(self):
        observations=[]
        original=self.host.serve_rpc
        def observe(*args,**kwargs):
            state=self.host._state()
            self.assertIsNotNone(state.guard._state().http_socket)
            self.assertIs(state.manager.provider.deadline_guard,state.guard)
            self.assertIs(state.manager.authority.deadline_guard,state.guard)
            publish=kwargs['publish']
            def output(payload):
                from evolvmem.library_catalog.adapter import _HELD
                held=_HELD.get()
                self.assertIsNotNone(held)
                self.assertTrue(held.session.raw.protected)
                # The write keeps its exact keyed connection/session locks
                # through publication, with the confirmation transaction done.
                self.assertTrue(all(int(c.info.transaction_status)==0 for c in self.port.connections
                                    if c.db is not None and not c.closed))
                observations.append(payload)
                publish(payload)
            kwargs['publish']=output
            return original(*args,**kwargs)
        self.host.serve_rpc=observe
        code,payload=self.request({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'memory_add','arguments':{'library_id':L,'request_id':'http-write',
                'key':'http-reference','value':'Synthetic HTTP reference through the real business implementation.',
                'tier':'reference'}}})
        self.assertEqual(200,code)
        added=json.loads(payload['result']['content'][0]['text'])
        self.assertEqual('added',added['status'])
        for tool,args in (
            ('context_read',{'id':added['context_id'],'layer':'l2'}),
            ('memory_replace',{'key':'http-reference','value':'Replacement content from an actual HTTP call.','request_id':'http-replace'}),
            ('memory_remove',{'id':2,'request_id':'http-remove'}),
        ):
            code,reply=self.request({'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':tool,'arguments':dict(args,library_id=L)}})
            self.assertEqual(200,code)
            dto=json.loads(reply['result']['content'][0]['text'])
            self.assertNotIn('error',dto)
        self.assertEqual(4,len(observations))
        self.assertTrue(all(c.closed for c in self.port.connections))
        self.assertEqual(0,self.host.active_requests)

    def test_discovery_only_advertises_four_truthful_tools(self):
        status,result=self.request({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual(200,status)
        tools=result['result']['tools']
        self.assertEqual({'memory_add','memory_replace','memory_remove','context_read'},set(t['name'] for t in tools))
        for tool in tools:
            self.assertIn('library_id',tool['inputSchema']['required'])
            self.assertFalse(tool['inputSchema']['additionalProperties'])

    def test_identity_is_only_catalog_token_and_mcp_endpoint(self):
        payload={'jsonrpc':'2.0','id':1,'method':'ping'}
        self.assertEqual(401,self.request(payload,token='wrong-token')[0])
        for path in ('/owner/mcp','/embedding','/rerank'):
            self.assertEqual(403,self.request(payload,path=path)[0])
        self.assertEqual(403,self.request(payload,extra={'X-EvolvMem-Expected-User':'fake'})[0])
        self.assertEqual(403,self.request(payload,extra={'Origin':'https://untrusted.invalid'})[0])

    def test_server_close_revokes_host_without_starting_sqlite(self):
        self.server.shutdown();self.thread.join(2)
        self.server.server_close()
        from evolvmem.library_foundation import FoundationError
        with self.assertRaisesRegex(FoundationError,'library_host_closed'):
            with self.host.request_scope():pass


class ImportTests(unittest.TestCase):
    def test_library_only_transport_has_no_eager_legacy_bootstrap_import(self):
        source_root=Path(__file__).resolve().parents[2]
        script="""import sys
sys.path.insert(0,sys.argv[1])
for module in ('evolvmem.lan_runtime','evolvmem.lan_tools','evolvmem.embedding_client','evolvmem.reranking'):
    sys.modules[module]=None
from evolvmem.lan_server import make_http_server
assert callable(make_http_server)
"""
        result=subprocess.run([sys.executable,'-B','-c',script,str(source_root)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)


if __name__=='__main__':unittest.main()
