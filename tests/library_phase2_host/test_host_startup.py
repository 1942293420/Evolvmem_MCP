"""New host wiring checks only: no port, DB, model process or service is started."""
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import UUID
import json
import os
import sys
import threading
import unittest

HERE=Path(__file__).resolve()
artifact=next((p for p in HERE.parents if (p/'candidate/evolvmem/library_host.py').is_file()
              and (p/'baseline/evolvmem/library_host.py').is_file()),None)
BASE=artifact or HERE.parents[2]
ART=BASE.parent
source=Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT',
    str(ART/'pg-library-host-20261101-v2'/'candidate' if artifact else BASE)))
overlay=Path(os.environ.get('EVOLVMEM_TEST_OVERLAY',str(BASE/'candidate' if artifact else source)))
paths=[overlay/'evolvmem']+([BASE/'baseline'/'evolvmem',ART/'pg-cross-library-parallel-20261102'/'candidate'/'evolvmem',
       ART/'pg-project-acl-wiring-20261101'/'candidate'/'evolvmem'] if artifact else [])+[source/'evolvmem']
package=ModuleType('evolvmem');package.__path__=[str(p) for p in paths];sys.modules['evolvmem']=package
for pkg in ('library_foundation','library_deadline','library_project_acl'):
    import importlib.util
    candidates=[p/pkg for p in paths]+[ART/'pg-project-acl-20261101'/'candidate'/'evolvmem'/pkg]
    init=next(p/'__init__.py' for p in candidates if (p/'__init__.py').exists())
    spec=importlib.util.spec_from_file_location('evolvmem.'+pkg,init,submodule_search_locations=[str(p) for p in candidates])
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
from evolvmem.library_host import LibraryHost
from evolvmem.library_startup import parse_library_backend,build_library_host,ServerStartup,requires_library_backend
from evolvmem.library_foundation import FoundationError,RequestIdentity
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.config import Config
from evolvmem.lan_config import LanSettings

def uid(n):return str(UUID(int=n))
def payload():
    path=str((BASE/'synthetic-secret').resolve())
    return dict(enabled=True,legacy_mode='reject',semantic_merge_enabled=False,model_contract='trusted-model',
      control=dict(host='localhost',port=5432,database='control',role='reader',password_ref='control'),
      content_bindings=[dict(connection_ref='one',library_id=uid(1),principal_id=uid(2),credential_id=uid(3),
        device_id=uid(4),host='localhost',port=5432,database='content',role='runtime',password_ref='content')],
      secret_files=dict(control=path,content=path))
def startup(value):return ServerStartup(LanSettings(BASE/'no-data',BASE/'no-owner',{'jiangli':'1'*64,'kane':'2'*64}),parse_library_backend(value))
def host(**extra):
    cfg=parse_library_backend(payload())
    model_contract=extra.pop('model_contract','trusted-model')
    return LibraryHost(control_binding=cfg.control_binding,content_bindings=cfg.content_bindings,
        control_connector=lambda **k:None,content_connector=lambda **k:None,secret_resolver=lambda r:'test',
        config=Config(),model_contract=model_contract,enabled=True,semantic_merge_enabled=False,**extra)

