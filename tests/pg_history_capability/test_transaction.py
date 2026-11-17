"""Real SQLite effect-port tests. PG gates/FK/RLS/sequence DDL remain untested."""
import test_capability
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
import test_stream as fixture
from test_stream import source,parent,TARGET,RUN,N,P,M
from sql_port import Connection
from evolvmem.history_migration.capability_importer import CapabilityImporter


class Effects(fixture.Stream):
    # Do not inherit/re-run the existing stream test suite.
    test_stream_128_boundary_preserves_nullable_keys_and_max_integer=None
    test_plan_preserves_absence_exhaustion_and_rejects_fake_empty_table=None
    test_oversize_wrong_storage_and_missing_table_fail_without_completion=None
    def build(self):
        ref=source(self.temp.name);self.source_ref=ref
        with N.Phase(ref) as phase:previous=parent(phase.binding)
        self.plan=P.plan_source(ref,previous,TARGET,Path(self.temp.name)/'plan.json',run_id=RUN)
        with N.Phase(ref,plan=self.plan) as phase:self.args=phase.read(self.plan.page(0))
        self.target=Path(self.temp.name)/'target-effect-port.db';self.sql=[];self.fault=None
        with closing(sqlite3.connect(self.target)) as c:
            for table in M.TABLES:
                fields=['library_id TEXT NOT NULL','project_id TEXT','source_rowid INTEGER NOT NULL']
                fields += [M.quote(n)+' '+kind+('' if nullable else ' NOT NULL') for n,kind,nullable in M.SPECS[table]]
                c.execute('CREATE TABLE '+table+'('+','.join(fields)+',PRIMARY KEY(library_id,source_rowid))')
            c.execute('CREATE TABLE history_capability_runs(run_id TEXT PRIMARY KEY,library_id TEXT UNIQUE,parent_run_id TEXT,manifest_hash TEXT,manifest TEXT,loaded_counts TEXT,loaded_bytes INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE history_capability_chunks(run_id TEXT,table_code TEXT,chunk_key TEXT,input_hash TEXT,row_keys TEXT,row_count INTEGER,input_bytes INTEGER,PRIMARY KEY(run_id,table_code,chunk_key))')
            c.execute('CREATE TABLE history_capability_progress(run_id TEXT PRIMARY KEY,manifest_hash TEXT,stage TEXT,next_load INTEGER DEFAULT 0,next_verify INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,verify_generation INTEGER,count_table INTEGER DEFAULT 0,count_rows INTEGER DEFAULT 0,count_last TEXT)')
            for table in M.TABLES+('history_capability_chunks',):
                for event in ('INSERT','UPDATE','DELETE'):
                    c.execute('CREATE TRIGGER '+table+'_'+event+' AFTER '+event+' ON '+table+' BEGIN UPDATE history_capability_progress SET generation=generation+1; END')
            c.commit()
        class Port(CapabilityImporter):
            def _gate(self,c):pass # PG catalog/security NOT simulated.
            def _parent(self,c,plan):
                M.check(type(plan) is P.Plan,'trusted_capability_plan_required');plan.require_importable()
        self.port=Port(lambda:Connection(self),TARGET,capacity_guard=lambda:None)
        self.port.begin(self.plan)
    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.target)) as c:
            result=c.execute(sql,args).fetchall();c.commit();return result
    def test_all_seven_tables_load_verify_count_and_latest_assessment_survives(self):
        self.build()
        with N.Phase(self.source_ref,plan=self.plan) as phase:
            for i in range(self.plan.page_count):
                result=self.port.load_page(self.plan,i,phase.read(self.plan.page(i)))
        self.assertTrue(phase.proven_complete)
        self.assertEqual(result['progress']['stage'],'verifying')
        for i in range(self.plan.page_count):result=self.port.verify_page(self.plan,i)
        while result['progress']['count_table']<len(M.TABLES):result=self.port.count_page(self.plan)
        self.assertEqual(result['progress']['stage'],'counting')
        self.assertEqual(result['progress']['loaded_counts'],self.plan.counts)
        self.assertEqual(self.query('SELECT assessment_id,value,verification_status,details FROM capability_assessments ORDER BY assessment_id DESC LIMIT 1'),[(9223372036854775807,'ignore','unverified','raw manual')])
        self.assertEqual(self.query('SELECT memory_id,category,content,occurred_at FROM capability_memories'),[(9,'c','unchanged',None)])
        self.assertEqual(self.query('SELECT source_id,memory_id,source_kind,evidence_status FROM capability_sources'),[(3,9,'manual','unverified')])
    def test_pre_receipt_failure_rolls_back_nullable_rows_and_frontier(self):
        self.build();self.fault='before_receipt'
        with self.assertRaisesRegex(M.ImportError,'import_transaction_failed'):self.port.load_page(self.plan,0,self.args)
        self.assertEqual(self.query('SELECT count(*) FROM capability_categories'),[(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM history_capability_chunks'),[(0,)])
        self.assertEqual(self.query('SELECT next_load FROM history_capability_progress'),[(0,)])
    def test_unknown_commit_explicit_resume_uses_receipt_not_duplicate_null_ids(self):
        self.build();self.fault='after_commit'
        with self.assertRaisesRegex(M.ImportError,'commit_outcome_unknown'):self.port.load_page(self.plan,0,self.args)
        self.fault=None
        self.assertTrue(self.port.load_page(self.plan,0,self.args)['replayed'])
        self.assertEqual(self.query('SELECT count(*),count(id) FROM capability_categories'),[(128,1)])
        self.assertEqual(self.query('SELECT count(*) FROM history_capability_chunks'),[(1,)])
    def test_readback_rejects_manual_history_tamper(self):
        self.build();self.port.load_page(self.plan,0,self.args)
        self.query("UPDATE capability_categories SET label='changed' WHERE source_rowid=1")
        with self.assertRaisesRegex(M.ImportError,'capability_target_digest_changed'):self.port.load_page(self.plan,0,self.args)
    def test_readback_and_receipt_budgets_reject_before_large_values_are_loaded(self):
        self.build();self.port.load_page(self.plan,0,self.args)
        self.query('UPDATE capability_categories SET label=? WHERE source_rowid=1',('x'*(M.MAX_BYTES+1),))
        with self.assertRaisesRegex(M.ImportError,'capability_target_readback_budget'):self.port.load_page(self.plan,0,self.args)
        self.query('UPDATE history_capability_chunks SET row_keys=?',(json.dumps([['x'*9000]]),))
        with self.assertRaisesRegex(M.ImportError,'capability_receipt_metadata_budget'):self.port.load_page(self.plan,0,self.args)


class OperatorProof(fixture.Stream):
    test_stream_128_boundary_preserves_nullable_keys_and_max_integer=None
    test_plan_preserves_absence_exhaustion_and_rejects_fake_empty_table=None
    test_oversize_wrong_storage_and_missing_table_fail_without_completion=None
    def test_completion_requires_runner_exit_proof_and_current_generation(self):
        path=Path(os.environ['B5_NATIVE_OPERATOR']) if os.environ.get('B5_NATIVE_OPERATOR') else test_capability.ROOT/'integration/merged_operator.py'
        spec=importlib.util.spec_from_file_location('evolvmem.history_migration._b5_operator',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=SimpleNamespace(work_dir=self.temp.name,binding_hash='b'*64,source=SimpleNamespace(receipt_sha256='c'*64))
        kernels=SimpleNamespace(ready=lambda *a:True)
        operator=module.Operator(config,kernels=kernels);(Path(self.temp.name)/'plans').mkdir()
        plan=SimpleNamespace(sha256='a'*64)
        progress=dict(stage='verified',generation=3,verify_generation=3)
        self.assertFalse(operator._capability_proof(plan,progress=progress))
        with self.assertRaises(M.ImportError):operator._capability_proof(plan,dict(target_domain_verified=True),progress=progress)
        result=dict(capability_domain_complete=True,target_domain_verified=True,source_phase_complete=True,source_hash_passes=2,manifest_sha256=plan.sha256)
        self.assertTrue(operator._capability_proof(plan,result,progress=progress))
        with self.assertRaises(M.ImportError):operator._capability_proof(plan,progress=dict(progress,generation=4,verify_generation=4))
        args=module.parser().parse_args(['--config','test.json','status','--domain','capability'])
        self.assertEqual(args.domain,'capability')
        self.assertEqual(module.PARENTS['governance'],('core','sources','organization'))
        governance=module.parser().parse_args(['--config','test.json','status','--domain','governance'])
        self.assertEqual(governance.domain,'governance')
        self.assertFalse(operator._governance_proof(plan,progress=progress))

if __name__=='__main__':unittest.main()
