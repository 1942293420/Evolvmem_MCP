"""Snapshot-local fixed query reuse; no PostgreSQL runtime claims."""
import unittest
from evolvmem.pg_business_core import historical_validation as H
from evolvmem.library_foundation import FoundationError
from .test_chain import BINDING, LIB, SqlPort


class Port:
    def __init__(self):self.calls=0;self.value='first';self.write=False
    def execute(self,sql,params=()):
        if sql not in H.SQL.values():
            if sql==H.IDENTITY_SQL and self.write:
                b=BINDING
                return [(b.database,b.owner,'off','repeatable read',False,False,False,False,False,True,True,True)]
            return SqlPort(None).execute(sql,params)
        self.calls+=1
        return [({'nested': [self.value]},)]


class QueryReuseTests(unittest.TestCase):
    def test_reuse_isolates_results_and_proof(self):
        port=Port();scope=H._QuarantineRead(port,BINDING);scope.gate()
        first=scope.query('sources',(LIB,'item'));proof=scope.proof()
        first[0]['nested'].append('poison')
        second=scope.query('sources',(LIB,'item'))
        self.assertEqual(second,[{'nested':['first']}])
        second[0]['nested'].clear()
        self.assertEqual(scope.proof(),proof)
        self.assertEqual(scope.query('sources',(LIB,'item')),[{'nested':['first']}])
        self.assertEqual(port.calls,1)
        self.assertEqual(len(scope.dependencies),2)

    def test_closed_scope_refuses_hit_and_new_scope_reads_fresh(self):
        port=Port();scope=H._QuarantineRead(port,BINDING);scope.gate()
        scope.query('sources',(LIB,'item'));scope.active=False
        with self.assertRaisesRegex(FoundationError,'quarantine_scope_closed'):
            scope.query('sources',(LIB,'item'))
        port.value='changed'
        fresh=H._QuarantineRead(port,BINDING);fresh.gate()
        self.assertEqual(fresh.query('sources',(LIB,'item')),[{'nested':['changed']}])
        self.assertEqual(port.calls,2)

    def test_ungated_and_write_scopes_do_not_reuse(self):
        port=Port();scope=H._QuarantineRead(port,BINDING)
        for _ in range(2):scope.query('sources',(LIB,'item'))
        self.assertEqual(port.calls,2)
        scope.gate();scope.query('sources',(LIB,'item'))
        port.write=True;scope.gate(for_write=True);port.value='after write'
        for _ in range(2):
            self.assertEqual(scope.query('sources',(LIB,'item')),[{'nested':['after write']}])
        self.assertEqual(port.calls,5)
