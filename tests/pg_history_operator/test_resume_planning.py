"""Metadata-only planner tests; no services or real source data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from evolvmem.history_migration import resume_planning as P


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'final.json';self.now=0.;self.seen=[];self.fail_exit=False
        self.source=SimpleNamespace(sha256='a'*64,receipt_sha256='b'*64,namespace='owner')
        self.clock=patch.object(P.time,'monotonic',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
    def phase(self,seconds):
        test=self
        class Phase:
            def __enter__(s):
                s.deadline=test.now+seconds;s.binding={'seal':'same'};s.proven_complete=False
                test.now+=.1;return s
            def __exit__(s,*args):
                test.now+=.1
                if test.fail_exit:raise RuntimeError('exit_digest_failed')
                s.proven_complete=args[0] is None
        return Phase()
    def page(self,phase,table,after,ordinal):
        self.seen.append((table,after));self.now+=.15
        end=(after or 0)+1
        return dict(table=table,ordinal=ordinal,chunk_key='p'+str(ordinal).zfill(6),after=after,end=end,
            exhausted=end==20,row_count=1,native_hash='c'*64,input_hash='d'*64,input_bytes=10),{},'e'*64
    def scan(self,**kw):
        args=dict(run_id='00000000-0000-0000-0000-000000000001',domain='core',tables=('t',),count_tables=('t',),
            phase_factory=self.phase,read_page=self.page,binding={'parents':[]},seconds=1,max_rows=100,max_pages=100,max_input_bytes=10000)
        args.update(kw);return P.scan(self.source,self.path,**args)
    def test_many_phases_resume_without_repeating_pages(self):
        for _ in range(100):
            result=self.scan()
            if not isinstance(result,P.PlanningProgress):break
            self.assertFalse(self.path.exists())
        self.assertEqual({'t':20},result.counts);self.assertEqual(20,len(result.pages))
        self.assertEqual([('t',None)]+[('t',n) for n in range(1,20)],self.seen)
        self.assertNotIn('rows',json.dumps(json.loads(next(self.path.parent.glob('*.draft.json')).read_bytes())))
    def test_changed_binding_or_tampered_draft_is_rejected(self):
        self.scan()
        with self.assertRaisesRegex(Exception,'planning_binding_changed'):self.scan(binding={'parents':['changed']})
        self.source.receipt_sha256='f'*64
        with self.assertRaisesRegex(Exception,'planning_binding_changed'):self.scan()
        self.source.receipt_sha256='b'*64
        draft=next(self.path.parent.glob('*.draft.json'));d=json.loads(draft.read_bytes());d['state']['input_bytes']+=1
        draft.write_text(json.dumps(d))
        with self.assertRaisesRegex(Exception,'planning_draft_authentication'):self.scan()
    def test_exit_failure_never_advances_durable_frontier(self):
        self.scan();draft=next(self.path.parent.glob('*.draft.json'));before=draft.read_bytes()
        self.fail_exit=True
        with self.assertRaisesRegex(Exception,'exit_digest_failed'):self.scan()
        self.assertEqual(before,draft.read_bytes());self.assertFalse(self.path.exists())

    def test_insufficient_window_stops_without_durable_false_progress(self):
        with self.assertRaisesRegex(Exception,'planning_phase_no_progress'):self.scan(seconds=.3)
        self.assertEqual([],list(self.path.parent.glob('*.draft.json')))

    def test_budget_and_cas_or_metadata_drift_never_publish(self):
        with self.assertRaisesRegex(Exception,'planning_budget_exceeded'):self.scan(max_input_bytes=1)
        self.assertEqual([],list(self.path.parent.glob('*.draft.json')))
        self.scan();draft=next(self.path.parent.glob('*.draft.json'));before=draft.read_bytes()
        page=self.page
        def drift(*args):
            p,sq,sh=page(*args);return p,sq,'f'*64
        with self.assertRaisesRegex(Exception,'source_metadata_changed'):self.scan(read_page=drift)
        self.assertEqual(before,draft.read_bytes())
        def raced(*args):
            result=page(*args);draft.write_bytes(before+b' ');return result
        with self.assertRaisesRegex(Exception,'planning_draft_cas_changed'):self.scan(read_page=raced)
        self.assertEqual(before+b' ',draft.read_bytes())

    def test_core_resumed_plan_is_exact_old_plan_and_class(self):
        from evolvmem.history_migration import static_wal as W
        from evolvmem.history_migration.history_core import mapping as M
        seq={t:dict(present=True,storage_type='integer',value=20,autoincrement_ddl_verified=True,
                    allocation_history_verified=False) for t in W.S.AUTO}
        original_phase=self.phase
        def phase(source,seconds):
            phase=original_phase(seconds)
            def page(table,after,ordinal,projects):
                self.seen.append((table,after));self.now+=.15;end=(after or 0)+1
                return dict(table=table,ordinal=ordinal,chunk_key='p'+str(ordinal).zfill(6),after=after,end=end,
                    exhausted=end==3,row_count=1,native_hash='c'*64,input_hash='d'*64,input_bytes=10),None,seq,'e'*64
            phase.page=page;return phase
        rid='00000000-0000-0000-0000-000000000001'
        expected_pages=[]
        for table in M.TABLES:
            for i in range(3):
                n=len(expected_pages)
                expected_pages.append(dict(ordinal=n,table=table,chunk_key='p'+str(n).zfill(6),after=i or None,end=i+1,
                    exhausted=i==2,row_count=1,native_hash='c'*64,input_hash='d'*64,input_bytes=10))
        expected=W.Plan(dict(format='history-retained-six-v1',run_id=rid,source={'seal':'same'},source_schema_hash='e'*64,
            sequence=seq,highwaters_ready=True,project_mapping=[],counts=dict.fromkeys(M.TABLES,3),input_bytes=180,
            pages=expected_pages,complete=True,sqlite_snapshot_token_resumable=False))
        with patch.object(W,'SealedPhase',phase):
            for _ in range(50):
                result=W.plan_source(self.source,self.path,run_id=rid,seconds=1)
                if not isinstance(result,P.PlanningProgress):break
                self.assertFalse(self.path.exists())
        self.assertIs(type(result),W.Plan);self.assertEqual(expected.sha256,result.sha256)
        self.assertEqual(expected.document,result.document)
        self.assertEqual([(t,i or None) for t in M.TABLES for i in range(3)],self.seen)


class OperatorProgressTests(unittest.TestCase):
    def test_incomplete_plan_is_success_and_pointer_only_after_completion(self):
        from evolvmem.history_migration import operator as O
        with tempfile.TemporaryDirectory() as tmp:
            c=SimpleNamespace(work_dir=Path(tmp),command_seconds=60,binding_hash='a'*64,
                source=SimpleNamespace(receipt_sha256='b'*64),migration_id='00000000-0000-0000-0000-000000000001')
            class Kernel:
                calls=0
                def plan(s,domain,c,parents,path,run_id,seconds):
                    s.calls+=1
                    if s.calls==1:return P.PlanningProgress('core',4,512,1000,1,1)
                    path.write_text('{}');return SimpleNamespace(sha256='c'*64)
            with patch.object(O,'require_private_directory'):
                op=O.Operator(c,kernels=Kernel());one=op.execute('plan','core')
                self.assertEqual('planning',one['state']);self.assertFalse((Path(tmp)/'plans/core.json').exists())
                two=op.execute('plan','core');self.assertEqual('planned',two['state'])
                self.assertTrue((Path(tmp)/'plans/core.json').exists())
            self.assertEqual(2,len(list((Path(tmp)/'attempts').glob('*.success.json'))))
            self.assertEqual([],list((Path(tmp)/'attempts').glob('*.failure.json')))


if __name__=='__main__':unittest.main()
