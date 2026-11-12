"""Only B4 synthetic preservation checks; no server, PG, or real history."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ['B4_NATIVE_PACKAGE_ROOT']) if os.environ.get('B4_NATIVE_PACKAGE_ROOT') else ROOT.parent/'pg-history-capture-integration-20261101/candidate'
sys.path.insert(0,str(BASE))
import evolvmem.history_migration as package
if not os.environ.get('B4_NATIVE_PACKAGE_ROOT'):
    package.__path__.insert(0,str(ROOT/'candidate/evolvmem/history_migration'))

class Preservation(unittest.TestCase):
    def setUp(self):
        try:self.m=importlib.import_module('evolvmem.history_migration.knowledge_mapping')
        except ModuleNotFoundError:self.fail('B4 preservation implementation missing')
    def test_nullable_ids_and_raw_json_survive_without_merging(self):
        row=dict(source_rowid=-8,knowledge_id=None,fingerprint=' f ',category='manual',item_id=None,
                 history_json=' {"a":1, "a":2} ',decision_json='not-json',revision=7,retired=1,
                 created_at='verbatim-date',updated_at='',parent_id='')
        second=dict(row,source_rowid=12)
        mapped=self.m.ordered_rows('knowledge_objects',[second,row])
        self.assertEqual(mapped,[row,second])
        self.assertEqual(self.m.unmap_row('knowledge_objects',mapped[0]),row)
        with self.assertRaises(self.m.ImportError):self.m.ordered_rows('knowledge_objects',[row,row])
    def test_integer_alias_mismatch_and_missing_columns_are_rejected(self):
        row=dict(source_rowid=8,id=8,framework=' { raw } ',revision=3)
        self.assertEqual(self.m.map_row('learning_settings',row),row)
        for changed in (dict(row,id=9),dict(row,id=None),dict(row,revision=True),{'source_rowid':8}):
            with self.assertRaises(self.m.ImportError):self.m.map_row('learning_settings',changed)
    def test_absence_is_namespace_specific_not_an_empty_table(self):
        self.assertEqual(len(self.m.applicable('owner')),23)
        self.assertEqual(self.m.absent('owner'),())
        self.assertEqual(self.m.absent('kane'),('skill_proposals',))
        self.assertEqual(self.m.absent('public'),('skill_proposals',))
        with self.assertRaises(self.m.ImportError):self.m.applicable('new-device')
    def test_chunk_receipt_hash_changes_on_manual_choice_and_rowid(self):
        r=dict(source_rowid=1,group_id='g',conditions_json=' {"x": 1} ',version_id='v1')
        original=self.m.chunk_hash('knowledge_fact_choices',[r])
        for changed in (dict(r,version_id='v2'),dict(r,source_rowid=2),dict(r,conditions_json='{"x":1}')):
            self.assertNotEqual(self.m.chunk_hash('knowledge_fact_choices',[changed]),original)
    def test_parameterized_quoted_insert_keeps_null_and_text(self):
        r=dict(source_rowid=6,version_id='v',knowledge_id='k',source_key='s',start=0,end=2,quote="' hi",role='manual',active=1)
        sql,args=self.m.insert_statement('knowledge_fact_sources',r,'00000000-0000-0000-0000-000000000001')
        self.assertIn('"end"',sql)
        self.assertNotIn("' hi",sql)
        self.assertIn("' hi",args)
    def test_reviewed_schemas_create_and_absence_is_detected(self):
        scan=importlib.import_module('evolvmem.history_migration.knowledge_scan')
        for namespace in ('owner','kane','public'):
            with sqlite3.connect(':memory:') as c:
                for spec in self.m._EVIDENCE['namespaces'][namespace].values():
                    c.execute(spec['ddl'])
                    for index in reversed(spec['indexes']):
                        if index['ddl']:c.execute(index['ddl'])
                scan.validate_schema(c,namespace)
                if namespace!='owner':
                    c.execute('CREATE TABLE skill_proposals(id INTEGER)')
                    with self.assertRaises(self.m.ImportError):scan.validate_schema(c,namespace)
                else:
                    c.execute('ALTER TABLE knowledge_fact_choices ADD COLUMN unexpected TEXT')
                    with self.assertRaises(self.m.ImportError):scan.validate_schema(c,namespace)

class SequenceBoundary(unittest.TestCase):
    def test_absent_zero_deleted_highwater_and_exhausted_have_distinct_evidence(self):
        try:s=importlib.import_module('evolvmem.history_migration.knowledge_sequences')
        except ModuleNotFoundError:self.fail('B4 sequence boundary implementation missing')
        absent=dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,allocation_history_verified=False)
        zero=dict(absent,present=True,value=0,storage_type='integer')
        high=dict(zero,value=9000)
        exhausted=dict(zero,value=9223372036854775807)
        self.assertEqual(s.allocation(absent,0),(1,False))
        self.assertEqual(s.allocation(absent,8),(9,False))
        self.assertEqual(s.allocation(zero,0),(1,False))
        self.assertNotEqual(absent,zero)
        self.assertEqual(s.allocation(high,8),(9001,False))
        self.assertEqual(s.allocation(exhausted,8),(9223372036854775807,True))
        self.assertEqual(s.allocation(absent,9223372036854775807),(9223372036854775807,True))
        for invalid in (dict(absent,value=0),dict(zero,value=True),dict(zero,value=9223372036854775808)):
            with self.assertRaises(s.M.ImportError):s.allocation(invalid,0)

if __name__=='__main__':unittest.main()
