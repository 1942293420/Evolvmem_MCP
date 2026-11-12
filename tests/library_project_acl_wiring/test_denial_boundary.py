"""One narrow regression: real lease/guard, synthetic denied SQL, no services."""
from contextlib import contextmanager,nullcontext
from pathlib import Path
from types import SimpleNamespace,ModuleType
import os
import sys
import threading
import unittest
from uuid import UUID

ROOT=Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT',str(Path(__file__).resolve().parents[2])))
OVERLAY=os.environ.get('EVOLVMEM_TEST_OVERLAY')
PATHS=([Path(OVERLAY)/'evolvmem'] if OVERLAY else [])+[ROOT/'evolvmem']
package=ModuleType('evolvmem');package.__path__=[str(p) for p in PATHS];sys.modules['evolvmem']=package
business=ModuleType('evolvmem.pg_business_core');business.__path__=[str(p/'pg_business_core') for p in PATHS]
sys.modules[business.__name__]=business
wire=ModuleType('evolvmem.pg_business_core.wire')
class ForbiddenWire:
    def __init__(self,*args,**kwargs):raise AssertionError('denial_reached_business_wire')
wire.LegacyWire=ForbiddenWire;sys.modules[wire.__name__]=wire

from evolvmem.library_foundation import AuthorizedRoute,LibraryDescriptor,RequestIdentity,FoundationError
from evolvmem.library_foundation.sessions import SessionManager,DatabaseRole,DatabaseCredential,ContentSessionState
from evolvmem.library_catalog.adapter import ControlAuthorityLease
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.pg_business_core.executor import PgBusinessExecutor
from evolvmem.library_host import LibraryHost
from evolvmem.config import Config

def uid(value):return str(UUID(int=value))

class DenialBoundary(unittest.TestCase):
    def test_project_denial_is_published_inside_original_lease_only(self):
        for tool in ('context_read','memory_add','memory_replace','memory_remove'):
            for mode in ('host_denial','internal_default','sql_failure'):
                with self.subTest(tool=tool,mode=mode):
                    guard=SyncRequestDeadlineGuard(3)
                    identity=RequestIdentity(uid(1),uid(2),uid(3),1)
                    library=LibraryDescriptor(uid(4),'device','postgres','target',7,1,'pg_active','ready','postgres',
                                              device_id=uid(9),owner_principal_id=uid(10),active=True)
                    route=AuthorizedRoute(identity,library,tool,'read' if tool=='context_read' else 'write')
                    binding=SimpleNamespace(role='synthetic_runtime')
                    lease={'active':False,'entries':0};queries=[];outputs=[]
                    class Connection:
                        def begin(self,**kwargs):pass
                        def inspect_role(self):return DatabaseRole(binding.role,binding.role,False,False,False,True)
                        def session_context(self):return ContentSessionState(identity.principal_id,library.library_id,7,1,'write','pg_active','ready')
                        def pin_session_context(self,*args):return self.session_context()
                        def session_credential(self):return DatabaseCredential(identity.credential_id,identity.device_id,1)
                        def execute(self,sql,params=()):
                            queries.append(sql)
                            if 'resolve_project_scope' not in sql:raise AssertionError('denial_reached_receipt_or_effect')
                            if mode=='sql_failure':raise FoundationError('database_operation_failed')
                            return []
                        def acquire_session_locks(self,keys):pass
                        def commit(self):pass
                        def rollback(self):pass
                        def close(self):pass
                    catalog=SimpleNamespace(provider=SimpleNamespace(deadline_guard=guard))
                    authority=ControlAuthorityLease(catalog,deadline_guard=guard)
                    @contextmanager
                    def pin(routes):
                        self.assertFalse(lease['active']);lease['active']=True;lease['entries']+=1
                        try:yield
                        finally:lease['active']=False
                    authority._hold_routes=pin
                    manager=SessionManager(SimpleNamespace(revalidate=lambda route:guard.check()),
                        SimpleNamespace(resolve=lambda route:binding),
                        SimpleNamespace(deadline_guard=guard,open=lambda binding:Connection()),authority)
                    def publish(payload):
                        self.assertTrue(lease['active'])
                        guard.check()
                        outputs.append(payload)
                    args={'id':31} if tool in ('context_read','memory_remove') else {'key':'test-key','value':'test-value'}
                    with (self.assertRaises(FoundationError) if mode!='host_denial' else nullcontext()), guard.enforce():
                        if mode=='host_denial':
                            state=SimpleNamespace(identity=identity,served=False,published=False,guard=guard,
                                manager=manager,router=SimpleNamespace(plan=lambda *a,**k:route))
                            host=object.__new__(LibraryHost)
                            host._state=lambda:state;host._check_open=lambda:None
                            host._condition=threading.Condition();host._acl_enabled=False;host._recall_enabled=False
                            host._config=Config();host._model_contract='synthetic';host._semantic_merge=False
                            full=dict(args,library_id=library.library_id,project_key='project-b')
                            if tool!='context_read':full['request_id']='denial-1'
                            host.serve_rpc(identity,dict(jsonrpc='2.0',id=1,method='tools/call',
                                params=dict(name=tool,arguments=full)),publish=publish)
                            self.assertEqual(len(outputs),1)
                            self.assertTrue(outputs[0]['result']['isError'])
                            self.assertIn('project_scope_unavailable',outputs[0]['result']['content'][0]['text'])
                            guard.check()  # no cancellation or new guard was needed
                        else:
                            executor=PgBusinessExecutor(manager,Config(),model_contract='synthetic',
                                semantic_merge_enabled=False,project_key='project-b')
                            with self.assertRaises(FoundationError) as caught:
                                if tool=='context_read':executor.read(route,args,publish=publish,
                                    preflight_errors_as_results=mode=='sql_failure')
                                else:executor.mutate(route,args,request_id='denial-1',publish=publish,
                                    preflight_errors_as_results=mode=='sql_failure')
                            self.assertEqual(caught.exception.code,'database_operation_failed' if mode=='sql_failure' else 'project_scope_unavailable')
                            self.assertEqual(outputs,[])
                            # The genuine failing path is still cancelled.
                            with self.assertRaises(FoundationError):guard.check()
                    self.assertFalse(lease['active'])
                    self.assertEqual(lease['entries'],1)
                    self.assertTrue(queries)
                    self.assertTrue(all('resolve_project_scope' in sql for sql in queries))

if __name__=='__main__':unittest.main()
