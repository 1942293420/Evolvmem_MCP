"""Run only B6 checks against installed code; path adapter, not altered tests.

python -B tests/native_runner.py --package-root /canonical --tmpdir /scratch
Optional --pattern test_transaction.py and --filter completion narrow a delta.
No PG connection, credential lookup, production SQLite open, or service start.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-root',required=True)
    parser.add_argument('--tmpdir',required=True)
    parser.add_argument('--operator')
    parser.add_argument('--pattern',default='test_*.py')
    parser.add_argument('--filter')
    parser.add_argument('--receipt')
    args=parser.parse_args()
    paths={name:Path(value) for name,value in [('package',args.package_root),('scratch',args.tmpdir)]}
    if not all(p.is_absolute() for p in paths.values()):parser.error('absolute package-root and tmpdir required')
    package=paths['package'].resolve();scratch=paths['scratch'].resolve()
    operator=Path(args.operator).resolve() if args.operator else package/'evolvmem/history_migration/operator.py'
    if not (package/'evolvmem/history_migration/continuity_mapping.py').is_file() or not operator.is_file():
        parser.error('installed B6 package/operator missing')
    scratch.mkdir(parents=True,exist_ok=True)
    os.environ['B6_NATIVE_PACKAGE_ROOT']=str(package)
    os.environ['B6_NATIVE_OPERATOR']=str(operator)
    os.environ['B6_TEST_TMPDIR']=str(scratch)
    os.environ['TMPDIR']=str(scratch)
    sys.dont_write_bytecode=True
    here=Path(__file__).resolve().parent
    sys.path.insert(0,str(here))
    loader=unittest.TestLoader()
    if args.filter:loader.testNamePatterns=['*'+args.filter+'*']
    started=time.monotonic()
    suite=loader.discover(str(here),pattern=args.pattern)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    receipt=dict(scope='B6 native installed-package synthetic checks',tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),passed=result.wasSuccessful(),
        elapsed_seconds=round(time.monotonic()-started,6),package_root=str(package),scratch=str(scratch),
        operator_sha256=hashlib.sha256(operator.read_bytes()).hexdigest(),actual_pg=False,
        real_history_read=False,services_started=False,path_adapter_only=True)
    if args.receipt:Path(args.receipt).write_text(json.dumps(receipt,indent=2),encoding='utf8')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
