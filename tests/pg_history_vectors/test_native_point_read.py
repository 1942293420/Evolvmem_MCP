"""Native point reads exercise real qualification with an isolated SQL seam."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace
from uuid import UUID
import pytest

ROOT=Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT',str(Path(__file__).resolve().parents[2])))
OVERLAY=os.environ.get('EVOLVMEM_TEST_OVERLAY')
PATHS=([Path(OVERLAY)/'evolvmem'] if OVERLAY else [])+[ROOT/'evolvmem']
package=ModuleType('evolvmem');package.__path__=[str(p) for p in PATHS];sys.modules['evolvmem']=package
business=ModuleType('evolvmem.pg_business_core');business.__path__=[str(p/'pg_business_core') for p in PATHS]
sys.modules[business.__name__]=business

from evolvmem.context_models import ContextLayer,ContextReadRequest
from evolvmem.library_foundation import FoundationError,RequestIdentity,LibraryDescriptor,AuthorizedRoute
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.pg_business_core.native_store import PgNativeRecallStore
from evolvmem.pg_business_core.executor import PgBusinessExecutor
from evolvmem.pg_business_core.native_qualification_sql import READ_CONTEXT,BATCH

uid=lambda n:str(UUID(int=n))
LIB=uid(1);PROJECT=project_uuid('project-a')
digest=lambda text:hashlib.sha256(text.encode()).hexdigest()

def fixture():
    item=dict(id=1,library_id=LIB,project_id=PROJECT,revision=1,identity_key='manual:one',
        project='project-a',projects=[],scope='project',content_type='reference',status='active',
        success_count=0,experience_payload=None,source_count=1,source_state='evidence',personal_general=False,
        created_at='2026-10-01 00:00:00',updated_at='2026-10-01 00:00:00',expires_at=None,
        effective_from=None,effective_until=None,superseded_by=None,supersedes=None,
        importance=0.5,confidence=1.0,tags=[],tier='reference',failure_count=0,access_count=0,
        last_accessed=None,last_verified_at=None,occurred_at=None,mentioned_at=None)
    meta=dict(library_id=LIB,project_id=PROJECT,item_id=1,revision=1)
    source=dict(library_id=LIB,project_id=PROJECT,item_id=1,id=10,archive_id=None,
        source_kind='manual',source_ref='manual:reference',extraction_version='native-v1')
    source['qualification']=dict(meta,source_id=10,state='available',verification='verified',
        source_revision=1,content_hash=digest('source'),source_kind=source['source_kind'],
        source_ref=source['source_ref'],extraction_version=source['extraction_version'],archive_id=None)
    graph=dict(item=item,item_l1_hash=digest('full l1'),learning=[],decision=[],resolution=[],
        registry=[dict(meta,project='project-a',project_uuid=PROJECT,status='active')],
        sources=[source],derivations=[],evidence=[],rollup=[])
    return dict(graph=graph,layers=[(name,text,digest(text),'fixture',len(text.encode()))
        for name,text in [('l0','summary'),('l1','full l1'),('l2','full original l2')]])

def session(state):
    class Connection:
        def execute(self,sql,params=()):
            graph=state.get('graph');item=graph['item'] if graph else None
            if sql==READ_CONTEXT:return [(1,'native-v1',7,1,2)]
            if sql==BATCH:return [(1,deepcopy(graph))] if graph and 1 in params[2] else []
            if sql.startswith('SELECT to_jsonb(c)'):return [(deepcopy(item),)] if item and 1 in params[2:] else []
            if sql.startswith('WITH wanted'):return [(item['identity_key'],item['project'],item['scope'],1)] if item else []
            if sql.startswith('SELECT item_id,layer,CASE'):
                return [(1,name,text if name=='l0' else None,h,g,n) for name,text,h,g,n in state['layers']]
            if sql.startswith('SELECT layer,content,content_hash,generator,octet_length(content)'):
                return deepcopy(state['layers'])
            if sql.startswith('SELECT count(*) FROM em_content.context_sources'):return [(1,)]
            raise AssertionError('unexpected SQL: '+sql)
    identity=RequestIdentity(uid(2),uid(3),uid(4),1)
    lib=LibraryDescriptor(LIB,'device','postgres','target',7,1,'pg_active','ready','postgres',
        device_id=uid(5),owner_principal_id=uid(3),active=True)
    actual=LibrarySession(AuthorizedRoute(identity,lib,'context_read','read'),Connection())
    scope=ProjectScope(LIB,PROJECT,'project-a',7,1,identity,'read')
    return actual,scope

def read(state,layer=ContextLayer.L2):
    actual,scope=session(state)
    return PgNativeRecallStore(actual,scope).read(ContextReadRequest(1,layer))

def execute(state,args=None,change=None):
    published=[];snapshots=[]
    class Manager:
        def run(self,route,operation,*,publish=None):return operation(session(state)[0])
        def run_many(self,routes,operation,*,finalize,combine,publish):
            initial=operation(session(state)[0]);snapshots.append(initial)
            if change:change(state)
            final=finalize(session(state)[0],initial);snapshots.append(final)
            result=combine(((routes[0],final),))
            if publish:publish(result)
            return result
    executor=PgBusinessExecutor(Manager(),SimpleNamespace(),model_contract='fixture',
        semantic_merge_enabled=False,project_key='project-a')
    executor._project_preflight=lambda actual,errors:session_scope(actual)
    route=session(state)[0].route
    return executor.read(route,args if args is not None else dict(id=1,layer='l2'),publish=published.append),published,snapshots

def session_scope(actual):
    return ProjectScope(LIB,PROJECT,'project-a',7,1,actual.route.identity,'read')

def test_qualified_manual_reference_exact_layer_is_readable():
    assert read(fixture()).content=='full original l2'

def test_native_executor_reads_manual_reference_with_fresh_qualified_snapshot():
    result,published,snapshots=execute(fixture())
    assert result==dict(id=1,layer='l2',content='full original l2')
    assert published==[result] and len(snapshots)==2

def test_native_verified_experience_requires_current_success_proof():
    state=fixture();graph=state['graph'];item=graph['item']
    item.update(content_type='experience',success_count=1,experience_payload=dict(project='project-a',
        problem='PostgreSQL recall latency',conditions={'database':'postgres'},steps=['Bound scan work'],
        rationale='Bound critical section',result='Low latency',applicability=['Concurrent recall'],
        exclusions=[],transferable=True,parent_experience_id=None))
    source=graph['sources'][0];source['source_kind']='tool_result'
    source['qualification']['source_kind']='tool_result'
    graph['evidence']=[dict(library_id=LIB,project_id=PROJECT,item_id=1,revision=1,evidence_id='ev-1',
        source_id=10,item_revision=1,item_content_hash=digest('full l1'),source_revision=1,
        source_hash=digest('source'),state='active',outcome='success',verification='verified',role='tool',
        task_id='task-1',event_id='event-1',event_order=1,conditions={'database':'postgres'},verification_level='technical')]
    assert read(state).content=='full original l2'
    graph['evidence'][0]['state']='withdrawn'
    result=read(state)
    assert result.error_code=='not_readable' and result.content==''

@pytest.mark.parametrize('mode,code', [('missing','not_found'),('withdrawn','not_readable'),
    ('inactive','not_readable'),('expired','expired'),('future','not_readable')])
def test_current_eligibility_and_time_refuse_content(mode,code):
    state=fixture();item=state['graph']['item']
    if mode=='missing':state['graph']=None
    if mode=='withdrawn':state['graph']['sources'][0]['qualification']['state']='withdrawn'
    if mode=='inactive':item['status']='deleted'
    if mode=='expired':item['expires_at']='2000-01-01 00:00:00'
    if mode=='future':item['effective_from']='2999-01-01 00:00:00'
    result=read(state)
    assert result.error_code==code and result.content==''

@pytest.mark.parametrize('name',['l0','l1','l2'])
def test_every_original_layer_hash_is_verified_before_disclosure(name):
    state=fixture();state['layers']=[(n,text,'0'*64 if n==name else h,g,size)
        for n,text,h,g,size in state['layers']]
    with pytest.raises(FoundationError,match='recall_content_hash_mismatch'):read(state)

@pytest.mark.parametrize('change', ['item','layer','source_version','withdrawal'])
def test_final_independent_snapshot_rejects_dependency_races(change):
    def mutate(state):
        if change=='item':state['graph']['item']['revision']+=1
        if change=='source_version':state['graph']['sources'][0]['qualification']['source_revision']+=1
        if change=='withdrawal':state['graph']['sources'][0]['qualification']['state']='withdrawn'
        if change=='layer':state['layers'][2]=('l2','new original',digest('new original'),'fixture',12)
    with pytest.raises(FoundationError,match='recall_selected_changed'):execute(fixture(),change=mutate)

@pytest.mark.parametrize('args',[dict(id=True),dict(id=0),dict(id=1,layer='l0'),dict(id=1,layer='invalid')])
def test_invalid_arguments_keep_mcp_error_semantics(args):
    result,_,_=execute(fixture(),args)
    assert result.get('error')=='invalid_arguments'
