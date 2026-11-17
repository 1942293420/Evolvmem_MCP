"""Actual HTTP/Host/executor; only the external PG driver is an explicit port."""
import hashlib
import http.client
import json
import unittest
from unittest.mock import patch

from tests.library_host import test_http
from tests.library_host.support import L,P,TOKEN


class BusinessErrorHttpTests(unittest.TestCase):
    setUp=test_http.HttpTests.setUp
    finish=test_http.HttpTests.finish
    request=test_http.HttpTests.request

    def call(self,arguments,request_id='business-error-request'):
        wire={'jsonrpc':'2.0','id':71,'method':'tools/call','params':{'name':'memory_add',
            'arguments':dict(arguments,library_id=L,request_id=request_id)}}
        self.cleaned.clear()  # A prior request's completion is not this request.
        try:return self.request(wire)
        except (http.client.RemoteDisconnected,ConnectionResetError):
            # A closed socket is also a failing outcome, never a successful
            # conflict classification. Do not change Host/HTTP observations.
            self.assertTrue(self.cleaned.wait(2),'actual request_scope did not retire after disconnect')
            return None,None

    def args(self):
        return dict(key='synthetic-business-error-key',value='Synthetic reference retained after request conflict.',tier='reference')

    def assert_business_error(self,response,code):
        self.assertEqual((200,{'jsonrpc':'2.0','id':71,'result':{'isError':True,
            'content':[{'type':'text','text':json.dumps({'error':code})}]}}),response)

    def test_real_same_request_different_payload_is_explicit_conflict(self):
        args=self.args();status,first=self.call(args)
        self.assertEqual(200,status)
        self.assertEqual('added',json.loads(first['result']['content'][0]['text'])['status'])
        db=self.port.databases['content_0'][1]
        before=(db.execute('SELECT * FROM memories').fetchall(),db.execute('SELECT * FROM lan_write_requests').fetchall())
        sql_start=len(self.port.calls)
        self.assert_business_error(self.call(dict(args,value='Different synthetic request payload.')),'request_id_conflict')
        self.assertEqual(before,(db.execute('SELECT * FROM memories').fetchall(),db.execute('SELECT * FROM lan_write_requests').fetchall()))
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT ','UPDATE ','DELETE '))
            for raw,sql,_ in self.port.calls[sql_start:] if raw.db is not None))
        self.assertEqual(0,self.host.active_requests)
        self.assertTrue(all(c.closed for c in self.port.connections))

    def test_real_pending_intent_is_explicit_indeterminate_and_not_replayed(self):
        args=self.args()
        payload=hashlib.sha256(json.dumps(args,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        db=self.port.databases['content_0'][1]
        db.execute('INSERT INTO lan_write_requests(library_id,principal_id,tool,request_id,payload_hash,created_at) VALUES(?,?,?,?,?,?)',
            (L,P,'memory_add','business-error-request',payload,'2030-01-01 00:00:00'))
        before=db.execute('SELECT * FROM lan_write_requests').fetchall()
        self.assert_business_error(self.call(args),'request_indeterminate')
        self.assertEqual([],db.execute('SELECT * FROM memories').fetchall())
        self.assertEqual(before,db.execute('SELECT * FROM lan_write_requests').fetchall())

    def direct(self,args):
        from evolvmem.pg_business_core.executor import PgBusinessExecutor
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN);state=self.host._state()
            route=state.router.plan(identity,'memory_add',library_id=L)
            executor=PgBusinessExecutor(state.manager,self.host._config,
                model_contract='synthetic-reviewed-contract',semantic_merge_enabled=True)
            return executor.mutate(route,args,request_id='business-error-request')

    def test_direct_executor_keeps_conflict_and_indeterminate_exceptions(self):
        from evolvmem.pg_business_core.domain import UnsupportedBusiness
        args=self.args();self.assertEqual(200,self.call(args)[0])
        with self.assertRaisesRegex(UnsupportedBusiness,'^request_id_conflict$'):
            self.direct(dict(args,value='Different payload for direct caller.'))
        db=self.port.databases['content_0'][1]
        db.execute('UPDATE lan_write_requests SET result_json=NULL,completed_at=NULL')
        with self.assertRaisesRegex(UnsupportedBusiness,'^request_indeterminate$'):self.direct(args)

    def test_unknown_sql_error_never_becomes_business_rejection(self):
        private='synthetic-private-sql-error'
        def fault(raw,sql,params):
            if raw.db is not None and 'INSERT INTO em_content.memories' in sql:
                raise RuntimeError(private)
        self.port.on_sql=fault
        status,payload=self.call(self.args())
        self.assertNotEqual(200,status)
        self.assertNotIn(private,json.dumps(payload))
        db=self.port.databases['content_0'][1]
        self.assertEqual(0,db.execute('SELECT count(*) FROM memories').fetchone()[0])
        self.assertEqual(1,db.execute('SELECT count(*) FROM lan_write_requests WHERE result_json IS NULL').fetchone()[0])

    def test_effect_unsupported_error_is_not_caught_as_safe_preflight(self):
        from evolvmem.pg_business_core.domain import UnsupportedBusiness
        from evolvmem.pg_business_core.wire import LegacyWire
        private='synthetic-private-effect-error'
        with patch.object(LegacyWire,'memory_add',side_effect=UnsupportedBusiness(private)):
            status,payload=self.call(self.args())
        self.assertNotEqual(200,status)
        self.assertNotIn(private,json.dumps(payload))
        db=self.port.databases['content_0'][1]
        self.assertEqual(0,db.execute('SELECT count(*) FROM memories').fetchone()[0])
        self.assertEqual(1,db.execute('SELECT count(*) FROM lan_write_requests WHERE result_json IS NULL').fetchone()[0])

    def test_cancelled_preflight_does_not_publish_business_error(self):
        args=self.args();self.assertEqual(200,self.call(args)[0])
        def cancel(raw,sql,params):
            if raw.db is not None and 'SELECT payload_hash,result_json' in sql:
                self.host._state().guard.cancel()
        self.port.on_sql=cancel
        status,payload=self.call(dict(args,value='Different payload after cancellation.'))
        self.assertNotEqual(200,status)
        self.assertNotIn('request_id_conflict',json.dumps(payload))
        self.assertTrue(all(c.closed for c in self.port.connections))
        self.assertEqual(0,self.host.active_requests)


if __name__=='__main__':unittest.main()
