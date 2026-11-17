"""New integration invariants only; never starts a database or reads history."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


class CompletionBoundaries(unittest.TestCase):
    def test_three_added_domains_require_source_exit_and_current_generation(self):
        expected = {'capability': ('core', 'sources'), 'continuity': ('core',), 'operations': ('core', 'sources')}
        for domain, parents in expected.items():
            with self.subTest(domain=domain), tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
                (Path(directory) / 'plans').mkdir()
                config = SimpleNamespace(work_dir=directory, binding_hash='b' * 64,
                                         source=SimpleNamespace(receipt_sha256='c' * 64))
                operator = OP.Operator(config, kernels=OP.Kernels())
                proof = getattr(operator, '_' + domain + '_proof')
                count = 9 if domain == 'continuity' else 7 if domain == 'capability' else 9
                plan = SimpleNamespace(sha256='a' * 64, page_count=3,
                                       counts={str(i): 0 for i in range(8 if domain == 'continuity' else count)})
                progress = dict(stage='verified', generation=19, verify_generation=19,
                                count_table=count, next_verify=3)
                self.assertEqual(OP.PARENTS[domain], parents)
                self.assertFalse(proof(plan, progress=progress))
                incomplete = dict(target_domain_verified=True, source_phase_complete=True,
                                  source_hash_passes=1, manifest_sha256=plan.sha256,
                                  **{domain + '_domain_complete': True})
                with self.assertRaises(OP.M.ImportError):
                    proof(plan, incomplete, progress=progress)
                self.assertFalse(list((Path(directory) / 'plans').iterdir()))
                complete = dict(incomplete, source_hash_passes=2)
                self.assertTrue(proof(plan, complete, progress=progress))
                self.assertTrue(proof(plan, progress=progress))
                with self.assertRaises(OP.M.ImportError):
                    proof(plan, progress=dict(progress, generation=20, verify_generation=20))
                self.assertEqual(OP.parser().parse_args(['--config', 'fixture', 'status', '--domain', domain]).domain, domain)

    def test_continuity_cannot_finish_before_escrow_count(self):
        plan = SimpleNamespace(page_count=3, counts={str(i): 0 for i in range(8)})
        progress = dict(stage='counting', count_table=8, next_verify=3)
        self.assertFalse(OP.Kernels().ready('continuity', plan, progress))
        self.assertTrue(OP.Kernels().ready('continuity', plan, dict(progress, count_table=9)))
        self.assertFalse(OP.Kernels().ready('continuity', plan, dict(progress, count_table=9, stage='restoring')))
        self.assertFalse(OP.Kernels().ready('continuity', plan, dict(progress, count_table=9, next_verify=2)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--operator', required=True)
    parser.add_argument('--tmpdir', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    paths = [Path(args.package_root), Path(args.operator), Path(args.tmpdir), Path(args.receipt)]
    if not all(p.is_absolute() for p in paths):
        parser.error('all paths must be absolute')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(paths[0]))
    SCRATCH = paths[2]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('evolvmem.history_migration._union_check', paths[1])
    OP = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(OP)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CompletionBoundaries))
    paths[3].write_text(json.dumps(dict(tests_run=result.testsRun, passed=result.wasSuccessful(),
        operator_sha256=hashlib.sha256(paths[1].read_bytes()).hexdigest(),
        actual_pg=False, production_changed=False), indent=2) + '\n', encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
