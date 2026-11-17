import json
import threading
import unittest
from tests.library_host.support import Port,TOKEN,L,L2,P,C,D,platform_shim

platform_shim()


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.port=Port();self.addCleanup(self.port.close)
        patcher=self.port.driver_patch();patcher.start();self.addCleanup(patcher.stop)
        self.host=self.port.options(enabled=True)
        self.addCleanup(self.host.close)

    def invoke(self,tool,args,request_id='stable-boundary'):
        outputs=[]
        with self.host.request_scope() as guard:
            identity=self.host.authenticate(TOKEN)
            self.host.serve_rpc(identity,{'jsonrpc':'2.0','id':9,'method':'tools/call',
                'params':{'name':tool,'arguments':dict(args,library_id=L,**(
                    {'request_id':request_id} if tool!='context_read' else {}))}},publish=outputs.append)
        self.assertEqual(1,len(outputs));return outputs[0]

    def reference(self):
        return {'key':'boundary-reference','value':'Synthetic durable value for precise receipt verification.','tier':'reference'}

    def test_effect_commit_unknown_preserves_classification_and_receipt_replay(self):
        from evolvmem.library_foundation import FoundationError
        def lose(raw):
            if raw.db is not None and not raw.read_only and raw.db.execute(
                    'SELECT count(*) FROM lan_write_requests WHERE result_json IS NOT NULL').fetchone()[0]:
                self.port.on_commit=None
                raise OSError('private connection string must not escape')
        self.port.on_commit=lose
        with self.assertRaises(FoundationError) as caught:
            self.invoke('memory_add',self.reference())
        self.assertEqual('commit_result_unknown',caught.exception.code)
        db=self.port.databases['content_0'][1]
        self.assertEqual(1,db.execute('SELECT count(*) FROM memories').fetchone()[0])
        self.assertEqual(1,db.execute('SELECT count(*) FROM lan_write_requests WHERE result_json IS NOT NULL').fetchone()[0])
        retry=self.invoke('memory_add',self.reference())
        self.assertEqual('added',json.loads(retry['result']['content'][0]['text'])['status'])
        self.assertEqual(1,db.execute('SELECT count(*) FROM memories').fetchone()[0])
        self.assertEqual(0,self.host.active_requests)
        self.assertTrue(all(c.closed for c in self.port.connections))

    def test_intent_commit_unknown_never_reexecutes_effect(self):
        from evolvmem.library_foundation import FoundationError
        self.port.commit_unknown=True
        with self.assertRaisesRegex(FoundationError,'commit_result_unknown'):
            self.invoke('memory_add',self.reference())
        reply=self.invoke('memory_add',self.reference())
        self.assertTrue(reply['result']['isError'])
        self.assertEqual({'error':'request_indeterminate'},json.loads(reply['result']['content'][0]['text']))
        db=self.port.databases['content_0'][1]
        self.assertEqual(0,db.execute('SELECT count(*) FROM memories').fetchone()[0])
        self.assertEqual(1,db.execute('SELECT count(*) FROM lan_write_requests WHERE result_json IS NULL').fetchone()[0])

    def test_admission_two_requests_no_waiters_and_close_releases_permits(self):
        from evolvmem.library_foundation import FoundationError
        entered=threading.Barrier(3);release=threading.Event();errors=[];guards=[]
        def request():
            try:
                with self.host.request_scope() as guard:
                    guards.append(guard)
                    self.host.authenticate(TOKEN)
                    entered.wait(2);release.wait(2)
            except BaseException as exc:errors.append(exc)
        threads=[threading.Thread(target=request) for _ in range(2)]
        for t in threads:t.start()
        try:
            entered.wait(2)
            self.assertEqual(2,self.host.active_requests)
            self.assertIsNot(guards[0],guards[1])
            with self.assertRaisesRegex(FoundationError,'library_host_busy'):
                with self.host.request_scope():pass
            self.host.close()
            with self.assertRaisesRegex(FoundationError,'library_host_closed'):
                with self.host.request_scope():pass
        finally:
            release.set()
            for t in threads:t.join(3)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual([],errors)
        self.assertEqual(0,self.host.active_requests)

    def test_request_identity_is_not_transferable_or_body_constructible(self):
        from evolvmem.library_foundation import FoundationError,RequestIdentity
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN)
            forged=RequestIdentity(P,C,D,3,2,4)
            self.assertEqual(identity,forged)
            with self.assertRaisesRegex(FoundationError,'unauthorized'):
                self.host.serve_rpc(forged,{'jsonrpc':'2.0','id':1,'method':'ping'},publish=lambda x:None)
        with self.host.request_scope():
            self.host.authenticate(TOKEN)
            with self.assertRaisesRegex(FoundationError,'unauthorized'):
                self.host.serve_rpc(identity,{'jsonrpc':'2.0','id':1,'method':'ping'},publish=lambda x:None)

    def test_each_identity_and_library_boundary_denies_before_content(self):
        for field,value in [('credential_active',False),('principal_active',False),('device_active',False),
                            ('credential_epoch',4),('principal_epoch',3),('device_epoch',5),
                            ('library_active',False),('acl_state','updating'),
                            ('authority_state','freeze_and_reconcile'),('permission',None)]:
            with self.subTest(field=field):
                original=self.port.rows[L][field]
                with self.host.request_scope():
                    identity=self.host.authenticate(TOKEN)
                    self.port.rows[L][field]=value
                    out=[]
                    self.host.serve_rpc(identity,{'jsonrpc':'2.0','id':1,'method':'tools/call',
                        'params':{'name':'context_read','arguments':{'library_id':L,'id':1}}},publish=out.append)
                    self.assertIn('error',out[0])
                self.port.rows[L][field]=original
        self.assertFalse(any(c.db is not None for c in self.port.connections))

    def test_permission_read_cannot_mutate_another_authorized_library(self):
        self.port.rows[L]['permission']='read'
        out=self.invoke('memory_add',self.reference())
        self.assertEqual('library_access_denied',out['error']['message'])
        self.assertFalse(any(c.db is not None for c in self.port.connections))

    def test_output_failure_does_not_repeat_publication_or_effect(self):
        from evolvmem.library_foundation import FoundationError
        count=[]
        def failed_output(payload):
            count.append(payload)
            raise BrokenPipeError('synthetic peer closed')
        with self.assertRaises(FoundationError):
            with self.host.request_scope():
                identity=self.host.authenticate(TOKEN)
                self.host.serve_rpc(identity,{'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'memory_add','arguments':dict(self.reference(),library_id=L,request_id='output')}},
                    publish=failed_output)
        self.assertEqual(1,len(count))
        self.assertEqual(0,self.host.active_requests)
        self.assertTrue(all(c.closed for c in self.port.connections))
        retry=self.invoke('memory_add',self.reference(),request_id='output')
        self.assertIn('result',retry)
        self.assertEqual(1,self.port.databases['content_0'][1].execute('SELECT count(*) FROM memories').fetchone()[0])

    def test_actual_guard_cancellation_cannot_be_replaced_to_emit_result(self):
        from evolvmem.library_foundation import FoundationError
        out=[]
        with self.assertRaisesRegex(FoundationError,'request_deadline_exceeded'):
            with self.host.request_scope() as guard:
                identity=self.host.authenticate(TOKEN)
                guard.cancel()
                self.host.serve_rpc(identity,{'jsonrpc':'2.0','id':1,'method':'ping'},publish=out.append)
        self.assertEqual([],out);self.assertEqual(0,self.host.active_requests)

    def test_publication_is_once_per_scope(self):
        from evolvmem.library_foundation import FoundationError
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN);out=[]
            request={'jsonrpc':'2.0','id':1,'method':'ping'}
            self.host.serve_rpc(identity,request,publish=out.append)
            with self.assertRaisesRegex(FoundationError,'library_rpc_already_served'):
                self.host.serve_rpc(identity,request,publish=out.append)
            self.assertEqual(1,len(out))

    def test_failed_resource_retirement_poison_is_host_wide(self):
        from evolvmem.library_foundation import FoundationError
        original=self.port.connect
        def broken_close(**kwargs):
            raw=original(**kwargs)
            def fail(): raise OSError('private cleanup diagnostic')
            raw.close=fail
            return raw
        self.host.close()
        self.host=self.port.options(enabled=True,control_connector=broken_close)
        with self.assertRaises(FoundationError):
            with self.host.request_scope():self.host.authenticate(TOKEN)
        self.assertEqual(0,self.host.active_requests)
        count=len(self.port.connections)
        with self.assertRaisesRegex(FoundationError,'library_host_cleanup_incomplete'):
            with self.host.request_scope():pass
        self.assertEqual(count,len(self.port.connections))
        self.host.close()

    def test_auth_failure_releases_admission_without_caching_identity(self):
        from evolvmem.library_foundation import FoundationError
        with self.assertRaisesRegex(FoundationError,'unauthorized'):
            with self.host.request_scope():self.host.authenticate('wrong')
        self.assertEqual(0,self.host.active_requests)
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN)
            self.assertEqual(P,identity.principal_id)
        self.assertTrue(all(c.closed for c in self.port.connections))


if __name__=='__main__':unittest.main()
