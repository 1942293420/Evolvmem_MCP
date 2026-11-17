"""Bounded operations-only checks against an installed package or local overlay.

No real PG, service, credentials or production history. All SQLite scratch files
are synthetic and created beneath the explicit absolute scratch directory.
"""
import argparse,hashlib,json,os,sys,time,unittest
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package-root');p.add_argument('--operator');p.add_argument('--tmpdir',required=True)
    p.add_argument('--receipt');p.add_argument('--pattern',default='test_*.py');p.add_argument('--filter')
    a=p.parse_args();scratch=Path(a.tmpdir)
    if not scratch.is_absolute():p.error('absolute scratch required')
    scratch.mkdir(exist_ok=True,parents=True)
    root=Path(__file__).resolve().parents[1]
    if a.package_root:
        package=Path(a.package_root)
        if not package.is_absolute() or not (package/'evolvmem/history_migration/operations_mapping.py').is_file():p.error('installed operations package missing')
        os.environ['OPERATIONS_NATIVE_PACKAGE_ROOT']=str(package)
    operator=Path(a.operator) if a.operator else (Path(a.package_root)/'evolvmem/history_migration/operator.py' if a.package_root else root/'integration/operator.py')
    if not operator.is_absolute() or not operator.is_file():p.error('absolute operator required')
    os.environ['OPERATIONS_NATIVE_OPERATOR']=str(operator)
    os.environ['OPERATIONS_TEST_TMPDIR']=str(scratch);sys.dont_write_bytecode=True
    here=Path(__file__).resolve().parent;sys.path.insert(0,str(here));loader=unittest.TestLoader()
    if a.filter:loader.testNamePatterns=['*'+a.filter+'*']
    started=time.monotonic();result=unittest.TextTestRunner(verbosity=2).run(loader.discover(str(here),pattern=a.pattern))
    report=dict(scope='B7 B8 operations synthetic checks',tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        passed=result.wasSuccessful(),elapsed_seconds=round(time.monotonic()-started,6),scratch=str(scratch),
        package_root=a.package_root,operator_sha256=hashlib.sha256(operator.read_bytes()).hexdigest(),
        actual_pg=False,real_history_read=False,services_started=False,production_route_changed=False)
    if a.receipt:Path(a.receipt).write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report));return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
