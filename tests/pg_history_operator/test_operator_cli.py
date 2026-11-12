"""Focused operator tests. Fake kernel port is not PostgreSQL evidence."""
import importlib.util
import json
import hashlib
import io
import os
import sqlite3
import subprocess
from contextlib import contextmanager,redirect_stdout
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'candidate').is_dir():sys.path.insert(0,str(ROOT/'candidate'))
SCRATCH=Path(os.environ['EVOLVMEM_TEST_TMP_ROOT']) if os.environ.get('EVOLVMEM_TEST_TMP_ROOT') else (
    Path('D:/EvolvMem-test-tmp/operator-cli-20261101') if os.name=='nt' else Path(tempfile.gettempdir())/'evolvmem-operator-cli-tests')
SCRATCH.mkdir(mode=0o700,parents=True,exist_ok=True)


class OperatorTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.find_spec('evolvmem.history_migration.operator')
        self.assertIsNotNone(spec,'bounded private operator entry point is missing')
        from evolvmem.history_migration import operator as O
        self.O=O
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH);self.addCleanup(self.tmp.cleanup)
        self.config=SimpleNamespace(binding_hash='a'*64,work_dir=Path(self.tmp.name),migration_id='00000000-0000-0000-0000-000000000001',
            source=SimpleNamespace(receipt_sha256='b'*64),command_seconds=30,project_mapping=())
        self.privacy=patch.object(O,'require_private_directory');self.privacy.start();self.addCleanup(self.privacy.stop)
        self.kernel=Kernel()
        self.op=O.Operator(self.config,kernels=self.kernel)

    def test_plan_dependencies_and_config_drift_fail_closed(self):
        with self.assertRaisesRegex(Exception,'parent_plan_required'):self.op.execute('plan','sources')
        self.op.execute('plan','core');self.op.execute('plan','sources')
        self.assertEqual(['core','sources'],self.kernel.planned)
        self.config.binding_hash='c'*64
        with self.assertRaisesRegex(Exception,'operator_plan_binding_changed'):self.op.execute('step','sources')
        self.assertEqual([],self.kernel.steps)

    def test_prepare_is_explicitly_one_domain(self):
        with patch.object(self.kernel,'prepare',create=True,return_value={'prepared':True}) as prepare:
            self.op.execute('prepare','sources')
            self.assertEqual((self.config,'sources'),prepare.call_args.args)

    def test_steps_are_bounded_one_domain_and_attempts_never_overwrite(self):
        self.op.execute('plan','core')
        for invalid in (0,33,True):
            with self.assertRaisesRegex(Exception,'operator_step_budget'):self.op.execute('step','core',max_steps=invalid)
        first=self.op.execute('step','core',max_steps=2);second=self.op.execute('step','core',max_steps=1)
        self.assertNotEqual(first['attempt_id'],second['attempt_id'])
        self.assertEqual([('core',2),('core',1)],self.kernel.steps)
        self.assertEqual(6,len(list((self.config.work_dir/'attempts').glob('*.json'))))

    def test_unknown_failure_stops_once_and_durable_failure_has_no_secret(self):
        self.op.execute('plan','core');self.kernel.failure=RuntimeError('password=TOP_SECRET SQL select raw_history')
        with self.assertRaisesRegex(Exception,'operator_failed'):self.op.execute('step','core')
        self.assertEqual(1,len(self.kernel.steps))
        text=''.join(p.read_text() for p in (self.config.work_dir/'attempts').glob('*.json'))
        self.assertNotIn('TOP_SECRET',text);self.assertIn('"automatic_retry":false',text)
        self.assertEqual(1,len(list((self.config.work_dir/'attempts').glob('*.failure.json'))))

    def test_lock_is_exclusive_and_incomplete_finalize_does_not_dispatch(self):
        self.op.execute('plan','core')
        with self.O.job_lock(self.config.work_dir):
            with self.assertRaisesRegex(Exception,'operator_busy'):self.op.execute('status','core')
        result=self.op.execute('finalize','core')
        self.assertFalse(result['domain_complete']);self.assertFalse(result['history_complete']);self.assertFalse(result['route_activated'])
        self.assertEqual(0,self.kernel.finalized)

    def test_interrupt_keeps_attempt_and_never_retries(self):
        self.op.execute('plan','core');self.kernel.failure=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.op.execute('step','core')
        self.assertEqual(1,len(self.kernel.steps))
        self.assertEqual(1,len(list((self.config.work_dir/'attempts').glob('*.failure.json'))))

    def test_failed_plan_can_resume_explicitly_without_overwriting_orphan(self):
        original=self.kernel.plan
        def failed(domain,c,parents,path,run_id,seconds):
            Path(str(path)+'.started').write_text('{}')
            raise KeyboardInterrupt()
        with patch.object(self.kernel,'plan',side_effect=failed):
            with self.assertRaises(KeyboardInterrupt):self.op.execute('plan','core')
        orphan=next((self.config.work_dir/'plans').glob('*.started'))
        result=self.op.execute('plan','core')
        self.assertEqual('planned',result['state']);self.assertEqual('{}',orphan.read_text())
        metadata=json.loads((self.config.work_dir/'plans/core.json').read_bytes())
        self.assertIn('manifest_file',metadata,'plans must bind a unique attempt manifest')
        self.assertNotEqual(str(orphan)[:-8],str(self.config.work_dir/'plans'/metadata['manifest_file']))

    def test_supervisor_deadline_preserves_started_and_records_unknown_without_retry(self):
        self.op.execute('plan','core')
        started=self.config.work_dir/'attempts'/('e'*32+'.started.json')
        started.write_text('{}')
        from evolvmem.history_migration import operator_config
        output=io.StringIO()
        with patch.object(operator_config,'load_config',return_value=self.config),patch.object(self.O.subprocess,'run',
                side_effect=subprocess.TimeoutExpired('worker',30)) as runner,redirect_stdout(output):
            code=self.O.main(['--config','private.json','step','--domain','core'])
        self.assertEqual(2,code);self.assertEqual(1,runner.call_count)
        self.assertEqual('{}',started.read_text())
        failure=started.with_name('e'*32+'.failure.json')
        self.assertEqual('operator_deadline',json.loads(failure.read_bytes())['code'])
        report=json.loads(output.getvalue());self.assertFalse(report['route_activated']);self.assertFalse(report['automatic_retry'])


