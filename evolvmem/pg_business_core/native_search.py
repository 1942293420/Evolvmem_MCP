"""Native hybrid search: qualified snapshots, explicit scope, lease-held output."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timezone

from evolvmem.context_models import ContextContentType,ContextSearchRequest
from evolvmem.context_retriever import ContextRetriever
from evolvmem.experience_ranking import rank_cases
from evolvmem.library_foundation import FoundationError
from evolvmem.library_project_acl.scope import resolve_project_scope
from .native_store import PgNativeRecallStore


def require(ok,code):
    if not ok:raise FoundationError(code)


class NativeRetriever(ContextRetriever):
    def _backing_allows(self,item_id):return self.store.eligible(item_id)
    def _project_results(self,results,request):
        # Native-v1 has no historical knowledge directory. Old history schemas
        # cannot activate its qualification gate, so none are silently omitted.
        return tuple(results)


@dataclass(frozen=True,slots=True)
class SelectionRead:
    project_key:str|None
    lexical:tuple=()
    vectors:tuple=()
    results:tuple=()
    projection:object=None
    proof:str=''
    dto:object=None
    denied:bool=False
    retrieval_support:str='native-hybrid'
    retrieval_reason:str|None=None


def context_dto(result):
    value={name:getattr(result,name) for name in ('id','identity_key','l0','project',
        'confidence','importance','score','rerank_score','effective_from','effective_until','occurred_at','mentioned_at')}
    for name in ('content_type','scope','status','tier'):value[name]=getattr(result,name).value
    for name in ('match_types','match_layers','available_layers'):value[name]=[v.value for v in getattr(result,name)]
    value.update(duplicate_ids=list(result.duplicate_ids),temporal_state=result.temporal_state,knowledge=list(result.knowledge))
    return value


class PgNativeSearch:
    def __init__(self,coordinator,config,*,contract=None,embedding=None,clock=None):
        self.coordinator,self.config=coordinator,deepcopy(config)
        self.contract,self.embedding=contract,embedding
        require((contract is None)==(embedding is None),'invalid_vector_query_binding')
        self.clock=clock or (lambda:datetime.now(timezone.utc).replace(tzinfo=None))

    @staticmethod
    def _scope(session,key):
        try:return resolve_project_scope(session,key)
        except FoundationError as exc:
            # Expected authorization rejection must be returned before closing
            # the parent lease; it never restarts a cancelled deadline guard.
            if exc.code=='project_scope_unavailable':return None
            raise

    def _vectors(self,session,scope,request,prior,*,experience=None,historical=None):
        # The caller already read the DB-owned mode on this exact session; only
        # a direct private call (tests) falls back to reading it here.
        if historical is None:
            from .history_qualification_sql import historical_mode
            historical=historical_mode(session)
        if historical:
            from evolvmem.pg_vector_port.history_repository import HistoryVectorRepository,installed,_ACTUAL
            if not installed(session):return (),None
            repository=HistoryVectorRepository(session,self.contract or _ACTUAL,scope)
            repository.check_schema()
            if self._historical_reason(repository,request) is not None:return (),repository
        else:
            if self.embedding is None or request.as_of is not None or scope.project_id is None:return (),None
            from evolvmem.pg_vector_port.repository import VectorRepository
            repository=VectorRepository(session,self.contract,scope)
            # candidates()/revalidate() below already run the authoritative
            # scope+model check; no separate native check_schema() is needed.
        from evolvmem.pg_vector_port.search import candidates,revalidate
        now=self.clock().strftime('%Y-%m-%d %H:%M:%S')
        if prior is not None:hits=prior.vectors
        else:
            options=dict(content_types=self._candidate_types(request),now=now,
                min_confidence=self.config.context_min_confidence,
                include_reference=ContextContentType.REFERENCE in request.content_types,experience=experience)
            hits=(repository.candidates(self.embedding,120,**options) if historical else
                candidates(repository,self.embedding,'context',120,**options))
        hits=revalidate(repository,hits,status_filter='active',
            content_types=tuple(t.value for t in request.content_types),now=now,allow_superseded=True)
        return hits,repository

    def _historical_reason(self,repository,request):
        if repository is None:return 'ann_absent'
        if request.as_of is not None:return 'ann_as_of_lexical_only'
        if self.embedding is None:return 'ann_provider_unconfigured'
        ann=repository.context['ann']
        return None if (ann['enabled'] and ann['coverage']=='complete' and ann['reason']=='ann_ready'
            and ann['target_count'] is not None and ann['indexed_count']==ann['target_count']) else ann['reason']

    @staticmethod
    def _candidate_types(request):
        return tuple(t.value for t in request.content_types) or tuple(t.value for t in ContextContentType
            if t is not ContextContentType.WORKSTREAM_CHECKPOINT)

    def _read(self,session,key,request,kind,experience,prior=None):
        scope=self._scope(session,key)
        if scope is None:return SelectionRead(key,denied=True)
        if prior is not None and prior.denied:return prior  # never upgrade a denied selection mid-request
        from .history_qualification_sql import historical_mode
        historical=historical_mode(session)
        if historical:
            from .history_store import PgHistoryRecallStore
            from .history_search import HistoryRetriever
            store=PgHistoryRecallStore(session,scope)
        else:
            store=PgNativeRecallStore(session,scope)
        candidate_experience=experience if kind=='experience_recall' else None
        moment=request.as_of or self.clock().strftime('%Y-%m-%d %H:%M:%S')
        lexical=(store.lexical_candidates(request.query,120,
            content_types=self._candidate_types(request),moment=moment,
            min_confidence=self.config.context_min_confidence,
            include_reference=ContextContentType.REFERENCE in request.content_types,
            experience=candidate_experience) if prior is None else prior.lexical)
        hits,repository=self._vectors(session,scope,request,prior,experience=candidate_experience,
            historical=historical)
        ids=tuple(dict.fromkeys([h.item_id for h in lexical]+[h.entity_id for h in hits]))
        store.prepare(ids)
        qualified=tuple(h for h in hits if store.eligible(h.entity_id))
        if repository is not None:
            projection=(repository.context_projection_proof(qualified,self.embedding) if self.embedding is not None else
                dict(context=deepcopy(repository.context)))
        else:projection=dict(installed=False) if historical else None
        reason=self._historical_reason(repository,request) if historical else None
        retriever=(HistoryRetriever if historical else NativeRetriever)(self.config,store,None,None,clock=self.clock)
        results=retriever.rank_candidates(request,lexical,
            tuple({'id':h.entity_id,'distance':h.distance} for h in qualified),allow_rerank=False)
        if kind=='memory_search':
            mapped=store.legacy_projection(tuple(r.id for r in results))
            dto=[]
            for hit in results:
                row=mapped.get(hit.id)
                if row is None:continue
                dto.append(dict(id=row['id'],key=row['key'],value=row['value'],status=row['status'],
                    attribute=row['attribute'],tags=row['tags'],score=hit.score,
                    match_type='+'.join(v.value for v in hit.match_types) or None,
                    created_at=row['created_at'],context_id=hit.id,
                    available_layers=[v.value for v in hit.available_layers]))
        elif kind=='experience_recall':
            cases=store.eligibility.cases(tuple(i for i in ids if store.eligible(i)))
            dto=rank_cases(cases,project=experience['project'],query=request.query,
                constraints=experience['constraints'],semantic={r.id:r for r in results},
                vector_weight=self.config.context_vector_weight,
                reranker_min_score=self.config.context_reranker_min_score,
                max_chars=self.config.context_inject_related_max_chars)
        else:dto=[context_dto(r) for r in results]
        return SelectionRead(key,tuple(lexical),tuple(hits),tuple(results),projection,store.proof(),dto,
            retrieval_support=('historical-lexical-only' if reason is not None else 'historical-hybrid')
                if historical else 'native-hybrid',retrieval_reason=reason)

    def search(self,routes,selections,request,*,kind='context_search',experience=None,publish=None):
        require(isinstance(request,ContextSearchRequest),'invalid_recall_request')
        require(kind in ('context_search','memory_search','experience_recall'),'unsupported_recall_tool')
        require(isinstance(selections,tuple) and 1<=len(selections)<=4,'invalid_library_selection')
        require(len(set(selections))==len(selections),'invalid_library_selection')
        expected={r.library.library_id for r in routes}
        require(expected=={lib for lib,_ in selections} and all(r.action=='read' for r in routes),'recall_library_mismatch')
        if experience is None:experience={}
        config=deepcopy(self.config)
        def stable():require(self.config==config,'recall_config_changed')
        def read(session):
            stable()
            values=[]
            for lib,key in selections:
                if lib!=session.route.library.library_id:continue
                scoped=ContextSearchRequest(query=request.query,project=key or '',top_k=request.top_k,
                    content_types=request.content_types,cross_project=request.cross_project,as_of=request.as_of)
                values.append(self._read(session,key,scoped,kind,experience))
            return tuple(values)
        def final(session,initial):
            stable();values=[]
            for prior in initial:
                scoped=ContextSearchRequest(query=request.query,project=prior.project_key or '',top_k=request.top_k,
                    content_types=request.content_types,cross_project=request.cross_project,as_of=request.as_of)
                current=self._read(session,prior.project_key,scoped,kind,experience,prior)
                if prior.denied or current.denied:
                    values.append(SelectionRead(prior.project_key,denied=True));continue
                selected=lambda result:tuple((r.id,r.duplicate_ids) for r in result)
                require(prior.proof==current.proof and prior.projection==current.projection
                    and prior.retrieval_support==current.retrieval_support and prior.retrieval_reason==current.retrieval_reason
                    and selected(prior.results)==selected(current.results),'recall_selected_changed')
                if kind=='experience_recall':
                    # Cases have their own eligibility/expiry and output budget;
                    # their selected IDs need not equal the generic top-k IDs.
                    require(tuple(r['id'] for r in prior.dto['results'])==
                        tuple(r['id'] for r in current.dto['results']),
                        'recall_selected_changed')
                # Preserve original recency scores but not stale dependencies.
                values.append(prior)
            return tuple(values)
        def combine(gathered):
            stable()
            if any(value.denied for _,values in gathered for value in values):
                return {'error':'project_scope_unavailable'}
            selections_support=[dict(library_id=route.library.library_id,project_key=value.project_key,
                retrieval_support=value.retrieval_support,reason=value.retrieval_reason)
                for route,values in gathered for value in values]
            historical=any(v['retrieval_support'].startswith('historical-') for v in selections_support)
            def support(response):
                if historical:
                    response['retrieval_support']=('historical-lexical-only' if any(
                        v['retrieval_support']=='historical-lexical-only' for v in selections_support) else 'historical-hybrid')
                    response['retrieval_selections']=selections_support
                return response
            rows=[]
            for route,values in gathered:
                for value in values:
                    items=value.dto['results'] if kind=='experience_recall' else value.dto
                    for entry in items:
                        entry=deepcopy(entry)
                        context_id=entry.get('context_id',entry['id'])
                        entry.update(library_id=route.library.library_id,project_key=value.project_key,
                            ref='library:'+route.library.library_id+':context:'+str(context_id))
                        rows.append(entry)
            rows.sort(key=lambda r:(-r['score'],r['library_id'],r['project_key'] or '',r['id']))
            if kind=='experience_recall':
                import json
                selected=[];used=0
                for row in rows:
                    cost=len(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')))
                    if used+cost>self.config.context_inject_related_max_chars:continue
                    selected.append(row);used+=cost
                    if len(selected)==3:break
                response=dict(results=selected,used_chars=used)
                return support(response)
            selected=rows[:request.top_k]
            response=dict(results=selected,count=len(selected))
            if kind=='context_search':response['as_of']=request.as_of
            return support(response)
        return self.coordinator.run_many(tuple(routes),read,finalize=final,combine=combine,publish=publish)
