"""Unregistered vector entry points over the existing canonical PG adapters."""
from dataclasses import dataclass
from datetime import datetime, timezone

from evolvmem.library_foundation.models import AuthorizedRoute,FoundationError
from evolvmem.library_foundation.sessions import SessionManager
from evolvmem.library_foundation._projection_conflict import (PreEffectRowConflict,
    ProjectionBatchConflict, ProjectionBatchDeferred, ProjectionConflictDeferred,
    is_serialization_failure)
from evolvmem.library_catalog.adapter import ControlAuthorityLease
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS
from .model import ModelContract,Embedding,require,vector,reusable
from evolvmem.library_project_acl.scope import resolve_project_scope
from .projection import Job,Prepared,PreparedPair,PairApplied,decide
from .repository import VectorRepository
from .search import validate_options,candidates,revalidate,merge_hits


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class ProjectionDeferred(Exception):
    """An authority transition rejected work before a mutation callback ran."""
    code='projection_authority_deferred'


class ProjectionSerializationDeferred(ProjectionDeferred):
    """A native locked read hit a confirmed, rolled-back row conflict.

    This is not an authority transition and not an unknown commit: no vector
    write or acknowledgement occurred. The worker still uses the same full
    interval and a completely fresh next factory, without counting the attempt
    as a completed job.
    """
    code='projection_serialization_deferred'


class ProjectionBatchSerializationDeferred(ProjectionDeferred):
    """A whole native batch was rolled back, verified IDLE and closed.

    Distinct from the older single/pair pre-effect exception: the batch may have
    performed uncommitted writes for earlier entities, all of which are gone.
    The worker records zero completed jobs, waits the unchanged interval and
    builds a completely fresh factory/identity/routes/snapshot next attempt.
    """
    code='projection_batch_serialization_deferred'


PROJECTION_AUTHORITY_CHANGES=frozenset(('policy_epoch_changed','library_unavailable',
    'authority_changed','authority_lease_busy','project_scope_unavailable','library_access_denied'))


class BorrowedEmbeddingProvider:
    """Borrow the application's already loaded, serialized engine and live config.

    No initialization, download, ownership transfer or close method is provided.
    Model inference is synchronous; the guard rejects late output but cannot
    preempt an arbitrary native inference call or promise a hard wall clock.
    """
    def __init__(self,engine):
        require(callable(getattr(engine,'encode_query',None))
                and callable(getattr(engine,'encode_document',None)),'embedding_provider_required')
        require(getattr(engine,'config',None) is not None,'embedding_provider_contract_unavailable')
        self._engine=engine

    @property
    def config(self):
        # A second config object cannot attest the loaded engine's model. Read
        # the actual engine dynamically so replacement during encode is caught.
        return getattr(self._engine,'config',None)

    @property
    def is_loaded(self):return self._engine.is_loaded

    def encode_query(self,text):return self._engine.encode_query(text)
    def encode_document(self,text):return self._engine.encode_document(text)


@dataclass(frozen=True,slots=True)
class SearchResult:
    hits:tuple
    candidate_counts:tuple
    candidate_limit_per_library:int
    underfilled:bool
    read_mode:str='sequential'


@dataclass(frozen=True,slots=True)
class ProjectionResult:
    job_id:int|None
    outcome:str
    # Exact rows acknowledged by the committed transaction. An omitted value
    # proves no acknowledgement; it is never inferred from job_id.
    acknowledged_job_ids:tuple = ()

    @property
    def acknowledged_job_count(self):
        return len(self.acknowledged_job_ids)


@dataclass(frozen=True,slots=True)
class ProjectionBatchResult(ProjectionResult):
    """Immutable aggregate of exact rows acknowledged by one committed batch.

    It is a ProjectionResult so the existing host private-feedback narrowing
    keeps recognizing the actual native port result; only the acknowledged
    identifier bound becomes four in the separately supplied host patch.
    """


def _is_history_repository(repository):
    from .history_repository import HistoryVectorRepository
    return isinstance(repository,HistoryVectorRepository)


