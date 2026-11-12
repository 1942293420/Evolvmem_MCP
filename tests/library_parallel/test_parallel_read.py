"""Small synthetic concurrency checks; no network, PostgreSQL or historical data."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import UUID
import os
import sys
import threading
import time
import unittest

HERE = Path(__file__).resolve()
ROOT = Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT', str(HERE.parents[3] / 'baseline')))
OVERLAY = Path(os.environ.get('EVOLVMEM_TEST_OVERLAY', str(HERE.parents[2])))
paths = [OVERLAY / 'evolvmem', ROOT / 'evolvmem']
for name, sub in (('evolvmem',''), ('evolvmem.library_foundation','library_foundation'),
                  ('evolvmem.library_deadline','library_deadline')):
    if sub == 'library_foundation':
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, ROOT/'evolvmem'/sub/'__init__.py',
            submodule_search_locations=[str(p/sub) for p in paths])
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    else:
        module=ModuleType(name);module.__path__=[str(p/sub) for p in paths];sys.modules[name]=module

from evolvmem.library_foundation import (AuthorizedRoute, LibraryDescriptor, RequestIdentity, FoundationError)
from evolvmem.library_foundation.sessions import (SessionManager, DatabaseRole, DatabaseCredential, ContentSessionState)
from evolvmem.library_catalog.adapter import ControlAuthorityLease
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation.parallel_read import ParallelReadCoordinator, ParallelReadBudget

def uid(n): return str(UUID(int=n))

class Harness:
    def __init__(self, timeout=3, count=3, capacity=3):
        self.guard=SyncRequestDeadlineGuard(timeout)
        self.identity=RequestIdentity(uid(1),uid(2),uid(3),1)
        self.routes=tuple(AuthorizedRoute(self.identity, LibraryDescriptor(uid(10+i),'device','postgres',
            'c'+str(i),i+1,1,'pg_active','ready','postgres',device_id=uid(3),owner_principal_id=uid(1),active=True),
            'memory_search','read') for i in range(count))
        self.parent=threading.get_ident();self.lease=False;self.revoked=False;self.connections=[];self.outputs=[]
        self.checks=[];self.budget=ParallelReadBudget(capacity)
        catalog=SimpleNamespace(provider=SimpleNamespace(deadline_guard=self.guard))
        authority=ControlAuthorityLease(catalog,deadline_guard=self.guard)
        @contextmanager
        def pin(routes):
            assert threading.get_ident()==self.parent and not self.lease
            self.lease=True
            try: yield
            finally: self.lease=False
        authority._hold_routes=pin
        def validate(route):
            assert threading.get_ident()==self.parent and self.lease
            self.guard.check();self.checks.append(route.library.library_id)
            if self.revoked: raise FoundationError('policy_epoch_changed')
        def binding(route):return SimpleNamespace(role='runtime',route=route)
        self.manager=SessionManager(SimpleNamespace(revalidate=validate),SimpleNamespace(resolve=binding),
            SimpleNamespace(deadline_guard=self.guard),authority)
        self.coordinator=ParallelReadCoordinator(self.manager,content_connector=lambda **k:None,
            secret_resolver=lambda ref:'synthetic',budget=self.budget)
        harness=self
        class Provider:
            def __init__(self,*args,deadline_guard,**kwargs):self.guard=self.deadline_guard=deadline_guard
            def open(self,binding):
                assert threading.get_ident()!=harness.parent and harness.lease
                guard=self.guard;route=binding.route
                class Connection:
                    def __init__(self):
                        self.thread=threading.get_ident();self.guard=guard;self.closed=False;self.read_only=None
                        self.deadline=guard.absolute_deadline();harness.connections.append(self)
                    def check(self):assert threading.get_ident()==self.thread;guard.check()
                    def begin(self,*,read_only):self.check();self.read_only=read_only;assert read_only
                    def inspect_role(self):self.check();return DatabaseRole('runtime','runtime',False,False,False,True)
                    def session_context(self):self.check();return ContentSessionState(harness.identity.principal_id,
                        route.library.library_id,route.library.policy_epoch,1,'read','pg_active','ready')
                    def session_credential(self):self.check();return DatabaseCredential(uid(2),uid(3),1)
                    def execute(self,sql,params=()):self.check();return ()
                    def rollback(self):
                        assert threading.get_ident()==self.thread
                        try:guard.check()
                        except FoundationError:raise FoundationError('rollback_result_unknown') from None
                    def commit(self):raise AssertionError('read must end with rollback')
                    def close(self):
                        assert threading.get_ident()==self.thread;self.closed=True
                        if getattr(harness,'close_error',False):raise RuntimeError('synthetic close failure')
                return Connection()
        self.patch=patch('evolvmem.library_foundation.parallel_read.PsycopgDirectProvider',Provider)
    def publish(self,value):
        assert self.lease and threading.get_ident()==self.parent
        self.outputs.append(value)
    def run(self,operation,finalize=None):
        with self.patch,self.guard.enforce():
            self.deadline=self.guard.absolute_deadline()
            return self.coordinator.run_many(self.routes,operation,finalize=finalize,
                combine=lambda results:tuple(value for route,value in results),publish=self.publish)

class ParallelReadTests(unittest.TestCase):
    def test_real_overlap_separate_guards_fresh_finalize_and_parent_publication(self):
        h=Harness(count=4);barrier=threading.Barrier(3);active=0;peak=0;lock=threading.Lock();seen=[]
        def read(session):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active);seen.append(session.route.library.library_id);number=len(seen)
            if number<=3:barrier.wait(1)
            with lock:active-=1
            return session.route.library.library_id
        def final(session,candidate):self.assertEqual(candidate,session.route.library.library_id);return candidate+'-verified'
        result=h.run(read,final)
        self.assertEqual(peak,3);self.assertEqual(len(result),4);self.assertEqual(h.outputs,[result])
        self.assertEqual(len(h.connections),8);self.assertTrue(all(c.closed and c.read_only for c in h.connections))
        self.assertEqual({c.deadline for c in h.connections},{h.deadline})
        self.assertEqual(len({id(c.guard) for c in h.connections}),8)
        self.assertFalse(h.lease);self.assertTrue(h.budget.close())

    def test_final_authority_change_and_branch_error_never_publish(self):
        for failure in ('revocation','sql'):
            h=Harness(count=2)
            def read(session):
                if failure=='sql':raise FoundationError('database_operation_failed')
                return 'candidate'
            def final(session,result):h.revoked=True;return result
            with self.assertRaises(FoundationError) as caught:h.run(read,final)
            self.assertEqual(caught.exception.code,'policy_epoch_changed' if failure=='revocation' else 'database_operation_failed')
            self.assertEqual(h.outputs,[]);self.assertFalse(h.lease);self.assertTrue(all(c.closed for c in h.connections))
            self.assertTrue(h.budget.close())

    def test_deadline_cancels_all_child_work_without_output(self):
        h=Harness(timeout=.06,count=2);started=threading.Barrier(2)
        def read(session):
            started.wait(1)
            while True:session.execute('SELECT bounded');time.sleep(.002)
        with self.assertRaises(FoundationError):h.run(read)
        self.assertEqual(h.outputs,[]);self.assertTrue(all(c.closed for c in h.connections))
        # A cancelled read has no commit effect; confirmed client close means
        # ordinary request timeout must not permanently disable the whole host.
        reservation=h.budget.reserve(1);reservation.release();self.assertTrue(h.budget.close())

    def test_global_capacity_fails_before_open_and_invalid_route_does_not_pin(self):
        h=Harness(count=3,capacity=2)
        with self.assertRaises(FoundationError) as caught:h.run(lambda s:1)
        self.assertEqual(caught.exception.code,'parallel_read_busy');self.assertEqual(h.connections,[])
        h=Harness(count=2);h.routes=(h.routes[0],replace(h.routes[1],identity=replace(h.identity,device_id=uid(99))))
        with self.assertRaises(FoundationError) as caught:h.run(lambda s:1)
        self.assertEqual(caught.exception.code,'invalid_read_routes');self.assertEqual(h.connections,[]);self.assertEqual(h.checks,[])

    def test_stuck_callback_quarantines_budget_and_never_publishes(self):
        h=Harness(timeout=.03,count=1);release=threading.Event();done=threading.Event()
        def read(session):
            try:release.wait(3);session.execute('SELECT after_cancel')
            finally:done.set()
        try:
            with self.assertRaises(FoundationError):h.run(read)
            self.assertEqual(h.outputs,[])
            with self.assertRaises(FoundationError):h.budget.reserve(1)
        finally:release.set();self.assertTrue(done.wait(1));h.budget.close(timeout_seconds=1)

    def test_context_is_not_inherited_and_close_failure_cannot_publish(self):
        parent_context=ContextVar('synthetic_control_context',default=None)
        token=parent_context.set('parent-secret-control-context')
        try:
            h=Harness(count=1)
            self.assertEqual(h.run(lambda session:parent_context.get()),(None,))
        finally:parent_context.reset(token)
        h=Harness(count=1);h.close_error=True
        with self.assertRaises(FoundationError) as caught:h.run(lambda session:'candidate')
        self.assertEqual(caught.exception.code,'parallel_read_cleanup_incomplete');self.assertEqual(h.outputs,[])
        with self.assertRaises(FoundationError):h.budget.reserve(1)

    def test_router_accepts_four_experience_libraries_but_not_five_or_duplicates(self):
        from evolvmem.library_foundation.routing import AuthorityRouter
        router=AuthorityRouter(None)
        router.plan=lambda identity,tool,library_id:(tool,library_id)
        self.assertEqual(len(router.plan_many(None,'experience_recall',[uid(i) for i in range(4)])),4)
        for ids in ([uid(i) for i in range(5)],[uid(1),uid(1)]):
            with self.assertRaises(FoundationError):router.plan_many(None,'experience_recall',ids)

if __name__=='__main__':unittest.main()
