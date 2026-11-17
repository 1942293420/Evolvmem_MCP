"""One explicit bounded seven-table step; no loop, retry, copy or activation."""
from pathlib import Path
import time
from . import source_mapping as M
from .source_plan import Plan
from .source_scan import Phase
from .source_complete import CompleteImporter
from .retained_chain import _private_write
from .batched_importer import batch_budget


def run_batch(source,plan,port,receipt_path,*,max_steps=8,phase_seconds=60):
    """One <=60s budget, <=128 steps plus begin and optional final transaction.

    The caller must supply the existing reviewed private factory/capacity gate
    and an outer process deadline. Each attempt has an exclusive local receipt.
    Unknown commit stops; a new explicit attempt rechecks target frontiers.
    No result is evidence of PG until the supplied port is verified as real PG.
    """
    M.check(type(plan) is Plan and isinstance(port,CompleteImporter),'trusted_source_chain_required')
    M.check(type(max_steps) is int and 1<=max_steps<=128,'source_batch_step_budget')
    plan.require_importable();path=Path(receipt_path);started=time.monotonic()
    _private_write(str(path)+'.started',dict(manifest_sha256=plan.sha256,complete=False,max_steps=max_steps))
    try:
        with batch_budget(port,phase_seconds) as budget:
            report=_run(source,plan,port,max_steps,phase_seconds,budget,started)
        _private_write(path,report);return report
    except BaseException as exc:
        code=exc.code if isinstance(exc,M.ImportError) else 'source_batch_failed'
        _private_write(str(path)+'.failed',dict(manifest_sha256=plan.sha256,complete=False,code=code,
            automatic_retry=False,source_domain_complete=False,history_complete=False,route_activated=False))
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
            elif stage=='counting' and status['count_table']<7:
                result=port.count_page(plan);steps.append(dict(stage=stage,table_index=result['table_index'],rows_checked=result['rows_checked']))
            else:break
            phase.tick();status=result['progress']
        phase.tick()
    # Original sealed phase hashes all three files before and after the batch.
    # Pages and committed intermediate target rows remain provisional until
    # this proof succeeds. Quarantine is never removed here.
    M.check(phase.proven_complete,'source_phase_incomplete');budget.check()
    if status['stage']=='verified' or status['stage']=='counting' and status['count_table']==7:
        result=port.finalize(plan)
        M.check(result.get('target_domain_verified') is True,'source_target_finalization_incomplete')
        result=dict(result,source_domain_complete=True)
    else:result=dict(state=status['stage'],tables=7,source_domain_complete=False,history_complete=False,route_activated=False)
    report=dict(result,manifest_sha256=plan.sha256,steps=steps,source_phase_complete=True,
        source_hash_passes=phase.hash_passes,elapsed_seconds=round(time.monotonic()-started,6),
        target_evidence='supplied_transaction_port',production_migration_proven=False,sqlite_snapshot_token_resumable=False)
    budget.check();return report