class Kernel:
    def __init__(self):self.planned=[];self.steps=[];self.failure=None;self.finalized=0
    def plan(self,domain,config,parents,path,run_id,seconds):
        self.planned.append(domain);path.write_text('{}');return SimpleNamespace(sha256='d'*64,run_id=run_id)
    def load(self,domain,path,sha):return SimpleNamespace(sha256=sha)
    def status(self,domain,config,plan):return dict(stage='loading',next_load=0)
    def step(self,domain,config,plan,path,max_steps,seconds):
        self.steps.append((domain,max_steps))
        if self.failure:raise self.failure
        return dict(state='loading')
    def finalize(self,*args):self.finalized+=1;return {}
    def ready(self,domain,plan,status):return False


class AdapterTests(unittest.TestCase):
    def test_organization_one_phase_dynamic_frontiers_and_closed_finalize(self):
        from evolvmem.history_migration import operator as O
        kernel=O.Kernels();events=[]
        plan=SimpleNamespace(document={'pages':[{}]*3,'counts':{'synthetic_a':0,'synthetic_b':0}})
        class Port:
            active=False
            @contextmanager
            def batch(self,*args,**kw):
                self.active=True;events.append('enter')
                try:yield self
                finally:self.active=False;events.append('exit')
            def begin(self,**kw):return dict(stage='verifying',next_verify=2)
            def verify_page(self,index,**kw):
                events.append(('verify',index));return dict(stage='counting',count_table=0,next_verify=3)
            def count_page(self,**kw):
                i=sum(isinstance(e,tuple) and e[0]=='count' for e in events)
                events.append(('count',i));return dict(stage='counting',count_table=i+1,next_verify=3)
            def finalize(self,*args,**kw):
                self_test.assertFalse(self.active);events.append('finalize');return {'state':'organization_domain_verified'}
        self_test=self;port=Port()
        with patch.object(kernel,'port',return_value=port):
            result=kernel.step('organization',SimpleNamespace(source=object()),plan,Path('unused'),3,10)
        self.assertEqual(['enter',('verify',2),('count',0),('count',1),'exit','finalize'],events)
        self.assertTrue(result['domain_complete'])

    def test_real_tiny_sqlite_core_plan_keeps_source_and_never_opens_runtime(self):
        from evolvmem.history_migration import operator as O,static_wal as W
        from evolvmem.history_migration.history_core import mapping as M
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            directory=Path(name);path=directory/'synthetic.db'
            schema=json.loads((Path(W.__file__).parent/'resources/core_reviewed_schema.json').read_bytes())['namespaces']['owner']
            c=sqlite3.connect(path)
            try:
                c.execute('PRAGMA journal_mode=WAL')
                for table in M.TABLES:c.execute(schema[table]['ddl'])
                # Synthetic first-seal fixture only; no real retained source is altered.
                for table in ('memories','context_items','context_layers'):
                    c.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(table,0))
                c.commit()
            finally:c.close()
            path.chmod(0o400)
            Path(str(path)+'-wal').write_bytes(b'');Path(str(path)+'-shm').write_bytes(bytes(32768))
            files={}
            for key,p in [('db',path),('wal',Path(str(path)+'-wal')),('shm',Path(str(path)+'-shm'))]:
                s=p.stat();files[key]=dict(stat={k:getattr(s,'st_'+k) for k in W.STAT_FIELDS},sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            raw=M.canonical(dict(format='controlled-retained-empty-wal-v1',namespace='owner',path=str(path.resolve()),
                basis='controlled_retained_copy_no_writers',os_immutable_proven=False,retention_evidence_sha256='e'*64,files=files))
            source=W.SealedWalSource.from_receipt(raw,hashlib.sha256(raw).hexdigest())
            before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
            kernel=O.Kernels()
            with patch.object(W,'_active_identities',return_value={(999999,999999)}),patch.object(kernel,'runtime',side_effect=AssertionError('planning must stay offline')):
                plan=kernel.plan('core',SimpleNamespace(source=source,project_mapping=()),{},directory/'core.json','00000000-0000-0000-0000-000000000021',10)
            self.assertEqual(dict.fromkeys(M.TABLES,0),plan.counts)
            for filename,digest in before.items():self.assertEqual(digest,hashlib.sha256((directory/filename).read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
