"""One bounded capture batch; no automatic retry, source copying or activation."""
from pathlib import Path
import time
from . import capture_mapping as M
from .capture_plan import Plan
from .capture_scan import Phase
from .capture_importer import CaptureImporter
from .retained_chain import _private_write
from .batched_importer import batch_budget

def run_batch(source,plan,port,receipt_path,*,max_steps=8,phase_seconds=60):
    """Requires the private, capacity-checked factory and outer process limit.

    Original SQL/lock limits and the same budget before SQL/commit remain.
    A fresh explicit attempt may read committed receipts after unknown commit;
    this function never replays an effect automatically.
    """
    M.check(type(plan) is Plan and isinstance(port,CaptureImporter),'trusted_capture_chain_required')
    M.check(type(max_steps) is int and 1<=max_steps<=128,'capture_batch_step_budget')
    plan.require_importable();path=Path(receipt_path);started=time.monotonic()
    _private_write(str(path)+'.started',dict(manifest_sha256=plan.sha256,complete=False,max_steps=max_steps))
    try:
        with batch_budget(port,phase_seconds) as budget:
            report=_run(source,plan,port,max_steps,phase_seconds,budget,started)
        _private_write(path,report);return report
    except BaseException as exc:
        code=exc.code if isinstance(exc,M.ImportError) else 'capture_batch_failed'
        _private_write(str(path)+'.failed',dict(manifest_sha256=plan.sha256,complete=False,code=code,
            automatic_retry=False,capture_domain_complete=False,history_complete=False,route_activated=False))
        raise

def _run(source,plan,port,max_steps,phase_seconds,budget,started):
    steps=[]
    with Phase(source,plan=plan,seconds=phase_seconds) as phase:
        status=port.begin(plan);phase.tick()
        for _ in range(max_steps):
            phase.tick();stage=status['stage']
            if stage=='loading':
                index=status['next_load'];args=phase.read(plan.page(index));phase.tick()
                result=port.load_page(plan,index,args);steps.append(dict(stage=stage,index=index))
            elif stage=='verifying':
                index=status['next_verify'];result=port.verify_page(plan,index);steps.append(dict(stage=stage,index=index))
            elif stage=='counting' and status['count_table']<3:
                result=port.count_page(plan);steps.append(dict(stage=stage,table_index=result['table_index'],rows_checked=result['rows_checked']))
            else:break
            phase.tick();status=result['progress']
        phase.tick()
    M.check(phase.proven_complete,'capture_phase_incomplete');budget.check()
    if status['stage']=='verified' or status['stage']=='counting' and status['count_table']==3:
        result=port.finalize(plan);M.check(result.get('target_domain_verified') is True,'capture_target_finalization_incomplete')
        result=dict(result,capture_domain_complete=True)
    else:result=dict(state=status['stage'],tables=3,capture_domain_complete=False,history_complete=False,route_activated=False)
    report=dict(result,manifest_sha256=plan.sha256,steps=steps,source_phase_complete=True,source_hash_passes=phase.hash_passes,
        elapsed_seconds=round(time.monotonic()-started,6),target_evidence='supplied_transaction_port',production_migration_proven=False,
        sqlite_snapshot_token_resumable=False)
    budget.check();return report
