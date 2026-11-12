"""Real SQLite effect-port tests. PG gates/FK/RLS/sequence DDL remain untested."""
import test_knowledge
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
from evolvmem.history_migration.knowledge_importer import KnowledgeImporter


class Effects(fixture.Stream):
    # Do not inherit/re-run the existing stream test suite.
    test_stream_128_boundary_preserves_nullable_keys_and_max_integer=None
    test_plan_preserves_absence_exhaustion_and_rejects_fake_empty_table=None
    test_oversize_wrong_storage_and_missing_table_fail_without_completion=None
    def build(self):
        ref=source(self.temp.name)
        with N.Phase(ref) as phase:previous=parent(phase.binding)
        self.plan=P.plan_source(ref,previous,TARGET,Path(self.temp.name)/'plan.json',run_id=RUN)
        with N.Phase(ref,plan=self.plan) as phase:self.args=phase.read(self.plan.page(0))
        self.target=Path(self.temp.name)/'target-effect-port.db';self.sql=[];self.fault=None
        with closing(sqlite3.connect(self.target)) as c:
            for table in M.TABLES:
                fields=['library_id TEXT NOT NULL','project_id TEXT','source_rowid INTEGER NOT NULL']
                fields += [M.quote(n)+' '+kind+('' if nullable else ' NOT NULL') for n,kind,nullable in M.SPECS[table]]
                c.execute('CREATE TABLE '+table+'('+','.join(fields)+',PRIMARY KEY(library_id,source_rowid))')
            c.execute('CREATE TABLE history_knowledge_runs(run_id TEXT PRIMARY KEY,library_id TEXT UNIQUE,parent_run_id TEXT,manifest_hash TEXT,manifest TEXT,loaded_counts TEXT,loaded_bytes INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE history_knowledge_chunks(run_id TEXT,table_code TEXT,chunk_key TEXT,input_hash TEXT,row_keys TEXT,row_count INTEGER,input_bytes INTEGER,PRIMARY KEY(run_id,table_code,chunk_key))')
            c.execute('CREATE TABLE history_knowledge_progress(run_id TEXT PRIMARY KEY,manifest_hash TEXT,stage TEXT,next_load INTEGER DEFAULT 0,next_verify INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,verify_generation INTEGER,count_table INTEGER DEFAULT 0,count_rows INTEGER DEFAULT 0,count_last TEXT)')
            for table in M.TABLES+('history_knowledge_chunks',):
                for event in ('INSERT','UPDATE','DELETE'):
                    c.execute('CREATE TRIGGER '+table+'_'+event+' AFTER '+event+' ON '+table+' BEGIN UPDATE history_knowledge_progress SET generation=generation+1; END')
            c.commit()
        class Port(KnowledgeImporter):
            def _gate(self,c):pass # PG catalog/security NOT simulated.
            def _parent(self,c,plan):
                M.check(type(plan) is P.Plan,'trusted_knowledge_plan_required');plan.require_importable()
        self.port=Port(lambda:Connection(self),TARGET,capacity_guard=lambda:None)
        self.port.begin(self.plan)
    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.target)) as c:
            result=c.execute(sql,args).fetchall();c.commit();return result
    def test_pre_receipt_failure_rolls_back_nullable_rows_and_frontier(self):
        self.build();self.fault='before_receipt'
        with self.assertRaisesRegex(M.ImportError,'import_transaction_failed'):self.port.load_page(self.plan,0,self.args)
        self.assertEqual(self.query('SELECT count(*) FROM knowledge_objects'),[(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM history_knowledge_chunks'),[(0,)])
        self.assertEqual(self.query('SELECT next_load FROM history_knowledge_progress'),[(0,)])
    def test_unknown_commit_explicit_resume_uses_receipt_not_duplicate_null_ids(self):
        self.build();self.fault='after_commit'
        with self.assertRaisesRegex(M.ImportError,'commit_outcome_unknown'):self.port.load_page(self.plan,0,self.args)
        self.fault=None
        self.assertTrue(self.port.load_page(self.plan,0,self.args)['replayed'])
        self.assertEqual(self.query('SELECT count(*),count(knowledge_id) FROM knowledge_objects'),[(128,0)])
        self.assertEqual(self.query('SELECT count(*) FROM history_knowledge_chunks'),[(1,)])
    def test_readback_rejects_manual_history_tamper(self):
        self.build();self.port.load_page(self.plan,0,self.args)
        self.query("UPDATE knowledge_objects SET decision_json='changed' WHERE source_rowid=1")
        with self.assertRaisesRegex(M.ImportError,'knowledge_target_digest_changed'):self.port.load_page(self.plan,0,self.args)
    def test_readback_and_receipt_budgets_reject_before_large_values_are_loaded(self):
        self.build();self.port.load_page(self.plan,0,self.args)
        self.query('UPDATE knowledge_objects SET history_json=? WHERE source_rowid=1',('x'*(M.MAX_BYTES+1),))
        with self.assertRaisesRegex(M.ImportError,'knowledge_target_readback_budget'):self.port.load_page(self.plan,0,self.args)
        self.query('UPDATE history_knowledge_chunks SET row_keys=?',(json.dumps([['x'*9000]]),))
        with self.assertRaisesRegex(M.ImportError,'knowledge_receipt_metadata_budget'):self.port.load_page(self.plan,0,self.args)


class OperatorProof(fixture.Stream):
    test_stream_128_boundary_preserves_nullable_keys_and_max_integer=None
    test_plan_preserves_absence_exhaustion_and_rejects_fake_empty_table=None
    test_oversize_wrong_storage_and_missing_table_fail_without_completion=None
    def test_completion_requires_runner_exit_proof_and_current_generation(self):
        path=Path(os.environ['B4_NATIVE_OPERATOR']) if os.environ.get('B4_NATIVE_OPERATOR') else test_knowledge.ROOT/'integration/merged_operator.py'
        spec=importlib.util.spec_from_file_location('evolvmem.history_migration._b4_operator',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=SimpleNamespace(work_dir=self.temp.name,binding_hash='b'*64,source=SimpleNamespace(receipt_sha256='c'*64))
        kernels=SimpleNamespace(ready=lambda *a:True)
        operator=module.Operator(config,kernels=kernels);(Path(self.temp.name)/'plans').mkdir()
        plan=SimpleNamespace(sha256='a'*64)
        progress=dict(stage='verified',generation=3,verify_generation=3)
        self.assertFalse(operator._knowledge_proof(plan,progress=progress))
        with self.assertRaises(M.ImportError):operator._knowledge_proof(plan,dict(target_domain_verified=True),progress=progress)
        result=dict(knowledge_domain_complete=True,target_domain_verified=True,source_phase_complete=True,source_hash_passes=2,manifest_sha256=plan.sha256)
        self.assertTrue(operator._knowledge_proof(plan,result,progress=progress))
        with self.assertRaises(M.ImportError):operator._knowledge_proof(plan,progress=dict(progress,generation=4,verify_generation=4))
        args=module.parser().parse_args(['--config','test.json','status','--domain','knowledge'])
        self.assertEqual(args.domain,'knowledge')
        self.assertEqual(module.PARENTS['governance'],('core','sources','organization'))
        governance=module.parser().parse_args(['--config','test.json','status','--domain','governance'])
        self.assertEqual(governance.domain,'governance')
        self.assertFalse(operator._governance_proof(plan,progress=progress))

if __name__=='__main__':unittest.main()
