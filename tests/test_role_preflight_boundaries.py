"""Independent real-SQLite preflight scope and memory-budget boundaries."""
from pathlib import Path
import sqlite3
import unittest

from tests import test_role_preflight_scope as F


class IndependentBoundaries(unittest.TestCase):
    setUp=F.RolePreflightScopeTests.setUp
    tearDown=F.RolePreflightScopeTests.tearDown
    read_scope=F.RolePreflightScopeTests.read_scope

    def _count(self,sql):
        F.RolePreflightScopeTests._count(self,sql)
        text=' '.join(sql.lower().split())
        if 'select max(length(text)),max(length(cast(text as blob)))' in text:
            self.counts['unit_budget_reads']+=1

    def tasks(self,count,revision_bytes=0):
        # Only this new synthetic file is written, before its read snapshot.
        for i in range(2,count+1):
            self.conn.execute('INSERT INTO organization_tasks SELECT ?,source_key,source_revision,rule_revision,status,superseded_by,source_snapshot FROM organization_tasks WHERE id=1',(i,))
            self.conn.execute("INSERT INTO organization_units SELECT ?,digest,text FROM organization_units WHERE task_id=1",(i,))
        if revision_bytes:
            self.conn.execute('UPDATE organization_tasks SET source_revision=?',('R'*revision_bytes,))
        self.conn.commit();self.counts.clear()

    def test_same_digest_always_rechecks_unit_while_task_metadata_reuses(self):
        with self.read_scope() as r:
            for _ in range(5):self.assertTrue(r.roles.preflight(1,'small')['ready'])
        self.assertEqual(5,self.counts['unit_budget_reads'])
        self.assertEqual(1,self.counts['task_budget_reads'])
        self.assertEqual(1,self.counts['member_budget_reads'])

    def test_failed_metadata_is_not_cached(self):
        self.conn.execute("UPDATE session_archives SET state='purged'");self.conn.commit()
        with self.read_scope() as r:
            for _ in range(3):
                self.assertEqual('unit_source_roles_unproven:structured_history_missing',r.roles.preflight(1,'small')['reason'])
        self.assertEqual(3,self.counts['task_budget_reads']);self.assertEqual(3,self.counts['member_budget_reads'])
        self.assertEqual(0,self.counts['structured_body_reads'])

    def test_first_unit_failure_does_not_cache_task_success(self):
        with self.read_scope() as r:
            self.assertEqual('unit_source_roles_unproven:unit_over_budget',r.roles.preflight(1,'large')['reason'])
            self.assertTrue(r.roles.preflight(1,'small')['ready'])
            self.assertTrue(r.roles.preflight(1,'small')['ready'])
        self.assertEqual(2,self.counts['task_budget_reads']);self.assertEqual(3,self.counts['unit_budget_reads'])

    def test_standalone_role_read_does_not_gain_metadata_scope(self):
        r=F.R.UnitRoleRead(self.store)
        self.assertFalse(self.conn.in_transaction)
        for _ in range(2):self.assertTrue(r.preflight(1,'small')['ready'])
        self.assertEqual(2,self.counts['task_budget_reads'])

    def test_query_only_caller_transaction_without_owned_provenance_scope_is_not_cacheable(self):
        self.conn.execute('PRAGMA query_only=ON');self.conn.execute('BEGIN')
        r=F.M.OriginalRead(self.store);r._begin_scope()
        try:
            for _ in range(2):self.assertTrue(r.roles.preflight(1,'small')['ready'])
            r._assert_scope()
        finally:r._end_scope()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(2,self.counts['task_budget_reads'])
        self.conn.rollback();self.conn.execute('PRAGMA query_only=OFF')

    def test_new_original_reader_does_not_borrow_first_metadata_cache(self):
        with F.P.provenance_snapshot(self.store):
            for _ in range(2):
                with self.read_scope(checked=False) as r:
                    self.assertTrue(r.roles.preflight(1,'small')['ready'])
                    self.assertTrue(r.roles.preflight(1,'small')['ready'])
        self.assertEqual(2,self.counts['task_budget_reads'])

    def test_nested_internal_provenance_scope_keeps_same_checked_reader(self):
        with self.read_scope() as r:
            first=F.P.scoped_provenance_reader(self.store)
            self.assertTrue(r.roles.preflight(1,'small')['ready'])
            with F.P.provenance_snapshot(self.store):
                self.assertIs(first,F.P.scoped_provenance_reader(self.store))
                self.assertTrue(r.roles.preflight(1,'small')['ready'])
        self.assertEqual(1,self.counts['task_budget_reads'])

    def test_exception_cleanup_and_rebegin_do_not_reuse_old_metadata(self):
        r=F.M.OriginalRead(self.store)
        with F.P.provenance_snapshot(self.store):
            r._begin_scope()
            try:
                self.assertTrue(r.roles.preflight(1,'small')['ready'])
                raise LookupError('synthetic interrupted scope')
            except LookupError:pass
            finally:r._end_scope()
            self.assertFalse(r._scope_active)
            r._begin_scope()
            try:
                self.assertTrue(r.roles.preflight(1,'small')['ready'])
                self.assertTrue(r.roles.preflight(1,'small')['ready']);r._assert_scope()
            finally:r._end_scope()
        self.assertEqual(2,self.counts['task_budget_reads'])

    def test_at_most_eight_task_entries_evicted_task_is_read_again(self):
        self.tasks(9)
        with self.read_scope() as r:
            for i in range(1,9):self.assertTrue(r.roles.preflight(i,'small')['ready'])
            for i in range(1,9):self.assertTrue(r.roles.preflight(i,'small')['ready'])
            self.assertEqual(8,self.counts['task_budget_reads'])
            self.assertTrue(r.roles.preflight(9,'small')['ready'])
            self.assertLessEqual(len(r.roles._preflight_tasks),8)
            missing=next(i for i in range(1,10) if i not in r.roles._preflight_tasks)
            before=self.counts['task_budget_reads']
            self.assertTrue(r.roles.preflight(missing,'small')['ready'])
            self.assertEqual(before+1,self.counts['task_budget_reads'])
            self.assertLessEqual(len(r.roles._preflight_tasks),8)

    def test_one_oversized_metadata_entry_is_ready_but_not_retained(self):
        self.tasks(1,2*1024*1024+1)
        with self.read_scope() as r:
            for _ in range(2):self.assertTrue(r.roles.preflight(1,'small')['ready'])
        self.assertEqual(2,self.counts['task_budget_reads'])

    def test_serialized_budget_is_aggregate_not_only_per_entry(self):
        # Each entry fits the conservative bound; together they cannot.
        # No particular eviction policy is required. Bodies remain tiny.
        self.tasks(7,310000)
        with self.read_scope() as r:
            total_weight=0
            for i in range(1,8):
                result=r.roles.preflight(i,'small');self.assertTrue(result['ready'])
                weight=F.R._metadata_weight(result);self.assertIsNotNone(weight);total_weight+=weight
                self.assertLessEqual(r.roles._preflight_bytes,2*1024*1024)
                self.assertEqual(r.roles._preflight_bytes,sum(F.R._metadata_weight(v) for v in r.roles._preflight_tasks.values()))
            self.assertGreater(total_weight,2*1024*1024)
            missing=next(i for i in range(1,8) if i not in r.roles._preflight_tasks)
            before=self.counts['task_budget_reads']
            self.assertTrue(r.roles.preflight(missing,'small')['ready'])
            self.assertEqual(before+1,self.counts['task_budget_reads'])
            self.assertLessEqual(r.roles._preflight_bytes,2*1024*1024)

    def test_unchecked_connection_switch_cannot_borrow_checked_owner(self):
        other=sqlite3.connect(self.path);other.row_factory=sqlite3.Row
        try:
            with self.read_scope() as r:
                self.assertTrue(r.roles.preflight(1,'small')['ready'])
                self.store._connection=lambda:other
                try:
                    self.assertTrue(r.roles.preflight(1,'small')['ready'])
                    self.assertEqual(2,self.counts['task_budget_reads'])
                    self.assertFalse(r.roles._preflight_tasks)
                finally:self.store._connection=lambda:self.conn
        finally:other.close()

    def test_other_checked_connection_owner_cannot_authorize_original_connection_cache(self):
        other=sqlite3.connect(self.path);other.row_factory=sqlite3.Row
        try:
            with self.read_scope() as r:
                self.assertTrue(r.roles.preflight(1,'small')['ready'])
                self.store._connection=lambda:other
                try:
                    with F.P.provenance_snapshot(self.store):
                        self.assertIs(other,F.P.scoped_provenance_reader(self.store).conn)
                        self.assertTrue(r.roles.preflight(1,'small')['ready'])
                        self.assertEqual(2,self.counts['task_budget_reads'])
                        self.assertFalse(r.roles._preflight_tasks)
                finally:self.store._connection=lambda:self.conn
        finally:other.close()

    def test_nested_returned_metadata_mutation_cannot_poison_hit(self):
        with self.read_scope() as r:
            first=r.roles.preflight(1,'small')
            original=first['members'][0]['identity']['content_hash']
            first['members'][0]['identity']['content_hash']='mutated outside cache'
            first['task']['source_revision']='mutated outside cache'
            second=r.roles.preflight(1,'small')
            self.assertEqual(original,second['members'][0]['identity']['content_hash'])
            self.assertEqual('synthetic-revision',second['task']['source_revision'])
        self.assertEqual(1,self.counts['task_budget_reads'])

    def test_query_only_drift_is_rejected_before_metadata_cache_hit(self):
        with self.read_scope() as r:
            self.assertTrue(r.roles.preflight(1,'small')['ready'])
            self.conn.execute('PRAGMA query_only=OFF')
            try:
                with self.assertRaisesRegex((RuntimeError,ValueError),'snapshot_changed|scope_changed'):
                    r.roles.preflight(1,'small')
            finally:self.conn.execute('PRAGMA query_only=ON')


if __name__=='__main__':unittest.main(verbosity=2)
