"""New local-only wiring checks. No socket, service, PG or old suite."""
from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from uuid import UUID

ROOT=Path(__file__).resolve().parents[2]
package=types.ModuleType('evolvmem'); package.__path__=[str(ROOT/'evolvmem')]
sys.modules['evolvmem']=package
for name in ('library_project_acl','pg_business_core'):
    item=types.ModuleType('evolvmem.'+name)
    item.__path__=[str(ROOT/'evolvmem'/name)]
    sys.modules[item.__name__]=item

from evolvmem.library_foundation import AuthorizedRoute, LibraryDescriptor, RequestIdentity, FoundationError
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.coordinator import project_uuid, ProjectAclIntent, ProjectAclCoordinator
from evolvmem.library_catalog import VerifiedProjection
from evolvmem.library_project_acl.scope import resolve_project_scope
from evolvmem.library_host import LibraryHost
from evolvmem.config import Config

def uid(n): return str(UUID(int=n))

class ScopeChecks(unittest.TestCase):
    def setUp(self):
        self.identity=RequestIdentity(uid(1),uid(2),uid(3),1)
        self.library=LibraryDescriptor(uid(4),'device','postgres','target',7,1,'pg_active','ready','postgres',
            device_id=uid(9),owner_principal_id=uid(10),active=True)
        self.route=AuthorizedRoute(self.identity,self.library,'memory_add','write')
        self.rows=[(project_uuid('procurement'),'procurement',7,1)]
        self.calls=[]
        self.session=LibrarySession(self.route,types.SimpleNamespace(execute=self.execute))
    def execute(self,sql,params):
        self.calls.append((sql,params));return self.rows
    def test_registered_key_resolves_for_actual_authorized_session(self):
        scope=resolve_project_scope(self.session,'procurement')
        self.assertEqual(scope.project_id,project_uuid('procurement'))
        self.assertEqual(self.calls[0][1],(uid(4),'procurement','write'))
        self.assertNotEqual(self.identity.device_id,self.library.device_id)
    def test_wrong_epoch_and_unregistered_key_fail_closed(self):
        for rows in ([],[(project_uuid('procurement'),'procurement',6,1)],[(uid(42),'procurement',7,1)],
                     [(project_uuid('procurement'),'procurement',7,True)]):
            self.rows=rows
            with self.assertRaises(FoundationError):resolve_project_scope(self.session,'procurement')
    def test_explicit_unclassified_read_uses_owner_gated_sql_and_never_writes(self):
        with self.assertRaises(FoundationError):resolve_project_scope(self.session,None)
        self.session.route=AuthorizedRoute(self.identity,self.library,'context_read','read')
        self.rows=[]
        with self.assertRaises(FoundationError):resolve_project_scope(self.session,None)
        self.rows=[(None,None,7,1)]
        scope=resolve_project_scope(self.session,None)
        self.assertIsNone(scope.project_id)
        self.assertEqual(self.calls[-1][1],(uid(4),None,'read'))
        self.assertEqual(LibraryHost._arguments('context_read',dict(library_id=uid(4),project_key=None,id=31)),dict(id=31))
    def test_scope_cannot_move_to_another_authenticated_identity(self):
        scope=resolve_project_scope(self.session,'procurement')
        self.session.route=AuthorizedRoute(RequestIdentity(uid(20),uid(21),uid(22),1),self.library,'memory_add','write')
        with self.assertRaises(FoundationError):scope.require(self.session)
    def test_host_demands_key_and_rejects_client_project_uuid(self):
        args=dict(library_id=uid(4),request_id='r1',key='fact',value='value')
        with self.assertRaises(FoundationError):LibraryHost._arguments('memory_add',args)
        args['project_key']='procurement'
        self.assertEqual(LibraryHost._arguments('memory_add',args),dict(key='fact',value='value'))
        args['project_id']=project_uuid('procurement')
        with self.assertRaises(FoundationError):LibraryHost._arguments('memory_add',args)

    def host(self,state):
        host=object.__new__(LibraryHost)
        host._acl_enabled=True;host._acl_targets=(types.SimpleNamespace(library_id=uid(4)),)
        host._state=lambda:state;host._check_open=lambda:None
        host._publish=lambda state,payload,publish:publish(payload)
        host._config=Config();host._model_contract='test-model';host._semantic_merge=False
        return host

    def test_real_rpc_dispatch_uses_same_v2_workflow_for_both_acl_kinds(self):
        for tool in ('library_acl_update','library_project_acl_update'):
            calls=[];operation_state={'value':'pending'}
            class ControlSession:
                def execute(self,sql,params):
                    calls.append((sql,params))
                    if 'confirm_acl_projection' in sql:operation_state['value']='applying'
                    if 'finish_acl_change' in sql:operation_state['value']='complete'
                    return [dict(operation_id=uid(6),library_id=uid(4),target_policy_epoch=8,
                                 operation_state=operation_state['value'],expected_route_epoch=1,connection_ref='target')]
                def commit(self):pass
            class Provider:
                @contextmanager
                def open(self,**kwargs):yield ControlSession()
            coordinator=ProjectAclCoordinator(Provider())
            projected=[]
            def projector(intent,operation):
                projected.append(intent)
                return VerifiedProjection(uid(6),uid(4),8,1,'target',bytes(32))
            state=types.SimpleNamespace(identity=self.identity,served=False,published=False,
                guard=types.SimpleNamespace(check=lambda:None),acl_coordinator=coordinator,acl_projector=projector)
            host=self.host(state);published=[]
            args=dict(library_id=uid(4),target_principal_id=uid(5),permission='read',expected_policy_epoch=7,
                      operation_id=uid(6),request_id='stable-1')
            if tool=='library_project_acl_update':args['project_key']='procurement'
            host.serve_rpc(self.identity,dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=tool,arguments=args)),publish=published.append)
            self.assertIn('complete',published[0]['result']['content'][0]['text'])
            self.assertEqual(len(projected),1)
            self.assertEqual(isinstance(projected[0],ProjectAclIntent),tool=='library_project_acl_update')
            self.assertTrue(any('confirm_acl_projection' in sql for sql,_ in calls))
            self.assertTrue(any('finish_acl_change' in sql for sql,_ in calls))

    def test_real_core_queries_and_outbox_use_selected_project(self):
        from evolvmem.pg_business_core.core import PgBusinessCore,KeyLease
        from evolvmem.context_migration import LegacyMemoryMigrator
        logs=[]
        def execute(sql,params):
            logs.append((sql,params))
            if 'SELECT to_jsonb(m)' in sql:return [({'value':'fact','revision':1},)]
            if 'RETURNING id' in sql:return [(31,)]
            return []
        scope=resolve_project_scope(self.session,'procurement')
        self.session._connection=types.SimpleNamespace(execute=execute)
        core=PgBusinessCore(self.session,Config(),model_contract='test-model',project_scope=scope,
            lease=KeyLease(uid(4),LegacyMemoryMigrator.identity_key_for('shared',1),scope.project_id))
        core.get_by_key('shared')
        self.assertIn('project_id IS NOT DISTINCT FROM %s',logs[-1][0])
        self.assertEqual(logs[-1][1],(uid(4),scope.project_id,'shared'))
        core._pending('legacy',31,'upsert')
        self.assertEqual(logs[-1][1][:2],(uid(4),scope.project_id))
        with self.assertRaises(Exception):core.lease.require(uid(4),'shared',project_uuid('different'))

    def test_explicit_registered_scope_does_not_reinfer_project_from_content(self):
        from evolvmem.pg_business_core.core import PgBusinessCore
        from evolvmem.legacy_models import LegacyAddRequest
        core=PgBusinessCore(self.session,Config(),model_contract='test-model',
                            project_scope=resolve_project_scope(self.session,'procurement'))
        draft,decision=core._draft(dict(id=31,key='ordinary-fact',value='A fact',status='active',tier='normal'),
                                   LegacyAddRequest(key='ordinary-fact',value='A fact'))
        self.assertEqual(draft.project,'procurement')
        self.assertTrue(draft.identity_key.startswith('project:'+project_uuid('procurement')+':'))
        self.assertEqual(decision.evidence[0]['source'],'authorized_project_registry')

    def test_host_executor_receipt_and_outbox_call_path_stays_under_lease(self):
        # Stub only the unchanged legacy DTO facade; execute actual host,
        # executor, scoped core SQL and receipt phases against a bounded recorder.
        from evolvmem.pg_business_core.domain import projection_payload
        from evolvmem.legacy_models import LegacyAddRequest
        class Wire:
            def __init__(self,core,**kwargs):self.core=core
            def memory_add(self,args):
                self.core._write(args['key'])
                legacy_id=self.core._insert_projection(projection_payload(LegacyAddRequest(**args)))
                self.core._pending('legacy',legacy_id,'upsert')
                return dict(id=legacy_id,index_state='degraded')
        wire_module=types.ModuleType('evolvmem.pg_business_core.wire');wire_module.LegacyWire=Wire
        with patch.dict(sys.modules,{'evolvmem.pg_business_core.wire':wire_module}):
            sys.modules.pop('evolvmem.pg_business_core.executor',None)
            executor_module=importlib.import_module('evolvmem.pg_business_core.executor')
        logs=[];receipt={};lease={'active':False};published=[]
        def execute(sql,params):
            self.assertTrue(lease['active'])
            logs.append((sql,params))
            if 'resolve_project_scope' in sql:return self.rows
            if 'SELECT payload_hash,result_json' in sql:return []
            if 'INSERT INTO em_content.lan_write_requests' in sql:receipt['intent']=params;return []
            if 'UPDATE em_content.lan_write_requests' in sql:
                receipt['result']=json.loads(params[0]);return [('stable-write',)]
            if 'SELECT result_json' in sql:return [(receipt['result'],)]
            if 'INSERT INTO em_content.memories' in sql:return [(31,)]
            if 'SELECT to_jsonb(m)' in sql:return [(dict(value='A fact',revision=1),)]
            return []
        session=LibrarySession(self.route,types.SimpleNamespace(execute=execute))
        class Scope:
            @contextmanager
            def transaction(scope_self,**kwargs):yield session
            def publish(scope_self,result,publish):
                self.assertTrue(lease['active']);publish(result);return result
        class Manager:
            @contextmanager
            def keyed(manager_self,route,keys):
                lease['active']=True
                try:yield Scope()
                finally:lease['active']=False
        state=types.SimpleNamespace(identity=self.identity,served=False,published=False,
            guard=types.SimpleNamespace(check=lambda:None),manager=Manager(),
            router=types.SimpleNamespace(plan=lambda *args,**kwargs:self.route))
        host=self.host(state)
        args=dict(library_id=uid(4),project_key='procurement',request_id='stable-write',key='fact',value='A fact')
        with patch.dict(sys.modules,{'evolvmem.pg_business_core.executor':executor_module,
                                     'evolvmem.pg_business_core.wire':wire_module}):
            host.serve_rpc(self.identity,dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='memory_add',arguments=args)),publish=published.append)
        self.assertEqual(receipt['intent'][1],project_uuid('procurement'))
        outbox=[params for sql,params in logs if 'INSERT INTO em_content.vector_outbox' in sql]
        self.assertEqual(outbox[0][1],project_uuid('procurement'))
        self.assertIn('31',published[0]['result']['content'][0]['text'])
        self.assertFalse(lease['active'])

if __name__=='__main__':unittest.main()
