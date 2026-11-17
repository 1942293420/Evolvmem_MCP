from copy import deepcopy
from types import SimpleNamespace
import unittest
from .test_chain import SqlPort,BINDING,H
from evolvmem.library_foundation import FoundationError


class Boundaries(unittest.TestCase):
    def test_total_query_rows_and_retained_bytes_never_silently_truncate(self):
        class Port:
            def execute(self,sql,params):return [({'members':'a'},)]*513
        scope=H._QuarantineRead(Port(),BINDING)
        with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):scope.query('conversation',(BINDING.library_id,'x'))
        scope=H._QuarantineRead(Port(),BINDING)
        with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):scope._record('test',(),['x'*(H.MAX_BYTES//6+1)])
        scope.row_count=4096
        with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):scope._record('test',(),[None])
    def test_psycopg_cursor_is_bounded_and_closed(self):
        from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
        from evolvmem.library_deadline.driver import GuardedCursor
        class Cursor:
            description=('one',)
            count=0
            def fetchone(self):
                self.count+=1
                return (1,)  # endless synthetic raw driver: runtime must bound it
            def close(self):self.closed=True
        raw=Cursor();guard=SyncRequestDeadlineGuard()
        cursor=GuardedCursor(SimpleNamespace(guard=guard,kind='content',retired=False),raw)
        class Port:
            def execute(self,sql,params):return cursor
        scope=H._QuarantineRead(Port(),BINDING)
        with guard.enforce():
            guard._register(cursor.connection)
            try:self.assertEqual([(1,)]*513,scope._execute('known fixture test'))
            finally:guard._unregister(cursor.connection)
        self.assertEqual(513,raw.count);self.assertTrue(raw.closed)
    def test_timeout_contract_and_actual_readonly_owner_required(self):
        port=SqlPort(None);original=port.execute
        for slot,value in ((2,'off'),(3,'read committed'),(4,True),(5,True),(6,True),(9,False),(10,False),(11,False)):
            def execute(sql,params=()):
                row=list(original(H.IDENTITY_SQL)[0]);row[slot]=value;return [tuple(row)]
            port.execute=execute
            with self.assertRaisesRegex(FoundationError,'quarantine_owner_read_transaction_required'):
                H._QuarantineRead(port,BINDING).gate()
    def test_no_owner_scope_or_rows_survive_context(self):
        class Port:
            def execute(self,sql,params):return []
        scope=H._QuarantineRead(Port(),BINDING);store=H._HistoricalStore(scope);scope.active=False
        with self.assertRaisesRegex(FoundationError,'quarantine_scope_closed'):store.get_retrieval_records(())
        with self.assertRaisesRegex(FoundationError,'quarantine_scope_closed'):scope.proof()
    def test_unknown_query_or_foreign_library_rejected_before_sql(self):
        class Port:
            def execute(self,sql,params):raise AssertionError('must not execute')
        scope=H._QuarantineRead(Port(),BINDING)
        for name,params in (('arbitrary',(BINDING.library_id,1)),('item',('foreign',1))):
            with self.assertRaisesRegex(FoundationError,'invalid_qualification_query'):scope.query(name,params)
