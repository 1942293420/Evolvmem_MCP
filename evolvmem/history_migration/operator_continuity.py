"""Additive operator B6 API; callers supply the existing trusted OperatorRuntime.

No independent DSN, credentials, configuration, service or public route is added.
CLI dispatch can call these entrypoints without changing shared scope modules.
"""
from dataclasses import asdict
from pathlib import Path
import re
from . import continuity_mapping as M
from . import continuity_plan
from .continuity_importer import ContinuityImporter,COUNT_TABLES
from .batched_importer import BatchedImporter
from .retained_chain import Plan as SourcePlan
from .operator_scope import OperatorRuntime,sql_statements
from .history_core.importer import transaction
from .run_continuity_batch import run_batch


def _bound(runtime,parent):
    M.check(type(runtime) is OperatorRuntime and type(parent) is SourcePlan,'trusted_continuity_operator_required')
    parent.require_importable()
    M.check(parent.header['source']['seal_receipt_sha256']==runtime.config.source.receipt_sha256
        and parent.header['source']['namespace']==runtime.config.source.namespace,'continuity_operator_source_changed')
    M.schema_hash(runtime.config.source.namespace)


def preflight(runtime):
    M.check(type(runtime) is OperatorRuntime,'trusted_continuity_operator_required')
    with transaction(lambda:runtime._open(False)) as c:
        exists=c.execute("SELECT to_regclass('em_content.history_continuity_schema_migrations') IS NOT NULL").fetchall()
        if exists==[(False,)]:return False
        runtime._content(c.raw,True)
        ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)._gate(c)
    return True


def prepare(runtime,parent):
    _bound(runtime,parent)
    port=BatchedImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)
    with transaction(runtime.factory) as c:
        port._gate(c)
        from .continuity_parent import checked_progress,check_parent_trigger_qualifications
        check_parent_trigger_qualifications(c)
        progress=checked_progress(c,port,parent)
        M.check(progress['stage']=='verified' and progress['generation']==progress['verify_generation'],
            'verified_core_parent_required')
        exists=c.execute("SELECT to_regclass('em_content.history_continuity_schema_migrations') IS NOT NULL").fetchall()
        if exists==[(True,)]:
            ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)._gate(c)
            applied=[]
        else:
            values={**asdict(runtime.target),'database_name':runtime.target.database,
                'parent_run_id':parent.run_id,'parent_manifest_sha256':parent.sha256}
            raw=(Path(__file__).parent/'sql/001_history_continuity.sql').read_text(encoding='utf8')
            raw='\n'.join(line for line in raw.splitlines() if not line.startswith('\\') and line not in ('BEGIN;','COMMIT;'))
            raw=re.sub(r":'([a-z_][a-z0-9_]*)'",lambda m:"'"+str(values[m[1]]).replace("'","''")+"'",raw)
            for statement in sql_statements(raw):c.execute(statement)
            ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)._gate(c)
            applied=['001_history_continuity.sql']
    return dict(prepared=True,domain='continuity',applied=applied,
        not_applicable=list(M.absent(runtime.config.source.namespace)),route_active=False,complete=False)


def plan(runtime,parent,path,*,run_id,seconds=60):
    _bound(runtime,parent);runtime.guard()
    return continuity_plan.plan_source(runtime.config.source,parent,runtime.target,path,run_id=run_id,seconds=seconds)


def status(runtime,manifest):
    _bound(runtime,manifest.parent_plan)
    return ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard).status(manifest)


def batch(runtime,manifest,receipt_path,*,max_steps=8,phase_seconds=60):
    _bound(runtime,manifest.parent_plan)
    port=ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)
    return run_batch(runtime.config.source,manifest,port,receipt_path,max_steps=max_steps,phase_seconds=phase_seconds)
