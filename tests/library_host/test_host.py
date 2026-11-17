import json
import unittest
from tests.library_host.support import Port,TOKEN,L,L2,P,platform_shim

platform_shim()


class HostTests(unittest.TestCase):
    def setUp(self):
        self.port=Port();self.addCleanup(self.port.close)
        patcher=self.port.driver_patch();patcher.start();self.addCleanup(patcher.stop)
        self.host=self.port.options(enabled=True)
        self.addCleanup(self.host.close)

    def call(self,tool,args,*,lib=L,request_id='request-1'):
        wire=dict(args,library_id=lib)
        if tool!='context_read':wire['request_id']=request_id
        outputs=[]
        with self.host.request_scope() as guard:
            identity=self.host.authenticate(TOKEN)
            self.host.serve_rpc(identity,dict(jsonrpc='2.0',id=1,method='tools/call',
                params={'name':tool,'arguments':wire}),publish=outputs.append)
            self.assertEqual(1,len(outputs))
        self.assertTrue(all(x.closed for x in self.port.connections))
        return outputs[0]

    def dto(self,response):
        self.assertIn('result',response,response)
        return json.loads(response['result']['content'][0]['text'])

    def test_disabled_default_has_no_secret_or_connection_effect(self):
        from evolvmem.library_foundation import FoundationError
        disabled=self.port.options()
        with self.assertRaisesRegex(FoundationError,'library_host_disabled'):
            with disabled.request_scope():pass
        self.assertEqual([],self.port.connections);self.assertEqual([],self.port.secrets)

    def test_actual_four_tools_effect_receipt_and_same_request_remove(self):
        added=self.dto(self.call('memory_add',{'key':'portable-reference','value':'Synthetic reference content for the original domain path.','tier':'reference'}))
        self.assertEqual('added',added['status'])
        context_id=added['context_id']
        read=self.dto(self.call('context_read',{'id':context_id,'layer':'l2'}))
        self.assertIn('Synthetic reference',read['content'])
        replacement=self.dto(self.call('memory_replace',{'key':'portable-reference','value':'Replacement synthetic content through the exact original handler.'},request_id='request-2'))
        self.assertEqual('replaced',replacement['status'])
        removed=self.dto(self.call('memory_remove',{'id':replacement['new_id']},request_id='request-3'))
        self.assertEqual('deleted',removed['status'])
        db=self.port.databases['content_0'][1]
        self.assertEqual(3,db.execute('SELECT count(*) FROM lan_write_requests WHERE result_json IS NOT NULL').fetchone()[0])
        self.assertGreater(db.execute('SELECT count(*) FROM vector_outbox').fetchone()[0],0)
        self.assertLessEqual(self.port.peak,4)
        self.assertTrue(all(x.kwargs['sslmode']=='verify-full' for x in self.port.connections))

    def test_explicit_library_is_not_a_numeric_id_namespace(self):
        a=self.dto(self.call('memory_add',{'key':'same-key','value':'Synthetic first library reference content.','tier':'reference'}))
        b=self.dto(self.call('memory_add',{'key':'same-key','value':'Synthetic second library reference content.','tier':'reference'},lib=L2))
        self.assertEqual(a['id'],b['id'])
        self.assertIn('first',self.dto(self.call('context_read',{'id':a['context_id'],'layer':'l2'}))['content'])
        self.assertIn('second',self.dto(self.call('context_read',{'id':b['context_id'],'layer':'l2'},lib=L2))['content'])

    def test_identity_override_never_reaches_content_connector(self):
        for field in ('dsn','db_role','principal_id','connection_ref','device_id','user','data_dir'):
            with self.subTest(field=field):
                reply=self.call('context_read',{'id':1,field:'untrusted'})
                self.assertIn('error',reply)
        self.assertFalse(any(x.db is not None for x in self.port.connections))

    def test_search_experience_are_explicitly_unsupported(self):
        for tool in ('context_search','memory_search','capability_search','experience_recall'):
            reply=self.call(tool,{'query':'synthetic'})
            self.assertEqual('library_tool_unsupported',reply['error']['message'])
        self.assertFalse(any(x.db is not None for x in self.port.connections))

    def test_final_revocation_prevents_payload_publication(self):
        def revoke(raw,sql,params):
            if raw.db is not None and 'SELECT to_jsonb(c)' in sql:
                self.port.rows[L]['credential_active']=False
        self.port.on_sql=revoke
        from evolvmem.library_foundation import FoundationError
        with self.assertRaises(FoundationError):
            self.call('context_read',{'id':1})
        self.assertTrue(all(x.closed for x in self.port.connections))
        self.assertEqual(0,self.host.active_requests)

    def test_close_during_content_rejects_success(self):
        def close(raw,sql,params):
            if raw.db is not None and 'SELECT to_jsonb(c)' in sql:self.host.close()
        self.port.on_sql=close
        from evolvmem.library_foundation import FoundationError
        with self.assertRaises(FoundationError):
            self.call('context_read',{'id':1})
        self.assertTrue(all(x.closed for x in self.port.connections))
        self.assertEqual(0,self.host.active_requests)


if __name__=='__main__':unittest.main()
