"""Small synthetic B6 checks. No production records or PostgreSQL connection."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ['B6_NATIVE_PACKAGE_ROOT']) if os.environ.get('B6_NATIVE_PACKAGE_ROOT') else ROOT.parent/'pg-history-governance-20261101/candidate'
sys.path.insert(0,str(BASE))
import evolvmem.history_migration as package
if not os.environ.get('B6_NATIVE_PACKAGE_ROOT'):package.__path__.insert(0,str(ROOT/'candidate/evolvmem/history_migration'))

class Preservation(unittest.TestCase):
    def setUp(self):
        try:self.m=importlib.import_module('evolvmem.history_migration.continuity_mapping')
        except ModuleNotFoundError:self.fail('B6 preservation implementation missing')
    def test_temporal_and_manual_revision_values_survive(self):
        row=dict(source_rowid=-3,event_id=-3,project='p',event_type='milestone',occurred_at=None,
            recorded_at='manual recorded date',title=' t ',content=' {"duplicate":1,"duplicate":2} ',
            source_ref='',fingerprint='f',status='corrected',created_at='',updated_at='older')
        self.assertEqual(self.m.unmap_row('project_events',self.m.map_row('project_events',row)),row)
        for value in (dict(row,occurred_at=''),dict(row,recorded_at='other'),dict(row,source_rowid=-4,event_id=-4)):
            self.assertNotEqual(self.m.chunk_hash('project_events',[row]),self.m.chunk_hash('project_events',[value]))
        repair=dict(source_rowid=0,plan_sha256=None,project='p',applied_at='manual',plan_json='not-json')
        self.assertEqual(self.m.map_row('project_history_date_repairs',repair),repair)
    def test_namespace_absence_and_real_fk_boundary(self):
        self.assertEqual(self.m.absent('owner'),())
        self.assertEqual(self.m.absent('kane'),('project_history_date_repairs',))
        self.assertEqual(self.m.absent('public'),('project_history_date_repairs',))
        e=self.m._EVIDENCE['namespaces']['owner']
        self.assertEqual(e['continuity_events']['foreign_keys'],[])
        self.assertEqual({f['from_column'] for f in e['continuity_workstreams']['foreign_keys']},{'parent_id','current_context_id'})
    def test_rowid_alias_bool_integer_and_boundaries(self):
        row=dict(source_rowid=0,id=0,workstream_id=None,event_type='checkpoint',before_revision=None,
            after_revision=19,before_state_version=-1,after_state_version=20,error_code='',created_at='raw')
        self.assertEqual(self.m.map_row('continuity_events',row),row)
        for wrong in (dict(row,id=1),dict(row,id=None),dict(row,after_revision=True)):
            with self.assertRaises(self.m.ImportError):self.m.map_row('continuity_events',wrong)
        with self.assertRaises(self.m.ImportError):self.m.prepare_chunk('continuity_events',[dict(row,source_rowid=i,id=i) for i in range(129)],None)
    def test_schema_rejects_added_absent_table(self):
        scan=importlib.import_module('evolvmem.history_migration.continuity_scan')
        c=sqlite3.connect(':memory:')
        for e in self.m._EVIDENCE['namespaces']['kane'].values():
            c.execute(e['ddl'])
            for index in reversed(e['indexes']):
                if index['ddl']:c.execute(index['ddl'])
        scan.validate_schema(c,'kane')
        c.execute(self.m._EVIDENCE['namespaces']['owner']['project_history_date_repairs']['ddl'])
        with self.assertRaises(self.m.ImportError):scan.validate_schema(c,'kane')
        c.close()
    def test_sequence_absent_zero_deleted_highwater_and_int64max(self):
        s=importlib.import_module('evolvmem.history_migration.continuity_sequences')
        absent=dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,allocation_history_verified=False)
        zero=dict(absent,present=True,value=0,storage_type='integer')
        self.assertEqual(s.allocation(absent,0),(1,False))
        self.assertEqual(s.allocation(zero,0),(1,False))
        self.assertEqual(s.allocation(dict(zero,value=500),5),(501,False))
        self.assertEqual(s.allocation(dict(zero,value=2**63-1),0),(2**63-1,True))
        self.assertEqual(s.allocation(absent,2**63-1),(2**63-1,True))

if __name__=='__main__':unittest.main()
