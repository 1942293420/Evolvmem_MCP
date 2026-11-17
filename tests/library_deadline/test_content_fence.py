from contextlib import contextmanager
from types import SimpleNamespace as NS
import unittest
from evolvmem.library_foundation.sessions import (SessionManager, ContentSessionState,
    DatabaseRole, DatabaseCredential, _PsycopgConnection)
from evolvmem.library_foundation import FoundationError


class Connection:
    def __init__(self): self.events=[]; self.epoch=1
    def begin(self, *, read_only): self.events.append(('begin', read_only))
    def inspect_role(self): return DatabaseRole('runtime','runtime',False,False,False,True)
    def session_context(self): return ContentSessionState('p','l',1,1,'write','pg_active','ready')
    def pin_session_context(self, policy, route):
        self.events.append(('pin',policy,route))
        return ContentSessionState('p','l',self.epoch,1,'write','pg_active','ready')
    def session_credential(self): return DatabaseCredential('c','d',1)
    def acquire_session_locks(self, keys): self.events.append(('lock',))
    def execute(self, sql, params=()): self.events.append(('effect',)); return ()
    def commit(self): self.events.append(('commit',))
    def rollback(self): self.events.append(('rollback',))
    def close(self): self.events.append(('close',))


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.connection=Connection()
        self.route=NS(action='write',identity=NS(principal_id='p',credential_id='c',device_id='d',credential_epoch=1),
            library=NS(library_id='l',policy_epoch=1,route_epoch=1))
        @contextmanager
        def hold(route): yield
        self.manager=SessionManager(NS(revalidate=lambda route:None),NS(resolve=lambda route:NS(role='runtime')),
            NS(open=lambda binding:self.connection),NS(hold=hold))
    def test_normal_write_pins_on_same_connection_before_effect(self):
        self.manager.run(self.route, lambda session: session.execute('effect'))
        self.assertIn(('pin',1,1),self.connection.events)
        self.assertLess(self.connection.events.index(('pin',1,1)),self.connection.events.index(('effect',)))
        self.assertLess(self.connection.events.index(('effect',)),self.connection.events.index(('commit',)))
    def test_keyed_write_pins_but_readonly_preflight_confirmation_do_not(self):
        with self.manager.keyed(self.route,(1,)) as scope:
            with scope.transaction() as session: session.execute('effect')
            with scope.transaction(read_only=True): pass
        self.assertEqual(1,self.connection.events.count(('pin',1,1)))
        self.assertLess(self.connection.events.index(('lock',)),self.connection.events.index(('pin',1,1)))
        self.assertEqual([True,False,True],[e[1] for e in self.connection.events if e[0]=='begin'])
    def test_stale_pin_cannot_reach_business_effect(self):
        self.connection.epoch=2
        with self.assertRaises(FoundationError):
            self.manager.run(self.route, lambda session: session.execute('effect'))
        self.assertNotIn(('effect',),self.connection.events)
        self.assertIn(('rollback',),self.connection.events)
    def test_driver_emits_actual_existing_pin_function(self):
        self.assertTrue(callable(getattr(_PsycopgConnection,'pin_session_context',None)), 'SQL pin adapter missing')
        class RawConnection(_PsycopgConnection):
            def execute(self,sql,params=()):
                self.sql,self.params=sql,params
                return (('p','l',3,4,'write','pg_active','ready'),)
        driver=RawConnection(None)
        result=driver.pin_session_context(3,4)
        self.assertIn('em_security.pin_session_context',driver.sql)
        self.assertEqual((3,4),driver.params)
        self.assertEqual(3,result.policy_epoch)

