import test_operations
from contextlib import closing
import importlib.util,json,os
from pathlib import Path
import sqlite3,unittest
from types import SimpleNamespace
from test_stream import Fixture,source,parent,TARGET,RUN,N,P,M
from sql_port import Connection
from evolvmem.history_migration.operations_importer import OperationsImporter

class Effects(Fixture):
    def build(self,namespace='owner'):
        self.ref=source(self.temp.name,namespace)
        with N.Phase(self.ref) as phase:previous=parent(phase.binding)
        self.plan=P.plan_source(self.ref,previous,TARGET,Path(self.temp.name)/'plan.json',run_id=RUN)
        with N.Phase(self.ref,plan=self.plan) as phase:self.args=[phase.read(self.plan.page(i)) for i in range(self.plan.page_count)]
        self.target=Path(self.temp.name)/'target-effects.db';self.sql=[];self.fault=None;self.parent_valid=True
        with closing(sqlite3.connect(self.target)) as c:
            for table in M.TABLES:
                fields=['library_id TEXT NOT NULL','project_id TEXT','source_rowid INTEGER NOT NULL']
                fields.extend(M.quote(n)+' '+kind+('' if nullable else ' NOT NULL') for n,kind,nullable in M.SPECS[table])
                c.execute('CREATE TABLE '+M.target_table(table)+'('+','.join(fields)+',PRIMARY KEY(library_id,source_rowid))')
            c.execute('CREATE TABLE history_operations_runs(run_id TEXT PRIMARY KEY,library_id TEXT UNIQUE,parent_run_id TEXT,manifest_hash TEXT,manifest TEXT,loaded_counts TEXT,loaded_bytes INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE history_operations_chunks(run_id TEXT,table_code TEXT,chunk_key TEXT,input_hash TEXT,row_keys TEXT,row_count INTEGER,input_bytes INTEGER,PRIMARY KEY(run_id,table_code,chunk_key))')
            c.execute('CREATE TABLE history_operations_progress(run_id TEXT PRIMARY KEY,manifest_hash TEXT,stage TEXT,next_load INTEGER DEFAULT 0,next_verify INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,verify_generation INTEGER,count_table INTEGER DEFAULT 0,count_rows INTEGER DEFAULT 0,count_last TEXT)')
            for table in M.TARGET_TABLES+('history_operations_chunks',):
                for event in ('INSERT','UPDATE','DELETE'):
                    c.execute('CREATE TRIGGER '+table+'_'+event+' AFTER '+event+' ON '+table+' BEGIN UPDATE history_operations_progress SET generation=generation+1; END')
            c.commit()
        env=self
        class Port(OperationsImporter):
            def _gate(self,c):pass # PG catalogs/RLS/locks deliberately not simulated.
            def _parent(self,c,plan):
                M.check(env.parent_valid,'verified_source_parent_required')
                M.check(type(plan) is P.Plan,'trusted_operations_plan_required');plan.require_importable()
        self.port=Port(lambda:Connection(self),TARGET,capacity_guard=lambda:None);self.port.begin(self.plan)
    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.target)) as c:
            rows=c.execute(sql,args).fetchall();c.commit();return rows
    def advance_to_blob(self):
        self.index=next(i for i in range(self.plan.page_count) if self.plan.page(i)['table']=='organization_review_vectors')
        for i in range(self.index):self.port.load_page(self.plan,i,self.args[i])
    def test_atomic_failure_rolls_back_bytes_receipt_and_frontier(self):
        self.build();self.advance_to_blob();self.fault='before_receipt'
        with self.assertRaisesRegex(M.ImportError,'import_transaction_failed'):self.port.load_page(self.plan,self.index,self.args[self.index])
        self.assertEqual(self.query('SELECT count(*) FROM organization_review_vectors'),[(0,)])
        self.assertEqual(self.query('SELECT next_load FROM history_operations_progress'),[(self.index,)])
        self.assertEqual(self.query("SELECT count(*) FROM history_operations_chunks WHERE table_code='organization_review_vectors'"),[(0,)])
    def test_unknown_commit_explicit_resume_and_byte_fidelity(self):
        self.build();self.advance_to_blob();self.fault='after_commit'
        with self.assertRaisesRegex(M.ImportError,'commit_outcome_unknown'):self.port.load_page(self.plan,self.index,self.args[self.index])
        self.fault=None
        self.assertTrue(self.port.load_page(self.plan,self.index,self.args[self.index])['replayed'])
        self.assertEqual(self.query('SELECT count(*),count(cache_key) FROM organization_review_vectors'),[(128,0)])
        self.assertEqual(self.query('SELECT vector FROM organization_review_vectors WHERE source_rowid=?',(M.INT64_MIN,)),[(b'\x00\xff\x80\x00\x00',)])
        self.assertEqual(self.query('SELECT generation FROM context_health_generation'),[(92,)])
        self.assertEqual(self.query('SELECT revision FROM lan_memory_revision'),[(123,)])
        self.assertEqual(self.query('SELECT result FROM history_lan_write_requests'),[(' {"x":1,"x":2} ',)])
    def test_blob_budget_precedes_fetch_and_tamper_changes_hash(self):
        self.build();self.advance_to_blob();self.port.load_page(self.plan,self.index,self.args[self.index])
        self.query('UPDATE organization_review_vectors SET vector=? WHERE source_rowid=1',(b'changed',))
        with self.assertRaisesRegex(M.ImportError,'operations_target_digest_changed'):self.port.load_page(self.plan,self.index,self.args[self.index])
        self.query('UPDATE organization_review_vectors SET vector=? WHERE source_rowid=1',(bytes(M.MAX_BYTES+1),))
        self.sql=[]
        with self.assertRaisesRegex(M.ImportError,'operations_target_readback_budget'):self.port.load_page(self.plan,self.index,self.args[self.index])
        self.assertFalse(any(s.startswith('SELECT "source_rowid","cache_key","vector"') for s in self.sql))
        self.query('UPDATE history_operations_chunks SET row_keys=? WHERE table_code=?',(json.dumps([['x'*9000]]),'organization_review_vectors'))
        with self.assertRaisesRegex(M.ImportError,'operations_receipt_metadata_budget'):self.port.load_page(self.plan,self.index,self.args[self.index])
    def test_frontier_parent_and_generation_fail_closed(self):
        self.build()
        with self.assertRaisesRegex(M.ImportError,'operations_load_frontier_conflict'):self.port.load_page(self.plan,1,self.args[1])
        self.parent_valid=False
        with self.assertRaisesRegex(M.ImportError,'verified_source_parent_required'):self.port.status(self.plan)
        self.parent_valid=True
        for i,args in enumerate(self.args):self.port.load_page(self.plan,i,args)
        self.query('UPDATE context_health_generation SET generation=generation+1')
        with self.assertRaisesRegex(M.ImportError,'operations_verification_generation_changed'):self.port.verify_page(self.plan,0)
    def test_public_finalization_and_sealed_runner_proof(self):
        self.build('public') # No auto sequences; no PG sequence model needed.
        from evolvmem.history_migration.run_operations_batch import run_batch
        report=run_batch(self.ref,self.plan,self.port,Path(self.temp.name)/'receipt.json',max_steps=32)
        self.assertTrue(report['operations_domain_complete']);self.assertTrue(report['target_domain_verified'])
        self.assertTrue(report['source_phase_complete']);self.assertEqual(report['source_hash_passes'],2)
        self.assertFalse(report['history_complete']);self.assertFalse(report['route_activated'])
        self.assertEqual(self.query('SELECT context_id,author,active,title,summary,project FROM lan_publications'),[(8,'jiangli',0,'old-title','raw-summary','original-project')])

