"""Explicit bounded step runner. No automatic loop, retry, DB creation or copy."""
from pathlib import Path
import time
from .history_core import mapping as M
from .retained_chain import Plan,SourcePhase,_private_write
from .batched_importer import BatchedImporter,batch_budget


def run_batch(source,plan,port,receipt_path,*,max_steps=8,phase_seconds=60):
    """One <=60-second budget; <=32 work + one begin + optional final transaction.

    The caller supplies the reviewed real private factory/capacity guard and
    enforces a process deadline. Started/failed/success receipts are never
    overwritten. A failed attempt requires explicit inspection/new attempt;
    PG frontiers/readback, not the new local filename, decide replay behavior.
    """
    M.check(type(plan) is Plan and isinstance(port,BatchedImporter),'trusted_chain_parts_required')
    M.check(type(max_steps) is int and 1<=max_steps<=32,'batch_step_budget')
    plan.require_importable();path=Path(receipt_path);started=time.monotonic()
    _private_write(str(path)+'.started',dict(manifest_sha256=plan.sha256,complete=False,max_steps=max_steps))
    try:
        with batch_budget(port,phase_seconds) as budget:
            report=_run(source,plan,port,max_steps,phase_seconds,budget,started)
        _private_write(path,report);return report
    except BaseException as exc:
        code=exc.code if isinstance(exc,M.ImportError) else 'batch_failed'
        _private_write(str(path)+'.failed',dict(manifest_sha256=plan.sha256,complete=False,code=code,
            automatic_retry=False,history_complete=False,route_activated=False))
        raise


def _run(source,plan,port,max_steps,phase_seconds,budget,started):
        steps=[]
        with SourcePhase(source,plan=plan,seconds=phase_seconds) as phase:
            status=port.begin(plan);phase.tick()
            for _ in range(max_steps):
                phase.tick();stage=status['stage']
                if stage=='loading':
                    index=status['next_load'];page=plan.page(index)
                    args=phase.read(page);phase.tick();result=port.load_page(plan,index,args)
                    steps.append(dict(stage=stage,index=index))
                elif stage=='restoring':
                    index=status['next_restore'];result=port.restore_page(plan,index);steps.append(dict(stage=stage,index=index))
                elif stage=='verifying':
                    index=status['next_verify'];result=port.verify_page(plan,index);steps.append(dict(stage=stage,index=index))
                elif stage=='counting' and status['count_table']<6:
                    result=port.count_page(plan);steps.append(dict(stage=stage,table_index=result['table_index'],rows_checked=result['rows_checked']))
                else:break
                phase.tick();status=result['progress']
            phase.tick()
        # End SHA must succeed before this runner can publish a phase receipt
        # or ask the target to make its final (still quarantined) state change.
        M.check(phase.proven_complete,'source_phase_incomplete')
        budget.check()
        if status['stage']=='verified' or status['stage']=='counting' and status['count_table']==6:
            result=port.finalize(plan)
        else:result=dict(state=status['stage'],tables=6,history_complete=False,route_activated=False)
        report=dict(result,manifest_sha256=plan.sha256,steps=steps,source_phase_complete=True,
            source_hash_passes=phase.hash_passes,elapsed_seconds=round(time.monotonic()-started,6),
            target_evidence='supplied_transaction_port',production_migration_proven=False,
            sqlite_snapshot_token_resumable=False)
        budget.check();return report
