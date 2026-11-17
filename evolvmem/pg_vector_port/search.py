"""Vector candidates only; no hybrid/FTS, access telemetry or capability port."""
from dataclasses import dataclass
import math
from .model import require,text_hash
from .projection import SEARCHABLE_TYPES


@dataclass(frozen=True,slots=True)
class Hit:
    library_id:str
    index_kind:str
    entity_id:int
    revision:int
    content_hash:str
    distance:float
    project_id:str|None = None

    @property
    def vector_score(self):
        score=1-self.distance/2
        return max(0.0,score) if self.index_kind=='legacy' else score


def validate_options(index_kind,top_k,candidate_limit,status_filter,content_types):
    require(index_kind in ('legacy','context'),'unsupported_vector_index')
    require(type(top_k) is int and type(candidate_limit) is int
            and 1<=top_k<=60 and top_k<=candidate_limit<=240,'invalid_vector_budget')
    require(status_filter in ('active','superseded','archived','deleted'),'unsupported_status_filter')
    require(isinstance(content_types,tuple) and all(kind in SEARCHABLE_TYPES for kind in content_types),
            'unsupported_context_evidence')
    require((index_kind=='context' and status_filter=='active') or (index_kind=='legacy' and not content_types),
            'unsupported_vector_filter')


def merge_hits(groups,top_k):
    values=[hit for group in groups for hit in group]
    return tuple(sorted(values,key=lambda hit:(hit.distance,hit.library_id,hit.index_kind,hit.entity_id))[:top_k])


def candidates(repository,embedding,index_kind,candidate_limit,*,content_types=(),now=None,min_confidence=0,include_reference=True,experience=None):
    repository.check_model()
    session=repository.session
    # Bounded HNSW work; the model/library/kind partitions isolate candidate
    # populations. This does not claim USearch/pgvector ANN candidate parity.
    session.execute("SELECT pg_catalog.set_config('hnsw.ef_search',%s,true),"
        "pg_catalog.set_config('hnsw.iterative_scan','strict_order',true),"
        "pg_catalog.set_config('hnsw.max_scan_tuples','2000',true),"
        "pg_catalog.set_config('hnsw.scan_mem_multiplier','1',true),"
        "pg_catalog.set_config('work_mem','4MB',true)",(str(max(40,candidate_limit)),))
    params=[embedding.literal,repository.lib,repository.project,repository.contract.key,index_kind]
    filters=''
    require(experience is None or index_kind=='context' and now is not None,'invalid_experience_candidates')
    if index_kind=='context' and now is not None:
        # Apply request filters before the ANN limit: unrelated types must not
        # consume the entire bounded pool for a typed request.
        filters=''' AND EXISTS (SELECT 1 FROM em_content.context_items c
          WHERE c.library_id=v.library_id AND c.project_id=v.project_id AND c.id=v.entity_id
           AND c.status IN ('active','superseded')
           AND (c.status='active' OR (c.content_type='decision'
                AND (c.effective_from IS NOT NULL OR c.occurred_at IS NOT NULL)))
           AND (%s::text[]='{}'::text[] OR c.content_type=ANY(%s::text[]))
           AND c.confidence >= %s
           AND (%s OR c.tier <> 'reference')
           AND (c.expires_at IS NULL OR c.expires_at > %s)
           AND (c.effective_from IS NULL OR c.effective_from <= %s)
           AND (c.effective_until IS NULL OR c.effective_until > %s)'''
        params.extend((list(content_types),list(content_types),min_confidence,include_reference,now,now,now))
        if experience is not None:
            from evolvmem.pg_business_core.native_store import experience_candidate_filter
            experience_sql,experience_params=experience_candidate_filter(experience)
            filters+=experience_sql
            params.extend(experience_params)
        filters+=')'
    params.extend((embedding.literal,candidate_limit))
    rows=session.execute('''SELECT entity_id,entity_revision,content_hash,
        embedding <=> %s::public.vector AS distance FROM em_content.vector_projections v
        WHERE library_id=%s AND project_id=%s AND model_contract=%s AND index_kind=%s AND projection_state='ready' '''
        +filters+' ORDER BY embedding <=> %s::public.vector LIMIT %s',tuple(params))
    result=[]
    for ident,revision,digest,distance in rows:
        require(type(ident) is int and type(revision) is int and isinstance(digest,str)
                and isinstance(distance,(int,float)) and math.isfinite(distance),'invalid_vector_candidate')
        result.append(Hit(repository.lib,index_kind,ident,revision,digest,float(distance),repository.project))
    return tuple(result)


def revalidate(repository,hits,*,status_filter,content_types,now,allow_superseded=False):
    require(type(allow_superseded) is bool,'invalid_vector_temporal_option')
    repository.check_model()
    require(all((hit.library_id,hit.project_id)==(repository.lib,repository.project) for hit in hits),'projection_scope_mismatch')
    require(len({hit.index_kind for hit in hits})<=1,'invalid_vector_candidate_batch')
    documents=(repository.documents(hits[0].index_kind,tuple(hit.entity_id for hit in hits)) if hits else {})
    result=[]
    for hit in hits:
        document=documents.get(hit.entity_id)
        if (document is None or document.revision!=hit.revision or text_hash(document.text)!=hit.content_hash
                or (document.status!=status_filter and not(allow_superseded and hit.index_kind=='context'
                    and status_filter=='active' and document.status=='superseded'))
                or (document.expires_at is not None and document.expires_at<=now)):
            continue
        if hit.index_kind=='context':
            require(document.content_type in SEARCHABLE_TYPES,'unsupported_context_type')
            if content_types and document.content_type not in content_types:continue
        result.append(hit)
    return tuple(result)
