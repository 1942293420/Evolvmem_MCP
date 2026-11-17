"""Typed historical ANN boundary. Source qualification remains in HistoryStore.

Only the twelve fixed definer functions cross the data boundary. The actual
session action chooses the read versus owner maintenance gate; NULL project is
an owner-global read or a library queue, never a project wildcard.
"""
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import datetime
import json
import math
import re
from uuid import UUID

from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from .model import ModelContract, Embedding, require, text_hash, vector
from .projection import Job, Document, Prepared
from .repository import BackfillPage
from .search import Hit


MARKER="SELECT to_regclass('em_content.history_vector_schema_migrations') IS NOT NULL"
SQL = {
    'context': 'SELECT em_security.history_vector_context(%s,%s,%s)',
    'index_context': 'SELECT em_security.history_vector_index_context(%s,%s)',
    'next_job': 'SELECT em_content.history_vector_next_job(%s,%s)',
    'document': 'SELECT em_content.history_vector_document(%s,%s,%s)',
    'documents': 'SELECT id,document FROM em_content.history_vector_documents(%s,%s,%s,%s::bigint[])',
    'cache_entry': 'SELECT em_content.history_vector_cache_entry(%s,%s,%s)',
    'enqueue_page': 'SELECT em_content.history_vector_enqueue_page(%s,%s,%s,%s)',
    'apply': 'SELECT em_content.history_vector_apply(%s,%s,%s,%s::jsonb,%s::public.vector,%s)',
    'candidates': 'SELECT entity_id,entity_revision,content_hash,distance FROM em_content.history_vector_candidates(%s,%s,%s,%s::public.vector,%s,%s::text[],%s,%s,%s,%s,%s::jsonb)',
    'projection_proof': 'SELECT em_content.history_vector_projection_proof(%s,%s,%s,%s::bigint[],%s::public.vector)',
    'delta_page': 'SELECT em_content.history_vector_delta_page(%s,%s,%s,%s,%s,%s)',
    'finish': 'SELECT em_content.history_vector_finish(%s,%s)',
}
_REVISION='cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c'
_ACTUAL=ModelContract('bge-base-zh-v1.5.f16.gguf',768,'','为这个句子生成表示以用于检索相关文章：',512,_REVISION)
_GENERATION=('core','sources','organization','capture','governance','knowledge','capability','continuity','operations')


def installed(session):
    rows=session.execute(MARKER)
    require(len(rows)==1 and len(rows[0])==1 and type(rows[0][0]) is bool,'invalid_history_vector_marker')
    return rows[0][0]


def _shape(value, names, code='invalid_history_vector_shape'):
    require(isinstance(value,dict) and set(value)==set(names),'invalid_history_vector_shape' if code is None else code)
    return value


def _integer(value, minimum=0, maximum=None):
    return type(value) is int and value>=minimum and (maximum is None or value<=maximum)


