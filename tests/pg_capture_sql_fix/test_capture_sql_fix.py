"""Real prepare renderer and real SourceRun contract; no PostgreSQL execution."""
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evolvmem.history_migration import operator_scope as baseline_scope
from evolvmem.history_migration import source_mapping as M,source_plan as SP
from evolvmem.history_migration.source_importer import SourceRun
from evolvmem.history_migration.source_complete import CompleteImporter
from evolvmem.history_migration.history_core.importer import Target

ROOT=Path(__file__).resolve().parents[1]
CANDIDATE=ROOT/'candidate/evolvmem/history_migration/operator_scope.py'
if CANDIDATE.exists():
    spec=importlib.util.spec_from_file_location('evolvmem.history_migration._capture_sql_fix_scope',CANDIDATE)
    S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)
else:S=baseline_scope


def source_plan(namespace='owner'):
    """Construct validated metadata with the actual SourceRun and Plan classes."""
    run=SourceRun('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001',
        'b'*64,namespace,'c'*64,M.schema_hash(namespace),tuple((t,0) for t in M.TABLES),tuple((t,0) for t in M.AUTO))
    pages=[dict(ordinal=i,table=t,chunk_key='s'+str(i).zfill(6),after=None,end=None,exhausted=True,
        row_count=0,native_hash='d'*64,input_hash=None,input_bytes=0) for i,t in enumerate(M.TABLES)]
    return SP.Plan(dict(format='history-sources-seven-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
        source=dict(namespace=namespace,file_sha256=run.source_file_sha256,seal_receipt_sha256='a'*64),run=run.manifest,
        source_schema_hash=run.source_schema_hash,sequence={t:dict(present=True,storage_type='integer',value=0) for t in M.AUTO},
        highwaters_ready=True,input_bytes=0,pages=pages))


class PrepareRegressionTests(unittest.TestCase):
    def test_actual_capture_prepare_expands_sha256_variable_and_closes_each_lease(self):
        statements=[];leases=[]
        class Lease:
            closed=False;autocommit=True;info=SimpleNamespace(transaction_status=0)
            def __init__(self):self.raw=self;self.commits=0;leases.append(self)
            def execute(self,sql,args=None):statements.append(sql);return self
            def fetchall(self):return [(True,)]
            def commit(self):self.commits+=1
            def rollback(self):pass
            def close(self):self.closed=True
        r=S.OperatorRuntime.__new__(S.OperatorRuntime)
        r.target=Target('em_pilot_sql_fix','em_sql_fix_owner','em_sql_fix_maint','00000000-0000-0000-0000-000000000009',1,1)
        r.config=SimpleNamespace(source=SimpleNamespace(namespace='owner',receipt_sha256='a'*64))
        r._open=lambda *_:Lease();r.factory=lambda:Lease();r._content=lambda *_:None
        r._schema_ready=lambda *_:False
        parent=source_plan();result=r.prepare('capture',parent_plan=parent)
        rendered='\n'.join(statements)
        self.assertEqual([],re.findall(r":'([a-z_][a-z0-9_]*)'",rendered),'all trusted psql values must be rendered before driver execution')
        self.assertIn("set_config('history.capture_parent_hash', '"+parent.sha256+"', true)",rendered)
        self.assertIn("set_config('history.capture_parent', '"+parent.run_id+"', true)",rendered)
        self.assertEqual(['001_history_capture.sql'],result['applied'])
        self.assertEqual(2,len(leases));self.assertTrue(all(l.closed and l.commits==1 for l in leases))

    def test_sql_namespace_gate_reads_actual_persisted_run_not_disk_plan_shape(self):
        sql=(Path(S.__file__).parent/'sql/001_history_capture.sql').read_text()
        match=re.search(r"r\.manifest->'source'->>'([a-z_]+)' IN \('owner','kane'\)",sql)
        self.assertIsNotNone(match,'retain the explicit owner/kane namespace restriction')
        key=match[1]
        port=CompleteImporter.__new__(CompleteImporter)
        port.target=Target('em_pilot_sql_fix','em_sql_fix_owner','em_sql_fix_maint','00000000-0000-0000-0000-000000000009',1,1)
        for namespace in ('owner','kane','public'):
            with self.subTest(namespace=namespace):
                plan=source_plan(namespace);persisted=port.bound_manifest(plan.run)
                self.assertEqual(namespace,plan.document['source']['namespace'])
                self.assertEqual(namespace,persisted['source'][key])
                self.assertNotIn('namespace',persisted['source'])
                self.assertEqual(namespace!='public',persisted['source'][key] in ('owner','kane'))


if __name__=='__main__':unittest.main()
