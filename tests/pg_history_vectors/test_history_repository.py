"""Fixed SQL adapter tests. The DB boundary is substituted, not qualification."""
import importlib
import json
from dataclasses import asdict
from types import SimpleNamespace
import pytest

from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from evolvmem.pg_vector_port.model import ModelContract, VectorError, text_hash, vector
from evolvmem.pg_vector_port.projection import Job, Prepared
from evolvmem.pg_vector_port.search import Hit

LIB='00000000-0000-0000-0000-000000000001'
PROJECT='00000000-0000-0000-0000-000000000002'
REV='cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c'
CONTRACT=ModelContract('bge-base-zh-v1.5.f16.gguf',768,'','为这个句子生成表示以用于检索相关文章：',512,REV)
EMBEDDING=vector([1.0]+[0.0]*767,CONTRACT)

def context():
    return dict(model=dict(runtime_version='history-vector.v1',model_contract=CONTRACT.key,
        dimension=768,document_contract_text=json.dumps(CONTRACT.document_fields,ensure_ascii=False,separators=(',',':')),
        query_prefix=CONTRACT.query_prefix,model_revision=REV),
        source=dict(library_id=LIB,policy_epoch=1,route_epoch=1,history_readiness_revision=1,
        mapping_revision=1,membership_hash='a'*64,completion_epoch=LIB,completion_hash='b'*64,
        source_seal='c'*64,generation={k:dict(run_id=LIB,generation=0) for k in
        ('core','sources','organization','capture','governance','knowledge','capability','continuity','operations')},model_contract=CONTRACT.key),
        ann=dict(revision=1,enabled=True,coverage='complete',indexed_count=1,target_count=1,reason='ann_ready'))

def document(project=None):
    return dict(library_id=LIB,index_kind='context',entity_id=7,revision=3,text='原始 L0',status='active',
        expires_at=None,content_type='fact',source_state='evidence',projects=['A'],personal_general=False,
        has_experience_payload=False,unsupported_sources=0,project_id=project)

JOB=Job(1,LIB,'context',7,'upsert',3,text_hash('原始 L0'),CONTRACT.key,None)

class Connection:
    def __init__(self,answers):self.answers,self.calls=answers,[]
    def execute(self,sql,params=()):
        self.calls.append((sql,params))
        for name,value in self.answers.items():
            if name in sql:
                if isinstance(value,Exception):raise value
                return value
        raise AssertionError('unexpected SQL: '+sql)

def session(answers,*,action='read',project=PROJECT):
    connection=Connection(answers)
    identity=object()
    route=SimpleNamespace(action=action,identity=identity,library=SimpleNamespace(library_id=LIB,policy_epoch=1,route_epoch=1))
    actual=LibrarySession(route,connection)
    scope=ProjectScope(LIB,project,'A' if project else None,1,1,identity,action)
    return actual,scope,connection

def repository(answers,*,action='read',project=PROJECT):
    try:module=importlib.import_module('evolvmem.pg_vector_port.history_repository')
    except ModuleNotFoundError:pytest.fail('historical typed SQL repository is missing')
    actual,scope,connection=session(answers,action=action,project=project)
    return module.HistoryVectorRepository(actual,CONTRACT,scope if action=='read' else None),connection

def test_maintenance_uses_index_gate_and_maps_exact_job_document():
    ctx=context();ctx.pop('ann')
    repo,conn=repository({'history_vector_index_context':((ctx,),),
        'history_vector_next_job':((asdict(JOB),),),'history_vector_document':((document(),),)},action='write')
    assert repo.check_schema() is True
    assert repo.next_job()==JOB
    doc=repo.document(JOB)
    assert (doc.text,doc.projects,doc.project_id,doc.source_state)==('原始 L0',('A',),None,'evidence')
    assert all('resolve_project_scope' not in sql and 'history_item_inputs' not in sql for sql,_ in conn.calls)

@pytest.mark.parametrize('mutate',[
    lambda c:c['source'].update(extra='forbidden'),
    lambda c:c['source'].update(policy_epoch=True),
    lambda c:c['source']['generation'].update(unknown_domain=dict(run_id=LIB,generation=0)),
    lambda c:c['source'].update(generation={}),
    lambda c:c['model'].update(document_contract_text='[]'),
    lambda c:c['model'].update(model_contract='0'*64),
    lambda c:c['ann'].update(indexed_count=True),
    lambda c:c['ann'].update(enabled=True,coverage='complete',target_count=2),
])
def test_read_context_rejects_malformed_or_inconsistent_proof(mutate):
    ctx=context();mutate(ctx)
    repo,_=repository({'history_vector_context':((ctx,),)})
    with pytest.raises(VectorError):repo.check_schema()