class VectorPort:
    # Set only by the private native projection subtype. It is not a caller
    # option and never bypasses manager/route/role/project/contract validation.
    _projection_only = False

    def __init__(self,manager,read_routes,contract,*,project_key,embedding_provider=None,cache_lookup=None):
        self._bind(manager,read_routes,contract,project_key=project_key,
                   embedding_provider=embedding_provider,cache_lookup=cache_lookup)
        # Construction is the actual composition/activation gate. No caller can
        # supply a boolean readiness assertion. Every configured library passes
        # real read sessions, catalog leases, RLS and exact schema/model checks.
        manager.run_many(self.routes,lambda s:self.repository(s).check_schema(),combine=tuple)

    def _bind(self,manager,read_routes,contract,*,project_key,embedding_provider,cache_lookup):
        require(isinstance(manager,SessionManager)
                and isinstance(manager.authority,ControlAuthorityLease),'actual_vector_manager_required')
        self.guard=manager.authority.deadline_guard
        require(isinstance(self.guard,SyncRequestDeadlineGuard)
                and manager.provider.deadline_guard is self.guard,'actual_vector_guard_required')
        require(isinstance(contract,ModelContract),'trusted_model_contract_required')
        require(isinstance(read_routes,tuple) and 1<=len(read_routes)<=4
                and all(isinstance(r,AuthorizedRoute) and r.action=='read' for r in read_routes),
                'invalid_read_routes')
        require(len({r.library.library_id for r in read_routes})==len(read_routes)
                and all(r.identity==read_routes[0].identity for r in read_routes),'invalid_read_routes')
        require(cache_lookup is None or callable(cache_lookup),'invalid_vector_cache')
        require(project_key is None or isinstance(project_key,str) and bool(project_key),'vector_project_required')
        self.project_key=project_key
        self.manager,self.contract=manager,contract
        self.routes=tuple(sorted(read_routes,key=lambda r:r.library.library_id))
        self.provider,self.cache_lookup=embedding_provider,cache_lookup
        self._historical_libraries=set()

    @staticmethod
    def _native_projection_factory(manager,read_routes,contract,*,project_key,embedding_provider=None):
        """Private construction used only by the host native projection branch."""
        return _NativeProjectionPort(manager,read_routes,contract,
            project_key=project_key,embedding_provider=embedding_provider)

    def repository(self,session):
        from evolvmem.pg_business_core.history_qualification_sql import historical_mode
        if historical_mode(session):
            from .history_repository import HistoryVectorRepository
            self._historical_libraries.add(session.route.library.library_id)
            scope=resolve_project_scope(session,self.project_key) if session.route.action=='read' else None
            return HistoryVectorRepository(session,self.contract,scope)
        return VectorRepository(session,self.contract,resolve_project_scope(session,self.project_key))

    def _scope(self,route):
        require(isinstance(route,AuthorizedRoute),'invalid_vector_route')
        original=next((r for r in self.routes if r.library.library_id==route.library.library_id),None)
        require(original is not None and route.identity==original.identity
                and route.library.connection_ref==original.library.connection_ref
                and route.library.route_epoch==original.library.route_epoch
                and route.library.backend==original.library.backend
                and route.library.kind==original.library.kind
                and route.library.project_id==original.library.project_id,'vector_activation_scope_changed')
        return route

    def _fresh_routes(self):
        # Planning itself opens control connections. Rearm before that first
        # connect, using the existing guard's no-resource/no-output checks;
        # never create a new request or move its absolute deadline.
        self.guard.begin_authority()
        return tuple(self._scope(self.manager.router.plan(r.identity,r.tool,
            library_id=r.library.library_id,expected_route_epoch=r.library.route_epoch)) for r in self.routes)

    def _embedding(self,text,*,query):
        require(isinstance(text,str) and bool(text),'vector_text_required')
        self.guard.check()
        self.contract.check_provider(self.provider,input_tokens=EMBEDDING_INPUT_TOKENS)
        values=(self.provider.encode_query(text,deadline=self.guard.absolute_deadline()) if query else
                self.provider.encode_document(text,deadline=self.guard.absolute_deadline()))
        self.guard.check()
        self.contract.check_provider(self.provider,input_tokens=EMBEDDING_INPUT_TOKENS)
        return vector(values,self.contract)

    def search(self,query,*,index_kind,top_k,candidate_limit,status_filter='active',content_types=(),publish=None):
        require(not self._historical_libraries,'historical_vector_search_requires_business_recall')
        validate_options(index_kind,top_k,candidate_limit,status_filter,content_types)
        with self.guard.enforce():
            self.guard.begin_authority()  # Reject an active/published scope before model work too.
            embedding=self._embedding(query,query=True)
            return self.search_embedding(embedding,index_kind=index_kind,top_k=top_k,
                candidate_limit=candidate_limit,status_filter=status_filter,content_types=content_types,publish=publish)

    def search_embedding(self,embedding,*,index_kind,top_k,candidate_limit,status_filter='active',content_types=(),publish=None):
        """Trusted precomputed-query entry, not an MCP input or unproved cache."""
        require(not self._historical_libraries,'historical_vector_search_requires_business_recall')
        validate_options(index_kind,top_k,candidate_limit,status_filter,content_types)
        require(isinstance(embedding,Embedding) and vector(embedding.values,self.contract)==embedding,
                'prepared_embedding_invalid')
        with self.guard.enforce():
            routes=self._fresh_routes()
            def checked(session,hits):
                repo=self.repository(session)
                repo.check_schema()
                current=revalidate(repo,hits,status_filter=status_filter,content_types=content_types,now=utc_now())
                eligible,qualification=repo.qualification(current)
                projection=repo.projection_proof(eligible,embedding)
                return eligible,qualification,projection
            def initial(session):
                hits=candidates(self.repository(session),embedding,index_kind,candidate_limit)
                return hits,len(hits),checked(session,hits)
            def final(session,result):
                hits,count,original=result
                current=checked(session,hits)
                require(current==original,'recall_selected_changed')
                return current[0],count
            def combine(rows):
                hits=merge_hits((result[0] for route,result in rows),top_k)
                return SearchResult(hits,tuple((route.library.library_id,result[1]) for route,result in rows),
                    candidate_limit,len(hits)<top_k)
            return self.manager.run_many(routes,initial,finalize=final,combine=combine,publish=publish)

    def project_one(self,write_route,*,completion_budget=1):
        """Prepare one stable job (or one exact pair), then publish+ack atomically.

        An unknown commit propagates; there is no automatic retry or second
        effect. ``completion_budget=1`` keeps the pre-existing single-job path.
        """
        require(type(completion_budget) is int and completion_budget in (1,2),
                'invalid_vector_completion_budget')
        native_only=self._projection_only
        self._scope(write_route)
        require(write_route.action in ('write','admin'),'vector_write_scope_required')
        with self.guard.enforce():
            self.guard.begin_authority()
            def prepare(session):
                repository=self.repository(session)
                if native_only:
                    # This exact host-native profile never falls through to a
                    # historical repository: reject before any history call or
                    # native job/document work. The actual schema/model check
                    # precedes next_job/document/encoding and also covers the
                    # valid empty queue.
                    if _is_history_repository(repository):
                        raise FoundationError('native_projection_history_drift')
                    repository.check_schema()
                job=repository.next_job()
                if job is None:return None,None,None,None,None,None
                document=repository.document(job)
                from .history_repository import HistoryVectorRepository
                native=not isinstance(repository,HistoryVectorRepository)
                if native_only and not native:
                    raise FoundationError('native_projection_history_drift')
                # Historical removals can retain a superseded original row
                # after losing membership. SQL rechecks the immutable job and
                # structural admission; native temporal upserts stay unchanged.
                decision=('remove' if isinstance(repository,HistoryVectorRepository) and job.action=='remove'
                    else decide(job,document,self.contract,utc_now()))
                cached=repository.cache_entry(job) if decision=='upsert' and hasattr(repository,'cache_entry') else None
                pair=None
                # One exact successor of the same entity only: no supersedes
                # traversal, no other entity, and never at budget one.
                if (native and completion_budget==2 and document is not None
                        and document.revision>job.revision and decision=='stale_revision'):
                    current=repository.current_job(job,document)
                    if current is not None:
                        require(current.id>job.id and current.revision>job.revision,
                                'invalid_projection_pair')
                    if current is not None and current.id!=job.id:
                        current_decision=decide(current,document,self.contract,utc_now())
                        if current_decision in ('upsert','remove'):
                            pair=PreparedPair(job,current,current_decision,None)
                return job,document,decision,cached,native,pair
            prepare_operation=(self.manager._projection_prepare(prepare)
                               if native_only else prepare)
            try:
                job,document,decision,cached,native,pair=self.manager.run(write_route,prepare_operation)
            except FoundationError as exc:
                # prepare only reads; no projection or acknowledgement exists.
                if exc.code in PROJECTION_AUTHORITY_CHANGES:raise ProjectionDeferred() from None
                raise
            if job is None:return ProjectionResult(None,'no_pending_for_model',())
            encoding=pair.current if pair is not None else job
            encoding_decision=pair.decision if pair is not None else decision
            embedding=None
            if encoding_decision=='upsert':
                if pair is None and isinstance(cached,tuple) and len(cached)==2:
                    embedding=reusable(document.text,cached[0],cached[1],self.contract)
                if embedding is None and self.cache_lookup is not None:
                    external=self.cache_lookup(encoding.library_id,encoding.index_kind,encoding.entity_id)
                    if isinstance(external,tuple) and len(external)==2:
                        embedding=reusable(document.text,external[0],external[1],self.contract)
                if embedding is None:embedding=self._embedding(document.text,query=False)
            self.guard.check()
            prepared=(PreparedPair(pair.anchor,pair.current,pair.decision,embedding)
                      if pair is not None else Prepared(job,embedding))
            self.guard.begin_authority()
            entered=False
            def apply(session):
                nonlocal entered
                entered=True
                repository=self.repository(session)
                if native_only and _is_history_repository(repository):
                    # A historical repository selected after a native prepare
                    # is always fatal: never history-apply, defer or report zero
                    # success. No claim, vector write or ACK has begun yet.
                    raise FoundationError('native_projection_history_drift')
                if pair is not None:return repository.apply_pair(prepared,utc_now())
                return repository.apply(prepared,utc_now())
            # Only the native repository's locked-row path may opt into the
            # confirmed pre-effect rollback. The historical single-statement
            # apply keeps its prior propagation; no retry or reentry exists.
            opt_in=getattr(self.manager,'_projection_apply',None) if native else None
            apply_operation=opt_in(apply) if opt_in is not None else apply
            try:outcome=self.manager.run(write_route,apply_operation)
            except ProjectionConflictDeferred:
                raise ProjectionSerializationDeferred() from None
            except FoundationError as exc:
                # Never retry a started apply, SQL failure or unknown commit.
                if not entered and exc.code in PROJECTION_AUTHORITY_CHANGES:
                    raise ProjectionDeferred() from None
                raise
            if pair is not None:
                require(isinstance(outcome,PairApplied), 'invalid_projection_pair_result')
                ids=outcome.acknowledged_job_ids
                pair_ids=tuple(sorted((pair.anchor.id,pair.current.id)))
                if ids==():
                    # No claim: the transaction committed no effect and no ACK.
                    require(outcome.outcome=='already_claimed_or_processed',
                            'invalid_projection_pair_result')
                elif ids==(pair.anchor.id,):
                    # Safe fallback: the original anchor alone was acknowledged
                    # stale after the committed transaction (never a forged row).
                    require(isinstance(outcome.outcome,str)
                            and outcome.outcome.startswith('stale_'),
                            'invalid_projection_pair_result')
                else:
                    require(ids==pair_ids and outcome.outcome in ('upsert','remove'),
                            'invalid_projection_pair_result')
                return ProjectionResult(job.id,outcome.outcome,ids)
            acknowledged=((job.id,) if outcome in ('upsert','remove')
                          or isinstance(outcome,str) and outcome.startswith('stale_') else ())
            return ProjectionResult(job.id,outcome,acknowledged)
    def enqueue_current_page(self,write_route,*,index_kind,after_id=0,limit=64):
        self._scope(write_route)
        require(write_route.action in ('write','admin'),'vector_write_scope_required')
        with self.guard.enforce():
            self.guard.begin_authority()
            return self.manager.run(write_route,lambda session:self.repository(session).enqueue_current_page(index_kind,after_id,limit,utc_now()))

    def drain(self,write_route,*,max_jobs=4):
        """Bounded attempts; the acknowledged-row total never exceeds max_jobs."""
        require(type(max_jobs) is int and 1<=max_jobs<=16,'invalid_vector_job_budget')
        outcomes=[];acknowledged=0;attempts=0
        while acknowledged<max_jobs and attempts<max_jobs:
            attempts+=1
            result=self.project_one(write_route,
                completion_budget=min(2,max_jobs-acknowledged));outcomes.append(result)
            acknowledged+=result.acknowledged_job_count
            if result.job_id is None:break
        return tuple(outcomes)

    def business_executor(self,config,*,semantic_merge_enabled):
        """Only available after this object's real all-library activation gate."""
        require(not self._historical_libraries,'historical_vector_search_requires_business_recall')
        require(ModelContract.from_config(config,input_tokens=EMBEDDING_INPUT_TOKENS,model_revision=self.contract.model_revision)==self.contract,
                'embedding_contract_changed')
        from evolvmem.pg_business_core.executor import PgBusinessExecutor
        executor=PgBusinessExecutor(self.manager,config,model_contract=self.contract.key,
            semantic_merge_enabled=semantic_merge_enabled,project_key=self.project_key)
        return _ActivatedBusiness(self,executor,config)


