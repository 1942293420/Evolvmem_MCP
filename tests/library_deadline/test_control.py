"""Run the real catalog lease/provider against the frozen SQL-port double."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import threading
import time
import unittest
from unittest.mock import patch

from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_catalog.adapter import ControlProvider, ControlBinding, ControlCatalog, ControlAuthorityLease
from evolvmem.library_foundation import Authenticator, AuthorityRouter, FoundationError
from evolvmem.library_foundation.sessions import SessionManager
from tests.library_deadline.test_guard import CancelTarget

SOURCE = Path(__file__).resolve().parents[1] / 'library_catalog/test_catalog.py'
SPEC = importlib.util.spec_from_file_location('frozen_catalog_driver_tests', SOURCE)
FROZEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FROZEN)


class Status(NS):
    def __int__(self): return {'IDLE':0,'INTRANS':2}[self.name]
class Cursor(FROZEN.Cursor):
    def close(self): pass
class Raw(FROZEN.Raw):
    def __init__(self, engine, kwargs):
        super().__init__(engine, kwargs)
        self.info.transaction_status=Status(name='IDLE')
        self.cancels=0
    def cursor(self): return Cursor(self)
    def cancel_safe(self, *, timeout): self.cancels+=1
    def close(self):
        self.engine.closed_after.append(None if self.engine.target is None else self.engine.target.retired)
        self.pinned=False
        super().close()
class Engine(FROZEN.Engine):
    def __init__(self): super().__init__(); self.closed_after=[]; self.target=None
    def connect(self, **kwargs):
        raw=Raw(self,kwargs);self.connections.append(raw);return raw


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.guard=SyncRequestDeadlineGuard(.12)
        self.engine=Engine()
        patcher=patch.dict(sys.modules,{'psycopg':NS(Connection=Raw,capabilities=NS(has_cancel_safe=lambda **kw:True))})
        patcher.start();self.addCleanup(patcher.stop)
    def assembly(self):
        self.assertIn('deadline_guard',__import__('inspect').signature(ControlProvider).parameters,
                      'control provider bypasses concrete guard')
        binding=ControlBinding('127.0.0.1',5432,'synthetic','catalog_reader','secret')
        provider=ControlProvider(binding,self.engine.connect,lambda ref:'synthetic',deadline_guard=self.guard)
        catalog=ControlCatalog(provider)
        identity=Authenticator(catalog).authenticate('synthetic-token')
        router=AuthorityRouter(catalog,implemented_tools={'postgres':{'context_read'}})
        route=router.plan(identity,'context_read',library_id=FROZEN.L)
        return catalog,route
    def test_control_authentication_connections_registered_and_closed(self):
        with self.guard.enforce():
            self.assembly()
        self.assertTrue(self.engine.connections)
        self.assertTrue(all(raw.closed for raw in self.engine.connections))
        self.assertTrue(all(raw.kwargs['sslmode']=='verify-full' for raw in self.engine.connections))
    def test_control_pin_survives_until_local_content_quiescence(self):
        with self.assertRaises(FoundationError):
            with self.guard.enforce():
                catalog,route=self.assembly()
                lease=ControlAuthorityLease(catalog,deadline_guard=self.guard)
                with lease.hold(route):
                    self.engine.target=CancelTarget()
                    self.guard._register(self.engine.target)
                    with self.guard.operation(self.engine.target):
                        self.assertTrue(self.engine.target.cancelled.wait(.5))
                        self.assertTrue(self.engine.connections[-1].pinned)
        self.assertEqual(0,self.engine.connections[-1].cancels)
        self.assertTrue(self.engine.closed_after[-1])
        self.assertTrue(self.engine.connections[-1].closed)
    def test_lease_rejects_unrelated_provider_guard(self):
        catalog=NS(provider=NS(deadline_guard=SyncRequestDeadlineGuard(1)))
        with self.assertRaises(ValueError):
            ControlAuthorityLease(catalog,deadline_guard=self.guard)
    def test_session_manager_rejects_unguarded_content_provider_with_guarded_lease(self):
        with self.assertRaises(ValueError):
            SessionManager(None,None,NS(deadline_guard=None),NS(hold=lambda route:None,deadline_guard=self.guard))
    def test_lease_cleanup_fault_preserves_unknown_commit_classification(self):
        with self.assertRaises(FoundationError) as caught:
            with self.guard.enforce():
                catalog,route=self.assembly()
                lease=ControlAuthorityLease(catalog,deadline_guard=self.guard)
                with patch.object(self.guard,'quiesce',side_effect=FoundationError('deadline_cleanup_incomplete')):
                    with lease.hold(route):
                        raise FoundationError('commit_result_unknown')
        self.assertEqual('commit_result_unknown',caught.exception.code)

    def test_sequential_lookup_and_mutation_leases_share_original_deadline(self):
        # memory_remove's immutable-key lookup uses manager.run before its
        # keyed mutation. Both enter this real authority hold sequentially.
        with self.guard.enforce():
            catalog,route=self.assembly()
            lease=ControlAuthorityLease(catalog,deadline_guard=self.guard)
            before=self.guard.remaining_seconds()
            with lease.hold(route): time.sleep(.02)
            self.assertFalse(self.engine.connections[-1].pinned)
            self.assertTrue(self.engine.connections[-1].closed)
            failure=None
            try:
                with lease.hold(route):
                    self.assertLess(self.guard.remaining_seconds(),before-.01)
            except FoundationError as exc: failure=exc.code
            self.assertIsNone(failure,'second authority segment was incorrectly sealed')

