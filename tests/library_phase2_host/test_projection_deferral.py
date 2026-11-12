"""Only the new pre-effect ACL-deferral boundary; no DB, model or service."""
from pathlib import Path
from types import ModuleType,SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import unittest

path=Path(__file__).with_name('test_host_startup.py')
spec=importlib.util.spec_from_file_location('phase2_host_check_support',path)
support=importlib.util.module_from_spec(spec);sys.modules[spec.name]=support;spec.loader.exec_module(support)
from evolvmem.library_foundation import FoundationError,RequestIdentity
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.library_project_acl import scope as scope_module
from evolvmem.pg_vector_port import repository as repository_module
import evolvmem.library_host as module
from evolvmem.pg_vector_port.runtime import ProjectionResult


class ProjectionDeferralCheck(unittest.TestCase):
    def test_only_clean_pre_effect_authority_changes_are_deferred(self):
        allowed=('policy_epoch_changed','authority_changed','authority_lease_busy',
                 'library_unavailable','library_access_denied','project_scope_unavailable')
        cases=[('before',code) for code in allowed]+[
            ('before','commit_result_unknown'),('before','database_operation_failed'),
            ('poison','policy_epoch_changed'),('after','policy_epoch_changed'),
            ('after_deferred','authority_changed'),('after_deferred_poison','authority_changed')]
        for mode,code in cases:
            with self.subTest(mode=mode,code=code):
                provider=SimpleNamespace(contract=SimpleNamespace(key='trusted-model'),
                    encode_query=lambda *a,**k:None,close=lambda:None)
                host=support.host(recall_enabled=True,embedding_provider=provider)
                wire=ModuleType('evolvmem.pg_vector_port.worker')
                runtime=ModuleType('evolvmem.pg_vector_port.runtime')
                class ProjectionDeferred(RuntimeError):pass
                class Service:
                    def __init__(self,factory,**kw):self.factory=factory
                    def start(self):pass
                    def close(self,**kw):pass
                class Router:
                    def __init__(self,*a,**k):pass
                    def plan(self,identity,tool,library_id):
                        if mode in ('before','poison'):
                            if mode=='poison':host._state().guard._unhealthy.set()
                            raise FoundationError(code)
                        return (library_id,tool)
                class Manager:
                    def __init__(self,*a,**k):pass
                    def run(self,route,op):
                        def execute(sql,params=()):
                            if sql=="SELECT to_regclass('em_security.history_readiness') IS NOT NULL":
                                assert params==()
                                return ((False,),)
                            assert sql.startswith('SELECT p.project_id::text,p.project_key,COALESCE(w.has_pending,false) FROM em_security.list_recall_projects(')
                            assert 'LEFT JOIN LATERAL (SELECT true AS has_pending' in sql
                            assert "o.status='pending' LIMIT 1) w ON true" in sql
                            assert params==(support.uid(1),'write',None,support.uid(1),'trusted-model')
                            return ((project_uuid('alpha'),'alpha',True),)
                        return op(SimpleNamespace(execute=execute))
                wire.ProjectionDeferred=ProjectionDeferred;wire.ProjectionService=Service
                runtime.ProjectionResult=ProjectionResult
                runtime.VectorPort=lambda *a,**k:object()
                def auth(token):
                    identity=RequestIdentity(support.uid(2),support.uid(3),support.uid(4),1)
                    host._state().identity=identity
                    return identity
                host.authenticate=auth
                with patch.dict(sys.modules,{wire.__name__:wire,runtime.__name__:runtime}),\
                        patch.object(module,'AuthorityRouter',Router),patch.object(module,'SessionManager',Manager),\
                        patch.object(module,'ParallelReadCoordinator',lambda *a,**k:None):
                    host.start_projection(token_loader=lambda:'synthetic',library_ids=(support.uid(1),))
                    with self.assertRaises(Exception) as raised:
                        with host._projection_worker.factory():
                            if mode=='after_deferred_poison':host._state().guard._unhealthy.set()
                            if mode.startswith('after_deferred'):raise ProjectionDeferred(code)
                            if mode=='after':raise FoundationError(code)
                            self.fail('pre-effect error unexpectedly yielded')
                    if (mode=='before' and code in allowed) or mode=='after_deferred':
                        self.assertIsInstance(raised.exception,ProjectionDeferred)
                    else:
                        self.assertIsInstance(raised.exception,FoundationError)
                        if mode=='after_deferred_poison':self.assertEqual(raised.exception.code,'library_host_cleanup_incomplete')
                    self.assertIsNone(host._request.get());self.assertEqual(host.active_requests,0)
                host.close()

    def test_host_passes_the_trusted_idle_probe_budget(self):
        provider=SimpleNamespace(contract=SimpleNamespace(key='trusted-model'),
            encode_query=lambda *a,**k:None,close=lambda:None)
        host=support.host(recall_enabled=True,embedding_provider=provider)
        wire=ModuleType('evolvmem.pg_vector_port.worker')
        runtime=ModuleType('evolvmem.pg_vector_port.runtime')
        captured={}
        class ProjectionDeferred(RuntimeError):pass
        class Service:
            def __init__(self,factory,**kw):captured.update(kw);self.factory=factory
            def start(self):pass
            def close(self,**kw):pass
        wire.ProjectionDeferred=ProjectionDeferred;wire.ProjectionService=Service
        runtime.ProjectionResult=ProjectionResult
        runtime.VectorPort=lambda *a,**k:object()
        with patch.dict(sys.modules,{wire.__name__:wire,runtime.__name__:runtime}):
            host.start_projection(token_loader=lambda:'synthetic',library_ids=(support.uid(1),))
            self.assertEqual(8,captured['idle_probe_budget'])
            self.assertTrue(captured['enabled'])
            self.assertEqual(1,captured['max_jobs_per_pass'])
        host.close()

    def test_real_host_factory_rotates_projects_and_marks_fresh_closed_requests(self):
        # Exercise the real host factory and the real worker epoch. Only the
        # SQL/model boundary is faked: the listing honours the after cursor and
        # the authorized port yields jobs for A.shared only.
        from evolvmem.pg_vector_port import worker as worker_module
        from evolvmem.pg_vector_port import runtime as runtime_module
        from evolvmem.pg_vector_port.runtime import ProjectionResult
        A, B = support.uid(11), support.uid(12)
        projects = {A: [('hidden', project_uuid('hidden')),
                        ('shared', project_uuid('shared'))],
                    B: [('shared', project_uuid('shared'))]}
        selections = []
        auths = []
        job_probes = []
        clock = [0.0]
        provider = SimpleNamespace(contract=SimpleNamespace(key='trusted-model'),
            encode_query=lambda *a, **k: None, close=lambda: None)
        host = support.host(recall_enabled=True, embedding_provider=provider)

        class Stop:
            def __init__(self): self.stopped = False; self.waits = []
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, seconds):
                assert host._request.get() is None
                assert host.active_requests == 0
                self.waits.append(seconds)
                clock[0] += seconds
                if len(self.waits) >= 15: self.set()
                return self.stopped
        stop = Stop()

        class Router:
            def __init__(self, *a, **k): pass
            def plan(self, identity, tool, library_id): return (library_id, tool)

        class Manager:
            def __init__(self, *a, **k): pass
            def run(self, route, operation):
                def execute(sql, params=()):
                    if sql == "SELECT to_regclass('em_security.history_readiness') IS NOT NULL":
                        assert params == ()
                        return ((False,),)
                    assert sql.startswith('SELECT p.project_id::text,p.project_key,COALESCE(w.has_pending,false) FROM em_security.list_recall_projects(')
                    library, action, after, library_again, model = params
                    assert model == 'trusted-model' and action == 'write' and library_again == library
                    rows = [(pid, key, (library, key) == (A, 'shared'))
                            for key, pid in projects[library]
                            if after is None or key > after][:1]
                    selections.append((library, rows[0][1] if rows else None))
                    return tuple(rows)
                return operation(SimpleNamespace(execute=execute))

        class Port:
            def __init__(self, manager, routes, contract, **kw):
                self.library = routes[0][0]
                self.key = kw['project_key']
                self.guard = SimpleNamespace(timeout_seconds=30)
            def project_one(self, write_route, *, completion_budget=1):
                assert 1 <= completion_budget <= 2
                assert write_route[0] == self.library
                if (self.library, self.key) == (A, 'shared'):
                    job_probes.append(len(selections))
                    return ProjectionResult(len(job_probes), 'upsert', (len(job_probes),))
                return ProjectionResult(None, 'no_pending_for_model', ())

        identity = RequestIdentity(support.uid(21), support.uid(22), support.uid(23), 1)
        def auth(token):
            state = host._state()
            auths.append((state, host.active_requests))
            state.identity = identity
            return identity
        host.authenticate = auth

        with patch.object(module, 'AuthorityRouter', Router),                 patch.object(module, 'SessionManager', Manager),                 patch.object(module, 'ParallelReadCoordinator', lambda *a, **k: None),                 patch.object(scope_module, 'resolve_project_scope', lambda session, key: (session, key)),                 patch.object(repository_module, 'VectorRepository', lambda session, contract, scope: SimpleNamespace(check_schema=lambda: True)),                 patch.object(worker_module.ProjectionService, 'start', lambda self: None),                 patch.object(worker_module, 'time', SimpleNamespace(monotonic=lambda: clock[0])),                 patch.object(worker_module, 'VectorPort', Port),                 patch.object(runtime_module, 'VectorPort', Port):
            host.start_projection(token_loader=lambda: 'synthetic', library_ids=(A, B))
            worker = host._projection_worker
            self.assertEqual(worker.idle_probe_budget, 8)
            worker._stop = stop
            worker._epoch()

        # The ordinary fallback has no private feedback contract. Every
        # request rediscovers; empty tails wrap once and libraries alternate.
        self.assertEqual(selections[:15], [
            (A, 'hidden'), (B, 'shared'), (A, 'shared'), (B, None),
            (B, 'shared'), (A, None), (A, 'hidden'), (B, None),
            (B, 'shared'), (A, 'shared'), (B, None), (B, 'shared'),
            (A, None), (A, 'hidden'), (B, None)])
        self.assertEqual(job_probes, [3, 10, 17, 24])
        self.assertEqual(worker.status()['completed_jobs'], 4)
        self.assertEqual(stop.waits, [.01] * 15)
        self.assertEqual(len(auths), 15)
        self.assertEqual(len({id(state) for state, _ in auths}), 15)
        self.assertTrue(all(active == 1 for _, active in auths))
        self.assertEqual(host.active_requests, 0)
        self.assertIsNone(host._request.get())
        host.close()


if __name__=='__main__':unittest.main()