def _hash(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _uuid(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except ValueError:return False


def _moment(value):
    if not isinstance(value,str) or re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d',value) is None:return False
    try:datetime.strptime(value,'%Y-%m-%d %H:%M:%S');return True
    except ValueError:return False


def _finite(value):return type(value) in (int,float) and math.isfinite(value)


class HistoryVectorRepository:
    historical=True

    def __init__(self,session,contract,project_scope=None):
        require(isinstance(session,LibrarySession) and not session._closed
            and isinstance(contract,ModelContract),'actual_vector_session_required')
        require(session.route.action in ('read','write','admin'),'invalid_project_permission')
        if session.route.action=='read':
            require(isinstance(project_scope,ProjectScope),'actual_vector_session_required')
            project_scope.require(session)
        else:
            require(project_scope is None,'history_index_scope_must_be_library')
        self.session,self.contract,self.scope=session,contract,project_scope
        self.lib=session.route.library.library_id
        self.project=None if project_scope is None else project_scope.project_id
        self.context=None

    def _read(self):
        require(self.session.route.action=='read' and self.scope is not None,'read_session_required')
        self.scope.require(self.session)

    def _write(self):
        require(self.session.route.action in ('write','admin') and self.scope is None,'vector_write_scope_required')

    def _one(self,name,params):
        rows=self.session.execute(SQL[name],params)
        require(len(rows)==1 and len(rows[0])==1,'invalid_history_vector_result')
        return rows[0][0]

    def _context(self,value,*,index=False):
        _shape(value,('model','source') if index else ('model','source','ann'))
        model=_shape(value['model'],('runtime_version','model_contract','dimension','document_contract_text','query_prefix','model_revision'))
        expected=dict(runtime_version='history-vector.v1',model_contract=self.contract.key,
            dimension=self.contract.dimension,document_contract_text=json.dumps(self.contract.document_fields,ensure_ascii=False,separators=(',',':')),
            query_prefix=self.contract.query_prefix,model_revision=self.contract.model_revision)
        require(self.contract==_ACTUAL and type(model['dimension']) is int and model==expected,'registered_history_vector_model_required')
        source=_shape(value['source'],('library_id','policy_epoch','route_epoch','history_readiness_revision','mapping_revision',
            'membership_hash','completion_epoch','completion_hash','source_seal','generation','model_contract'))
        require(_uuid(source['library_id']) and source['library_id']==self.lib
            and source['model_contract']==self.contract.key
            and all(_integer(source[k],1) for k in ('policy_epoch','route_epoch','history_readiness_revision','mapping_revision'))
            and source['policy_epoch']==self.session.route.library.policy_epoch
            and source['route_epoch']==self.session.route.library.route_epoch
            and _uuid(source['completion_epoch'])
            and all(_hash(source[k]) for k in ('membership_hash','completion_hash','source_seal')),'invalid_history_vector_source')
        generation=source['generation']
        # The completed catch-up epoch declares the actual imported domains.
        # Public libraries have fewer domains; no placeholder progress rows or
        # invented run IDs are needed. SQL proves equality to that exact epoch.
        require(isinstance(generation,dict) and bool(generation)
            and set(generation)<=set(_GENERATION),'invalid_history_vector_generation')
        for entry in generation.values():
            _shape(entry,('run_id','generation'))
            require(_uuid(entry['run_id']) and _integer(entry['generation']),'invalid_history_vector_generation')
        if not index:
            ann=_shape(value['ann'],('revision','enabled','coverage','indexed_count','target_count','reason'))
            reasons={'pending':'ann_not_built','partial':'ann_coverage_partial','complete':'ann_ready'}
            require(_integer(ann['revision'],1) and type(ann['enabled']) is bool
                and isinstance(ann['coverage'],str) and ann['coverage'] in reasons and ann['reason']==reasons[ann['coverage']]
                and _integer(ann['indexed_count']) and (ann['target_count'] is None or _integer(ann['target_count']))
                and (ann['coverage']!='complete' or ann['enabled'] and ann['target_count'] is not None
                    and ann['indexed_count']==ann['target_count'])
                and (not ann['enabled'] or ann['coverage']=='complete'),'invalid_history_vector_ann_state')
        return deepcopy(value)

    def check_model(self):
        if self.session.route.action=='read':
            self._read()
            value=self._one('context',(self.lib,self.project,self.contract.key))
        else:
            self._write()
            value=self._one('index_context',(self.lib,self.contract.key))
        self.context=self._context(value,index=self.session.route.action!='read')
        return self.context

    def check_schema(self):self.check_model();return True

    def _job(self,value):
        _shape(value,(f.name for f in fields(Job)))
        require(all(_integer(value[k],1) for k in ('id','entity_id','revision'))
            and value['library_id']==self.lib and _uuid(value['library_id']) and value['project_id'] is None
            and value['index_kind']=='context' and value['action'] in ('upsert','remove')
            and _hash(value['content_hash']) and value['model_contract']==self.contract.key,'invalid_vector_job')
        return Job(**value)

    def next_job(self):
        self._write();self.check_model()
        value=self._one('next_job',(self.lib,self.contract.key))
        return None if value is None else self._job(value)

    def _document(self,value,project):
        _shape(value,(f.name for f in fields(Document)))
        require(value['library_id']==self.lib and value['index_kind']=='context'
            and value['project_id']==project and (project is None or _uuid(project))
            and all(_integer(value[k],1) for k in ('entity_id','revision'))
            and isinstance(value['text'],str) and len(value['text'])<=32768 and len(value['text'].encode('utf-8'))<=8*1024*1024
            and all(isinstance(value[k],str) for k in ('status','content_type','source_state'))
            and (value['expires_at'] is None or _moment(value['expires_at']))
            and isinstance(value['projects'],list) and all(isinstance(p,str) for p in value['projects'])
            and all(type(value[k]) is bool for k in ('personal_general','has_experience_payload'))
            and type(value['unsupported_sources']) is int and value['unsupported_sources']==0,'invalid_history_vector_document')
        return Document(**dict(value,projects=tuple(value['projects'])))

    def document(self,job,*,lock=False):
        self._write();require(type(lock) is bool,'invalid_history_vector_document')
        require(isinstance(job,Job) and self._job(asdict(job))==job,'invalid_vector_job')
        value=self._one('document',(self.lib,self.contract.key,job.id))
        if value is None:return None
        document=self._document(value,None)
        require((document.entity_id,document.revision,text_hash(document.text))==(job.entity_id,job.revision,job.content_hash),
            'context_layer_proof_invalid')
        return document

    @staticmethod
    def _ids(ids):
        require(isinstance(ids,tuple) and len(ids)<=240 and all(_integer(i,1) for i in ids)
            and len(set(ids))==len(ids),'invalid_vector_candidate_batch')
        return sorted(ids)

    def documents(self,index_kind,ids):
        self._read();require(index_kind=='context','unsupported_vector_index')
        selected=self._ids(ids)
        if not selected:return {}
        rows=self.session.execute(SQL['documents'],(self.lib,self.project,self.contract.key,selected))
        require(len(rows)<=len(selected),'invalid_vector_candidate_batch')
        result={};previous=0
        for ident,value in rows:
            require(_integer(ident,1) and ident in selected and ident>previous,'invalid_vector_candidate_batch')
            doc=self._document(value,self.project)
            require(doc.entity_id==ident,'projection_scope_mismatch')
            result[ident]=doc;previous=ident
        return result

    def cache_entry(self,job):
        self._write();require(isinstance(job,Job) and self._job(asdict(job))==job,'invalid_vector_job')
        value=self._one('cache_entry',(self.lib,self.contract.key,job.id))
        if value is None:return None
        _shape(value,('values','document_contract','content_hash','vector_hash'))
        require(isinstance(value['values'],list) and isinstance(value['document_contract'],list)
            and len(value['document_contract'])==4
            and type(value['document_contract'][1]) is int and type(value['document_contract'][3]) is int
            and value['document_contract']==self.contract.document_fields
            and value['content_hash']==job.content_hash and _hash(value['vector_hash']),'invalid_history_vector_cache')
        embedding=vector(value['values'],self.contract)
        require(embedding.pg_digest==value['vector_hash'],'invalid_history_vector_cache')
        # reusable() accepts the established little-endian proof. Only a
        # verified new PG object can be converted into that proof here.
        return embedding.values,dict(contract=value['document_contract'],text=value['content_hash'],vector=embedding.digest)

    def apply(self,prepared,now):
        self._write();require(isinstance(prepared,Prepared),'prepared_vector_required')
        require(isinstance(prepared.job,Job) and self._job(asdict(prepared.job))==prepared.job,'invalid_vector_job')
        embedding=prepared.embedding
        if embedding is not None:
            require(prepared.job.action=='upsert' and isinstance(embedding,Embedding)
                and vector(embedding.values,self.contract)==embedding,'prepared_embedding_invalid')
        value=self._one('apply',(self.lib,self.contract.key,prepared.job.id,
            json.dumps(asdict(prepared.job),ensure_ascii=False,separators=(',',':')),
            None if embedding is None else embedding.literal,None if embedding is None else embedding.pg_digest))
        require(value in ('upsert','remove','stale_missing','stale_revision','stale_content','stale_action',
            'stale_source','already_claimed_or_processed'),'invalid_history_vector_apply_result')
        return value

    def enqueue_current_page(self,index_kind,after_id,limit,now):
        self._write()
        require(index_kind=='context' and _integer(after_id) and _integer(limit,1,64),'invalid_backfill_page')
        value=self._one('enqueue_page',(self.lib,self.contract.key,after_id,limit))
        _shape(value,(f.name for f in fields(BackfillPage)))
        require(value['index_kind']=='context' and _integer(value['after_id'],after_id)
            and _integer(value['scanned'],0,limit) and _integer(value['enqueued'],0,limit)
            and type(value['complete']) is bool,'invalid_backfill_page')
        return BackfillPage(**value)

    def candidates(self,embedding,candidate_limit,*,content_types=(),now=None,min_confidence=0,include_reference=True,experience=None):
        self._read();self.check_model()
        require(isinstance(embedding,Embedding) and vector(embedding.values,self.contract)==embedding,'prepared_embedding_invalid')
        require(_integer(candidate_limit,1,240) and isinstance(content_types,tuple)
            and all(isinstance(t,str) for t in content_types) and _moment(now)
            and _finite(min_confidence) and 0<=min_confidence<=1 and type(include_reference) is bool,'invalid_vector_candidate_batch')
        from evolvmem.pg_business_core.native_store import experience_candidate_filter
        _,params=experience_candidate_filter(experience)
        experience_project=experience['project'] if experience is not None else None
        require(experience_project is None or len(experience_project)<=256,'invalid_experience_candidates')
        rows=self.session.execute(SQL['candidates'],(self.lib,self.project,self.contract.key,embedding.literal,
            candidate_limit,list(content_types),now,min_confidence,include_reference,
            experience_project,params[1] if experience is not None else None))
        require(len(rows)<=candidate_limit,'recall_candidate_limit')
        result=[];seen=set()
        for ident,revision,digest,distance in rows:
            require(_integer(ident,1) and ident not in seen and _integer(revision,1)
                and _hash(digest) and _finite(distance),'invalid_vector_candidate')
            seen.add(ident);result.append(Hit(self.lib,'context',ident,revision,digest,float(distance),self.project))
        require(not result or self.context['ann']['enabled'] and self.context['ann']['coverage']=='complete','invalid_history_vector_ann_state')
        return tuple(result)

    def context_projection_proof(self,hits,embedding):
        self._read()
        require(isinstance(hits,tuple) and all(isinstance(h,Hit) and h.index_kind=='context'
            and (h.library_id,h.project_id)==(self.lib,self.project) for h in hits),'invalid_vector_candidate_batch')
        ids=self._ids(tuple(h.entity_id for h in hits))
        require(isinstance(embedding,Embedding) and vector(embedding.values,self.contract)==embedding,'prepared_embedding_invalid')
        proof=self._one('projection_proof',(self.lib,self.project,self.contract.key,ids,embedding.literal))
        _shape(proof,('context','query_vector_hash','rows'))
        self.context=self._context(proof['context'])
        require(proof['query_vector_hash']==embedding.pg_digest and isinstance(proof['rows'],list)
            and len(proof['rows'])==len(ids),'recall_vector_changed')
        expected={h.entity_id:h for h in hits}
        for ident,row in zip(ids,proof['rows']):
            _shape(row,('entity_id','revision','content_hash','dimension','vector_hash','distance'),'recall_vector_changed')
            hit=expected[ident]
            require(_integer(row['entity_id'],1) and row['entity_id']==ident
                and _integer(row['revision'],1) and row['revision']==hit.revision
                and _hash(row['content_hash']) and row['content_hash']==hit.content_hash
                and type(row['dimension']) is int and row['dimension']==self.contract.dimension
                and _hash(row['vector_hash']) and _finite(row['distance']) and row['distance']==hit.distance,'recall_vector_changed')
        return deepcopy(proof)

    def delta_page(self,epoch_id,after_ordinal=-1,after_change_ordinal=-1,limit=64):
        self._write()
        require(_uuid(epoch_id) and _integer(after_ordinal,-1) and _integer(after_change_ordinal,-1)
            and _integer(limit,1,64),'invalid_history_delta_page')
        value=self._one('delta_page',(self.lib,self.contract.key,epoch_id,after_ordinal,after_change_ordinal,limit))
        _shape(value,('epoch_id','after_ordinal','after_change_ordinal','fanout_after_id','scanned','enqueued','complete'))
        require(value['epoch_id']==epoch_id and _integer(value['after_ordinal'],-1)
            and _integer(value['after_change_ordinal'],-1) and (value['fanout_after_id'] is None or _integer(value['fanout_after_id']))
            and _integer(value['scanned'],0,limit) and _integer(value['enqueued'],0,limit)
            and type(value['complete']) is bool,'invalid_history_delta_page')
        return deepcopy(value)

    def finish(self):
        self._write()
        value=self._one('finish',(self.lib,self.contract.key))
        _shape(value,('context',))
        return dict(context=self._context(value['context']))
