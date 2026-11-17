"""B7/B8 small synthetic checks only. No production data, credentials or PG."""
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('OPERATIONS_NATIVE_PACKAGE_ROOT',str(ROOT.parent/'pg-history-governance-20261101/candidate')))
sys.path.insert(0,str(BASE))
import evolvmem.history_migration as package
if not os.environ.get('OPERATIONS_NATIVE_PACKAGE_ROOT'):
    package.__path__.insert(0,str(ROOT/'candidate/evolvmem/history_migration'))

class Preservation(unittest.TestCase):
    def setUp(self):
        try:self.m=importlib.import_module('evolvmem.history_migration.operations_mapping')
        except ModuleNotFoundError:self.fail('operations fixed mapping implementation missing')
    def test_namespaces_and_actual_aliases(self):
        m=self.m
        self.assertEqual([len(m.applicable(n)) for n in ('owner','kane','public')],[8,4,3])
        self.assertEqual(m.AUTO,('local_capture_files',))
        self.assertEqual(m.SCHEMA['round_metrics']['rowid_alias'],'round')
        self.assertEqual(m.SCHEMA['lan_publications']['rowid_alias'],'context_id')
        self.assertEqual(sum(len(t['foreign_keys']) for t in m.SCHEMA.values()),1)
    def test_binary_nul_non_utf8_and_null_key_survive(self):
        m=self.m;row=dict(source_rowid=-9,cache_key=None,vector=b'\x00\xff\x80{"raw":1}')
        self.assertEqual(m.map_row('organization_review_vectors',row),row)
        self.assertEqual(m.unmap_row('organization_review_vectors',row),row)
        self.assertNotEqual(m.canonical(b'abc'),m.canonical('abc'))
        self.assertNotEqual(m.canonical(b'abc'),m.canonical({'$sqlite_blob_hex':'616263'}))
        sql,args=m.insert_statement('organization_review_vectors',row,'00000000-0000-0000-0000-000000000001')
        self.assertIn(row['vector'],args)
        self.assertNotIn('\\xff',sql)
        for bad in ('[1,2,3]',[1,2,3],memoryview(b'x')):
            with self.assertRaises(m.ImportError):m.map_row('organization_review_vectors',dict(row,vector=bad))
    def test_raw_request_result_and_identity_unchanged(self):
        m=self.m;r=dict(source_rowid=-9223372036854775808,user='u',tool='publish',request_id='r',payload_hash='old',result=' {"a":1,"a":2} ')
        self.assertEqual(m.unmap_row('lan_write_requests',m.map_row('lan_write_requests',r)),r)
        self.assertEqual(m.map_row('lan_write_requests',dict(r,result=None))['result'],None)
        self.assertNotEqual(m.chunk_hash('lan_write_requests',[r]),m.chunk_hash('lan_write_requests',[dict(r,result='{"a":2}')]))
        self.assertEqual(m.map_row('context_health_generation',dict(source_rowid=1,id=1,version=7,generation=92,schema_version=4875))['generation'],92)
    def test_schema_exact_and_absence_not_empty(self):
        m=self.m;scan=importlib.import_module('evolvmem.history_migration.operations_scan')
        for ns in ('owner','kane','public'):
            with sqlite3.connect(':memory:') as c:
                for spec in m._EVIDENCE['namespaces'][ns].values():
                    c.execute(spec['ddl'])
                    for index in reversed(spec['indexes']):
                        if index['ddl']:c.execute(index['ddl'])
                scan.validate_schema(c,ns)
                missing=m.absent(ns)[0];c.execute('CREATE TABLE '+missing+'(id INTEGER)')
                with self.assertRaises(m.ImportError):scan.validate_schema(c,ns)
    def test_sequence_absence_zero_deleted_and_exhausted(self):
        s=importlib.import_module('evolvmem.history_migration.operations_sequences');m=self.m
        absent=dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,allocation_history_verified=False)
        zero=dict(absent,present=True,value=0,storage_type='integer')
        self.assertEqual(s.allocation(absent,0),(1,False));self.assertEqual(s.allocation(zero,0),(1,False))
        self.assertNotEqual(absent,zero)
        self.assertEqual(s.allocation(dict(zero,value=9000),2),(9001,False))
        self.assertEqual(s.allocation(dict(zero,value=m.INT64_MAX),0),(m.INT64_MAX,True))
        with self.assertRaises(m.ImportError):s.allocation(dict(absent,value=0),0)

if __name__=='__main__':unittest.main()