class HostStartupChecks(unittest.TestCase):
    def test_selection_forms_and_bounds(self):
        for args in (dict(library_id=uid(1),project_key='alpha'),dict(library_ids=[uid(1),uid(2)],project_key='alpha'),
                     dict(selections=[dict(library_id=uid(1),project_key='alpha'),dict(library_id=uid(1),project_key='beta')])):
            selections,request,experience=LibraryHost._recall_arguments('context_search',dict(args,query='query'))
            self.assertTrue(selections);self.assertEqual(request.query,'query');self.assertIsNone(experience)
        bad=(dict(library_id=uid(1),library_ids=[uid(1)],project_key='alpha'),
             dict(selections=[dict(library_id=uid(1),project_key='alpha')]*2),
             dict(library_id=uid(1),project_key='alpha',principal_id=uid(2)),
             dict(library_id=uid(1),project_key='alpha',project='other'),
             dict(library_id=uid(1),project_key='alpha',content_types=['not-a-type']),
             dict(library_id=uid(1),project_key='alpha',top_k=True))
        for args in bad:
            with self.subTest(args=args),self.assertRaises(FoundationError):
                LibraryHost._recall_arguments('context_search',dict(args,query='query'))
        with self.assertRaises(FoundationError):LibraryHost._recall_arguments('experience_recall',
            dict(library_id=uid(1),project_key='alpha',query='q',constraints={'x':' '*4}))

    def test_config_defaults_strict_secret_refs_and_projection_gates(self):
        self.assertFalse(parse_library_backend(payload()).recall_enabled)
        value=payload();value['max_requests']=32;value['recall']=dict(enabled=True,vector_enabled=False)
        self.assertTrue(parse_library_backend(value).recall_enabled)
        value['recall']['projection']=dict(enabled=True,token_file=str(BASE/'worker-token'),library_ids=[uid(1)])
        with self.assertRaises(ValueError):parse_library_backend(value)
        value['recall']['vector_enabled']=True
        self.assertTrue(parse_library_backend(value).projection_enabled)
        value['secret_files']['worker_token']=str(BASE/'worker-token')
        with self.assertRaises(ValueError):parse_library_backend(value)

    def test_recall_advertised_only_when_enabled_and_routing_markers(self):
        self.assertFalse({'memory_search','context_search','experience_recall'} & {x['name'] for x in LibraryHost._specs()})
        specs=LibraryHost._specs(include_recall=True)
        self.assertTrue({'memory_search','context_search','experience_recall'} <= {x['name'] for x in specs})
        for key in ('library_ids','selections'):
            self.assertTrue(requires_library_backend(dict(method='tools/call',params=dict(arguments={key:[]}))))

    def test_real_rpc_routes_all_before_encoding_and_native_dispatch(self):
        events=[];provider=SimpleNamespace(contract=SimpleNamespace(key='trusted-model'))
        def encode(query,*,deadline):events.append(('encode',query,deadline));return (1.,)
        provider.encode_query=encode;provider.close=lambda:None
        h=host(recall_enabled=True,embedding_provider=provider)
        native=ModuleType('evolvmem.pg_business_core.native_search')
        class Search:
            def __init__(self,coordinator,config,**kwargs):events.append(('construct',kwargs))
            def search(self,routes,selections,request,**kwargs):
                events.append(('search',routes,selections));kwargs['publish']({'matches':[]});return 'done'
        native.PgNativeSearch=Search
        model=ModuleType('evolvmem.pg_vector_port.model');model.vector=lambda values,contract:('frozen',values)
        model.VectorError=type('VectorError',(RuntimeError,),{})
        guard=SyncRequestDeadlineGuard(2);identity=RequestIdentity(uid(2),uid(3),uid(4),1);outputs=[]
        def plan_many(identity,tool,ids,*,recall_transition=False):
            self.assertIs(recall_transition,True)
            events.append(('plan',ids));return ('route',)
        state=SimpleNamespace(guard=guard,identity=identity,served=False,published=False,coordinator='coordinator',
            router=SimpleNamespace(plan_many=plan_many))
        with patch.dict(sys.modules,{native.__name__:native,model.__name__:model}),guard.enforce():
            token=h._request.set(state)
            try:
                result=h.serve_rpc(identity,dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='context_search',
                    arguments=dict(query='query',selections=[dict(library_id=uid(1),project_key='alpha'),
                        dict(library_id=uid(1),project_key='beta')]))),publish=outputs.append)
            finally:h._request.reset(token)
        self.assertEqual(result,'done');self.assertEqual([e[0] for e in events],['plan','encode','construct','search'])
        self.assertEqual(events[0][1],(uid(1),));self.assertEqual(len(outputs),1);h.close()

    def test_startup_model_mismatch_closes_provider(self):
        import evolvmem.library_startup as mod
        value=payload();value['recall']=dict(enabled=True,vector_enabled=True)
        events=[];encoder=ModuleType('evolvmem.pg_vector_port.encoder')
        class Provider:
            contract=SimpleNamespace(key='wrong')
            def __init__(self,config):events.append('constructed')
            def initialize(self):events.append('initialize')
            def close(self):events.append('close')
        encoder.ManagedEmbeddingProvider=Provider
        with patch.dict(sys.modules,{encoder.__name__:encoder}),patch.object(Config,'from_file',return_value=Config()),self.assertRaises(Exception):
            build_library_host(startup(value),connector=lambda **k:None,secret_reader=lambda path:'synthetic')
        self.assertEqual(events,['constructed','initialize','close'])

    def test_close_stops_worker_then_provider_then_children_even_on_error(self):
        h=host(recall_enabled=True);events=[]
        h._projection_worker=SimpleNamespace(close=lambda **kw:events.append('worker'))
        h._embedding_provider=SimpleNamespace(close=lambda:events.append('provider'))
        h._read_budget=SimpleNamespace(close=lambda **kw:events.append('budget') or True)
        h.close();self.assertEqual(events,['worker','provider','budget'])
        h=host(recall_enabled=True);events=[]
        def failed(**kw):events.append('worker');raise RuntimeError('synthetic')
        h._projection_worker=SimpleNamespace(close=failed)
        h._embedding_provider=SimpleNamespace(close=lambda:events.append('provider'))
        h._read_budget=SimpleNamespace(close=lambda **kw:events.append('budget') or True)
        with self.assertRaises(FoundationError):h.close()
        self.assertEqual(events,['worker','provider','budget'])
        self.assertFalse(h._resources_closed)
        h._projection_worker=SimpleNamespace(close=lambda **kw:events.append('worker-confirmed'))
        h.close();self.assertTrue(h._resources_closed)

    def test_projection_factory_has_fresh_worker_scopes_and_private_auth(self):
        import evolvmem.library_host as mod
        from evolvmem.library_project_acl.coordinator import project_uuid
        provider=SimpleNamespace(contract=SimpleNamespace(key='trusted-model'),encode_query=lambda *a,**k:None,close=lambda:None)
        h=host(recall_enabled=True,embedding_provider=provider)
        worker_mod=ModuleType('evolvmem.pg_vector_port.worker');runtime=ModuleType('evolvmem.pg_vector_port.runtime')
        events=[];jobs=[];errors=[];states=[];guards=[]
        class Service:
            def __init__(self,factory,**kw):self.factory=factory;self.options=kw
            def start(self):events.append('start')
            def close(self,**kw):events.append('close')
        class Port:
            def __init__(self,manager,routes,contract,**kw):
                state=h._state();states.append(state);guards.append(state.guard)
                jobs.append((threading.get_ident(),state.identity,kw['project_key'],state.guard.timeout_seconds))
        class Router:
            def __init__(self,*a,**k):pass
            def plan(self,identity,tool,library_id):
                self_guard=h._state().guard
                assert not self_guard._state().sealed
                return (library_id,tool)
        class Manager:
            def __init__(self,router,registry,provider,authority):self.guard=authority.deadline_guard
            def run(self,route,operation):
                def execute(sql,params=()):
                    if sql=="SELECT to_regclass('em_security.history_readiness') IS NOT NULL":
                        assert params==()
                        return ((False,),)
                    assert sql.startswith('SELECT p.project_id::text,p.project_key,COALESCE(w.has_pending,false) FROM em_security.list_recall_projects(')
                    assert params[:2]==(uid(1),'write')
                    assert params[3]==uid(1) and params[4]=='trusted-model'
                    assert params[2] in (None,'alpha')
                    events.append(('listing',params));return ((project_uuid('alpha'),'alpha',True),)
                result=operation(SimpleNamespace(execute=execute));self.guard.quiesce(cancel=False);return result
        worker_mod.ProjectionDeferred=type('ProjectionDeferred',(RuntimeError,),{})
        from evolvmem.pg_vector_port.runtime import ProjectionResult as RealProjectionResult
        runtime.ProjectionResult=RealProjectionResult
        worker_mod.ProjectionService=Service;runtime.VectorPort=Port
        identity=RequestIdentity(uid(2),uid(3),uid(4),1)
        def auth(token):
            self.assertEqual(token,'private-worker-token');h._state().identity=identity
            events.append(('auth',threading.get_ident()));return identity
        h.authenticate=auth
        with patch.dict(sys.modules,{worker_mod.__name__:worker_mod,runtime.__name__:runtime}),\
                patch.object(mod,'AuthorityRouter',Router),patch.object(mod,'SessionManager',Manager),\
                patch.object(mod,'ParallelReadCoordinator',lambda *a,**k:None):
            h.start_projection(token_loader=lambda:'private-worker-token',library_ids=(uid(1),))
            with h.request_scope():
                parent_state=h._state()
                def work():
                    try:
                        for _ in range(2):
                            with h._projection_worker.factory() as context:self.assertEqual(len(context),2)
                            self.assertIsNone(h._request.get())
                    except BaseException as exc:errors.append(exc)
                thread=threading.Thread(target=work);thread.start();thread.join(2)
                self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
                self.assertTrue(all(state is not parent_state for state in states))
        self.assertIsNot(states[0],states[1]);self.assertIsNot(guards[0],guards[1])
        self.assertTrue(all(job[0]!=threading.get_ident() and job[1]==identity and job[3]<=30 for job in jobs))
        self.assertEqual(len([event for event in events if isinstance(event,tuple) and event[0]=='auth']),2)
        h.close()

if __name__=='__main__':unittest.main()
