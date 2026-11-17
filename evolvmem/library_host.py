"""Opt-in composition of PostgreSQL project-scoped business tools and ACL administration.

No environment/secret-file loader, schema bootstrap, model construction or
production registration lives here. Connectors and role-bound references are
trusted application configuration, never fields accepted from an RPC client.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import sys
import threading
import time

from evolvmem.library_project_acl.coordinator import ProjectAclCoordinator, ProjectAclIntent, project_uuid
from evolvmem.library_project_acl.worker import AclProjector, ProjectionTarget
from evolvmem.library_catalog import AclIntent
from evolvmem.config import Config
from evolvmem.library_catalog import ControlBinding, ControlProvider, ControlCatalog, ControlAuthorityLease
from evolvmem.library_catalog.adapter import RecallPinRefusal
from evolvmem.library_foundation.routing import RecallTransientWitness, RecallWitness
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import (Authenticator, AuthorityRouter, ConnectionBinding,
    ConnectionRegistry, FoundationError, RequestIdentity)
from evolvmem.library_foundation.sessions import SessionManager, PsycopgDirectProvider
from evolvmem.library_foundation.parallel_read import ParallelReadBudget, ParallelReadCoordinator


TOOLS = frozenset(('memory_add', 'memory_replace', 'memory_remove', 'context_read'))
RECALL_TOOLS = frozenset(('memory_search','context_search','experience_recall'))
ADMIN_TOOLS = frozenset(('library_acl_update','library_project_acl_update'))
PROTOCOLS = ('2025-11-25', '2025-03-26')
_FIELDS = {
    'memory_add': frozenset(('key', 'value', 'attribute', 'tags', 'importance', 'tier', 'expires_at')),
    'memory_replace': frozenset(('key', 'value')),
    'memory_remove': frozenset(('id',)),
    'context_read': frozenset(('id', 'layer')),
}


@dataclass
class _Request:
    guard: SyncRequestDeadlineGuard
    catalog: ControlCatalog
    router: AuthorityRouter
    manager: SessionManager
    identity: RequestIdentity | None = None
    published: bool = False
    served: bool = False
    acl_coordinator: object = None
    acl_projector: object = None
    coordinator: object = None


class _RecallTransitionBudget:
    """One per-call observation budget shared by initial updating and pin retry."""
    __slots__ = ('started', 'boundary', 'remaining', 'slot')

    def __init__(self, started, boundary):
        self.started = started
        self.boundary = boundary
        self.remaining = 4
        self.slot = 0

    @classmethod
    def start(cls, guard, window=2.0):
        now = time.monotonic()
        return cls(now, min(guard.absolute_deadline(), now + window))

    def wait_for_observation(self, guard):
        # Absolute slots belong to this shared budget, not to one observation
        # loop. A later pin refusal cannot reset the schedule or count. Leave
        # the final quarter-window for the last fresh plan and retry check.
        target = self.started + (self.boundary - self.started) * self.slot / 4
        while self.observation_open():
            guard.check()
            delay = target - time.monotonic()
            if delay <= 0:
                return True
            time.sleep(min(0.05, delay))
        guard.check()
        return False

    def observation_open(self):
        return self.remaining > 0 and time.monotonic() < self.boundary

    def retry_open(self):
        return time.monotonic() < self.boundary


def _rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class LibraryHost:
    """A host has no connections; each admitted HTTP request owns its resources.

    close() prevents new admissions/publications. An already-started publication
    cannot be withdrawn; active synchronous requests unwind under their existing
    deadline. No caller is promised hard OS cancellation or a remote rollback.
    """
    def __init__(self, *, control_binding, content_bindings, control_connector,
                 content_connector, secret_resolver, config, model_contract,
                 semantic_merge_enabled=True, enabled=False, max_requests=2,
                 timeout_seconds=30, acl_control_binding=None, acl_projection_targets=(),
                 recall_enabled=False, embedding_provider=None,
                 control_request_reuse=False, agent_registry=None, actor_resolver=None):
        if (type(enabled) is not bool or type(semantic_merge_enabled) is not bool
                or type(control_request_reuse) is not bool
                or type(max_requests) is not int or not 1 <= max_requests <= 32
                or not isinstance(control_binding, ControlBinding)
                or not isinstance(config, Config)
                or not isinstance(content_bindings, tuple) or not content_bindings
                or any(not isinstance(x, ConnectionBinding) for x in content_bindings)
                or any(not callable(x) for x in (control_connector, content_connector, secret_resolver))
                or not isinstance(model_contract, str) or not 1 <= len(model_contract) <= 512):
            raise ValueError('trusted library host configuration required')
        if (type(recall_enabled) is not bool or (embedding_provider is not None and
                (not recall_enabled or not callable(getattr(embedding_provider,'encode_query',None))
                 or not callable(getattr(embedding_provider,'close',None))
                 or embedding_provider.contract.key != model_contract))):
            raise ValueError('trusted recall configuration required')
        self._recall_enabled = recall_enabled
        self._embedding_provider = embedding_provider
        self._read_budget = ParallelReadBudget(max(16,min(64,max_requests*3)))
        self._projection_worker = None
        self._resources_closed = False
        self._closing_resources = False
        if (not isinstance(acl_projection_targets,tuple)
                or any(not isinstance(t,ProjectionTarget) for t in acl_projection_targets)
                or (acl_control_binding is None) != (not acl_projection_targets)
                or (acl_control_binding is not None and not isinstance(acl_control_binding,ControlBinding))):
            raise ValueError('trusted ACL coordinator/target configuration required')
        if len({t.library_id for t in acl_projection_targets})!=len(acl_projection_targets):
            raise ValueError('one registered ACL projection route per library required')
        self._acl_binding=acl_control_binding
        self._acl_targets=acl_projection_targets
        self._acl_enabled=acl_control_binding is not None
        # Validates the real deadline contract without starting a watchdog/I/O.
        SyncRequestDeadlineGuard(timeout_seconds)
        self._registry = ConnectionRegistry(content_bindings)
        self._control_binding = control_binding
        self._control_connector, self._content_connector = control_connector, content_connector
        self._secret_resolver = secret_resolver
        self._config = deepcopy(config)
        self._model_contract, self._semantic_merge = model_contract, semantic_merge_enabled
        self._enabled, self._maximum, self._timeout = enabled, max_requests, timeout_seconds
        # Narrow, default-off per-request common-reader raw reuse.  Only the
        # request-scope control provider below ever receives this opt-in.
        self._control_request_reuse = control_request_reuse
        self._condition = threading.Condition()
        self._active = 0
        self._closed = self._unhealthy = False
        self._request = ContextVar('evolvmem_library_host_request', default=None)
        self._agent_registry = agent_registry
        self._actor_resolver = actor_resolver
        self._attribution_lock = threading.RLock()
        self._attributions = {}
        self._latest_device_attribution = {}

    def _ensure_attribution_state(self):
        lock = getattr(self, '_attribution_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._attribution_lock = lock
            self._attributions = {}
            self._latest_device_attribution = {}
        return lock

    def last_attribution(self, device_id, *, session_id=None, principal_id=None):
        dev_key = str(device_id)
        with self._ensure_attribution_state():
            if session_id is not None:
                sess_key = str(session_id)
                if principal_id is not None:
                    return self._attributions.get((str(principal_id), dev_key, sess_key))
                for (p_key, d_key, s_key), attr in self._attributions.items():
                    if d_key == dev_key and s_key == sess_key:
                        return attr
                return None
            return self._latest_device_attribution.get(dev_key)

    @property
    def active_requests(self):
        with self._condition:
            return self._active

    def _check_open(self):
        with self._condition:
            if self._closed:
                raise FoundationError('library_host_closed')
            if self._unhealthy:
                raise FoundationError('library_host_cleanup_incomplete')
            if not self._enabled:
                raise FoundationError('library_host_disabled')

    @contextmanager
    def request_scope(self, connection=None, *, timeout_seconds=None):
        """One admission covers authentication, body reading, SQL and output.

        Admission is nonblocking; no unbounded waiter queue or second request
        deadline is introduced. The real guard bounds each admitted request to
        at most four live connections, including its control lease.
        """
        if self._request.get() is not None:
            raise FoundationError('library_request_already_active')
        with self._condition:
            self._check_open()
            if self._active >= self._maximum:
                raise FoundationError('library_host_busy')
            self._active += 1
        if timeout_seconds is not None and (type(timeout_seconds) not in (int,float)
                or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=self._timeout):
            with self._condition:
                self._active-=1
                self._condition.notify_all()
            raise ValueError('bounded trusted request timeout required')
        guard = SyncRequestDeadlineGuard(self._timeout if timeout_seconds is None else timeout_seconds)
        token = None
        provider = None
        try:
            with guard.enforce():
                if connection is not None:
                    guard.bind_socket(connection)
                provider = ControlProvider(self._control_binding, self._control_connector,
                    self._secret_resolver, deadline_guard=guard,
                    control_request_reuse=self._control_request_reuse)
                catalog = ControlCatalog(provider)
                router = AuthorityRouter(catalog, implemented_tools={'postgres':
                    TOOLS | (RECALL_TOOLS if self._recall_enabled else frozenset())})
                authority = ControlAuthorityLease(catalog, deadline_guard=guard)
                content = PsycopgDirectProvider(self._content_connector, self._secret_resolver,
                                               deadline_guard=guard)
                manager = SessionManager(router, self._registry, content, authority)
                request_state=_Request(guard,catalog,router,manager)
                if self._recall_enabled:
                    request_state.coordinator=ParallelReadCoordinator(manager,
                        content_connector=self._content_connector,secret_resolver=self._secret_resolver,
                        budget=self._read_budget,max_parallel=3)
                if self._acl_enabled:
                    # The trusted ACL coordinator drains existing leases before
                    # fencing; all its control phases share this bounded budget.
                    # Ordinary readers retain 2s, lock waits 500ms, and this guard.
                    coordinator_provider=ControlProvider(self._acl_binding,self._control_connector,
                        self._secret_resolver,deadline_guard=guard,statement_timeout_ms=4000)
                    request_state.acl_coordinator=ProjectAclCoordinator(coordinator_provider)
                    request_state.acl_projector=AclProjector(coordinator_provider,self._acl_targets,
                        self._content_connector,self._secret_resolver,deadline_guard=guard)
                token = self._request.set(request_state)
                self._check_open()
                failure = None
                try:
                    yield guard
                finally:
                    # Physical close of the single idle raw stays inside the
                    # existing deadline scope, on every success/failure/cancel
                    # and before guard.enforce() exits.  A cleanup failure
                    # never replaces the request's primary BaseException.
                    failure = sys.exc_info()[0]
                    try:
                        provider.close_reuse()
                    except BaseException:
                        if failure is None:
                            raise
        finally:
            if token is not None:
                self._request.reset(token)
            with self._condition:
                # Never replace a poisoned request guard and silently keep
                # admitting work after unconfirmed local resource retirement.
                if guard._unhealthy.is_set():
                    self._unhealthy = True
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        """Close admission, stop owned projection, then encoder and child reads."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            if self._resources_closed:return
            if self._closing_resources:raise FoundationError('library_host_cleanup_incomplete')
            self._closing_resources=True
        failed=False
        for resource in (self._projection_worker,self._embedding_provider,self._read_budget):
            if resource is None:continue
            try:
                if resource is self._projection_worker:resource.close(timeout=35)
                elif resource is self._read_budget:
                    if not resource.close(timeout_seconds=2):failed=True
                else:resource.close()
            except BaseException:
                failed=True
        with self._condition:
            self._closing_resources=False
            self._resources_closed=not failed
            if failed:self._unhealthy=True
            self._condition.notify_all()
        if failed:
            raise FoundationError('library_host_cleanup_incomplete') from None

    def start_projection(self, *, token_loader, library_ids, interval_seconds=1):
        """Trusted startup only. Every job owns a fresh worker-thread request."""
        from evolvmem.library_foundation.models import _uuid
        from evolvmem.pg_vector_port.worker import ProjectionService,ProjectionDeferred
        from evolvmem.pg_vector_port.runtime import ProjectionResult
        from evolvmem.pg_vector_port.runtime import VectorPort
        if (not self._recall_enabled or self._embedding_provider is None or not callable(token_loader)
                or not isinstance(library_ids,tuple) or not 1<=len(library_ids)<=64
                or len(set(library_ids))!=len(library_ids)):
            raise ValueError('trusted projection configuration required')
        for library_id in library_ids:_uuid(library_id)
        self._check_open()
        with self._condition:
            if self._projection_worker is not None:raise FoundationError('projection_worker_already_started')
        after={library_id:None for library_id in library_ids}
        index=0
        # Bounded productive run only: [library_id,project_key,productive
        # visits]. A hint is retained only after a clean request scope and a
        # validated native result with at least one exact acknowledgement. On
        # every other outcome it is cleared and the cursor advances past the
        # served key; the normal round-robin schedule then resumes.
        hold=[None,None,0]
        def clear_hold():
            hold[:]=[None,None,0]
        def invalidate(library_id,key):
            # Never pin a failed, deferred or non-productive selected key.
            if key is not None:
                after[library_id]=key
            clear_hold()
        def acknowledged(result):
            # Narrowly trust only the immutable result emitted by the private
            # native port. A zero-ACK result (no_pending/already_claimed) and
            # any malformed/foreign value is non-productive.
            if not isinstance(result,ProjectionResult):
                return None
            ids=getattr(result,'acknowledged_job_ids',None)
            job_id=getattr(result,'job_id',None)
            if (type(ids) is not tuple or not 1<=len(ids)<=4
                    or any(type(value) is not int or value<=0 for value in ids)
                    or len(set(ids))!=len(ids)
                    or type(job_id) is not int or job_id<=0
                    or not isinstance(getattr(result,'outcome',None),str)
                    or not result.outcome):
                return None
            return ids
        @contextmanager
        def context():
            nonlocal index
            hinted=hold[0] is not None
            if hinted:
                library_id=hold[0]
                selected_key=hold[1]
            else:
                library_id=library_ids[index % len(library_ids)];index+=1
                selected_key=None
            guard=None
            exposed=False
            native_factory=None
            port=None
            try:
                with self.request_scope(timeout_seconds=min(self._timeout,30)) as guard:
                    token=token_loader()
                    try:identity=self.authenticate(token)
                    finally:token=None
                    state=self._state()
                    if not hinted:
                        route=state.router.plan(identity,'context_read',library_id=library_id)
                        def selection(session):
                            from evolvmem.pg_business_core.history_qualification_sql import historical_mode
                            if historical_mode(session):return True,()
                            # The authorized page stays authoritative: annotate every
                            # row, never filter after its LIMIT. SELECT on the outbox
                            # is the existing runtime grant and still passes RLS.
                            def page(cursor):
                                rows=session.execute(
                                    'SELECT p.project_id::text,p.project_key,'
                                    'COALESCE(w.has_pending,false) '
                                    'FROM em_security.list_recall_projects(%s,%s,%s) p '
                                    'LEFT JOIN LATERAL (SELECT true AS has_pending '
                                    'FROM em_content.vector_outbox o WHERE o.library_id=%s '
                                    'AND o.project_id=p.project_id AND o.model_contract=%s '
                                    'AND o.status=\'pending\' LIMIT 1) w ON true '
                                    'ORDER BY p.project_key COLLATE "C"',
                                    (library_id,'write',cursor,
                                     library_id,self._embedding_provider.contract.key))
                                # Validate the complete page before trusting a witness.
                                if (len(rows)>128 or any(len(row)!=3 or not isinstance(row[1],str)
                                        or str(project_uuid(row[1]))!=str(row[0])
                                        or type(row[2]) is not bool for row in rows)
                                        or any(rows[i][1]>=rows[i+1][1] for i in range(len(rows)-1))):
                                    raise FoundationError('projection_project_listing_invalid')
                                return rows
                            rows=page(after[library_id])
                            if not rows and after[library_id] is not None:
                                # An exhausted tail may wrap to the head once, inside
                                # this same read transaction. A null-start page never
                                # queries twice and nothing scans recursively.
                                after[library_id]=None
                                rows=page(None)
                            if rows and not any(row[2] for row in rows):
                                # The old port construction probed schema/model even
                                # for a project with no work. Reuse this same read
                                # selection session and transaction: no new connect,
                                # lease, request or write. The listing already proved
                                # the witness write-authorized.
                                from evolvmem.library_project_acl.scope import resolve_project_scope
                                from evolvmem.pg_vector_port.repository import VectorRepository
                                VectorRepository(session,self._embedding_provider.contract,
                                    resolve_project_scope(session,rows[0][1])).check_schema()
                            return False,rows
                        historical,rows=state.manager.run(route,selection)
                        if historical:
                            # Historical mode keeps its original single-visit
                            # rotation: no hint, cursor or listing is involved.
                            clear_hold()
                            # Existing router authority is still mandatory. The
                            # SQL index gate independently proves actual owner/admin.
                            if route.library.writer_backend!='postgres':
                                raise FoundationError('history_projection_routing_incompatible')
                            state.guard.begin_authority()
                            read_route=state.router.plan(identity,'context_read',library_id=library_id)
                            write_route=state.router.plan(identity,'memory_add',library_id=library_id)
                            port=VectorPort(state.manager,(read_route,),self._embedding_provider.contract,
                                project_key=None,embedding_provider=self._embedding_provider)
                            exposed=True
                            yield (port,write_route)
                            return
                        if not rows:
                            after[library_id]=None
                            clear_hold()
                            exposed=True
                            yield None
                            return
                        pending=next(((position,row) for position,row in enumerate(rows) if row[2]),None)
                        if pending is None:
                            # A page without pending work advances to its last key;
                            # no row is skipped or reinterpreted.
                            after[library_id]=rows[-1][1]
                            clear_hold()
                            exposed=True
                            yield None
                            return
                        position,pending=pending
                        selected_key=pending[1]
                    # The metadata read sealed the preceding authority segment.
                    # Rearm the same deadline before routing opens another
                    # connection. Hinted visits skip only the discovery read
                    # route and manager.run(selection) above.
                    state.guard.begin_authority()
                    read_route=state.router.plan(identity,'context_read',library_id=library_id)
                    write_route=state.router.plan(identity,'memory_add',library_id=library_id)
                    # Native work uses the private projection-only construction:
                    # no eager constructor activation session; project_one
                    # merges actual schema/model authorization into its
                    # read-only preparation. Alternate VectorPort classes that
                    # do not expose the private factory keep the ordinary eager
                    # constructor (never a weakening of checks).
                    native_factory=getattr(VectorPort,'_native_projection_factory',None)
                    port=(native_factory(state.manager,(read_route,),
                            self._embedding_provider.contract,project_key=selected_key,
                            embedding_provider=self._embedding_provider)
                          if native_factory is not None else
                          VectorPort(state.manager,(read_route,),
                            self._embedding_provider.contract,project_key=selected_key,
                            embedding_provider=self._embedding_provider))
                    exposed=True
                    yield (port,write_route)
                # Reached only after the outer request scope exited normally.
                # A poisoned cleanup invalidates the hint and remains fatal;
                # it is never silently accepted as a completed success.
                if guard is None or guard._unhealthy.is_set() or self._unhealthy:
                    invalidate(library_id,selected_key)
                    raise FoundationError('library_host_cleanup_incomplete')
                result=(getattr(port,'_projection_result',None)
                        if (native_factory is not None and port is not None
                            and getattr(port,'_projection_only',False) is True)
                        else None)
                if acknowledged(result) is None:
                    invalidate(library_id,selected_key)
                    return
                if not hinted:
                    hold[:]=[library_id,selected_key,1]
                else:
                    visits=hold[2]+1
                    if visits>=4:
                        # Fourth consecutive productive visit: advance after
                        # its key and rotate; the normal schedule resumes.
                        after[library_id]=selected_key
                        clear_hold()
                    else:
                        hold[2]=visits
            except ProjectionDeferred:
                # The port may prove no apply effect, but its outer request
                # cleanup must also be confirmed before the service can idle.
                invalidate(library_id,selected_key)
                if guard is None or guard._unhealthy.is_set() or self._unhealthy:
                    raise FoundationError('library_host_cleanup_incomplete') from None
                raise
            except FoundationError as exc:
                # A failed or deferred project is never pinned: the next fresh
                # request advances past its key and follows the normal schedule.
                invalidate(library_id,selected_key)
                # Only setup BEFORE exposing a port can be retried by making a
                # completely new authenticated request next interval. SQL,
                # timeouts, unknown commits and errors sent back through yield
                # never enter this branch's narrow deferral set.
                if (not exposed and guard is not None and not guard._unhealthy.is_set()
                        and not self._unhealthy and exc.code in {
                            'policy_epoch_changed','authority_changed','authority_lease_busy',
                            'library_unavailable','library_access_denied','project_scope_unavailable'}):
                    raise ProjectionDeferred(exc.code) from None
                raise
            except BaseException:
                # Any otherwise unclassified failure (unknown runtime error,
                # cancellation, cleanup exception) only drops the hint and is
                # re-raised unchanged. It is never deferred or retried.
                invalidate(library_id,selected_key)
                raise
        worker=ProjectionService(context,enabled=True,interval_seconds=interval_seconds,
            max_runtime_seconds=300,max_jobs_per_pass=1,max_jobs=256,idle_probe_budget=8)
        with self._condition:
            self._check_open()
            if self._projection_worker is not None:raise FoundationError('projection_worker_already_started')
            self._projection_worker=worker
            worker.start()

    @staticmethod
    def _recall_arguments(tool,args):
        from evolvmem.context_models import ContextSearchRequest,ContextContentType
        from evolvmem.library_foundation.models import _uuid
        common={'query','top_k','content_types','as_of','project','library_id','library_ids','project_key','selections'}
        if (tool not in RECALL_TOOLS or not isinstance(args,dict)
                or set(args)-(common | ({'constraints'} if tool=='experience_recall' else set()))):
            raise FoundationError('routing_override_forbidden')
        try:
            forms=sum(key in args for key in ('library_id','library_ids','selections'))
            if forms!=1:raise ValueError()
            if 'selections' in args:
                if 'project_key' in args:raise ValueError()
                raw=args['selections']
                if (not isinstance(raw,list) or not 1<=len(raw)<=4
                        or any(not isinstance(row,dict) or set(row)!={'library_id','project_key'} for row in raw)):
                    raise ValueError()
                selections=tuple((row['library_id'],row['project_key']) for row in raw)
            else:
                if 'project_key' not in args:raise ValueError()
                ids=[args['library_id']] if 'library_id' in args else args['library_ids']
                if not isinstance(ids,list) or not 1<=len(ids)<=4:raise ValueError()
                selections=tuple((library_id,args['project_key']) for library_id in ids)
            for library_id,key in selections:
                _uuid(library_id)
                if key is not None:project_uuid(key)
            if len(set(selections))!=len(selections):raise ValueError()
            query=args.get('query')
            if (not isinstance(query,str) or not query.strip()
                    or len(query)>(2000 if tool=='experience_recall' else 4000)):raise ValueError()
            project=args.get('project','')
            if not isinstance(project,str):raise ValueError()
            if tool=='experience_recall':
                if project and project not in {key for _,key in selections}:raise ValueError()
            elif 'project' in args and any(key!=project for _,key in selections):raise ValueError()
            types=args.get('content_types',[])
            if (not isinstance(types,list) or len(types)>12 or any(not isinstance(t,str) for t in types)
                    or len(set(types))!=len(types)):raise ValueError()
            content_types=tuple(ContextContentType(t) for t in types)
            if tool=='experience_recall' and not content_types:
                content_types=(ContextContentType.EXPERIENCE,ContextContentType.PLAYBOOK)
            request=ContextSearchRequest(query=query,project=project,top_k=args.get('top_k',10),
                content_types=content_types,as_of=args.get('as_of'))
            experience=None
            if tool=='experience_recall':
                constraints=args.get('constraints',{})
                if (not isinstance(constraints,dict) or len(constraints)>20
                        or any(not isinstance(k,str) or not k.strip() or len(k)>80
                               or not isinstance(v,str) or not v.strip() or len(v)>250 for k,v in constraints.items())):
                    raise ValueError()
                normalized={k.strip():v.strip() for k,v in constraints.items()}
                if len(normalized)!=len(constraints):raise ValueError()
                experience={'project':project,'constraints':normalized}
            return selections,request,experience
        except (ValueError,TypeError,AttributeError):
            raise FoundationError('invalid_arguments') from None

    def _recall(self,state,tool,arguments,deliver):
        selections,request,experience=self._recall_arguments(tool,arguments)
        library_ids=tuple(dict.fromkeys(library_id for library_id,_ in selections))
        transition=tool in self._TRANSITION_TOOLS
        identity=state.identity
        budget=None
        try:
            options={'recall_transition':True} if transition else {}
            routes=state.router.plan_many(identity,tool,library_ids,**options)
        except RecallTransientWitness as witness:
            if not transition:
                raise
            budget=_RecallTransitionBudget.start(state.guard, self._TRANSITION_WINDOW)
            routes=self._observe_recall_transition(state,tool,library_ids,witness.routes,
                witness.witnesses,budget=budget,require_advance=False,
                public_code='library_unavailable')
        if budget is not None and not budget.retry_open():
            raise FoundationError('library_unavailable')
        from evolvmem.pg_business_core.native_search import PgNativeSearch
        contract=embedding=None
        if self._embedding_provider is not None and request.as_of is None:
            from evolvmem.pg_vector_port.model import vector,VectorError
            try:
                state.guard.check()
                contract=self._embedding_provider.contract
                if contract.key!=self._model_contract:raise FoundationError('embedding_contract_changed')
                values=self._embedding_provider.encode_query(request.query,deadline=state.guard.absolute_deadline())
                state.guard.check()
                if self._embedding_provider.contract!=contract:raise FoundationError('embedding_contract_changed')
                embedding=vector(values,contract)
            except VectorError as exc:
                raise FoundationError(exc.code) from None
        search=PgNativeSearch(state.coordinator,self._config,contract=contract,embedding=embedding)
        if budget is not None and not budget.retry_open():
            raise FoundationError('library_unavailable')
        try:
            return search.search(routes,selections,request,kind=tool,experience=experience,publish=deliver)
        except RecallPinRefusal as refusal:
            if not transition:
                raise
            if budget is None:
                budget=_RecallTransitionBudget.start(state.guard, self._TRANSITION_WINDOW)
            if refusal.failed_library_id is None:
                # An ambiguous recovery identity cannot prove which selected
                # library's policy actually advanced.
                raise FoundationError('authority_changed') from None
            routes=self._observe_recall_transition(state,tool,library_ids,refusal.routes,(),
                budget=budget,require_advance=True,public_code='authority_changed',
                failed_library_id=refusal.failed_library_id)
            if not budget.retry_open():
                raise FoundationError('authority_changed')
            # Reuse only the stateless search wrapper and the already encoded
            # query. No provider call and no prior rows/DTO/proofs are reused.
            if embedding is not None:
                state.guard.check()
                if (self._embedding_provider is None
                        or self._embedding_provider.contract != contract
                        or self._embedding_provider.contract.key != self._model_contract):
                    raise FoundationError('embedding_contract_changed')
            state.guard.check()
            if state.published or state.identity != identity:
                raise FoundationError('authority_changed')
            if not budget.retry_open():
                raise FoundationError('authority_changed')
            try:
                return search.search(routes,selections,request,kind=tool,experience=experience,
                                     publish=deliver)
            except RecallPinRefusal:
                raise FoundationError('authority_changed') from None

    _TRANSITION_TOOLS = frozenset(('context_search', 'experience_recall'))
    _TRANSITION_OBSERVATIONS = 4
    _TRANSITION_WINDOW = 2.0
    _SCOPE_FIELDS = ('library_id', 'kind', 'backend', 'connection_ref', 'writer_backend',
                     'project_id', 'device_id', 'owner_principal_id')

    @classmethod
    def _recall_scope(cls, route):
        return (route.tool, route.action, route.identity,
                tuple(getattr(route.library, name) for name in cls._SCOPE_FIELDS))

    @classmethod
    def _witness_scope(cls, witness):
        return (witness.tool, 'read', witness.identity,
                tuple(getattr(witness.library, name) for name in cls._SCOPE_FIELDS))

    @classmethod
    def _scope_compatible(cls, baseline, current):
        # Same route/library/backend/trusted binding identity only. Epochs are
        # checked separately; no route or backend switch is ever accepted.
        return baseline == current

    def _observe_recall_transition(self, state, tool, library_ids, baseline_routes,
                                   baseline_witnesses, *, budget, require_advance,
                                   public_code, failed_library_id=None):
        """Bounded fresh full-control observations; no content or publication."""
        identity = state.identity
        if tool not in self._TRANSITION_TOOLS or state.published:
            raise FoundationError(public_code)
        baseline = {}
        for route in baseline_routes:
            if route.identity != identity or route.tool != tool:
                raise FoundationError(public_code)
            baseline[route.library.library_id] = (self._recall_scope(route),
                                                  route.library.policy_epoch,
                                                  route.library.route_epoch)
        for witness in baseline_witnesses:
            if witness.identity != identity or witness.tool != tool:
                raise FoundationError(public_code)
            baseline.setdefault(witness.library_id, (self._witness_scope(witness),
                                                     witness.library.policy_epoch,
                                                     witness.library.route_epoch))
        if set(baseline) != set(library_ids):
            # No proof for every selected library means no recovery identity.
            raise FoundationError(public_code)
        if require_advance and failed_library_id not in baseline:
            raise FoundationError(public_code)
        last_policy = {library_id: value[1] for library_id, value in baseline.items()}
        while budget.remaining > 0 and budget.observation_open():
            if not budget.wait_for_observation(state.guard):
                break
            budget.remaining -= 1
            budget.slot += 1
            state.guard.check()
            if state.published or state.identity is not identity:
                raise FoundationError(public_code)
            try:
                routes = state.router.plan_many(identity, tool, tuple(library_ids),
                                                recall_transition=True)
            except RecallTransientWitness as observed:
                if observed.tool != tool or set(observed.library_ids) != set(library_ids):
                    raise FoundationError(public_code)
                seen = set()
                for witness in observed.witnesses:
                    library_id = witness.library_id
                    previous = baseline.get(library_id)
                    if (previous is None or witness.identity != identity
                            or witness.tool != tool
                            or not self._scope_compatible(previous[0], self._witness_scope(witness))
                            or witness.library.route_epoch != previous[2]
                            or witness.library.policy_epoch < last_policy[library_id]):
                        raise FoundationError(public_code)
                    last_policy[library_id] = witness.library.policy_epoch
                    seen.add(library_id)
                for route in observed.routes:
                    library_id = route.library.library_id
                    previous = baseline.get(library_id)
                    if (previous is None or route.identity != identity
                            or route.tool != tool
                            or not self._scope_compatible(previous[0], self._recall_scope(route))
                            or route.library.route_epoch != previous[2]
                            or route.library.policy_epoch < last_policy[library_id]):
                        raise FoundationError(public_code)
                    last_policy[library_id] = route.library.policy_epoch
                    seen.add(library_id)
                if seen != set(library_ids):
                    raise FoundationError(public_code)
                continue
            if (not isinstance(routes, tuple) or len(routes) != len(library_ids)
                    or {route.library.library_id for route in routes} != set(library_ids)):
                raise FoundationError(public_code)
            advance = False
            for route in routes:
                library_id = route.library.library_id
                previous = baseline.get(library_id)
                if (previous is None or route.identity != identity
                        or route.tool != tool
                        or not self._scope_compatible(previous[0], self._recall_scope(route))
                        or route.library.route_epoch != previous[2]
                        or route.library.policy_epoch < last_policy[library_id]):
                    raise FoundationError(public_code)
                if (require_advance and library_id == failed_library_id
                        and route.library.policy_epoch > previous[1]):
                    advance = True
                last_policy[library_id] = route.library.policy_epoch
            if require_advance and not advance:
                raise FoundationError(public_code)
            state.guard.check()
            if state.published or state.identity is not identity:
                raise FoundationError(public_code)
            if not budget.retry_open():
                raise FoundationError(public_code)
            return routes
        state.guard.check()
        raise FoundationError(public_code)

    def _state(self):
        state = self._request.get()
        if state is None:
            raise FoundationError('library_request_scope_required')
        state.guard.check()  # includes owner-thread affinity
        return state

    def authenticate(self, token):
        self._check_open()
        state = self._state()
        if state.identity is not None or state.served:
            raise FoundationError('library_identity_already_bound')
        identity = Authenticator(state.catalog).authenticate(token)
        self._check_open()
        state.guard.check()
        state.identity = identity
        return identity

    def _publish(self, state, payload, publish):
        state.guard.check()
        # Linearize publication admission with close, then release the mutex
        # before network I/O; close never waits on a slow peer holding it.
        with self._condition:
            self._check_open()
            if state.published:
                raise FoundationError('library_response_already_published')
            state.published = True
        with state.guard.operation(kind='output'):
            publish(payload)

    @staticmethod
    def _arguments(tool, args):
        if not isinstance(args, dict):
            raise FoundationError('invalid_arguments')
        if not (tool=='context_read' and 'project_key' in args and args['project_key'] is None):
            project_uuid(args.get('project_key'))  # SQL resolves authorization
        routing = {'library_id','project_key'} | ({'request_id'} if tool != 'context_read' else set())
        if set(args) - _FIELDS[tool] - routing:
            raise FoundationError('routing_override_forbidden')
        if tool != 'context_read' and (not isinstance(args.get('request_id'), str)
                or not 1 <= len(args['request_id']) <= 200):
            raise FoundationError('invalid_request_id')
        if tool in ('memory_add', 'memory_replace'):
            if any(not isinstance(args.get(x), str) or not args[x] for x in ('key', 'value')):
                raise FoundationError('invalid_arguments')
        elif type(args.get('id')) is not int or args['id'] <= 0:
            raise FoundationError('invalid_arguments')
        if ('tags' in args and (not isinstance(args['tags'], list)
                or any(not isinstance(x, str) for x in args['tags']))):
            raise FoundationError('invalid_arguments')
        if 'importance' in args and (type(args['importance']) not in (int, float)
                or not math.isfinite(args['importance'])):
            raise FoundationError('invalid_arguments')
        if any(k in args and not isinstance(args[k], str) for k in ('attribute', 'tier', 'expires_at', 'layer')):
            raise FoundationError('invalid_arguments')
        return {k: v for k, v in args.items() if k not in routing}

    @staticmethod
    def _specs(*, include_acl=False, include_recall=False):
        # Copy the actual standalone argument/annotation definitions; explicit
        # library selection and stable mutation IDs are this transport's extras.
        from evolvmem.context_models import ContextMode
        from evolvmem.mcp_contract import tool_specs, CODEX_ADAPTER
        result = []
        for spec in tool_specs(adapter=CODEX_ADAPTER, mode=ContextMode.PRIMARY, health=None, catalog=True):
            if spec.name not in TOOLS:
                continue
            schema = deepcopy(spec.input_schema)
            schema['additionalProperties'] = False
            schema['properties']['library_id'] = {'type': 'string', 'format': 'uuid'}
            schema['properties']['project_key'] = {'type':['string','null'] if spec.name=='context_read' else 'string','minLength':1,'maxLength':256}
            schema['required'] = list(schema.get('required', ())) + ['library_id','project_key']
            if spec.name != 'context_read':
                schema['properties']['request_id'] = {'type': 'string', 'minLength': 1, 'maxLength': 200}
                schema['required'].append('request_id')
            result.append({'name': spec.name, 'description': spec.description,
                           'inputSchema': schema, 'annotations': dict(spec.annotations)})
        if include_recall:
            from evolvmem.context_models import ContextContentType
            selection=dict(type='object',additionalProperties=False,
                properties=dict(library_id=dict(type='string',format='uuid'),
                    project_key=dict(type=['string','null'],minLength=1,maxLength=256)),
                required=['library_id','project_key'])
            for name in sorted(RECALL_TOOLS):
                props=dict(query=dict(type='string',minLength=1,maxLength=2000 if name=='experience_recall' else 4000),
                    top_k=dict(type='integer',minimum=1,maximum=20),
                    content_types=dict(type='array',maxItems=12,uniqueItems=True,
                        items=dict(type='string',enum=[t.value for t in ContextContentType])),
                    project=dict(type='string',maxLength=256),as_of=dict(type=['string','null']),
                    library_id=dict(type='string',format='uuid'),
                    library_ids=dict(type='array',minItems=1,maxItems=4,uniqueItems=True,items=dict(type='string',format='uuid')),
                    project_key=dict(type=['string','null'],minLength=1,maxLength=256),
                    selections=dict(type='array',minItems=1,maxItems=4,uniqueItems=True,items=selection))
                if name=='experience_recall':props['constraints']=dict(type='object',maxProperties=20,
                    propertyNames=dict(minLength=1,maxLength=80),additionalProperties=dict(type='string',minLength=1,maxLength=250))
                schema=dict(type='object',additionalProperties=False,properties=props,required=['query'],oneOf=[
                    dict(required=['library_id','project_key'],**{'not':{'anyOf':[{'required':['library_ids']},{'required':['selections']}]}}),
                    dict(required=['library_ids','project_key'],**{'not':{'anyOf':[{'required':['library_id']},{'required':['selections']}]}}),
                    dict(required=['selections'],**{'not':{'anyOf':[{'required':['library_id']},{'required':['library_ids']},{'required':['project_key']}]}})])
                result.append(dict(name=name,description='Recall authorized library/project selections; results retain qualified references.',
                    inputSchema=schema,annotations=dict(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)))
        if include_acl:
            for name in sorted(ADMIN_TOOLS):
                properties=dict(library_id={'type':'string','format':'uuid'},target_principal_id={'type':'string','format':'uuid'},
                    permission={'type':['string','null'],'enum':['read','write','admin',None]},
                    expected_policy_epoch={'type':'integer','minimum':1},operation_id={'type':'string','format':'uuid'},
                    request_id={'type':'string','minLength':1,'maxLength':128})
                if name=='library_project_acl_update':
                    properties['project_key']={'type':'string','minLength':1,'maxLength':256}
                result.append(dict(name=name,description='Update an authorized library/project ACL; null permission revokes. Stable request_id and operation_id required.',
                    inputSchema=dict(type='object',additionalProperties=False,properties=properties,required=list(properties)),
                    annotations=dict(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=False)))
        return result

    def _admin(self,state,tool,args):
        if not self._acl_enabled or state.acl_coordinator is None or state.acl_projector is None:
            raise FoundationError('library_acl_administration_disabled')
        fields={'library_id','target_principal_id','permission','expected_policy_epoch','operation_id','request_id'}
        if tool=='library_project_acl_update': fields.add('project_key')
        if not isinstance(args,dict) or set(args)!=fields:
            raise FoundationError('invalid_arguments')
        if args['library_id'] not in {t.library_id for t in self._acl_targets}:
            raise FoundationError('unregistered_projection_target')
        values=(state.identity,args['library_id'],args['target_principal_id'],args['permission'],
                args['expected_policy_epoch'],args['operation_id'],args['request_id'])
        intent=(ProjectAclIntent(*values,project_uuid(args['project_key']),args['project_key'])
                if tool=='library_project_acl_update' else AclIntent(*values))
        # A shared business AuthorityLease must NOT surround an ACL mutation:
        # begin takes the conflicting exclusive lock and drains previous leases.
        # The SQL coordinator performs fresh actor/owner/admin checks and fences
        # the library before the content projection. No caller identity is used.
        operation=state.acl_coordinator.apply(intent,state.acl_projector)
        confirmed=state.acl_coordinator.lookup(intent)
        if confirmed!=operation or operation.operation_state!='complete':
            raise FoundationError('acl_completion_unconfirmed')
        return dict(operation_id=operation.operation_id,library_id=operation.library_id,
                    policy_epoch=operation.target_policy_epoch,state=operation.operation_state)

    def serve_rpc(self, identity, request, *, publish, session_id=None, client_hint=None):
        state = self._state()
        self._check_open()
        if identity is not state.identity or not isinstance(identity, RequestIdentity):
            raise FoundationError('unauthorized')
        if state.served or not callable(publish):
            raise FoundationError('library_rpc_already_served')
        state.served = True
        request_id = request.get('id') if isinstance(request, dict) else None
        try:
            if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                    or set(request) - {'jsonrpc', 'id', 'method', 'params'}
                    or not isinstance(request.get('method'), str)
                    or ('id' in request and type(request_id) not in (str, int))):
                raise FoundationError('invalid_request')
            method = request['method']
            params = request.get('params', {})
            if not isinstance(params, dict):
                raise FoundationError('invalid_arguments')
            if method == 'notifications/initialized' and 'id' not in request:
                return self._publish(state, None, publish)
            if 'id' not in request:
                raise FoundationError('request_id_required')
            recall_enabled = bool(getattr(self, '_recall_enabled', False))
            acl_enabled = bool(getattr(self, '_acl_enabled', False))
            actor_resolver = getattr(self, '_actor_resolver', None)
            agent_registry = getattr(self, '_agent_registry', None)
            if method == 'initialize':
                protocol = params.get('protocolVersion')
                if protocol not in PROTOCOLS:
                    raise FoundationError('unsupported_protocol_version')
                from evolvmem.experience_v2.control.agent_identity import resolve_mcp_client_info
                actor_id = actor_resolver(identity.principal_id) if callable(actor_resolver) else identity.principal_id
                c_info = dict(params['clientInfo']) if isinstance(params.get('clientInfo'), dict) else {}
                if isinstance(client_hint, dict):
                    for k in ('name', 'model'):
                        if not c_info.get(k) and client_hint.get(k):
                            c_info[k] = client_hint[k]
                merged_info = c_info or None
                meta_info = params.get('_meta')
                if agent_registry is not None:
                    attr = agent_registry.record_seen(actor=actor_id, client_info=merged_info, device_id=identity.device_id, meta=meta_info)
                else:
                    attr = resolve_mcp_client_info(merged_info, actor=actor_id, device_id=identity.device_id, meta=meta_info)
                sess_key = str(session_id) if session_id else '__default__'
                with self._ensure_attribution_state():
                    if len(self._attributions) >= 4096:
                        self._attributions.pop(next(iter(self._attributions)))
                    self._attributions[(str(identity.principal_id), str(identity.device_id), sess_key)] = attr
                    self._latest_device_attribution[str(identity.device_id)] = attr
                result = {'protocolVersion': protocol, 'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'evolvmem-library-host', 'version': '0.1'},
                    'instructions': 'Select an authorized library_id and registered project_key. '
                    'Mutations require stable request_id. ACL administration is advertised only when '
                    'trusted coordinator configuration is enabled. Historical recall activation remains separate.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                if params:
                    raise FoundationError('invalid_arguments')
                result = {'tools': self._specs(include_acl=acl_enabled,include_recall=recall_enabled)}
            elif method == 'tools/call':
                if set(params) != {'name', 'arguments'} or params.get('name') not in (TOOLS
                        | (RECALL_TOOLS if recall_enabled else frozenset())
                        | (ADMIN_TOOLS if acl_enabled else frozenset())):
                    raise FoundationError('library_tool_unsupported')
                from evolvmem.experience_v2.control.agent_identity import bind_active_attribution, resolve_mcp_client_info
                actor_id = actor_resolver(identity.principal_id) if callable(actor_resolver) else identity.principal_id
                active_attr = self.last_attribution(
                    identity.device_id, session_id=session_id, principal_id=identity.principal_id
                ) if session_id is not None else None
                if active_attr is None and isinstance(client_hint, dict) and (client_hint.get('name') or client_hint.get('model')):
                    active_attr = (
                        agent_registry.record_seen(actor=actor_id, client_info=client_hint, device_id=identity.device_id)
                        if agent_registry is not None
                        else resolve_mcp_client_info(client_hint, actor=actor_id, device_id=identity.device_id)
                    )
                if active_attr is None:
                    active_attr = self.last_attribution(identity.device_id) or resolve_mcp_client_info(
                        None, actor=actor_id, device_id=identity.device_id
                    )
                tool, arguments = params['name'], params['arguments']
                with bind_active_attribution(active_attr):
                    if tool in RECALL_TOOLS:
                        def deliver_recall(dto):
                            self._publish(state,{'jsonrpc':'2.0','id':request_id,'result':{
                                'content':[{'type':'text','text':json.dumps(dto,ensure_ascii=False,allow_nan=False)}],
                                'isError':isinstance(dto,dict) and 'error' in dto}},publish)
                        return self._recall(state,tool,arguments,deliver_recall)
                    if tool in ADMIN_TOOLS:
                        dto=self._admin(state,tool,arguments)
                        return self._publish(state,{'jsonrpc':'2.0','id':request_id,'result':{
                            'content':[{'type':'text','text':json.dumps(dto,ensure_ascii=False)}],'isError':False}},publish)
                    args = self._arguments(tool, arguments)
                    route = state.router.plan(identity, tool, library_id=arguments.get('library_id'),
                                              request_fields=arguments)
                    # All validation/routing above precedes the first content
                    # connection and the legacy-wire import. No SQLite bootstrap.
                    from evolvmem.pg_business_core.executor import PgBusinessExecutor
                    executor = PgBusinessExecutor(state.manager, self._config,
                        model_contract=self._model_contract, semantic_merge_enabled=self._semantic_merge,
                        project_key=arguments['project_key'])
                    def deliver(dto):
                        payload = {'jsonrpc': '2.0', 'id': request_id, 'result': {
                            'content': [{'type': 'text', 'text': json.dumps(dto, ensure_ascii=False, allow_nan=False)}],
                            'isError': isinstance(dto, dict) and 'error' in dto}}
                        self._publish(state, payload, publish)
                    if tool == 'context_read':
                        return executor.read(route, args, publish=deliver, preflight_errors_as_results=True)
                    return executor.mutate(route, args, request_id=arguments['request_id'], publish=deliver,
                                           preflight_errors_as_results=True)
            else:
                raise FoundationError('method_not_supported')
        except FoundationError as exc:
            # A cancelled authorization/SQL segment cannot be revived merely to
            # serialize an error. check() preserves this guard's fail-closed
            # transport semantics and atomic receipts retain unknown writes.
            try:
                state.guard.check()
            except FoundationError:
                # Cancellation during authority cleanup is secondary to an
                # already classified unknown commit; preserve that outcome.
                raise exc from None
            if state.published:
                raise
            return self._publish(state, _rpc_error(request_id, -32000, exc.code), publish)
        return self._publish(state, {'jsonrpc': '2.0', 'id': request_id, 'result': result}, publish)