def test_public_generation_uses_completed_domain_subset_without_fake_rows():
    ctx=context()
    ctx['source']['generation']={k:ctx['source']['generation'][k] for k in ('core','sources','knowledge')}
    repo,_=repository({'history_vector_context':((ctx,),)})
    assert repo.check_schema() is True
    assert repo.context['source']['generation']==ctx['source']['generation']

def test_zero_hit_proof_still_carries_read_context_and_pg_query_hash():
    ctx=context()
    proof=dict(context=ctx,query_vector_hash=EMBEDDING.pg_digest,rows=[])
    repo,conn=repository({'history_vector_context':((ctx,),),'history_vector_projection_proof':((proof,),)})
    assert repo.context_projection_proof((),EMBEDDING)==proof
    assert any(params[-2:]==([],EMBEDDING.literal) for sql,params in conn.calls if 'projection_proof' in sql)
    proof['query_vector_hash']='0'*64
    with pytest.raises(VectorError,match='recall_vector_changed'):repo.context_projection_proof((),EMBEDDING)

def test_selected_projection_cannot_be_silently_dropped():
    ctx=context();proof=dict(context=ctx,query_vector_hash=EMBEDDING.pg_digest,rows=[])
    repo,_=repository({'history_vector_context':((ctx,),),'history_vector_projection_proof':((proof,),)})
    with pytest.raises(VectorError,match='recall_vector_changed'):
        repo.context_projection_proof((Hit(LIB,'context',7,3,JOB.content_hash,.25,PROJECT),),EMBEDDING)

def test_pg_cache_hash_is_verified_before_canonical_reuse_proof():
    cached=dict(values=list(EMBEDDING.values),document_contract=CONTRACT.document_fields,
        content_hash=JOB.content_hash,vector_hash=EMBEDDING.pg_digest)
    repo,_=repository({'history_vector_cache_entry':((cached,),)},action='write')
    values,proof=repo.cache_entry(JOB)
    assert proof==dict(contract=CONTRACT.document_fields,text=JOB.content_hash,vector=EMBEDDING.digest)
    assert values==EMBEDDING.values
    cached['vector_hash']=EMBEDDING.digest
    with pytest.raises(VectorError):repo.cache_entry(JOB)

def test_apply_sends_exact_job_and_actual_pg_vector_hash_once():
    repo,conn=repository({'history_vector_apply':(('upsert',),)},action='write')
    assert repo.apply(Prepared(JOB,EMBEDDING),'2026-11-03 00:00:00')=='upsert'
    assert len(conn.calls)==1
    params=conn.calls[0][1]
    assert json.loads(params[3])==asdict(JOB)
    assert params[4:]==(EMBEDDING.literal,EMBEDDING.pg_digest)

def test_sql_error_is_not_optional_ann_absence():
    repo,_=repository({'history_vector_context':RuntimeError('sql failed')})
    with pytest.raises(RuntimeError,match='sql failed'):repo.check_schema()

def test_candidates_map_requested_scope_and_validate_distance():
    ctx=context()
    repo,_=repository({'history_vector_context':((ctx,),),
        'history_vector_candidates':((7,3,JOB.content_hash,.25),)})
    assert repo.candidates(EMBEDDING,120,content_types=('fact',),now='2026-11-03 00:00:00')==(
        Hit(LIB,'context',7,3,JOB.content_hash,.25,PROJECT),)

def test_bootstrap_and_delta_preserve_bounded_durable_cursors():
    page=dict(index_kind='context',after_id=8,scanned=2,enqueued=1,complete=False)
    delta=dict(epoch_id=LIB,after_ordinal=1,after_change_ordinal=2,fanout_after_id=9,scanned=2,enqueued=1,complete=False)
    repo,_=repository({'history_vector_enqueue_page':((page,),),'history_vector_delta_page':((delta,),)},action='write')
    assert repo.enqueue_current_page('context',0,64,'2026-11-03 00:00:00').after_id==8
    assert repo.delta_page(LIB,-1,-1,64)==delta
    with pytest.raises(VectorError):repo.enqueue_current_page('context',0,65,'2026-11-03 00:00:00')


@pytest.mark.parametrize('bad_contract',[
    ['bge-base-zh-v1.5.f16.gguf@sha256:'+REV,768.0,'',512],
    ['bge-base-zh-v1.5.f16.gguf@sha256:'+REV,768,'',512.0],
])
def test_cache_rejects_noninteger_contract_dimensions_or_token_window(bad_contract):
    cached=dict(values=list(EMBEDDING.values),document_contract=bad_contract,
        content_hash=JOB.content_hash,vector_hash=EMBEDDING.pg_digest)
    repo,_=repository({'history_vector_cache_entry':((cached,),)},action='write')
    with pytest.raises(VectorError,match='invalid_history_vector_cache'):repo.cache_entry(JOB)
