"""Operator completion proof and metadata guards; no PostgreSQL runtime claims."""
import test_continuity
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from test_stream import Fixture,M

class Proof(Fixture):
    def test_cli_counts_escrow_and_proof_binds_source_exit_and_generation(self):
        path=Path(os.environ['B6_NATIVE_OPERATOR']) if os.environ.get('B6_NATIVE_OPERATOR') else test_continuity.ROOT/'integration/operator.py'
        spec=importlib.util.spec_from_file_location('evolvmem.history_migration._b6_operator',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=SimpleNamespace(work_dir=self.temp.name,binding_hash='b'*64,source=SimpleNamespace(receipt_sha256='c'*64))
        operator=module.Operator(config,kernels=module.Kernels());(Path(self.temp.name)/'plans').mkdir()
        plan=SimpleNamespace(sha256='a'*64,page_count=9,counts={str(i):0 for i in range(8)})
        p=dict(stage='verified',generation=3,verify_generation=3,count_table=9,next_verify=9)
        self.assertEqual(module.PARENTS['continuity'],('core',))
        self.assertEqual(module.PARENTS['knowledge'],('core','sources'))
        self.assertEqual(module.PARENTS['governance'],('core','sources','organization'))
        self.assertFalse(module.Kernels().ready('continuity',plan,dict(p,count_table=8)))
        self.assertTrue(module.Kernels().ready('continuity',plan,p))
        self.assertFalse(operator._continuity_proof(plan,progress=p))
        with self.assertRaises(M.ImportError):operator._continuity_proof(plan,dict(target_domain_verified=True),progress=p)
        result=dict(continuity_domain_complete=True,target_domain_verified=True,source_phase_complete=True,source_hash_passes=2,manifest_sha256=plan.sha256)
        self.assertTrue(operator._continuity_proof(plan,result,progress=p))
        with self.assertRaises(M.ImportError):operator._continuity_proof(plan,progress=dict(p,generation=4,verify_generation=4))
        self.assertEqual(module.parser().parse_args(['--config','test.json','status','--domain','continuity']).domain,'continuity')
    def test_parent_guards_refuse_large_metadata_and_conditional_triggers(self):
        from evolvmem.history_migration.continuity_parent import checked_progress,check_parent_trigger_qualifications,WATCHED_TABLES
        class Connection:
            def __init__(self,answer):self.answer=answer;self.calls=[]
            def execute(self,sql,args=()):self.calls.append(sql);return self
            def fetchall(self):return self.answer
        bad=Connection([(16*1024*1024+1,True)])
        with self.assertRaisesRegex(M.ImportError,'parent_manifest_budget'):
            checked_progress(bad,SimpleNamespace(),SimpleNamespace(run_id='fixture'))
        self.assertEqual(len(bad.calls),1)
        good=[(t,True,True,True,True) for t in sorted(WATCHED_TABLES)]
        check_parent_trigger_qualifications(Connection(good))
        damaged=list(good);damaged[0]=(damaged[0][0],False,True,True,True)
        with self.assertRaisesRegex(M.ImportError,'generation_qualification'):check_parent_trigger_qualifications(Connection(damaged))

if __name__=='__main__':unittest.main()
