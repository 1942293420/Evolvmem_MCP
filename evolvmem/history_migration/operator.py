"""Private bounded migration CLI. No HTTP exposure or activation capability."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from .history_core import mapping as M
from .resume_planning import PlanningProgress

DOMAINS=('core','sources','organization','capture','governance','knowledge','capability','continuity','operations')
PARENTS={'core':(), 'sources':('core',), 'organization':('core','sources'), 'capture':('core','sources'), 'governance':('core','sources','organization'), 'knowledge':('core','sources'), 'capability':('core','sources'), 'continuity':('core',), 'operations':('core','sources')}


def require_private_directory(path):
    """Production files require Unix ownership; Windows ACLs are not inferred."""
    s=path.lstat()
    M.check(os.name=='posix' and stat.S_ISDIR(s.st_mode) and not path.is_symlink()
            and s.st_uid==os.getuid() and s.st_mode & 0o077==0,'operator_private_directory_required')


def sync_directory(path):
    if os.name=='posix':
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def write_new(path,value):
    raw=M.canonical(value)
    M.check(len(raw)<=16*1024*1024,'operator_metadata_budget')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    sync_directory(path.parent)


@contextmanager
def job_lock(work_dir):
    """OS-held nonblocking lock: stale PID files never grant concurrency."""
    require_private_directory(work_dir)
    path=work_dir/'operator.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            if not os.fstat(fd).st_size:os.write(fd,b'0');os.fsync(fd)
            os.lseek(fd,0,0)
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError:raise M.ImportError('operator_busy') from None
        else:
            import fcntl
            s=os.fstat(fd)
            M.check(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and s.st_mode&0o077==0,'operator_private_lock_required')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise M.ImportError('operator_busy') from None
        acquired=True
        yield
    finally:
        if acquired:
            if os.name=='nt':os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def safe_code(exc):
    value=exc.code if isinstance(exc,M.ImportError) else 'operator_interrupted' if isinstance(exc,KeyboardInterrupt) else 'operator_failed'
    return value if type(value) is str and re.fullmatch('[a-z][a-z0-9_]{0,95}',value) else 'operator_failed'


def facts(**kwargs):
    return dict(kwargs,history_complete=False,route_activated=False,production_migration_proven=False,automatic_retry=False)


class Operator:
    def __init__(self,config,*,kernels=None):
        self.config=config;self.kernels=kernels if kernels is not None else Kernels()
        self.work=Path(config.work_dir)

    def _metadata(self,domain):
        p=self.work/'plans'/(domain+'.json')
        M.check(p.is_file() and not p.is_symlink(),'parent_plan_required')
        M.check(p.stat().st_size<=65536,'operator_metadata_budget')
        d=json.loads(p.read_bytes())
        M.check(type(d.get('manifest_file')) is str and re.fullmatch(domain+r'-[a-f0-9]{32}\.manifest\.json',d['manifest_file']),
                'operator_manifest_path_invalid')
        M.check(d['version']==1 and d['domain']==domain and d['config_binding']==self.config.binding_hash
                and d['source_seal']==self.config.source.receipt_sha256
                and d['run_id']==self.run_id(domain),'operator_plan_binding_changed')
        parents={name:self._metadata(name)['plan_sha256'] for name in PARENTS[domain]}
        M.check(d['parents']==parents,'operator_parent_plan_changed')
        return d

    def run_id(self,domain):return str(uuid.uuid5(uuid.UUID(self.config.migration_id),'history-operator-v1:'+domain))

    def _load(self,domain):
        d=self._metadata(domain)
        if domain in ('capture','governance','knowledge','capability','continuity','operations'):
            parents={p:self._load(p) for p in PARENTS[domain]}
            return self.kernels.load(domain,self.work/'plans'/d['manifest_file'],d['plan_sha256'],parents=parents,config=self.config)
        plan=self.kernels.load(domain,self.work/'plans'/d['manifest_file'],d['plan_sha256'])
        return plan

    def _capture_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('capture',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('capture',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'capture_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,capture_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('capture-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('capture_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'capture_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'capture_completion_proof_changed')
        return True

    def _governance_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('governance',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('governance',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'governance_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,governance_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('governance-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('governance_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'governance_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'governance_completion_proof_changed')
        return True

    def _knowledge_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('knowledge',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('knowledge',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'knowledge_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,knowledge_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('knowledge-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('knowledge_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'knowledge_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'knowledge_completion_proof_changed')
        return True

    def _capability_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('capability',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('capability',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'capability_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,capability_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('capability-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('capability_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'capability_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'capability_completion_proof_changed')
        return True

    def _continuity_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('continuity',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('continuity',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'continuity_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,continuity_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('continuity-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('continuity_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'continuity_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'continuity_completion_proof_changed')
        return True

    def _operations_proof(self,plan,result=None,*,progress=None):
        """Target verified alone is provisional until the runner's exit proof."""
        if progress is None:progress=self.kernels.status('operations',self.config,plan)
        M.check(progress['stage']=='verified' and self.kernels.ready('operations',plan,progress)
            and type(progress.get('generation')) is int and progress['generation']==progress.get('verify_generation'),
            'operations_source_completion_required')
        expected=dict(version=1,manifest_sha256=plan.sha256,config_binding=self.config.binding_hash,
            source_seal=self.config.source.receipt_sha256,source_phase_complete=True,operations_domain_complete=True,
            target_generation=progress['generation'])
        path=self.work/'plans'/('operations-'+plan.sha256+'.proof.json')
        if result is not None:
            M.check(result.get('operations_domain_complete') is True and result.get('target_domain_verified') is True
                and result.get('source_phase_complete') is True and type(result.get('source_hash_passes')) is int
                and result['source_hash_passes']>=2 and result.get('manifest_sha256')==plan.sha256,
                'operations_source_completion_required')
            if not path.exists():write_new(path,expected)
        if not path.exists():return False
        M.check(not path.is_symlink() and path.stat().st_size<=4096 and json.loads(path.read_bytes())==expected,
                'operations_completion_proof_changed')
        return True

    def execute(self,command,domain=None,*,max_steps=8):
        M.check(command in ('preflight','prepare','plan','status','step','verify','finalize'),'operator_command_invalid')
        M.check((domain in DOMAINS) if command!='preflight' else domain is None,'operator_domain_required')
        M.check(type(max_steps) is int and 1<=max_steps<=128,'operator_step_budget')
        with job_lock(self.work):
            for name in ('plans','attempts'):
                p=self.work/name;p.mkdir(mode=0o700,exist_ok=True);require_private_directory(p)
            attempt=uuid.uuid4().hex;prefix=self.work/'attempts'/attempt
            write_new(Path(str(prefix)+'.started.json'),facts(attempt_id=attempt,command=command,domain=domain,
                config_binding=self.config.binding_hash,source_seal=self.config.source.receipt_sha256,max_steps=max_steps))
            try:
                result=self._execute(command,domain,max_steps,prefix)
                result=facts(**{k:v for k,v in result.items() if k not in facts()},attempt_id=attempt)
                write_new(Path(str(prefix)+'.success.json'),result)
                return result
            except BaseException as exc:
                write_new(Path(str(prefix)+'.failure.json'),facts(attempt_id=attempt,code=safe_code(exc),
                    commit_outcome='unknown_inspect_target_before_explicit_resume'))
                if isinstance(exc,KeyboardInterrupt):raise
                raise M.ImportError(safe_code(exc)) from None

    def _execute(self,command,domain,max_steps,prefix):
        c=self.config;seconds=min(60,c.command_seconds)
        if command=='preflight':return self.kernels.preflight(c)
        if command=='prepare':return self.kernels.prepare(c,domain)
        if command=='plan':
            parents={p:self._load(p) for p in PARENTS[domain]}
            manifest=self.work/'plans'/(domain+'-'+prefix.name+'.manifest.json')
            M.check(not (self.work/'plans'/(domain+'.json')).exists(),'operator_plan_exists')
            plan=self.kernels.plan(domain,c,parents,manifest,self.run_id(domain),seconds)
            if isinstance(plan,PlanningProgress):
                return dict(state='planning',domain=domain,planning_complete=False,pages=plan.pages,rows=plan.rows,
                    input_bytes=plan.input_bytes,table_index=plan.table_index,planning_revision=plan.revision)
            # Kernels durably publish their manifest first; no DB connection is held here.
            sync_directory(manifest.parent)
            write_new(self.work/'plans'/(domain+'.json'),dict(version=1,domain=domain,config_binding=c.binding_hash,
                source_seal=c.source.receipt_sha256,run_id=self.run_id(domain),plan_sha256=plan.sha256,manifest_file=manifest.name,
                parents={name:p.sha256 for name,p in parents.items()}))
            return dict(state='planned',domain=domain,plan_sha256=plan.sha256)
        plan=self._load(domain)
        if command=='step':
            result=self.kernels.step(domain,c,plan,Path(str(prefix)+'.kernel.json'),max_steps,seconds)
            if domain in ('capture','governance','knowledge','capability','continuity','operations'):
                complete=result.get(domain+'_domain_complete') is True
                if complete:getattr(self,'_'+domain+'_proof')(plan,result)
                return dict(result,domain_complete=complete)
            return result
        progress=self.kernels.status(domain,c,plan)
        # Only numeric frontiers and a fixed stage are exposed; never proof/rows/SQL.
        safe={k:v for k,v in progress.items() if k in ('stage','next_load','next_restore','next_verify','count_table','count_rows','generation','verify_generation') and (type(v) is int or v is None or k=='stage' and v in ('loading','restoring','verifying','counting','verified'))}
        ready=self.kernels.ready(domain,plan,progress)
        if command=='status':
            complete=progress['stage']=='verified' and (domain not in ('capture','governance','knowledge','capability','continuity','operations') or getattr(self,'_'+domain+'_proof')(plan,progress=progress))
            return dict(state='status',domain=domain,progress=safe,domain_complete=complete)
        if not ready:return dict(state='incomplete',domain=domain,progress=safe,domain_complete=False)
        if domain in ('capture','governance','knowledge','capability','continuity','operations'):
            result=self.kernels.finalize(domain,c,plan,seconds,receipt_path=Path(str(prefix)+'.kernel.json'))
            getattr(self,'_'+domain+'_proof')(plan,result)
            return dict(result,domain=domain,domain_complete=True)
        return dict(self.kernels.finalize(domain,c,plan,seconds),domain=domain,domain_complete=True)


