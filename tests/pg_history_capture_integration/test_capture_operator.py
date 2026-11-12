"""New capture operator behavior only; fake kernel is not real PG evidence."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'candidate').exists():sys.path.insert(0,str(ROOT/'candidate'))
from evolvmem.history_migration import operator as O,operator_scope as S
SCRATCH=Path(os.environ.get('EVOLVMEM_TEST_TMP_ROOT', 'D:/EvolvMem-test-tmp/capture-operator-20261101' if os.name=='nt' else str(Path(tempfile.gettempdir())/'capture-operator-tests')))
SCRATCH.mkdir(mode=0o700,parents=True,exist_ok=True)


class Kernel:
    def __init__(self):
        self.generation=10
        self.result={'state':'capture_domain_verified','capture_domain_complete':True,'target_domain_verified':True,'source_phase_complete':True,'source_hash_passes':2,'manifest_sha256':'d'*64}
    def plan(self,domain,c,parents,path,run_id,seconds):path.write_text('{}');return SimpleNamespace(sha256='d'*64)
    def load(self,domain,path,sha,**kw):return SimpleNamespace(sha256=sha)
    def status(self,*args):return {'stage':'verified','count_table':3,'next_verify':5,'generation':self.generation,'verify_generation':self.generation}
    def ready(self,*args):return True
    def finalize(self,*args,**kw):return dict(self.result)
    def step(self,*args):return dict(self.result)


class CaptureOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH);self.addCleanup(self.tmp.cleanup)
        self.c=SimpleNamespace(work_dir=Path(self.tmp.name),binding_hash='a'*64,source=SimpleNamespace(receipt_sha256='b'*64),migration_id='00000000-0000-0000-0000-000000000001',command_seconds=30)
        self.privacy=patch.object(O,'require_private_directory');self.privacy.start();self.addCleanup(self.privacy.stop)
        self.k=Kernel();self.op=O.Operator(self.c,kernels=self.k)
        self.op.execute('plan','core');self.op.execute('plan','sources')

    def test_capture_uses_core_sources_parents_organization_stays_sibling(self):
        self.op.execute('plan','capture');self.op.execute('plan','organization')
        cap=json.loads((self.c.work_dir/'plans/capture.json').read_bytes())
        org=json.loads((self.c.work_dir/'plans/organization.json').read_bytes())
        self.assertEqual({'core':'d'*64,'sources':'d'*64},cap['parents'])
        self.assertEqual(cap['parents'],org['parents'])
        self.assertEqual(1,org['version'])

    def test_target_verified_alone_does_not_report_source_completed(self):
        self.op.execute('plan','capture')
        self.assertFalse(self.op.execute('status','capture')['domain_complete'])
        self.k.result['source_phase_complete']=False
        with self.assertRaisesRegex(Exception,'capture_source_completion_required'):self.op.execute('finalize','capture')
        self.assertFalse(self.op.execute('status','capture')['domain_complete'])

    def test_finalize_publishes_source_proof_only_after_successful_runner(self):
        self.op.execute('plan','capture')
        result=self.op.execute('finalize','capture')
        self.assertTrue(result['domain_complete']);self.assertTrue(result['capture_domain_complete'])
        self.assertTrue(self.op.execute('status','capture')['domain_complete'])
        self.assertFalse(result['history_complete']);self.assertFalse(result['route_activated'])
        self.assertFalse(result['production_migration_proven']);self.assertFalse(result['automatic_retry'])
        proof=json.loads((self.c.work_dir/'plans'/('capture-'+'d'*64+'.proof.json')).read_bytes())
        self.assertEqual(('a'*64,'b'*64,'d'*64,10),(proof['config_binding'],proof['source_seal'],proof['manifest_sha256'],proof['target_generation']))

    def test_incomplete_step_does_not_publish_completion_proof(self):
        self.op.execute('plan','capture');self.k.result.update(capture_domain_complete=False,state='loading')
        self.assertFalse(self.op.execute('step','capture',max_steps=1)['domain_complete'])
        self.assertFalse(self.op.execute('status','capture')['domain_complete'])

    def test_completed_proof_refuses_a_different_target_generation(self):
        self.op.execute('plan','capture');self.op.execute('finalize','capture')
        self.k.generation+=1
        with self.assertRaisesRegex(Exception,'capture_completion_proof_changed'):self.op.execute('status','capture')


class CapturePrepareTests(unittest.TestCase):
    def test_prepare_selects_only_capture_sql_and_preflight_reports_it(self):
        r=S.OperatorRuntime.__new__(S.OperatorRuntime)
        r.config=SimpleNamespace(source=SimpleNamespace(namespace='owner'))
        r.target=SimpleNamespace(library_id='00000000-0000-0000-0000-000000000001')
        r._open=lambda *_:None;r.factory=lambda:None;r._content=lambda *_:None
        r._schema_ready=lambda c,index:True
        class Cursor:
            raw=None
            def execute(self,*_):return self
            def fetchall(self):return [(True,)]
        @contextmanager
        def tx(_):yield Cursor()
        with patch.object(S,'transaction',tx),patch.object(r,'_capture_parent_variables',create=True,return_value={'parent_run_id':'run','parent_manifest_sha256':'d'*64}):
            prepared=r.prepare('capture');ready=r.preflight()
        self.assertEqual('capture',prepared['domain']);self.assertEqual([],prepared['applied'])
        self.assertTrue(ready['domains']['capture']);self.assertEqual(7,len(S.SCRIPTS))
        self.assertEqual(('001_history_capture.sql','history_capture_schema_migrations'),S.SCRIPTS[-1])
        r.config.source.namespace='public'
        with patch.object(S,'transaction',tx):public=r.preflight()
        self.assertTrue(public['domains']['core']);self.assertFalse(public['domains']['capture'])
        self.assertEqual('not_applicable',public['capture_applicability'])

    def test_capture_prepare_requires_parent_before_any_database_effect(self):
        r=S.OperatorRuntime.__new__(S.OperatorRuntime)
        r.config=SimpleNamespace(source=SimpleNamespace(namespace='owner'))
        with patch.object(S,'transaction',side_effect=AssertionError('no DB before parent validation')):
            with self.assertRaisesRegex(Exception,'trusted_capture_parent_plan_required'):r.prepare('capture')

    def test_public_capture_prepare_is_not_applicable_before_any_database_effect(self):
        r=S.OperatorRuntime.__new__(S.OperatorRuntime)
        r.config=SimpleNamespace(source=SimpleNamespace(namespace='public'))
        with patch.object(S,'transaction',side_effect=AssertionError('no DB for public capture')):
            with self.assertRaisesRegex(Exception,'capture_namespace_not_applicable'):r.prepare('capture')

    def test_public_cli_prepare_does_not_even_load_parent_plans(self):
        kernel=O.Kernels();config=SimpleNamespace(source=SimpleNamespace(namespace='public'),work_dir=Path('not-opened'))
        with patch.object(O.Operator,'_load',side_effect=AssertionError('public capture must refuse first')):
            with self.assertRaisesRegex(Exception,'capture_namespace_not_applicable'):kernel.prepare(config,'capture')

    def test_capture_prepare_rejects_parent_from_different_source_seal(self):
        from evolvmem.history_migration.source_plan import Plan
        parent=Plan.__new__(Plan)
        parent._header=O.M.canonical(dict(highwaters_ready=True,source={'seal_receipt_sha256':'c'*64}))
        r=S.OperatorRuntime.__new__(S.OperatorRuntime)
        r.config=SimpleNamespace(source=SimpleNamespace(namespace='owner',receipt_sha256='b'*64))
        with patch.object(S,'transaction',side_effect=AssertionError('no DB for mismatched source')):
            with self.assertRaisesRegex(Exception,'capture_parent_seal_mismatch'):r.prepare('capture',parent_plan=parent)


class CaptureAdapterTests(unittest.TestCase):
    def test_real_runner_finalizes_only_after_source_exit_and_never_on_exit_failure(self):
        from evolvmem.history_migration import capture_importer as I,run_capture_batch as R
        events=[]
        class Plan:
            sha256='d'*64
            def require_importable(self):pass
        plan=Plan();port=I.CaptureImporter.__new__(I.CaptureImporter)
        port.factory=lambda:(_ for _ in ()).throw(AssertionError('fake port must not open DB'))
        port.begin=lambda p:dict(stage='counting',count_table=3)
        def finalize(p):
            events.append('finalize');return dict(target_domain_verified=True,capture_domain_complete=False)
        port.finalize=finalize
        class Phase:
            fail=False
            def __init__(self,*args,**kw):self.proven_complete=False;self.hash_passes=2
            def tick(self):pass
            def __enter__(self):events.append('source-enter');return self
            def __exit__(self,*args):
                events.append('source-exit')
                if self.fail:raise O.M.ImportError('capture_phase_incomplete')
                self.proven_complete=True
        kernel=O.Kernels()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory,patch.object(kernel,'port',return_value=port),patch.object(R,'Plan',Plan),patch.object(R,'Phase',Phase):
            report=kernel.finalize('capture',SimpleNamespace(source=object()),plan,10,receipt_path=Path(directory)/'success.json')
            self.assertEqual(['source-enter','source-exit','finalize'],events)
            self.assertTrue(report['capture_domain_complete']);self.assertTrue(report['source_phase_complete'])
            events.clear();Phase.fail=True
            with self.assertRaisesRegex(Exception,'capture_phase_incomplete'):
                kernel.finalize('capture',SimpleNamespace(source=object()),plan,10,receipt_path=Path(directory)/'failure.json')
            self.assertEqual(['source-enter','source-exit'],events)

    def test_capture_load_passes_parent_and_exact_runtime_target(self):
        from evolvmem.history_migration import capture_plan
        kernel=O.Kernels();parent=object();target=object();config=object();path=Path('not-opened')
        with patch.object(kernel,'runtime',return_value=SimpleNamespace(target=target)),patch.object(capture_plan,'load_plan',return_value=object()) as loader:
            kernel.load('capture',path,'d'*64,parents={'sources':parent},config=config)
        self.assertEqual((path,'d'*64,parent,target),loader.call_args.args)


if __name__=='__main__':unittest.main()
