import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.pg_context_vector_read.vector_fixture import VectorFixture
from tests.pg_context_read_slice.tests.driver_port import L,TOKEN
from evolvmem.pg_vector_port.runtime import BorrowedEmbeddingProvider


class ContractTests(unittest.TestCase):
    def test_same_readonly_callable_acceptance_runs_actual_application_chain(self):
        f=VectorFixture();self.addCleanup(f.close)
        path=Path(__file__).parent/'fixture/contract.py'
        spec=importlib.util.spec_from_file_location('vector_read_contract',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for i,key in enumerate(module.KEYS):
            f.seed(key,'A synthetic ordinary reference '+str(i),component=.9-i*.2)
        with f.host.request_scope():
            identity=f.host.authenticate(TOKEN);state=f.host._state()
            bound=SimpleNamespace(manager=state.manager,config=f.config,deadline_guard=state.guard,
                guard=lambda:None,new_request=state.guard.enforce,
                route=lambda tool:state.router.plan(identity,tool,library_id=L))
            result=module.exercise(bound,contract=f.contract,
                embedding_provider=BorrowedEmbeddingProvider(f.engine),clock=lambda:f.now)
        self.assertTrue(result['generated_candidates'])
        self.assertTrue(result['two_snapshot_projection_and_body_proof'])
        self.assertFalse(result['complete_hybrid'])
        self.assertTrue(all(c.closed for c in f.port.connections))

    def test_candidate_and_first_body_proof_share_actual_readonly_connection(self):
        f=VectorFixture();self.addCleanup(f.close)
        f.seed('snapshot','A synthetic snapshot reference.')
        result=f.search();self.assertTrue(result)
        ann=[raw for raw,sql,_ in f.port.calls if 'ORDER BY embedding <=>' in sql]
        self.assertEqual(1,len(ann))
        proof=[raw for raw,sql,_ in f.port.calls if 'encode(sha256(public.vector_send' in sql]
        self.assertEqual(2,len(proof))
        self.assertIs(ann[0],proof[0]);self.assertIsNot(proof[0],proof[1])
        body=[raw for raw,sql,_ in f.port.calls if 'SELECT to_jsonb(c)' in sql and 'context_items' in sql]
        self.assertIn(proof[0],body);self.assertIn(proof[1],body)
        self.assertTrue(all(raw.read_only and raw.closed for raw in proof))


if __name__=='__main__':unittest.main()