class Kernels:
    """Direct adapters to the real page/frontier implementations, no row fixture."""
    def runtime(self,c):
        from .operator_scope import OperatorRuntime
        return OperatorRuntime(c)
    def preflight(self,c):
        from .operator_knowledge import preflight
        runtime=self.runtime(c);result=runtime.preflight()
        result['domains']['knowledge']=preflight(runtime)
        from .operator_capability import preflight as capability_preflight
        result['domains']['capability']=capability_preflight(runtime)
        from .operator_continuity import preflight as continuity_preflight
        result['domains']['continuity']=continuity_preflight(runtime)
        from .operator_operations import preflight as operations_preflight
        result['domains']['operations']=operations_preflight(runtime)
        return result
    def prepare(self,c,domain):
        if domain=='operations':
            from .operator_operations import prepare
            return prepare(self.runtime(c),Operator(c,kernels=self)._load('sources'))
        if domain=='continuity':
            from .operator_continuity import prepare
            return prepare(self.runtime(c),Operator(c,kernels=self)._load('core'))
        if domain=='capability':
            from .operator_capability import prepare
            return prepare(self.runtime(c),Operator(c,kernels=self)._load('sources'))
        if domain=='knowledge':
            from .operator_knowledge import prepare
            return prepare(self.runtime(c),Operator(c,kernels=self)._load('sources'))
        if domain=='governance':
            from .governance_mapping import schema_hash
            schema_hash(c.source.namespace)
            operator=Operator(c,kernels=self)
            return self.runtime(c).prepare(domain=domain,parent_plan=operator._load('sources'),organization_plan=operator._load('organization'))
        if domain=='capture':
            from .capture_mapping import schema_hash
            schema_hash(c.source.namespace)
            parent=Operator(c,kernels=self)._load('sources')
            return self.runtime(c).prepare(domain=domain,parent_plan=parent)
        return self.runtime(c).prepare(domain=domain)
    def port(self,domain,c):
        if domain=='continuity':
            from .continuity_importer import ContinuityImporter
            runtime=self.runtime(c)
            return ContinuityImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)
        from .batched_importer import BatchedImporter
        from .source_complete import CompleteImporter
        from .organization_complete import Importer
        from .capture_importer import CaptureImporter
        from .governance_importer import GovernanceImporter
        from .knowledge_importer import KnowledgeImporter
        from .capability_importer import CapabilityImporter
        from .operations_importer import OperationsImporter
        runtime=self.runtime(c)
        return {'core':BatchedImporter,'sources':CompleteImporter,'organization':Importer,'capture':CaptureImporter,'governance':GovernanceImporter,'knowledge':KnowledgeImporter,'capability':CapabilityImporter,'operations':OperationsImporter}[domain](
            runtime.factory,runtime.target,capacity_guard=runtime.guard)
    def plan(self,domain,c,parents,path,run_id,seconds):
        if domain=='operations':
            from .operator_operations import plan
            return plan(self.runtime(c),parents['sources'],path,run_id=run_id,seconds=seconds)
        if domain=='continuity':
            from .operator_continuity import plan
            return plan(self.runtime(c),parents['core'],path,run_id=run_id,seconds=seconds)
        if domain=='capability':
            from .operator_capability import plan
            return plan(self.runtime(c),parents['sources'],path,run_id=run_id,seconds=seconds)
        if domain=='knowledge':
            from .operator_knowledge import plan
            return plan(self.runtime(c),parents['sources'],path,run_id=run_id,seconds=seconds)
        from . import static_wal,source_plan,organization_complete,capture_plan
        if domain=='governance':
            from .governance_plan import plan_source
            return plan_source(c.source,parents['sources'],self.runtime(c).target,path,run_id=run_id,organization_plan=parents['organization'],seconds=seconds)
        if domain=='core':return static_wal.plan_source(c.source,path,run_id=run_id,project_mapping=c.project_mapping,seconds=seconds,planning_binding=getattr(c,'binding_hash',None))
        if domain=='sources':return source_plan.plan_source(c.source,parents['core'],self.runtime(c).target,path,run_id=run_id,seconds=seconds)
        if domain=='capture':return capture_plan.plan_source(c.source,parents['sources'],self.runtime(c).target,path,run_id=run_id,seconds=seconds)
        return organization_complete.make_plan(c.source,parents['sources'],run_id=run_id,path=path,seconds=seconds)
    def load(self,domain,path,sha,*,parents=None,config=None):
        if domain=='operations':
            from .operations_plan import load_plan
            return load_plan(path,sha,parents['sources'],self.runtime(config).target)
        if domain=='continuity':
            from .continuity_plan import load_plan
            return load_plan(path,sha,parents['core'],self.runtime(config).target)
        if domain=='capability':
            from .capability_plan import load_plan
            return load_plan(path,sha,parents['sources'],self.runtime(config).target)
        if domain=='knowledge':
            from .knowledge_plan import load_plan
            return load_plan(path,sha,parents['sources'],self.runtime(config).target)
        if domain=='governance':
            from .governance_plan import load_plan
            return load_plan(path,sha,parents['sources'],self.runtime(config).target,parents['organization'])
        from . import retained_chain,source_plan,organization_complete
        if domain=='capture':
            from .capture_plan import load_plan
            return load_plan(path,sha,parents['sources'],self.runtime(config).target)
        return {'core':retained_chain,'sources':source_plan,'organization':organization_complete}[domain].load_plan(path,sha)
    def status(self,domain,c,plan):
        port=self.port(domain,c)
        if domain!='organization':return port.status(plan)
        # Existing organization API has no public status; use its same short gates.
        with port._target_stage(min(60,c.command_seconds)) as conn:
            parent=super(type(port),port)._progress(conn,plan.parent)
            M.check(parent['stage']=='verified','organization_parent_not_verified')
            return port._progress_org(conn,plan,parent['generation'])
    def ready(self,domain,plan,p):
        if domain=='continuity':
            from .continuity_importer import COUNT_TABLES
            return p['stage'] in ('counting','verified') and p['count_table']==len(COUNT_TABLES) and p['next_verify']==plan.page_count
        tables=len(plan.counts) if domain!='organization' else len(plan.document['counts'])
        pages=plan.page_count if domain!='organization' else len(plan.document['pages'])
        return p['stage'] in ('counting','verified') and p['count_table']==tables and p['next_verify']==pages
    def step(self,domain,c,plan,path,max_steps,seconds):
        if domain=='operations':
            from .operator_operations import batch
            return batch(self.runtime(c),plan,path,max_steps=max_steps,phase_seconds=seconds)
        if domain=='continuity':
            from .operator_continuity import batch
            return batch(self.runtime(c),plan,path,max_steps=max_steps,phase_seconds=seconds)
        if domain=='capability':
            from .operator_capability import batch
            return batch(self.runtime(c),plan,path,max_steps=max_steps,phase_seconds=seconds)
        if domain=='knowledge':
            from .operator_knowledge import batch
            return batch(self.runtime(c),plan,path,max_steps=max_steps,phase_seconds=seconds)
        from . import static_batch,sources_batch,run_capture_batch,run_governance_batch
        port=self.port(domain,c)
        if domain!='organization':
            return {'core':static_batch,'sources':sources_batch,'capture':run_capture_batch,'governance':run_governance_batch}[domain].run_batch(c.source,plan,port,path,max_steps=max_steps,phase_seconds=seconds)
        with port.batch(plan,c.source,seconds=seconds) as batch:
            p=batch.begin(seconds=seconds)
            for _ in range(max_steps):
                if p['stage']=='loading':p=batch.load_page(p['next_load'],seconds=seconds)['progress']
                elif p['stage']=='verifying':p=batch.verify_page(p['next_verify'],seconds=seconds)
                elif p['stage']=='counting' and p['count_table']<len(plan.document['counts']):p=batch.count_page(seconds=seconds)
                else:break
        # Finalization is after the single shared source phase has closed.
        if self.ready(domain,plan,p):return dict(port.finalize(plan,c.source,seconds=seconds),domain_complete=True)
        return dict(state=p['stage'],domain_complete=False,source_phase_complete=True)
    def finalize(self,domain,c,plan,seconds,*,receipt_path=None):
        if domain=='operations':
            from .operator_operations import batch
            M.check(receipt_path is not None,'operations_finalization_receipt_required')
            return batch(self.runtime(c),plan,receipt_path,max_steps=1,phase_seconds=seconds)
        if domain=='continuity':
            from .operator_continuity import batch
            M.check(receipt_path is not None,'continuity_finalization_receipt_required')
            return batch(self.runtime(c),plan,receipt_path,max_steps=1,phase_seconds=seconds)
        if domain=='capability':
            from .operator_capability import batch
            M.check(receipt_path is not None,'capability_finalization_receipt_required')
            return batch(self.runtime(c),plan,receipt_path,max_steps=1,phase_seconds=seconds)
        if domain=='knowledge':
            from .operator_knowledge import batch
            M.check(receipt_path is not None,'knowledge_finalization_receipt_required')
            return batch(self.runtime(c),plan,receipt_path,max_steps=1,phase_seconds=seconds)
        from .static_wal import SealedPhase
        from .source_scan import Phase
        port=self.port(domain,c)
        if domain=='governance':
            from .run_governance_batch import run_batch
            M.check(receipt_path is not None,'governance_finalization_receipt_required')
            return run_batch(c.source,plan,port,receipt_path,max_steps=1,phase_seconds=seconds)
        if domain=='capture':
            from .run_capture_batch import run_batch
            M.check(receipt_path is not None,'capture_finalization_receipt_required')
            return run_batch(c.source,plan,port,receipt_path,max_steps=1,phase_seconds=seconds)
        if domain=='organization':return port.finalize(plan,c.source,seconds=seconds)
        # A fresh entry/exit proof is required even when no pages remain.
        with (SealedPhase if domain=='core' else Phase)(c.source,plan=plan,seconds=seconds) as phase:phase.tick()
        M.check(phase.proven_complete,'source_phase_incomplete')
        return port.finalize(plan)


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('command',choices=('preflight','prepare','plan','status','step','verify','finalize'))
    p.add_argument('--domain',choices=DOMAINS);p.add_argument('--max-steps',type=int,default=8)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        from .operator_config import load_config
        c=load_config(args.config)
        if not args.worker:
            # Supervises both source hashing and driver calls; never retries a child.
            command=[sys.executable,'-m','evolvmem.history_migration','--worker','--config',args.config,args.command,'--max-steps',str(args.max_steps)]
            if args.domain:command+=['--domain',args.domain]
            try:
                result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=c.command_seconds,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired:
                # Timeout kills the worker. Preserve its immutable started record and
                # append an unknown-outcome receipt; no optimistic completion claim.
                with job_lock(Path(c.work_dir)):
                    for started in (Path(c.work_dir)/'attempts').glob('*.started.json'):
                        stem=str(started)[:-len('.started.json')]
                        if not Path(stem+'.success.json').exists() and not Path(stem+'.failure.json').exists():
                            write_new(Path(stem+'.failure.json'),facts(code='operator_deadline',commit_outcome='unknown_inspect_target_before_explicit_resume'))
                raise M.ImportError('operator_deadline') from None
            M.check(len(result.stdout)<=65536,'operator_output_budget')
            report=json.loads(result.stdout)
            print(json.dumps(report,separators=(',',':')));return result.returncode
        report=Operator(c).execute(args.command,args.domain,max_steps=args.max_steps)
        raw=M.canonical(report);M.check(len(raw)<=65536,'operator_output_budget');print(raw.decode());return 0
    except BaseException as exc:
        print(M.canonical(facts(code=safe_code(exc),state='stopped')).decode());return 2
