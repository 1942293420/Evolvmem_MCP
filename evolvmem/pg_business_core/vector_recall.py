"""Internal ordinary-context vector leg, with generated-candidate version proof.

One authorized library, existing ANN budget, canonical context rank/DTO. No FTS,
HTTP registration, cross-library context merge, history or experience support.
Both content snapshots and publication remain inside one real control lease.
"""
from copy import deepcopy

from evolvmem.context_models import ContextContentType, ContextSearchRequest
from evolvmem.library_foundation import FoundationError
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS
from evolvmem.pg_vector_port.model import ModelContract, require
from evolvmem.pg_vector_port.runtime import VectorPort
from evolvmem.pg_vector_port.search import candidates, revalidate, validate_options
from .recall import CandidateBatch, _ReadRetriever
from .native_store import PgNativeRecallStore


class PgVectorContextRecall:
    def __init__(self, port, config, *, clock=None):
        require(isinstance(port,VectorPort),'actual_vector_port_required')
        self.port,self.config,self.clock=port,config,clock

    def search(self,library_id,request,*,candidate_limit=240,publish=None):
        if (not isinstance(request,ContextSearchRequest) or not request.content_types
                or any(t not in (ContextContentType.REFERENCE,ContextContentType.FACT)
                       for t in request.content_types)):
            raise FoundationError('unsupported_recall_content_type')
        if request.as_of is not None:
            raise FoundationError('unsupported_vector_history')
        # CandidateBatch keeps the existing typed request and bounded rank API;
        # callers cannot provide IDs, distances or proof-bearing objects here.
        CandidateBatch(library_id,request,(),()).checked()
        content_types=tuple(t.value for t in request.content_types)
        validate_options('context',request.top_k,candidate_limit,'active',content_types)
        config=deepcopy(self.config)
        if config.context_reranker_model_dir:
            raise FoundationError('unsupported_recall_reranking')
        def check():
            if config!=self.config:raise FoundationError('recall_config_changed')
            require(ModelContract.from_config(config,input_tokens=EMBEDDING_INPUT_TOKENS,
                    model_revision=self.port.contract.model_revision)==self.port.contract,
                    'embedding_contract_changed')
            self.port.contract.check_provider(self.port.provider,input_tokens=EMBEDDING_INPUT_TOKENS)
            self.port.guard.check()

        with self.port.guard.enforce():
            self.port.guard.begin_authority()
            check()
            embedding=self.port._embedding(request.query,query=True)
            routes=tuple(r for r in self.port._fresh_routes() if r.library.library_id==library_id)
            if len(routes)!=1:raise FoundationError('recall_library_mismatch')

            def read(session,hits):
                check()
                repository=self.port.repository(session)
                # Strict schema/model activation is still required even if an
                # existing port was constructed before a schema change.
                repository.check_schema()
                store=PgNativeRecallStore(session,repository.scope)
                retriever=_ReadRetriever(config,store,None,None,clock=self.clock)
                current=revalidate(repository,hits,status_filter='active',content_types=content_types,
                    now=retriever._moment(request))
                current,qualification=repository.qualification(current)
                store.prepare(tuple(hit.entity_id for hit in current))
                current=tuple(hit for hit in current if store.eligible(hit.entity_id))
                projection=repository.context_projection_proof(current,embedding)
                try:
                    results=retriever.rank_candidates(request,(),tuple(
                        {'id':h.entity_id,'distance':h.distance} for h in current),allow_rerank=False)
                    body=store.proof()
                except (KeyError,ValueError,TypeError,OverflowError):
                    raise FoundationError('unsupported_context_shape') from None
                check()
                return current,results,body,projection,qualification

            def initial(session):
                repository=self.port.repository(session)
                hits=candidates(repository,embedding,'context',candidate_limit)
                # ANN generation and body/projection proof use this exact
                # authorized RR session, with no intervening snapshot boundary.
                return read(session,hits)

            def final(session,initial):
                current=read(session,initial[0])
                selected=lambda result:tuple((r.id,r.duplicate_ids) for r in result)
                if (current[0]!=initial[0] or current[2:]!=initial[2:]
                        or selected(current[1])!=selected(initial[1])):
                    raise FoundationError('recall_selected_changed')
                return initial[1]  # continuous recency is not a new DTO version

            def checked_publish(result):
                check()
                if publish is not None:publish(result)

            return self.port.manager.run_many(routes,initial,finalize=final,
                combine=lambda gathered:gathered[0][1],publish=checked_publish)
