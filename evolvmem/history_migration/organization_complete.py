"""Concrete bounded organization chain. Owner quarantine only; no activation API."""
from pathlib import Path
import json, hashlib, sqlite3, time, threading
from contextlib import contextmanager
from dataclasses import asdict
from . import static_wal as W
from . import retained_chain as R
from . import source_plan as SP
from . import source_complete as SC
from . import organization_scan as S
from . import organization_plan as OP
from . import autoincrement_state as A
from .organization_importer import gate
_BATCH_TOKEN=object()
check=SC.check
rows=SC.rows

def digest(v):return hashlib.sha256(S.canonical(v)).hexdigest()

class Plan:
 def __init__(self,document):
  self._raw=S.canonical(document);check(len(self._raw)<=R.MAX_MANIFEST_BYTES,'organization_manifest_budget')
  self.sha256=hashlib.sha256(self._raw).hexdigest()
  d=self.document;self.parent=SP.Plan(d['parent']);self.parent.require_importable()
  SC.I.C.uuid_text(d['run_id']);check(d['run_id'] not in (self.parent.run_id,self.parent.run.core_run_id),'organization_run_identity')
  check(d['format']=='organization-bounded-v1' and d['source']==self.parent.header['source'],'organization_parent_seal_changed')
  counts=dict.fromkeys(S.TABLES,0);last={};ended=set();total=0;previous=-1
  check(0<len(d['pages'])<=R.MAX_PAGES,'organization_page_budget')
  for p in d['pages']:
   t=p['table'];check(t in counts and t not in ended and S.TABLES.index(t)>=previous,'organization_page_order');previous=S.TABLES.index(t)
   check(p['after']==last.get(t) and type(p['row_count']) is int and 0<=p['row_count']<=128 and type(p['exhausted']) is bool,'organization_range')
   check(type(p['input_bytes']) is int and 2<=p['input_bytes']<=S.M.MAX_BYTES and type(p['sha256']) is str and R.HASH.fullmatch(p['sha256']),'organization_digest')
   if p['row_count']:check(type(p['end']) is int and -(2**63)<=p['end']<2**63 and (p['after'] is None or p['end']>p['after']),'organization_range')
   else:check(p['exhausted'] and p['end']==p['after'] and p['sha256']==digest([]) and p['input_bytes']==2,'organization_empty_page')
   counts[t]+=p['row_count'];last[t]=p['end'];total+=p['input_bytes']
   if p['exhausted']:ended.add(t)
  check(ended==set(S.TABLES) and counts==d['counts'] and sum(counts.values())<=R.MAX_ROWS and total==d['input_bytes']<=OP.INPUT_CAP,'organization_totals')
  check(type(d['task_highwater']) is int and max(0,last['organization_tasks'] or 0)<=d['task_highwater']<2**63-1,'organization_highwater')
 @property
 def document(self):return json.loads(self._raw)
 @property
 def header(self):return self.document
 @property
 def run_id(self):return self.document['run_id']
 def page(self,index):
  check(type(index) is int and 0<=index<len(self.document['pages']),'organization_page_index');return self.document['pages'][index]

@contextmanager
def source_phase(source,parent,seconds=60):
 with W.SealedPhase(source,plan=parent,seconds=seconds) as phase:
  c=sqlite3.connect(source.path.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None,timeout=.1)
  try:
   c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,S.M.MAX_BYTES);c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
   c.set_progress_handler(lambda:(phase.tick(),0)[1],200)
   c.execute('BEGIN');S.validate_schema(c);yield c,phase;phase.tick()
  except sqlite3.Error:
   phase.tick();raise SC.M.ImportError('organization_source_sqlite_error') from None
  finally:c.close()
 check(phase.proven_complete,'organization_seal_incomplete')

