"""Durable metadata frontiers, never rows or SQLite cursor capabilities.

One explicit call opens one fresh sealed phase. Nothing advances durably until
its full exit hash succeeds. Completed callers still construct their old Plan.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import time
import uuid
from . import retained_chain as R
from .history_core import mapping as M

check=M.check


@dataclass(frozen=True)
class PlanningProgress:
    domain:str
    pages:int
    rows:int
    input_bytes:int
    table_index:int
    revision:int


@dataclass(frozen=True)
class ScanResult:
    pages:list
    counts:dict
    input_bytes:int
    sequence:dict
    schema:str
    source:dict
    maxima:dict


def _read(path,cap=R.MAX_MANIFEST_BYTES):
    s=path.lstat()
    check(stat.S_ISREG(s.st_mode) and not path.is_symlink() and s.st_nlink==1 and s.st_size<=cap,
          'planning_private_file_required')
    if os.name=='posix':
        check(s.st_uid==os.getuid() and s.st_mode&0o077==0,'planning_private_file_required')
    return path.read_bytes()


def _sync(directory):
    if os.name=='posix':
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


@contextmanager
def _lock(path):
    fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            if not os.fstat(fd).st_size:os.write(fd,b'0');os.fsync(fd)
            os.lseek(fd,0,0)
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError:raise M.ImportError('planning_busy') from None
        else:
            import fcntl
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise M.ImportError('planning_busy') from None
        acquired=True;yield
    finally:
        if acquired:
            if os.name=='nt':os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def _validate(state,tables,count_tables,max_rows,max_pages,max_input_bytes):
    check(set(state)=={'pages','counts','input_bytes','sequence','schema','source','table_index','after','metadata_bytes'},
          'planning_draft_shape')
    counts=dict.fromkeys(count_tables,0);size=metadata=0;index=0;after=None
    pages=state['pages'];check(type(pages) is list and len(pages)<=max_pages,'plan_page_budget')
    for ordinal,p in enumerate(pages):
        org='sha256' in p
        fields={'table','after','end','exhausted','row_count','input_bytes','sha256'} if org else {
            'ordinal','table','chunk_key','after','end','exhausted','row_count','input_bytes','native_hash','input_hash'}
        check(type(p) is dict and set(p)==fields,'planning_metadata_only')
        check(index<len(tables) and p['table']==tables[index] and p['after']==after,'planning_frontier_changed')
        check(type(p['row_count']) is int and 0<=p['row_count']<=128 and type(p['exhausted']) is bool
              and type(p['input_bytes']) is int and 0<=p['input_bytes']<=64*1024**2,'planning_page_invalid')
        for name in (('sha256',) if org else ('native_hash',)):
            check(type(p[name]) is str and R.HASH.fullmatch(p[name]),'planning_page_digest')
        if not org:
            check(p['ordinal']==ordinal and type(p['chunk_key']) is str,'planning_page_order')
        if p['row_count']:
            check(type(p['end']) is int and M.INT64_MIN<=p['end']<=M.INT64_MAX
                  and (after is None or p['end']>after) and p['input_bytes']>0,'planning_page_range')
            if not org:check(type(p['input_hash']) is str and R.HASH.fullmatch(p['input_hash']),'planning_page_digest')
        else:
            check(p['exhausted'] and p['end']==after and p['input_bytes']==(2 if org else 0),'planning_empty_page')
            if not org:check(p['input_hash'] is None,'planning_empty_page')
        counts[p['table']]+=p['row_count'];size+=p['input_bytes'];metadata+=len(M.canonical(p))+1
        if p['exhausted']:index+=1;after=None
        else:after=p['end']
    check(sum(counts.values())<=max_rows and size<=max_input_bytes
          and metadata<=R.MAX_MANIFEST_BYTES-32768,'planning_budget_exceeded')
    check(counts==state['counts'] and size==state['input_bytes'] and metadata==state['metadata_bytes']
          and index==state['table_index'] and after==state['after'],'planning_totals_changed')
    if pages:
        check(type(state['sequence']) is dict and type(state['schema']) is str
              and R.HASH.fullmatch(state['schema']) and type(state['source']) is dict,'planning_source_metadata')


def scan(source,path,*,run_id,domain,tables,count_tables,phase_factory,read_page,binding,seconds=60,
         max_rows=R.MAX_ROWS,max_pages=R.MAX_PAGES,max_input_bytes=R.MAX_INPUT_BYTES):
    """Return PlanningProgress or ScanResult; final Plan publication is caller-owned.

    phase_factory(seconds=...) must perform source entry/exit hashes. read_page
    returns (validated metadata-only page, sequence evidence, schema hash), using
    a fresh <=10s transaction and a rowid seek coordinate for each invocation.
    """
    M.uuid_text(run_id);check(type(domain) is str and domain in (
        'core','sources','organization','capture','governance','knowledge','capability','continuity','operations'),
        'planning_domain_invalid')
    check(type(seconds) in (int,float) and 0<seconds<=60,'phase_deadline_invalid')
    tables=tuple(tables);count_tables=tuple(count_tables)
    check(bool(tables) and len(set(tables))==len(tables) and set(tables)<=set(count_tables),'planning_tables_invalid')
    for value,ceiling in ((max_rows,R.MAX_ROWS),(max_pages,R.MAX_PAGES),(max_input_bytes,R.MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=ceiling,'planning_budget_invalid')
    binding=dict(format='metadata-planner-v1',run_id=run_id,domain=domain,source_sha256=source.sha256,
        seal_receipt_sha256=source.receipt_sha256,namespace=source.namespace,tables=list(tables),
        count_tables=list(count_tables),budgets=[max_rows,max_pages,max_input_bytes],context=binding)
    binding=json.loads(M.canonical(binding))  # Match the authenticated JSON representation after restart.
    stem=Path(path).parent/('.planner-'+domain+'-'+run_id)
    draft=Path(str(stem)+'.draft.json');keypath=Path(str(stem)+'.key')
    with _lock(Path(str(stem)+'.lock')):
        if not keypath.exists():
            check(not draft.exists(),'planning_key_missing')
            fd=os.open(keypath,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:out.write(os.urandom(32));out.flush();os.fsync(out.fileno())
            _sync(keypath.parent)
        key=_read(keypath,32);check(len(key)==32,'planning_key_invalid')
        previous=_read(draft) if draft.exists() else None;revision=0
        if previous is None:
            state=dict(pages=[],counts=dict.fromkeys(count_tables,0),input_bytes=0,sequence=None,schema=None,
                source=None,table_index=0,after=None,metadata_bytes=0)
        else:
            envelope=json.loads(previous);check(type(envelope) is dict and set(envelope)=={'revision','binding','state','auth'},'planning_draft_shape')
            auth=envelope.pop('auth')
            check(type(auth) is str and hmac.compare_digest(auth,hmac.new(key,M.canonical(envelope),hashlib.sha256).hexdigest()),
                  'planning_draft_authentication')
            check(envelope['binding']==binding,'planning_binding_changed')
            revision=envelope['revision'];check(type(revision) is int and revision>0,'planning_revision_invalid')
            state=envelope['state']
        _validate(state,tables,count_tables,max_rows,max_pages,max_input_bytes)
        initial_pages=len(state['pages']);started=time.monotonic();page_seconds=.01
        with phase_factory(seconds=seconds) as phase:
            entered=time.monotonic();base=getattr(phase,'base',phase)
            reserve=max(min(15.,seconds/4),2*(entered-started))
            if state['source'] is None:state['source']=phase.binding
            check(state['source']==phase.binding,'planning_source_binding_changed')
            if 'parent_source' in binding['context']:
                check(phase.binding==binding['context']['parent_source'],'planning_parent_seal_mismatch')
            while state['table_index']<len(tables):
                # Reserve a complete exit hash plus room for one bounded page.
                if time.monotonic()+reserve+max(min(10.,seconds/4),2*page_seconds)>=base.deadline:break
                began=time.monotonic()
                p,seq,schema=read_page(phase,tables[state['table_index']],state['after'],len(state['pages']))
                page_seconds=max(page_seconds,time.monotonic()-began)
                if state['sequence'] is None:state['sequence']=seq;state['schema']=schema
                check(state['sequence']==seq and state['schema']==schema,'source_metadata_changed')
                state['pages'].append(p);state['counts'][p['table']]+=p['row_count'];state['input_bytes']+=p['input_bytes']
                state['metadata_bytes']+=len(M.canonical(p))+1
                if p['exhausted']:state['table_index']+=1;state['after']=None
                else:state['after']=p['end']
                check(sum(state['counts'].values())<=max_rows and len(state['pages'])<=max_pages
                      and state['input_bytes']<=max_input_bytes
                      and state['metadata_bytes']<=R.MAX_MANIFEST_BYTES-32768,'planning_budget_exceeded')
        check(phase.proven_complete,'planning_source_exit_incomplete')
        check(state['table_index']==len(tables) or len(state['pages'])>initial_pages,'planning_phase_no_progress')
        _validate(state,tables,count_tables,max_rows,max_pages,max_input_bytes)
        # The lock serializes cooperative callers; raw comparison is the CAS.
        check((_read(draft) if draft.exists() else None)==previous,'planning_draft_cas_changed')
        envelope=dict(revision=revision+1,binding=binding,state=state)
        envelope['auth']=hmac.new(key,M.canonical(envelope),hashlib.sha256).hexdigest()
        temporary=Path(str(draft)+'.'+uuid.uuid4().hex+'.tmp')
        try:R._private_write(temporary,envelope);os.replace(temporary,draft);_sync(draft.parent)
        finally:
            if temporary.exists():temporary.unlink()
        if state['table_index']<len(tables):
            return PlanningProgress(domain,len(state['pages']),sum(state['counts'].values()),state['input_bytes'],
                                    state['table_index'],revision+1)
        maxima={t:0 for t in count_tables}
        for p in state['pages']:maxima[p['table']]=max(maxima[p['table']],p['end'] or 0)
        return ScanResult(state['pages'],{t:state['counts'][t] for t in count_tables},state['input_bytes'],
                          state['sequence'],state['schema'],state['source'],maxima)
