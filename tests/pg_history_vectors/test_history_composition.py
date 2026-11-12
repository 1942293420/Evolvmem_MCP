"""Real runtime/search control flow with bounded external SQL/model seams."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict,replace
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from evolvmem.context_models import ContextSearchRequest
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_vector_port.model import VectorError, reusable
from evolvmem.pg_vector_port.projection import Document
from evolvmem.pg_vector_port.runtime import VectorPort
from evolvmem.pg_business_core.native_search import PgNativeSearch
from test_history_repository import LIB,PROJECT,CONTRACT,EMBEDDING,JOB,context,document,session


class Guard:
    def begin_authority(self):pass
    def check(self):pass
    @contextmanager
    def enforce(self):yield


def port():
    instance=object.__new__(VectorPort)
    instance.project_key=None;instance.contract=CONTRACT
    instance._historical_libraries=set()
    return instance


def test_actual_history_marker_selects_library_maintenance_without_project_resolver():
    actual,_,conn=session({'history_readiness':((True,),)},action='write')
    instance=port()
    result=instance.repository(actual)
    from evolvmem.pg_vector_port.history_repository import HistoryVectorRepository
    assert isinstance(result,HistoryVectorRepository) and result.project is None
    assert len(conn.calls)==1


@pytest.mark.parametrize('method',('search','search_embedding'))
def test_generic_history_search_rejected_before_provider_work(method):
    instance=port();instance._historical_libraries={LIB}
    with pytest.raises(VectorError,match='historical_vector_search_requires_business_recall'):
        getattr(instance,method)('query',index_kind='context',top_k=1,candidate_limit=1)


@pytest.mark.parametrize('cache_kind,expected_encodes',(('pg',0),('legacy',1)))
def test_projection_pg_cache_reused_after_short_prepare_transaction(cache_kind,expected_encodes):
    instance=port();instance.guard=Guard();instance.cache_lookup=None
    instance._scope=lambda route:route
    entered=[];active=False;encodes=0
    doc=Document(**dict(document(),projects=('A',)))
    proof=dict(contract=CONTRACT.document_fields if cache_kind=='pg' else ['old.gguf',768,'',512],
        text=JOB.content_hash,vector=EMBEDDING.digest)
    class Repository:
        def next_job(self):return JOB
        def document(self,job):return doc
        def cache_entry(self,job):return EMBEDDING.values,proof
        def apply(self,prepared,now):
            assert active and prepared.embedding==EMBEDDING
            entered.append(prepared);return 'upsert'
    class Manager:
        def run(self,route,fn):
            nonlocal active
            active=True
            try:return fn(None)
            finally:active=False
    instance.manager=Manager();instance.repository=lambda session:Repository()
    def encode(text,*,query):
        nonlocal encodes
        assert not active and not query
        encodes+=1;return EMBEDDING
    instance._embedding=encode
    result=instance.project_one(SimpleNamespace(action='write'))
    assert result.outcome=='upsert' and encodes==expected_encodes and len(entered)==1


def test_unknown_apply_result_never_reenters_apply():
    instance=port();instance.guard=Guard();instance.cache_lookup=None;instance._scope=lambda route:route
    instance._embedding=lambda *args,**kwargs:EMBEDDING
    calls=[]
    class Repository:
        def next_job(self):return JOB
        def document(self,job):return Document(**dict(document(),projects=('A',)))
        def apply(self,prepared,now):calls.append(prepared);raise FoundationError('write_commit_unknown')
    instance.repository=lambda s:Repository()
    instance.manager=SimpleNamespace(run=lambda route,fn:fn(None))
    with pytest.raises(FoundationError,match='write_commit_unknown'):
        instance.project_one(SimpleNamespace(action='write'))
    assert len(calls)==1


def test_historical_superseded_membership_removal_never_encodes_or_reuses_cache():
    instance=port();instance.guard=Guard();instance.cache_lookup=None;instance._scope=lambda route:route
    removal=replace(JOB,action='remove')
    ctx=context();ctx.pop('ann')
    answers={'history_readiness':((True,),),'history_vector_index_context':((ctx,),),
        'history_vector_next_job':((asdict(removal),),),
        'history_vector_document':((dict(document(),status='superseded'),),),
        'history_vector_cache_entry':((None,),),'history_vector_apply':(('remove',),)}
    actual,_,conn=session(answers,action='write')
    instance.manager=SimpleNamespace(run=lambda route,fn:fn(actual))
    encodes=[]
    instance._embedding=lambda text,**kwargs:encodes.append(text) or EMBEDDING
    result=instance.project_one(actual.route)
    assert result.outcome=='remove' and encodes==[]
    applied=[params for sql,params in conn.calls if 'history_vector_apply' in sql]
    assert len(applied)==1 and applied[0][-2:]==(None,None)
    assert all('history_vector_cache_entry' not in sql for sql,_ in conn.calls)


def test_native_superseded_temporal_remove_retains_existing_upsert_decision():
    instance=port();instance.guard=Guard();instance.cache_lookup=None;instance._scope=lambda route:route
    removal=replace(JOB,action='remove',project_id=PROJECT)
    doc=Document(**dict(document(PROJECT),status='superseded',projects=('A',)))
    class NativeRepository:
        def next_job(self):return removal
        def document(self,job):return doc
        def apply(self,prepared,now):
            assert prepared.embedding==EMBEDDING
            return 'upsert'
    instance.repository=lambda s:NativeRepository()
    instance.manager=SimpleNamespace(run=lambda route,fn:fn(None))
    encodes=[];instance._embedding=lambda text,**kwargs:encodes.append(text) or EMBEDDING
    result=instance.project_one(SimpleNamespace(action='write'))
    assert result.outcome=='upsert' and encodes==['原始 L0']


def search_instance(*,provider=True):
    config=SimpleNamespace(context_min_confidence=0)
    return PgNativeSearch(None,config,contract=CONTRACT if provider else None,embedding=EMBEDDING if provider else None)


def history_read_answers(ctx,*,installed=True):
    return {'history_readiness':((True,),),'history_vector_schema_migrations':((installed,),),
        'history_vector_context':((ctx,),),'history_vector_candidates':(),
        'history_vector_documents':(),'history_vector_projection_proof':((dict(context=ctx,query_vector_hash=EMBEDDING.pg_digest,rows=[]),),)}


def test_ready_history_owner_global_reaches_fixed_ann_gate_with_zero_hits():
    ctx=context();actual,scope,conn=session(history_read_answers(ctx),project=None)
    hits,repo=search_instance()._vectors(actual,scope,ContextSearchRequest(query='事实'),None)
    assert hits==() and repo is not None
    proof=repo.context_projection_proof(hits,EMBEDDING)
    assert proof['context']['ann']['reason']=='ann_ready'
    assert any('history_vector_candidates' in sql and params[1] is None for sql,params in conn.calls)


@pytest.mark.parametrize('cause,want',(('absent','ann_absent'),('pending','ann_not_built'),
    ('partial','ann_coverage_partial'),('asof','ann_as_of_lexical_only'),('provider','ann_provider_unconfigured')))
def test_history_fallback_keeps_explicit_reason_and_full_ann_context(cause,want):
    ctx=context()
    if cause in ('pending','partial'):
        ctx['ann'].update(enabled=False,coverage=cause,indexed_count=0,target_count=1,
            reason='ann_not_built' if cause=='pending' else 'ann_coverage_partial')
    actual,scope,_=session(history_read_answers(ctx,installed=cause!='absent'),project=None)
    search=search_instance(provider=cause!='provider')
    request=ContextSearchRequest(query='事实',as_of='2026-11-01 00:00:00' if cause=='asof' else None)
    _,repo=search._vectors(actual,scope,request,None)
    assert search._historical_reason(repo,request)==want
    if repo is not None:assert repo.context==ctx


def test_native_null_project_vector_path_remains_unavailable():
    actual,scope,_=session({'history_readiness':((False,),)},project=None)
    assert search_instance()._vectors(actual,scope,ContextSearchRequest(query='事实'),None)==((),None)


def empty_history_search(monkeypatch,*,cause='ready',mutate=None):
    """Keep the real ANN adapter/finalizer; substitute expensive source graph IO."""
    import evolvmem.pg_business_core.history_store as stores
    import evolvmem.pg_business_core.history_search as retrievers
    ctx=context()
    if cause=='pending':ctx['ann'].update(enabled=False,coverage='pending',indexed_count=0,target_count=None,reason='ann_not_built')
    answers=history_read_answers(ctx,installed=cause!='absent')
    actual,scope,conn=session(answers,project=None)
    prepared=[]
    class Store:
        def __init__(self,session,scope):pass
        def lexical_candidates(self,*args,**kwargs):return ()
        def prepare(self,ids):prepared.append(ids)
        def eligible(self,ident):return False
        def proof(self):return 'unchanged independently qualified source proof'
    class Retriever:
        def __init__(self,*args,**kwargs):pass
        def rank_candidates(self,request,lexical,vectors,**kwargs):
            assert not vectors
            return ()
    class Coordinator:
        def run_many(self,routes,read,*,finalize,combine,publish):
            initial=read(actual)
            if mutate is not None:mutate(ctx,answers)
            final=finalize(actual,initial)
            return combine(((actual.route,final),))
    monkeypatch.setattr(stores,'PgHistoryRecallStore',Store)
    monkeypatch.setattr(retrievers,'HistoryRetriever',Retriever)
    search=search_instance();search.coordinator=Coordinator();search._scope=lambda session,key:scope
    return search,actual,prepared,answers


@pytest.mark.parametrize('cause,reason,support',(('ready',None,'historical-hybrid'),
    ('pending','ann_not_built','historical-lexical-only'),('absent','ann_absent','historical-lexical-only')))
def test_empty_result_reports_selection_reason_after_fresh_proof(monkeypatch,cause,reason,support):
    search,actual,prepared,_=empty_history_search(monkeypatch,cause=cause)
    result=search.search((actual.route,),((LIB,None),),ContextSearchRequest(query='事实'))
    assert result['results']==[] and result['retrieval_support']==support
    assert result['retrieval_selections']==[dict(library_id=LIB,project_key=None,retrieval_support=support,reason=reason)]
    assert prepared==[(),()]


@pytest.mark.parametrize('cause',('ready','pending','absent'))
def test_zero_result_ann_context_change_cannot_publish(monkeypatch,cause):
    def change(ctx,answers):
        if cause=='absent':answers['history_vector_schema_migrations']=((True,),)
        else:ctx['ann']['revision']+=1
    search,actual,_,_=empty_history_search(monkeypatch,cause=cause,mutate=change)
    with pytest.raises(FoundationError,match='recall_selected_changed'):
        search.search((actual.route,),((LIB,None),),ContextSearchRequest(query='事实'))


def test_ann_admission_never_replaces_source_qualification(monkeypatch):
    search,actual,prepared,answers=empty_history_search(monkeypatch)
    answers['history_vector_candidates']=((7,3,JOB.content_hash,.25),)
    answers['history_vector_documents']=((7,document(None)),)
    result=search.search((actual.route,),((LIB,None),),ContextSearchRequest(query='事实'))
    assert result['results']==[] and prepared==[(7,),(7,)]
