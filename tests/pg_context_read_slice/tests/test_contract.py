from pathlib import Path
from types import SimpleNamespace
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from tests.pg_context_read_slice.tests.fixture import Fixture
from tests.pg_context_read_slice.tests.driver_port import L,TOKEN
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_business_core import recall_store


class ContractTests(unittest.TestCase):
    def test_same_callable_contract_runs_with_actual_chain_and_explicit_driver_port(self):
        f=Fixture();self.addCleanup(f.close)
        path=Path(__file__).parents[1]/'fixture/contract.py'
        spec=importlib.util.spec_from_file_location('slice_shared_contract',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with f.host.request_scope():
            identity=f.host.authenticate(TOKEN);state=f.host._state()
            bound=SimpleNamespace(manager=state.manager,config=f.config,deadline_guard=state.guard,
                guard=lambda:None,new_request=state.guard.enforce,
                route=lambda tool:state.router.plan(identity,tool,library_id=L))
            with tempfile.TemporaryDirectory() as directory:
                result=module.exercise(bound,model_contract='synthetic-reviewed-contract',oracle_directory=directory)
            self.assertTrue(result['typed_dto_exact']);self.assertFalse(result['postgres_fts_tested'])
        with self.assertRaisesRegex(RuntimeError,'synthetic_slice_not_fresh'):
            with f.host.request_scope():
                identity=f.host.authenticate(TOKEN);state=f.host._state()
                bound=SimpleNamespace(manager=state.manager,config=f.config,deadline_guard=state.guard,
                    guard=lambda:None,new_request=state.guard.enforce,
                    route=lambda tool:state.router.plan(identity,tool,library_id=L))
                with tempfile.TemporaryDirectory() as directory:
                    module.exercise(bound,model_contract='synthetic-reviewed-contract',oracle_directory=directory)

    def test_oversized_proof_rejects_before_json_serialization(self):
        with patch.object(recall_store,'MAX_BYTES',100),patch.object(recall_store.json,'dumps',side_effect=AssertionError('must not serialize')):
            with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):
                recall_store._proof({'body':'x'*101})

    def test_cumulative_retention_cap_cannot_return_partial_results(self):
        f=Fixture();self.addCleanup(f.close)
        f.add('budget-ref','Synthetic reference: bounded complete dependencies.')
        f.mirror();batch=f.candidates(f.request())
        published=[]
        with patch.object(recall_store,'MAX_BYTES',100):
            with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):
                f.recall(batch,publish=published.append)
        self.assertEqual([],published)


if __name__=='__main__':unittest.main()