class OperatorProof(Fixture):
    def test_completion_proof_requires_exit_binding_and_current_generation(self):
        path=Path(os.environ.get('OPERATIONS_NATIVE_OPERATOR',str(test_operations.ROOT/'integration/operator.py')))
        spec=importlib.util.spec_from_file_location('evolvmem.history_migration._b78_operator',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=SimpleNamespace(work_dir=self.temp.name,binding_hash='b'*64,source=SimpleNamespace(receipt_sha256='c'*64))
        operator=module.Operator(config,kernels=SimpleNamespace(ready=lambda *a:True));(Path(self.temp.name)/'plans').mkdir()
        plan=SimpleNamespace(sha256='a'*64);progress=dict(stage='verified',generation=3,verify_generation=3)
        self.assertFalse(operator._operations_proof(plan,progress=progress))
        with self.assertRaises(M.ImportError):operator._operations_proof(plan,dict(target_domain_verified=True),progress=progress)
        result=dict(operations_domain_complete=True,target_domain_verified=True,source_phase_complete=True,source_hash_passes=2,manifest_sha256=plan.sha256)
        self.assertTrue(operator._operations_proof(plan,result,progress=progress))
        with self.assertRaises(M.ImportError):operator._operations_proof(plan,progress=dict(progress,generation=4,verify_generation=4))
        self.assertEqual(module.parser().parse_args(['--config','fixture.json','status','--domain','operations']).domain,'operations')
        self.assertEqual(module.PARENTS['operations'],('core','sources'))
        self.assertEqual(module.PARENTS['governance'],('core','sources','organization'))

if __name__=='__main__':unittest.main()
