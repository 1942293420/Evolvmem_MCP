"""Private independent catch-up CLI. Original operator configuration is immutable."""
from dataclasses import asdict
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid
from . import catchup_catalog as C
from .operator import job_lock, require_private_directory, write_new, safe_code
from .operator_config import load_config, private_read, _pairs
from .operator_scope import OperatorRuntime
from .static_wal import SealedWalSource
from .catchup_plan import Plan
from .catchup_resume import build_plan_step
from .catchup_importer import CatchupImporter, anchor, catalog
from .history_core.importer import transaction

def epoch_config(path):
    raw=private_read(path);d=json.loads(raw,object_pairs_hook=_pairs)
    C.check(type(d) is dict and set(d)==set('version epoch_id work_dir old_source_seal_file old_source_seal_sha256 new_source_seal_file new_source_seal_sha256 caught_up_to max_changes max_input_bytes'.split()),'catchup_config_fields')
    C.check(d['version']==1,'catchup_config_version');C.core.uuid_text(d['epoch_id'])
    directory=Path(d['work_dir']);C.check(directory.is_absolute() and directory.resolve()==directory,'catchup_private_work_path')
    require_private_directory(directory)
    for p in directory.parents:
        C.check(not p.stat().st_mode&0o022,'catchup_parent_not_private')
    try:boundary=datetime.fromisoformat(d['caught_up_to'].replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError):raise C.ImportError('catchup_snapshot_boundary_required') from None
    C.check(boundary.tzinfo is not None,'catchup_snapshot_boundary_required')
    C.check(type(d['max_changes']) is int and 1<=d['max_changes']<=100000,'catchup_change_budget')
    C.check(type(d['max_input_bytes']) is int and 0<d['max_input_bytes']<=4*1024**3,'catchup_input_budget')
    sources=[]
    for prefix in ('old','new'):
        sources.append(SealedWalSource.from_receipt(private_read(d[prefix+'_source_seal_file']),d[prefix+'_source_seal_sha256']))
    return d,directory,sources

def run(base_path,epoch_path,command,*,max_steps=8,attempt_id=None,recover_attempt=None):
    base=load_config(base_path);d,work,sources=epoch_config(epoch_path)
    runtime=OperatorRuntime(base);runtime.guard()
    attempt_id=attempt_id or str(uuid.uuid4());C.core.uuid_text(attempt_id)
    with job_lock(work):
        attempts=work/'attempts';attempts.mkdir(mode=0o700,exist_ok=True);require_private_directory(attempts)
        prefix=attempts/attempt_id
        write_new(Path(str(prefix)+'.started.json'),dict(command=command,attempt_id=attempt_id,epoch_id=d['epoch_id'],automatic_retry=False))
        try:
            if command=='plan':
                C.check(not (work/'plan.json').exists(),'catchup_plan_already_exists')
                previous=anchor(runtime)
                with transaction(runtime.factory) as c:cat=catalog(c,base.source.namespace)
                plan=build_plan_step(*sources,work/'plan.json',epoch_config_sha256=C.digest(d),base_binding=base.binding_hash,previous_proof=previous,target=asdict(runtime.target),catalog=cat,
                    projects=base.project_mapping,caught_up_to=d['caught_up_to'],epoch_id=d['epoch_id'],
                    seconds=min(60,base.command_seconds),max_changes=d['max_changes'],max_input_bytes=d['max_input_bytes'])
                if type(plan) is dict:
                    result=dict(plan,attempt_id=attempt_id,route_activated=False,production_cutover=False,automatic_retry=False)
                    write_new(Path(str(prefix)+'.success.json'),result);return result
                write_new(work/'plan.json',plan.document)
                write_new(work/'plan.receipt.json',dict(plan_sha256=plan.sha256,epoch_config_sha256=C.digest(d),base_binding=base.binding_hash))
                result=dict(state='planned',plan_hash=plan.sha256,pages=len(plan.document['pages']),changes=sum(len(p['changes']) for p in plan.document['pages']))
            else:
                receipt=json.loads(private_read(work/'plan.receipt.json'))
                C.check(receipt['epoch_config_sha256']==C.digest(d) and receipt['base_binding']==base.binding_hash,'catchup_config_binding_changed')
                plan=Plan(private_read(work/'plan.json',64*1024**2),receipt['plan_sha256'])
                supplement_path=work/'project-mapping-supplement.json'
                supplement_raw=private_read(supplement_path) if supplement_path.exists() or supplement_path.is_symlink() else None
                importer=CatchupImporter(runtime,plan,*sources,project_supplement=supplement_raw)
                if command=='recover':
                    C.check(recover_attempt is not None,'catchup_recover_attempt_required');result=importer.recover(recover_attempt)
                elif command=='step':result=importer.step(max_steps=max_steps,attempt_id=attempt_id)
                else:result=getattr(importer,command)()
                observed=private_read(supplement_path) if supplement_path.exists() or supplement_path.is_symlink() else None
                C.check(observed==supplement_raw,'catchup_project_supplement_changed')
                if importer.project_supplement_sha256 is not None:
                    result=dict(result,project_mapping_supplement_sha256=importer.project_supplement_sha256,
                        effective_project_mapping_sha256=C.digest(importer.projects))
            result=dict(result,attempt_id=attempt_id,route_activated=False,production_cutover=False,automatic_retry=False)
            write_new(Path(str(prefix)+'.success.json'),result);return result
        except BaseException as exc:
            write_new(Path(str(prefix)+'.failure.json'),dict(code=safe_code(exc),attempt_id=attempt_id,
                commit_outcome='unknown_read_receipt_before_explicit_new_attempt',automatic_retry=False))
            raise

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-config',required=True);p.add_argument('--epoch-config',required=True)
    p.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--attempt-id',help=argparse.SUPPRESS)
    p.add_argument('command',choices=('plan','prepare','begin','step','status','recover','finalize'))
    p.add_argument('--max-steps',type=int,default=8);p.add_argument('--recover-attempt')
    args=p.parse_args(argv);attempt=args.attempt_id or str(uuid.uuid4())
    try:
        base=load_config(args.base_config)
        if not args.worker:
            command=[sys.executable,'-m','evolvmem.history_migration.catchup','--worker',
                '--base-config',args.base_config,'--epoch-config',args.epoch_config,'--attempt-id',attempt,
                args.command,'--max-steps',str(args.max_steps)]
            if args.recover_attempt:command+=['--recover-attempt',args.recover_attempt]
            try:result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=base.command_seconds,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired:
                raise C.ImportError('catchup_process_deadline_unknown_commit') from None
            C.check(len(result.stdout)<=65536,'catchup_output_budget')
            report=json.loads(result.stdout);print(C.canonical(report).decode());return result.returncode
        result=run(args.base_config,args.epoch_config,args.command,max_steps=args.max_steps,
            attempt_id=attempt,recover_attempt=args.recover_attempt)
        C.check(len(C.canonical(result))<=65536,'catchup_output_budget');print(C.canonical(result).decode());return 0
    except BaseException as exc:
        print(C.canonical(dict(state='stopped',code=safe_code(exc),attempt_id=attempt,
            automatic_retry=False,commit_outcome='unknown_inspect_receipt',route_activated=False)).decode());return 2

if __name__=='__main__':raise SystemExit(main())