@contextmanager
def _planning_connection(phase):
 # A page owns its own short transaction. The timer also closes during Python
 # mapping; the lock prevents close racing a native SQLite execute/fetch.
 deadline=min(phase.deadline,time.monotonic()+10)
 check(time.monotonic()<deadline,'organization_page_deadline')
 lock=threading.RLock();closed=False
 c=sqlite3.connect(phase.source.path.as_uri()+'?mode=ro&immutable=1',uri=True,
     isolation_level=None,timeout=.1,check_same_thread=False)
 def tick():
  check(not closed and time.monotonic()<deadline,'organization_page_deadline');phase.tick()
 def close():
  nonlocal closed
  with lock:
   if not closed:c.close();closed=True
 def expire():
  try:c.interrupt()
  except sqlite3.Error:pass
  close()
 class Cursor:
  def __init__(self,raw):self.raw=raw
  def fetchone(self):
   with lock:tick();return self.raw.fetchone()
  def fetchall(self):
   with lock:tick();return self.raw.fetchall()
  def close(self):
   with lock:
    if not closed:self.raw.close()
 class Port:
  def execute(self,sql,args=()):
   with lock:tick();return Cursor(c.execute(sql,args))
 timer=threading.Timer(max(.000001,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
 try:
  c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,S.M.MAX_BYTES)
  c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
  c.set_progress_handler(lambda:(tick(),0)[1],200)
  port=Port();port.execute('BEGIN');S.validate_schema(port);yield port;tick()
 except sqlite3.Error:
  tick();raise SC.M.ImportError('organization_source_sqlite_error') from None
 finally:
  timer.cancel();close()


def make_plan(source,parent,*,run_id,path,seconds=60):
 from . import resume_planning as P
 check(type(parent) is SP.Plan,'organization_parent_plan_required');parent.require_importable()
 check(0<=source.path.stat().st_size<=OP.PHYSICAL_CAP,'physical_source_cap_2GiB')
 def read_page(phase,table,after,ordinal):
  check(phase.binding==parent.header['source'],'organization_parent_seal_changed')
  with _planning_connection(phase) as c:
   seq=S.task_sequence(c)
   p=S.page(c,table,after)
   # The source mapper checks every raw value before any metadata is retained.
   raw=S.canonical(p['rows']);ids=[r['source_rowid'] for r in p['rows']]
   check(hashlib.sha256(raw).hexdigest()==p['sha256'] and len(raw)==p['input_bytes'],'manifest_page_digest')
   check(len(ids)<=128 and ids==sorted(set(ids)) and
       (not ids or (after is None or ids[0]>after) and ids[-1]==p['end']),'manifest_range')
   check(bool(ids) or p['exhausted'] and p['end']==after,'manifest_empty_range')
   metadata={k:v for k,v in p.items() if k!='rows'}|{'row_count':len(ids)}
  return metadata,{'organization_tasks':seq},digest(S.M.EVIDENCE)
 result=P.scan(source,path,run_id=run_id,domain='organization',tables=S.TABLES,count_tables=S.TABLES,
     phase_factory=lambda **kw:W.SealedPhase(source,plan=parent,**kw),read_page=read_page,
     binding=dict(parents={'sources':parent.sha256},target_binding=parent.run.core_binding_hash),seconds=seconds,
     max_rows=R.MAX_ROWS,max_pages=min(R.MAX_PAGES,OP.MAX_PAGES),max_input_bytes=OP.INPUT_CAP)
 if isinstance(result,P.PlanningProgress):return result
 d=dict(format='organization-bounded-v1',source=result.source,parent=parent.document,run_id=run_id,
     counts=result.counts,pages=result.pages,input_bytes=result.input_bytes,
     task_highwater=A.resolved_highwater(dict(result.sequence['organization_tasks'],
       autoincrement_ddl_verified=True,allocation_history_verified=False),
       result.counts['organization_tasks'],result.maxima['organization_tasks']))
 plan=Plan(d);R._private_write(path,plan.document);return plan


def load_plan(path,sha256):
 p=Path(path);check(p.stat().st_size<=R.MAX_MANIFEST_BYTES,'organization_manifest_budget')
 raw=p.read_bytes();check(hashlib.sha256(raw).hexdigest()==sha256,'organization_manifest_changed')
 plan=Plan(json.loads(raw));check(plan.sha256==sha256,'organization_manifest_canonical');return plan

FIELDS='manifest_hash,stage,next_load,next_verify,generation,verify_generation,count_table,count_rows,count_last,parent_generation,proof'
LEDGERS=('history_organization_progress','history_organization_pages')
TOUCH='\nBEGIN\n UPDATE em_content.history_organization_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'
PARENT_GENERATION_TRIGGERS=('history_batch_generation','history_source_batch_generation')
IDENTITY_TABLES=('memories','context_items','session_archives','context_sources','context_evidence','organization_tasks')

def check_parent_trigger_qualifications(c):
 # Existing parent gates validate enabled/type/body; close their WHEN gap here
 # without changing those frozen modules.
 expected=sorted([(t,'history_batch_generation',True) for t in SC.I.C.TABLES+('history_import_chunks','history_import_links')]+
                 [(t,'history_source_batch_generation',True) for t in SC.WATCH])
 found=rows(c,"""SELECT c.relname,t.tgname,t.tgqual IS NULL
  FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='em_content' AND t.tgname=ANY(%s::text[]) ORDER BY c.relname,t.tgname""",
  (list(PARENT_GENERATION_TRIGGERS),))
 check(found==expected,'organization_parent_generation_qualification')

def check_identity_sequences(c):
 # Fixed allowlist includes every parent identity whose completion assumes
 # ascending allocation, plus the organization task identity. No caller SQL.
 found=rows(c,"""SELECT bound.table_name,
  pg_get_serial_sequence('em_content.'||bound.table_name,'id')='em_content.'||bound.table_name||'_id_seq',
  s.seqtypid='int8'::regtype,s.seqincrement,s.seqmin,s.seqmax,s.seqcache,s.seqcycle
  FROM unnest(%s::text[]) AS bound(table_name)
  LEFT JOIN pg_sequence s ON s.seqrelid=pg_get_serial_sequence('em_content.'||bound.table_name,'id')::regclass
  ORDER BY bound.table_name""",(list(IDENTITY_TABLES),))
 expected=[(t,True,True,1,1,2**63-1,1,False) for t in sorted(IDENTITY_TABLES)]
 check(found==expected and all(type(v) is bool for r in found for v in (r[1],r[2],r[7]))
       and all(type(v) is int for r in found for v in r[3:7]),'organization_identity_sequence_configuration')

class DeadlinePort:
 def __init__(self,raw,deadline):self.raw=raw;self.deadline=deadline
 def execute(self,sql,args=()):
  check(time.monotonic()<self.deadline,'organization_stage_deadline')
  result=self.raw.execute(sql,args)
  check(time.monotonic()<self.deadline,'organization_stage_deadline');return result

class Importer(SC.CompleteImporter):
 def _gate(self,c):
  super()._gate(c)
  check_parent_trigger_qualifications(c)
  class GateCursor:
   def execute(self,sql,args=()):self.last=c.execute(sql,args)
   def fetchall(self):
    try:return self.last.fetchall()
    finally:self.last.close()
  gate(GateCursor(),self.target.library_id)
  for t in LEDGERS:c.execute('LOCK TABLE em_content.'+t+' IN SHARE ROW EXCLUSIVE MODE')
  check(rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_organization_batch_schema_migrations')==[(1,'002_organization_bounded',self.target.library_id)],'organization_schema_required')
  private=rows(c,"""SELECT c.relname,c.relowner=current_user::regrole,c.relrowsecurity,c.relforcerowsecurity,
   p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
   pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
   (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
   WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) ORDER BY c.relname""",(list(LEDGERS)+['history_organization_batch_schema_migrations'],))
  check(private==[(t,True,True,True,True,True,True,True) for t in sorted((*LEDGERS,'history_organization_batch_schema_migrations'))],'organization_private_ledger')
  triggers=rows(c,"""SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,t.tgqual IS NULL,
   f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],f.prosrc=%s,
   fn.nspname='em_content' AND f.proname='history_organization_touch'
   FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
   WHERE n.nspname='em_content' AND t.tgname='history_organization_generation' ORDER BY c.relname""",(TOUCH,))
  check(triggers==[(t,True,True,True,True,True,True,True,True,True) for t in sorted((*S.TABLES,'history_organization_pages'))],'organization_generation_trigger')
 @contextmanager
 def batch(self,plan,source,*,seconds=60):
  """One source entry/exit SHA, many independently committed quarantine pages.
  A failed exit leaves pages quarantined and unfinalized; explicit replay is safe.
  """
  check(type(plan) is Plan,'organization_plan_required')
  with source_phase(source,plan.parent,seconds) as (src,phase):
   batch=SourceBatch(_BATCH_TOKEN,self,plan,source,src,phase)
   try:yield batch
   finally:batch.active=False
 @contextmanager
 def _target_stage(self,seconds):
  check(type(seconds) in (int,float) and 0<seconds<=60,'organization_stage_budget')
  deadline=time.monotonic()+seconds;self.capacity_guard()
  with SC.I.C.transaction(self.factory) as raw:
   c=DeadlinePort(raw,deadline);self._gate(c);yield c
   self.capacity_guard();check(time.monotonic()<deadline,'organization_stage_deadline')
 @contextmanager
 def _stage(self,plan,source,seconds):
  check(type(plan) is Plan,'organization_plan_required')
  if type(source) is not SourceBatch:
   # Full source hashes are outside target transactions and target locks.
   with self.batch(plan,source,seconds=seconds) as batch:
    with self._stage(plan,batch,seconds) as state:yield state
   return
  check(source.active and source.importer is self and source.plan.sha256==plan.sha256,'organization_batch_changed')
  src,phase=source.connection,source.phase;phase.tick()
  with self._target_stage(min(seconds,max(.000001,phase.deadline-time.monotonic()))) as c:
   parent=super()._progress(c,plan.parent);check(parent['stage']=='verified','organization_parent_not_verified')
   check(S.task_highwater(src)==plan.document['task_highwater'],'organization_sequence_changed')
   yield c,src,phase,parent['generation'];phase.tick()
 def _progress_org(self,c,plan,parentgen):
  found=rows(c,'SELECT '+FIELDS+' FROM em_content.history_organization_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
  check(len(found)==1,'organization_run_missing');p=dict(zip(FIELDS.split(','),found[0]))
  check(p['manifest_hash']==plan.sha256 and p['parent_generation']==parentgen,'organization_run_binding_changed')
  if p['stage']!='loading':check(p['generation']==p['verify_generation'],'organization_generation_changed')
  return p
 def begin(self,plan,source,*,seconds=60):
  with self._stage(plan,source,seconds) as (c,src,phase,g):
   existing=rows(c,'SELECT run_id::text FROM em_content.history_organization_progress')
   if existing:check(existing==[(plan.run_id,)],'organization_target_occupied')
   else:
    for t in S.TABLES:check(not rows(c,'SELECT 1 FROM em_content.'+t+' LIMIT 1'),'organization_target_occupied')
    check(not rows(c,'SELECT 1 FROM em_content.history_organization_pages LIMIT 1'),'organization_receipt_occupied')
    c.execute("INSERT INTO em_content.history_organization_progress(run_id,manifest_hash,stage,parent_generation) VALUES(%s::uuid,%s,'loading',%s)",(plan.run_id,plan.sha256,g))
   result=self._progress_org(c,plan,g)
  return result
 def _readback(self,c,plan,index,data):
  e=plan.page(index);t=e['table'];names=('source_rowid',)+tuple(S.M.SPECS[t]);quoted=','.join('"'+n+'"' for n in names)
  receipt=rows(c,'SELECT page_hash,row_count,input_bytes FROM em_content.history_organization_pages WHERE run_id=%s::uuid AND ordinal=%s',(plan.run_id,index))
  check(receipt==[(e['sha256'],e['row_count'],e['input_bytes'])],'organization_receipt_conflict')
  if data:
   condition=' FROM em_content.'+t+' WHERE library_id=%s::uuid AND source_rowid>=%s AND source_rowid<=%s ORDER BY source_rowid LIMIT 129'
   args=(self.target.library_id,data[0]['source_rowid'],data[-1]['source_rowid'])
   lengths='+'.join('COALESCE(octet_length("'+n+'"),0)' for n,spec in S.M.SPECS[t].items() if spec['type']!='INTEGER') or '0'
   sizes=rows(c,'SELECT '+lengths+condition,args)
   check(len(sizes)==len(data) and sum(r[0] for r in sizes)<=S.M.MAX_BYTES,'organization_readback_budget')
   got=rows(c,'SELECT '+quoted+condition,args)
   read=[dict(zip(names,row)) for row in got]
   for row in read:S.M.map_row(t,row)
   check(S.canonical(read)==S.canonical(data),'organization_readback_changed')
 def load_page(self,plan,source,index,*,seconds=60):
  e=plan.page(index)
  with self._stage(plan,source,seconds) as (c,src,phase,g):
   p=self._progress_org(c,plan,g);data=OP.checked_page(src,plan.document,index)
   if index<p['next_load']:self._readback(c,plan,index,data);replayed=True
   else:
    check(p['stage']=='loading' and index==p['next_load'],'organization_load_frontier')
    t=e['table'];names=('source_rowid',)+tuple(S.M.SPECS[t]);quoted=','.join('"'+n+'"' for n in names)
    for row in data:
     phase.tick();c.execute('INSERT INTO em_content.'+t+' (library_id,project_id,'+quoted+')'+(' OVERRIDING SYSTEM VALUE' if t=='organization_tasks' else '')+' VALUES(%s::uuid,NULL,'+','.join(['%s']*len(names))+')',(self.target.library_id,*(row[n] for n in names)))
    c.execute('INSERT INTO em_content.history_organization_pages(run_id,ordinal,page_hash,row_count,input_bytes) VALUES(%s::uuid,%s,%s,%s,%s)',(plan.run_id,index,e['sha256'],e['row_count'],e['input_bytes']))
    self._readback(c,plan,index,data)
    c.execute('UPDATE em_content.history_organization_progress SET next_load=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
    if index+1==len(plan.document['pages']):c.execute("UPDATE em_content.history_organization_progress SET stage='verifying',verify_generation=generation WHERE run_id=%s::uuid",(plan.run_id,))
    replayed=False
   result=dict(replayed=replayed,progress=self._progress_org(c,plan,g))
  return result
 def verify_page(self,plan,source,index,*,seconds=60):
  with self._stage(plan,source,seconds) as (c,src,phase,g):
   p=self._progress_org(c,plan,g);check(p['stage'] in ('verifying','counting','verified') and index<=p['next_verify'],'organization_verify_frontier')
   self._readback(c,plan,index,OP.checked_page(src,plan.document,index))
   if index==p['next_verify']:c.execute('UPDATE em_content.history_organization_progress SET next_verify=%s,stage=%s WHERE run_id=%s::uuid',(index+1,'counting' if index+1==len(plan.document['pages']) else 'verifying',plan.run_id))
   result=self._progress_org(c,plan,g)
  return result
 def count_page(self,plan,source,*,seconds=60):
  with self._stage(plan,source,seconds) as (c,src,phase,g):
   p=self._progress_org(c,plan,g);check(p['stage']=='counting' and p['count_table']<7,'organization_count_frontier')
   table=S.TABLES[p['count_table']];where='' if p['count_last'] is None else ' WHERE (library_id,source_rowid)>(%s::uuid,%s)';args=() if not where else (self.target.library_id,p['count_last'])
   found=rows(c,'SELECT library_id::text,project_id::text,source_rowid FROM em_content.'+table+where+' ORDER BY library_id,source_rowid LIMIT 128',args)
   check(all(r[0]==self.target.library_id and r[1] is None and type(r[2]) is int for r in found),'organization_scope_changed')
   total=p['count_rows']+len(found);expected=plan.document['counts'][table];done=len(found)<128
   check(total<=expected and (not done or total==expected),'organization_target_count_changed')
   c.execute('UPDATE em_content.history_organization_progress SET count_table=%s,count_rows=%s,count_last=%s WHERE run_id=%s::uuid',(p['count_table']+int(done),0 if done else total,None if done else found[-1][2],plan.run_id))
   result=self._progress_org(c,plan,g)
  return result
 def finalize(self,plan,source,*,seconds=60):
  check(type(source) is W.SealedWalSource,'organization_finalize_requires_closed_batch')
  with self._stage(plan,source,seconds) as (c,src,phase,g):
   p=self._progress_org(c,plan,g);check(p['stage'] in ('counting','verified') and p['count_table']==7 and p['next_verify']==len(plan.document['pages']),'organization_incomplete')
   check_identity_sequences(c)
   seq='em_content.organization_tasks_id_seq'
   check(rows(c,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.organization_tasks','id'))==[(seq,)],'organization_sequence_identity')
   last,called=rows(c,'SELECT last_value,is_called FROM '+seq)[0]
   high=plan.document['task_highwater'];next_id=max(1,high+1,last+int(called));check(next_id<2**63,'organization_sequence_exhausted')
   if p['stage']=='verified':
    check(type(p['proof']) is dict and type(p['proof'].get('next_task_id')) is int and last>=p['proof']['next_task_id']>=high+1,'organization_final_proof_changed')
    expected=dict(state='organization_domain_verified',manifest_hash=plan.sha256,source_sha256=source.sha256,generation=p['generation'],parent_generation=g,parent_plan_hash=plan.parent.sha256,next_task_id=p['proof']['next_task_id'],route_activated=False,history_complete=False)
    check(p['proof']==expected,'organization_final_proof_changed');result=p['proof']
   else:
    c.execute('ALTER SEQUENCE '+seq+' RESTART WITH '+str(next_id))
    result=dict(state='organization_domain_verified',manifest_hash=plan.sha256,source_sha256=source.sha256,generation=p['generation'],parent_generation=g,parent_plan_hash=plan.parent.sha256,next_task_id=next_id,route_activated=False,history_complete=False)
  # Source phase has exited successfully here. Publish in a fresh short target
  # transaction, rechecking parents and the exact previously verified generation.
  with self._target_stage(seconds) as c:
   check_identity_sequences(c)
   parent=super()._progress(c,plan.parent);check(parent['stage']=='verified','organization_parent_not_verified')
   p=self._progress_org(c,plan,parent['generation'])
   check(p['generation']==result['generation'] and parent['generation']==result['parent_generation'] and p['count_table']==7 and p['next_verify']==len(plan.document['pages']),'organization_publish_generation_changed')
   last,called=rows(c,'SELECT last_value,is_called FROM em_content.organization_tasks_id_seq')[0]
   check(last>=result['next_task_id'],'organization_sequence_regressed')
   c.execute("UPDATE em_content.history_organization_progress SET stage='verified',proof=%s::jsonb WHERE run_id=%s::uuid",(S.canonical(result).decode(),plan.run_id))
  return result

class SourceBatch:
 """Opaque live source-phase handle; never a serializable snapshot token."""
 def __init__(self,token,importer,plan,source,connection,phase):
  check(token is _BATCH_TOKEN,'organization_batch_factory_required')
  self.importer,self.plan,self.source,self.connection,self.phase=importer,plan,source,connection,phase
  self.active=True
 def begin(self,**kw):return self.importer.begin(self.plan,self,**kw)
 def load_page(self,index,**kw):return self.importer.load_page(self.plan,self,index,**kw)
 def verify_page(self,index,**kw):return self.importer.verify_page(self.plan,self,index,**kw)
 def count_page(self,**kw):return self.importer.count_page(self.plan,self,**kw)