class _NativeProjectionPort(VectorPort):
    """Worker-only native projection object with no public vector surface.

    Construction only binds the actual manager/provider guard, one authorized
    route, the trusted contract and a canonical non-null project key. It does
    not open an activation session: preparation performs the merged read-only
    check under the original write/admin route, and every project_one call
    revalidates from scratch.
    """
    _projection_only = True

    def __init__(self,manager,read_routes,contract,*,project_key,embedding_provider=None):
        self._bind(manager,read_routes,contract,project_key=project_key,
                   embedding_provider=embedding_provider,cache_lookup=None)
        require(len(self.routes)==1,'native_projection_single_library_required')
        require(isinstance(project_key,str) and bool(project_key),
                'native_projection_project_required')
        from evolvmem.library_project_acl.coordinator import project_uuid
        project_uuid(project_key)
        # Private, read-only scheduling feedback for the host. It is cleared
        # before every attempt and holds only an immutable completed result.
        self._projection_feedback=None

    @property
    def _projection_result(self):
        """Return the last successfully completed result; never mutate it."""
        return self._projection_feedback

    def project_one(self,write_route,*,completion_budget=1):
        self._projection_feedback=None
        try:
            result=super().project_one(write_route,completion_budget=completion_budget)
        except BaseException:
            self._projection_feedback=None
            raise
        require(isinstance(result,ProjectionResult),'invalid_projection_result')
        self._projection_feedback=result
        return result

    def project_batch(self,write_route,*,completion_budget=1):
        """Prepare+apply at most ``completion_budget`` acknowledged rows.

        One read-only prepare snapshot and one fresh read-write apply
        transaction. The private/local aggregate is exposed only after commit
        and the complete manager/authority cleanup, and is cleared on every
        exception. It never loops the public ``project_one`` entry point.
        """
        self._projection_feedback=None
        try:
            result=self._batch(write_route,completion_budget)
        except BaseException:
            self._projection_feedback=None
            raise
        require(isinstance(result,ProjectionBatchResult),'invalid_projection_batch_result')
        self._projection_feedback=result
        return result

    @staticmethod
    def _batch_acknowledged(prepared,outcome):
        """Validate one entity result against only its own prepared ids."""
        if isinstance(prepared,PreparedPair):
            require(isinstance(outcome,PairApplied),'invalid_projection_batch_result')
            ids=outcome.acknowledged_job_ids
            pair_ids=tuple(sorted((prepared.anchor.id,prepared.current.id)))
            if ids==():
                require(outcome.outcome=='already_claimed_or_processed',
                        'invalid_projection_batch_result')
            elif ids==(prepared.anchor.id,):
                require(isinstance(outcome.outcome,str)
                        and outcome.outcome.startswith('stale_'),
                        'invalid_projection_batch_result')
            else:
                require(ids==pair_ids and outcome.outcome in ('upsert','remove'),
                        'invalid_projection_batch_result')
            return ids
        if outcome in ('upsert','remove') or (isinstance(outcome,str)
                                              and outcome.startswith('stale_')):
            return (prepared.job.id,)
        return ()

    def _batch(self,write_route,budget):
        require(type(budget) is int and 1<=budget<=4,'invalid_vector_completion_budget')
        self._scope(write_route)
        require(write_route.action in ('write','admin'),'vector_write_scope_required')
        with self.guard.enforce():
            self.guard.begin_authority()
            def prepare(session):
                repository=self.repository(session)
                if _is_history_repository(repository):
                    raise FoundationError('native_projection_history_drift')
                # Actual schema/model check before any candidate/document work,
                # including the valid empty queue.
                repository.check_schema()
                candidates=repository.next_jobs(budget)
                require(isinstance(candidates,tuple) and len(candidates)<=budget,
                        'invalid_vector_candidate_batch')
                planned=[]
                job_ids=set()
                entities=set()
                slots=0
                for job in candidates:
                    if slots>=budget:break
                    entity=(job.index_kind,job.entity_id)
                    if job.id in job_ids or entity in entities:continue
                    document=repository.document(job)
                    decision=('stale_missing' if document is None
                              else decide(job,document,self.contract,utc_now()))
                    pair=None
                    # One exact same-entity successor only, and never when a
                    # pair would exceed the remaining acknowledgement slots.
                    if (document is not None and decision=='stale_revision'
                            and document.revision>job.revision and budget-slots>=2):
                        current=repository.current_job(job,document)
                        if current is not None:
                            require(current.id>job.id and current.revision>job.revision,
                                    'invalid_projection_pair')
                            require(current.id not in job_ids
                                    and (current.index_kind,current.entity_id) not in entities,
                                    'invalid_projection_pair')
                            current_decision=decide(current,document,self.contract,utc_now())
                            if current_decision in ('upsert','remove'):
                                pair=PreparedPair(job,current,current_decision,None)
                    planned.append((job,document,decision,pair))
                    job_ids.add(job.id)
                    entities.add(entity)
                    slots+=1
                    if pair is not None:
                        job_ids.add(pair.current.id)
                        slots+=1
                return tuple(planned)
            prepare_operation=self.manager._projection_prepare(prepare)
            try:
                planned=self.manager.run(write_route,prepare_operation)
            except FoundationError as exc:
                # The prepare snapshot only reads; no projection/ACK exists.
                if exc.code in PROJECTION_AUTHORITY_CHANGES:raise ProjectionDeferred() from None
                raise
            if not planned:
                return ProjectionBatchResult(None,'no_pending_for_model',())
            require(isinstance(planned,tuple),'invalid_projection_batch_plan')
            # Encode strictly outside any DB transaction, checking the current
            # model/provider contract again for every needed document.
            prepared=[]
            planned_ids=set()
            for job,document,decision,pair in planned:
                require(isinstance(job,Job) and decision in ('upsert','remove')
                        or isinstance(decision,str) and decision.startswith('stale_'),
                        'invalid_projection_batch_plan')
                encoding=pair.current if pair is not None else job
                encoding_decision=pair.decision if pair is not None else decision
                planned_ids.add(job.id)
                if pair is not None:planned_ids.add(pair.current.id)
                embedding=None
                if encoding_decision=='upsert':
                    require(document is not None,'invalid_projection_batch_plan')
                    embedding=self._embedding(document.text,query=False)
                prepared.append((PreparedPair(pair.anchor,pair.current,pair.decision,embedding)
                                 if pair is not None else Prepared(job,embedding)))
            self.guard.check()
            planned_ids=frozenset(planned_ids)
            require(len(prepared)==len(planned) and len(prepared)<=4,
                    'invalid_projection_batch_plan')
            self.guard.begin_authority()
            entered=False
            def apply(session):
                nonlocal entered
                entered=True
                repository=self.repository(session)
                if _is_history_repository(repository):
                    raise FoundationError('native_projection_history_drift')
                acknowledged=[]
                for item in prepared:
                    try:
                        outcome=(repository.apply_pair(item,utc_now())
                                 if isinstance(item,PreparedPair)
                                 else repository.apply(item,utc_now()))
                    except PreEffectRowConflict:
                        # Conversion happens ONLY inside this native apply
                        # callback, for the whole as-yet-uncommitted batch.
                        raise ProjectionBatchConflict() from None
                    except FoundationError as exc:
                        if is_serialization_failure(exc):
                            raise ProjectionBatchConflict() from None
                        raise
                    acknowledged.append(self._batch_acknowledged(item,outcome))
                final=tuple(row for chunk in acknowledged for row in chunk)
                require(len(set(final))==len(final)
                        and all(type(value) is int and value>0 for value in final)
                        and set(final)<=planned_ids
                        and len(final)<=budget,'invalid_projection_batch_result')
                return final
            apply_operation=self.manager._projection_batch_apply(apply)
            try:
                acknowledged=self.manager.run(write_route,apply_operation)
            except ProjectionBatchDeferred:
                # Rollback, IDLE verification, close and authority cleanup all
                # succeeded; the whole batch made no externally visible effect.
                raise ProjectionBatchSerializationDeferred() from None
            except FoundationError as exc:
                # Never retry a started batch, SQL failure or unknown commit.
                if not entered and exc.code in PROJECTION_AUTHORITY_CHANGES:
                    raise ProjectionDeferred() from None
                raise
            require(isinstance(acknowledged,tuple)
                    and len(set(acknowledged))==len(acknowledged)
                    and all(type(value) is int and value>0 for value in acknowledged)
                    and set(acknowledged)<=planned_ids
                    and len(acknowledged)<=budget,'invalid_projection_batch_result')
            return ProjectionBatchResult(acknowledged[0] if acknowledged else None,
                'batch_completed' if acknowledged else 'already_claimed_or_processed',acknowledged)

    def repository(self,session):
        from evolvmem.pg_business_core.history_qualification_sql import historical_mode
        if historical_mode(session):
            raise FoundationError('native_projection_history_drift')
        return VectorRepository(session,self.contract,
                                resolve_project_scope(session,self.project_key))

    @staticmethod
    def _forbidden(*args,**kwargs):
        raise FoundationError('native_projection_only_forbidden')

    search = _forbidden
    search_embedding = _forbidden
    enqueue_current_page = _forbidden
    drain = _forbidden
    business_executor = _forbidden


class _ActivatedBusiness:
    def __init__(self,port,executor,config):self.port,self.executor,self.config=port,executor,config
    def _check(self,route):
        self.port._scope(route)
        require(ModelContract.from_config(self.config,input_tokens=EMBEDDING_INPUT_TOKENS,model_revision=self.port.contract.model_revision)==self.port.contract,
                'embedding_contract_changed')
    def mutate(self,route,args,*,request_id,publish=None):
        self._check(route)
        with self.port.guard.enforce():
            return self.executor.mutate(route,args,request_id=request_id,publish=publish)
    def read(self,route,args,*,publish=None):
        self._check(route)
        with self.port.guard.enforce():return self.executor.read(route,args,publish=publish)
