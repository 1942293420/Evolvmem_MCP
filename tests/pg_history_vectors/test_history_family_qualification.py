"""Family denials are scoped, opaque and part of the fresh source proof."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import pytest

from evolvmem.context_models import ContextLayer,ContextReadRequest,ContextSearchRequest
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_business_core.history_knowledge import CATALOG,PgHistoryKnowledgeDirectory,project_catalog
from evolvmem.pg_business_core.history_store import PgHistoryRecallStore
from evolvmem.pg_business_core.native_search import PgNativeSearch,SelectionRead

LIB='00000000-0000-0000-0000-000000000001'
PROJECT='00000000-0000-0000-0000-000000000002'
GOOD=('good','A','project');BAD=('blocked','A','project')


def envelope(ident,key,*,successor=None,status='active',token='source-v1'):
    item=dict(id=ident,library_id=LIB,identity_key=key[0],project=key[1],scope=key[2],
        status=status,superseded_by=successor,expires_at=None,effective_from=None,effective_until=None)
    layers=[]
    for name in ('l0','l1','l2'):
        content=f'synthetic {ident} {name}'
        layers.append(dict(layer=name,content=content,content_hash=hashlib.sha256(content.encode()).hexdigest(),generator='fixture'))
    return dict(item=item,layers=layers,source_token=token)


class Scope:
    library_id=LIB;project_id=PROJECT;project_key='A'
    def require(self,session):pass


class Session:
    def __init__(self,rows,families):
        self.source=deepcopy(rows);self.families=families;self.loads=[];self.calls=[]
        self.route=SimpleNamespace(action='read',library=SimpleNamespace(library_id=LIB))
    def execute(self,sql,params=()):
        self.calls.append((sql,params))
        if 'history_identity_family' in sql:
            assert 'history_identity_family_qualified' in sql
            if isinstance(self.families,Exception):raise self.families
            return self.families
        if 'history_knowledge_inputs' in sql:
            return ((dict({k:[] for k in CATALOG},restricted_conflicts=False),),)
        if 'history_layer_read' in sql:
            ident,name=params[2:]
            layer=next(l for l in self.source[ident]['layers'] if l['layer']==name)
            return ((dict(item=dict(id=ident),layer=layer),),)
        raise AssertionError('unexpected SQL')


class Eligibility:
    now='2026-11-03 00:00:00'
    def __init__(self,session,scope):self.session=session;self.rows={}
    def _load(self,ids):
        for ident in ids:
            self.session.loads.append(ident);self.rows[ident]=deepcopy(self.session.source.get(ident))
    def eligible_ids(self,ids):return {i for i in ids if self.rows.get(i) is not None}
    def proof(self):return self.rows


@pytest.fixture
def store_factory(monkeypatch):
    import evolvmem.pg_business_core.history_store as module
    monkeypatch.setattr(module,'HistoryEligibilityRead',Eligibility)
    def make(rows=None,families=None):
        session=Session(rows or {1:envelope(1,GOOD),2:envelope(2,BAD,successor=9999)},
            families if families is not None else [(*GOOD,1,'admitted'),(*BAD,None,'denied')])
        return module.PgHistoryRecallStore(session,Scope()),session
    return make


def test_mixed_family_denial_keeps_good_point_read_and_never_fetches_hidden_successor(store_factory):
    store,session=store_factory();store.prepare((1,2))
    assert store.eligible(1) and not store.eligible(2)
    assert store.families[GOOD]==(1,) and store.families[BAD]==()
    assert set(session.loads)=={1,2} and 9999 not in session.loads
    denied=store.read(ContextReadRequest(id=2,layer=ContextLayer.L1))
    assert denied.error_code=='not_readable' and denied.content==''
    admitted=store.read(ContextReadRequest(id=1,layer=ContextLayer.L1))
    assert admitted.error_code is None and admitted.content=='synthetic 1 l1'
    assert all('score' not in sql for sql,_ in session.calls)


def test_denial_excludes_all_seeded_members_and_knowledge_extra_cannot_re_admit(store_factory):
    rows={1:envelope(1,GOOD),2:envelope(2,BAD),3:envelope(3,BAD)}
    store,_=store_factory(rows);store.prepare((1,2,3))
    store.allowed.update((2,3))  # Same operation as the existing knowledge extra loader.
    assert not store.eligible(2) and not store.eligible(3) and store.eligible(1)
    catalog=dict({k:[] for k in CATALOG},restricted_conflicts=False)
    catalog['knowledge_objects']=[dict(knowledge_id='bad-manual',item_id=2,retired=0,
        decision_json={'action':'include','projects':['A']},category='reference')]
    managed,items=project_catalog(store,catalog,(2,))
    assert managed=={2} and items==[]
    PgHistoryKnowledgeDirectory(store).load((1,))
    assert not store.eligible(2)


@pytest.mark.parametrize('error',[RuntimeError('history_family_scope_unavailable'),RuntimeError('statement_timeout'),
    RuntimeError('source_corrupt'),RuntimeError('deadline')])
def test_unseeded_foreign_and_unrelated_sql_source_deadline_errors_propagate(store_factory,error):
    store,_=store_factory(families=error)
    with pytest.raises(RuntimeError) as raised:store.prepare((1,))
    assert raised.value is error


@pytest.mark.parametrize('records',[
    [(*BAD,None,'denied'),(*BAD,2,'admitted'),(*GOOD,1,'admitted')],
    [(*GOOD,1,'admitted')],
    [(*BAD,2,'denied'),(*GOOD,1,'admitted')],
    [(*BAD,None,'other'),(*GOOD,1,'admitted')],
    [(*BAD,None,'denied'),(*GOOD,True,'admitted')],
    [(*BAD,None,'denied'),(*GOOD,1,'admitted'),(*GOOD,1,'admitted')],
    [(*BAD,None,'denied'),('foreign','B','project',3,'admitted')],
])
def test_malformed_or_incomplete_family_contract_refuses(store_factory,records):
    store,_=store_factory(families=records)
    with pytest.raises(FoundationError):store.prepare((1,2))


def test_family_boundary_accepts_512_and_rejects_513_before_fetch(store_factory):
    rows={i:envelope(i,GOOD) for i in range(1,514)}
    store,session=store_factory(rows,[(*GOOD,i,'admitted') for i in range(1,513)])
    store.prepare((1,));assert len(store.rows)==512 and store.eligible(512)
    store,session=store_factory(rows,[(*GOOD,i,'admitted') for i in range(1,514)])
    with pytest.raises(FoundationError,match='recall_dependency_limit'):store.prepare((1,))
    assert session.loads==[1]


def test_family_key_budget_240_is_retained(store_factory):
    store,session=store_factory()
    with pytest.raises(FoundationError,match='recall_candidate_limit'):store.prepare(tuple(range(1,242)))
    assert session.calls==[] and session.loads==[]


def test_fresh_family_marker_change_is_rejected_by_real_search_finalizer(store_factory):
    store,session=store_factory({1:envelope(1,GOOD),2:envelope(2,BAD)});store.prepare((1,2));first_proof=store.proof()
    class Coordinator:
        def run_many(self,routes,read,*,finalize,combine,publish):
            prior=read(session)
            session.families=[(*GOOD,1,'admitted'),(*BAD,2,'admitted')]
            return combine(((session.route,finalize(session,prior)),))
    search=PgNativeSearch(Coordinator(),SimpleNamespace(context_min_confidence=0))
    def read(s,key,request,kind,experience,prior=None):
        fresh=PgHistoryRecallStore(s,Scope());fresh.prepare((1,2))
        return SelectionRead('A',proof=fresh.proof(),dto=(),retrieval_support='historical-hybrid')
    search._read=read
    with pytest.raises(FoundationError,match='recall_selected_changed'):
        search.search((session.route,),((LIB,'A'),),ContextSearchRequest(query='synthetic'))
    assert first_proof==store.proof()


def test_unchanged_family_marker_still_retains_source_token_proof(store_factory):
    first,session=store_factory();first.prepare((1,2));prior=first.proof()
    session.source[1]['source_token']='source-v2'
    fresh=PgHistoryRecallStore(session,Scope());fresh.prepare((1,2))
    assert fresh.family_proof==first.family_proof and fresh.proof()!=prior
