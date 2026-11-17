"""Owner-only metadata sidecar for two otherwise-lost SQLite rowids.

Candidate API: prepare_locator, sync_locator, verify_locator, status_locator,
recover_locator. One page/transaction per call, no retries and no activation.
Original schemas, key spaces, content hashes and frozen readers are untouched.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import re

from . import retained_chain as R
from . import source_plan as SP
from . import source_complete as SC
from . import source_locator_source as S
from .operator_scope import OperatorRuntime
from .operator_config import private_read, trusted
from .organization_complete import check_parent_trigger_qualifications
from .history_core.mapping import canonical, digest, check, ImportError

VERSION='history-source-locator-two-v1'
LEDGERS=('history_source_locator_progress','history_source_locators','history_source_locator_operations')
TOUCH='\nBEGIN\n UPDATE em_content.history_source_locator_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'
FIELDS='binding_hash,binding,stage,load_table,load_after,load_count,verify_table,verify_after,verify_count,input_bytes,generation,verify_generation,parent_generations,proof'
MAX_ROWS=250000
MAX_INPUT_BYTES=1024**3
COLUMNS={
 LEDGERS[0]:(('singleton','bool',True),('binding_hash','text',True),('binding','jsonb',True),('stage','text',True),
  ('load_table','int4',True),('load_after','int8',False),('load_count','int8',True),('verify_table','int4',True),
  ('verify_after','int8',False),('verify_count','int8',True),('input_bytes','int8',True),('generation','int8',True),
  ('verify_generation','int8',False),('parent_generations','jsonb',True),('proof','jsonb',False)),
 LEDGERS[1]:(('library_id','uuid',True),('core_run_id','uuid',True),('source_run_id','uuid',True),('table_code','text',True),
  ('source_rowid','int8',True),('source_key_json','text',True),('source_key_hash','text',True),('visible_row_hash','text',True)),
 LEDGERS[2]:(('operation_hash','text',True),('intent_hash','text',True),('result','jsonb',True))}
UNIQUE_KEYS={LEDGERS[0]:[('p',['singleton'])],LEDGERS[1]:[
 ('p',['library_id','table_code','source_rowid']),('u',['library_id','table_code','source_key_hash'])],
 LEDGERS[2]:[('p',['operation_hash'])]}


def _context(runtime,core,sources):
    check(type(runtime) is OperatorRuntime,'locator_trusted_runtime_required');trusted(runtime.config)
    check(type(core) is R.Plan and type(sources) is SP.Plan,'locator_trusted_parent_plans_required')
    core.require_importable();sources.require_importable()
    source=runtime.config.source
    check(core.header['source']==sources.header['source'] and sources.run.core_run_id==core.run_id,'locator_parent_plan_mismatch')
    check(core.header['source'].get('seal_receipt_sha256')==source.receipt_sha256 and
          sources.run.source_file_sha256==source.sha256 and sources.run.source_namespace==source.namespace,'locator_source_binding_changed')
    check(tuple(sorted(core.project_mapping))==tuple(runtime.config.project_mapping),'locator_project_mapping_changed')
    counts={S.TABLES[0]:core.header['counts'][S.TABLES[0]],S.TABLES[1]:sources.counts[S.TABLES[1]]}
    check(all(type(v) is int and 0<=v<=MAX_ROWS for v in counts.values()) and sum(counts.values())<=MAX_ROWS,'locator_total_budget')
    binding=dict(format=VERSION,operator_binding=runtime.config.binding_hash,library_id=runtime.target.library_id,
                 target=asdict(runtime.target),namespace=source.namespace,source_seal_sha256=source.receipt_sha256,
                 source_file_sha256=source.sha256,core_run_id=core.run_id,core_manifest_hash=core.sha256,
                 source_run_id=sources.run_id,source_manifest_hash=sources.sha256,counts=counts)
    port=SC.CompleteImporter(runtime.factory,runtime.target,capacity_guard=runtime.guard)
    return port,binding


def _parents(port,c,core,sources):
    check_parent_trigger_qualifications(c)
    cp=port._core._progress(c,core);sp=port._progress(c,sources)
    check(all(p['stage']=='verified' and p['generation']==p['verify_generation'] for p in (cp,sp)),'locator_verified_parents_required')
    check(cp['loaded_counts']==core.header['counts'] and sp['loaded_counts']==sources.counts,'locator_parent_counts_incomplete')
    return dict(core=cp['generation'],sources=sp['generation'])


def _schema(c):
    for table in LEDGERS:c.execute('LOCK TABLE em_content.'+table+' IN SHARE ROW EXCLUSIVE MODE')
    rows=SC.rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
      p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
      pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
      (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1,
      NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a WHERE a.grantee<>c.relowner)
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
      WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_locator_owner_only' ORDER BY c.relname''',(list(LEDGERS),))
    check(rows==[(t,True,True,True,True,True,True,True,True) for t in sorted(LEDGERS)],'locator_private_schema_required')
    columns=SC.rows(c,'''SELECT c.relname,a.attname,t.typname,a.attnotnull,
      c.relkind='r' AND c.relpersistence='p' AND NOT c.relispartition,
      a.attidentity='' AND a.attgenerated='' AND a.attacl IS NULL
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_attribute a ON a.attrelid=c.oid JOIN pg_type t ON t.oid=a.atttypid
      WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND a.attnum>0 AND NOT a.attisdropped
      ORDER BY c.relname,a.attnum''',(list(LEDGERS),))
    expected=[(table,*column,True,True) for table in sorted(COLUMNS) for column in COLUMNS[table]]
    check(columns==expected,'locator_columns_changed')
    check(not SC.rows(c,'''SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid IN (i.inhrelid,i.inhparent)
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) LIMIT 1''',(list(LEDGERS),)),'locator_inheritance_forbidden')
    unique=SC.rows(c,'''SELECT c.relname,k.contype,
      ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY x(attnum,ord)
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=x.attnum ORDER BY x.ord),
      k.convalidated AND NOT k.condeferrable AND NOT k.condeferred,
      i.indisunique AND i.indisvalid AND i.indisready AND i.indimmediate
        AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=i.indnatts,
      am.amname='btree'
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_constraint k ON k.conrelid=c.oid
      JOIN pg_index i ON i.indexrelid=k.conindid JOIN pg_class ix ON ix.oid=i.indexrelid
      JOIN pg_am am ON am.oid=ix.relam WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[])
      AND k.contype IN ('p','u') ORDER BY c.relname,k.contype''',(list(LEDGERS),))
    check(unique==[(t,kind,names,True,True,True) for t in sorted(UNIQUE_KEYS) for kind,names in UNIQUE_KEYS[t]],'locator_keys_changed')
    triggers=SC.rows(c,'''SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,t.tgqual IS NULL,
      f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],f.prosrc=%s,
      fn.nspname='em_content' AND f.proname='history_source_locator_touch'
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
      WHERE n.nspname='em_content' AND t.tgname='history_source_locator_generation' ORDER BY c.relname''',(TOUCH,))
    check(triggers==[('history_source_locators',True,True,True,True,True,True,True,True,True)],'locator_generation_trigger_changed')
    check(SC.rows(c,'''SELECT c.relname,t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[])
      AND NOT t.tgisinternal ORDER BY c.relname,t.tgname''',(list(LEDGERS),))==
      [('history_source_locators','history_source_locator_generation')],'locator_extra_trigger')
    check(not SC.rows(c,'''SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) LIMIT 1''',(list(LEDGERS),)),'locator_rewrite_forbidden')


def _progress(c,binding,generations):
    sizes=SC.rows(c,'''SELECT octet_length(binding::text),octet_length(parent_generations::text),
      COALESCE(octet_length(proof::text),0) FROM em_content.history_source_locator_progress LIMIT 2''')
    check(len(sizes)==1 and all(type(n) is int and 0<=n<=32768 for n in sizes[0]) and sum(sizes[0])<=32768,'locator_progress_metadata_budget')
    values=SC.rows(c,'SELECT '+FIELDS+' FROM em_content.history_source_locator_progress WHERE singleton FOR UPDATE')
    check(len(values)==1,'locator_progress_missing');p=dict(zip(FIELDS.split(','),values[0]))
    check(p['binding_hash']==digest(binding) and p['binding']==binding,'locator_binding_changed')
    check(p['parent_generations']==generations,'locator_parent_generation_changed')
    check(p['stage'] in ('loading','verifying','verified'),'locator_stage_invalid')
    for field in ('load_table','verify_table'):check(type(p[field]) is int and 0<=p[field]<=2,'locator_progress_type')
    for field in ('load_count','verify_count','generation'):check(type(p[field]) is int and 0<=p[field]<=2**63-1,'locator_progress_type')
    check(type(p['input_bytes']) is int and 0<=p['input_bytes']<=MAX_INPUT_BYTES,'locator_input_budget')
    for prefix in ('load','verify'):
        index=p[prefix+'_table'];after=p[prefix+'_after'];count=p[prefix+'_count']
        check(after is None or type(after) is int and -(2**63)<=after<2**63,'locator_progress_type')
        check((after is None)==(count==0),'locator_progress_frontier')
        check(index<2 and count<=binding['counts'][S.TABLES[index]] or index==2 and after is None and count==0,'locator_progress_frontier')
    if p['stage']=='loading':
        check(p['load_table']<2 and p['verify_table']==0 and p['verify_after'] is None and p['verify_count']==0 and p['verify_generation'] is None and p['proof'] is None,'locator_loading_progress')
    else:
        check(type(p['verify_generation']) is int and p['generation']==p['verify_generation'],'locator_generation_changed')
        check(p['load_table']==2,'locator_loaded_frontier_incomplete')
        if p['stage']=='verifying':check(p['verify_table']<2 and p['proof'] is None,'locator_verifying_progress')
        else:
            check(p['verify_table']==2,'locator_verified_frontier_incomplete');_check_proof(p['proof'],binding,generations,p['generation'])
    return p


def _proof(binding,generations,generation,hash_passes=2):
    result=dict(format=VERSION,complete=True,binding_hash=digest(binding),source_phase_complete=True,
                source_seal_sha256=binding['source_seal_sha256'],source_file_sha256=binding['source_file_sha256'],
                source_hash_passes=hash_passes,parent_generations=generations,target_generation=generation,
                tables={t:dict(rows=binding['counts'][t],source_generation=generations['core' if t==S.TABLES[0] else 'sources'],target_generation=generation) for t in S.TABLES},
                history_complete=False,route_activated=False)
    result['proof_sha256']=digest(result);return result


def _check_proof(proof,binding,generations,generation):
    check(type(proof) is dict and type(proof.get('source_hash_passes')) is int and proof['source_hash_passes']>=2,'locator_proof_missing')
    check(proof==_proof(binding,generations,generation,proof['source_hash_passes']),'locator_proof_binding_changed')


@contextmanager
def _tx(runtime,core,sources,ready=True):
    port,binding=_context(runtime,core,sources)
    with port._tx() as c:
        generations=_parents(port,c,core,sources)
        if ready:
            _schema(c);p=_progress(c,binding,generations)
        else:p=None
        yield c,binding,generations,p


def _public(p):
    return dict(stage=p['stage'],complete=p['stage']=='verified',load_table=p['load_table'],load_after=p['load_after'],
                verify_table=p['verify_table'],verify_after=p['verify_after'],generation=p['generation'],
                verify_generation=p['verify_generation'],parent_generations=p['parent_generations'],proof=p['proof'])


def _receipt(runtime,path):
    p=Path(path);w=runtime.config.work_dir
    check(p.is_absolute() and p.resolve()==p and p.parent==w and re.fullmatch(r'locator-[a-z0-9_.-]{1,90}\.json',p.name)
          and p.name!='locator-pending.json','locator_private_receipt_required')
    return p


def _write(path,value):R._private_write(path,value)


def _no_pending(runtime):
    check(not (runtime.config.work_dir/'locator-pending.json').exists(),'locator_explicit_recovery_required')


def _clear_pending(pending,operation,name):
    check(json.loads(private_read(pending))==dict(operation=operation,receipt_name=name),'locator_pending_changed')
    pending.unlink()


def _effect(runtime,core,sources,path,kind,request,action,*,ready=True):
    """Durable intent -> one target transaction -> receipt. Never replay effects."""
    _,binding=_context(runtime,core,sources);path=_receipt(runtime,path)
    pending=runtime.config.work_dir/'locator-pending.json'
    check(not pending.exists(),'locator_explicit_recovery_required')
    check(not any(Path(str(path)+suffix).exists() for suffix in ('','.started','.unknown','.failed','.recovered')),'locator_receipt_already_used')
    intent=dict(format=VERSION,operation=kind,binding_hash=digest(binding),request=request,receipt_name=path.name)
    operation=digest(intent)
    _write(str(path)+'.started',intent)
    _write(pending,dict(operation=operation,receipt_name=path.name))
    result=None
    try:
        with _tx(runtime,core,sources,ready=ready) as (c,b,g,p):
            result=action(c,b,g,p)
            c.execute('INSERT INTO em_content.history_source_locator_operations(operation_hash,intent_hash,result) VALUES(%s,%s,%s::jsonb)',
                      (operation,digest(intent),canonical(result).decode()))
    except BaseException as exc:
        code=exc.code if isinstance(exc,ImportError) else 'locator_effect_failed'
        unknown=code=='commit_outcome_unknown'
        _write(str(path)+('.unknown' if unknown else '.failed'),dict(operation=operation,code=code,complete=False))
        if not unknown:_clear_pending(pending,operation,path.name)
        raise
    # If receipt IO fails after commit, leave pending; explicit recovery is safe.
    try:_write(path,dict(operation=operation,commit='confirmed',result=result))
    except OSError:
        # The database commit succeeded but a durable local final receipt did
        # not. Keep the intent+pending pair; recovery queries its atomic ledger.
        raise ImportError('locator_commit_confirmed_receipt_incomplete') from None
    _clear_pending(pending,operation,path.name)
    return result


def _ddl(c):
    # New sidecar only. Never adopt, drop or alter an existing relation.
    check(not SC.rows(c,"SELECT relname FROM pg_class WHERE relnamespace='em_content'::regnamespace AND relname=ANY(%s::text[])",(list(LEDGERS),)),'locator_schema_already_exists')
    c.execute('''CREATE TABLE em_content.history_source_locator_progress(
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),binding_hash text NOT NULL CHECK(binding_hash ~ '^[0-9a-f]{64}$'),binding jsonb NOT NULL,
      stage text NOT NULL CHECK(stage IN ('loading','verifying','verified')),
      load_table integer NOT NULL DEFAULT 0 CHECK(load_table BETWEEN 0 AND 2),load_after bigint,load_count bigint NOT NULL DEFAULT 0 CHECK(load_count>=0),
      verify_table integer NOT NULL DEFAULT 0 CHECK(verify_table BETWEEN 0 AND 2),verify_after bigint,verify_count bigint NOT NULL DEFAULT 0 CHECK(verify_count>=0),
      input_bytes bigint NOT NULL DEFAULT 0 CHECK(input_bytes BETWEEN 0 AND 1073741824),
      generation bigint NOT NULL DEFAULT 0,verify_generation bigint,parent_generations jsonb NOT NULL,proof jsonb)''')
    c.execute('''CREATE TABLE em_content.history_source_locators(
      library_id uuid NOT NULL,core_run_id uuid NOT NULL,source_run_id uuid NOT NULL,
      table_code text NOT NULL CHECK(table_code IN ('context_project_registry','session_archive_holds')),
      source_rowid bigint NOT NULL,source_key_json text NOT NULL,source_key_hash text NOT NULL CHECK(source_key_hash ~ '^[0-9a-f]{64}$'),
      visible_row_hash text NOT NULL CHECK(visible_row_hash ~ '^[0-9a-f]{64}$'),
      PRIMARY KEY(library_id,table_code,source_rowid),UNIQUE(library_id,table_code,source_key_hash))''')
    c.execute('''CREATE TABLE em_content.history_source_locator_operations(
      operation_hash text PRIMARY KEY CHECK(operation_hash ~ '^[0-9a-f]{64}$'),intent_hash text NOT NULL,result jsonb NOT NULL)''')
    for table in LEDGERS:
        c.execute('REVOKE ALL ON em_content.'+table+' FROM PUBLIC')
        c.execute('ALTER TABLE em_content.'+table+' ENABLE ROW LEVEL SECURITY')
        c.execute('ALTER TABLE em_content.'+table+' FORCE ROW LEVEL SECURITY')
        c.execute('CREATE POLICY history_locator_owner_only ON em_content.'+table+' TO CURRENT_USER USING(true) WITH CHECK(true)')
    c.execute("CREATE FUNCTION em_content.history_source_locator_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$"+TOUCH+'$body$')
    c.execute('REVOKE ALL ON FUNCTION em_content.history_source_locator_touch() FROM PUBLIC')
    c.execute('CREATE TRIGGER history_source_locator_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.history_source_locators FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_source_locator_touch()')
    c.execute('ALTER TABLE em_content.history_source_locators ENABLE ALWAYS TRIGGER history_source_locator_generation')


def prepare_locator(runtime,core_plan,sources_plan,receipt_path):
    _context(runtime,core_plan,sources_plan)
    _no_pending(runtime);_receipt(runtime,receipt_path)
    fresh=S.seal_exit(runtime.config.source,core_plan,sources_plan)
    def create(c,b,g,p):
        _ddl(c)
        c.execute("INSERT INTO em_content.history_source_locator_progress(singleton,binding_hash,binding,stage,parent_generations) VALUES(true,%s,%s::jsonb,'loading',%s::jsonb)",
                  (digest(b),canonical(b).decode(),canonical(g).decode()))
        _schema(c)
        return dict(_public(_progress(c,b,g)),source_phase_complete=fresh['source_phase_complete'])
    return _effect(runtime,core_plan,sources_plan,receipt_path,'prepare',{},create,ready=False)


def status_locator(runtime,core_plan,sources_plan):
    with _tx(runtime,core_plan,sources_plan) as (c,b,g,p):result=_public(p)
    return result


def _target_records(c,b,table,after,*,end=None,range_only=False):
    check(table in S.TABLES,'locator_table_not_allowed')
    where=' WHERE table_code=%s';args=[table]
    if after is not None:where+=' AND source_rowid>%s';args.append(after)
    if range_only:
        if end is None:return []
        where+=' AND source_rowid<=%s';args.append(end)
    tail=where+' ORDER BY source_rowid,library_id LIMIT 129'
    # Owner locks keep probe/read in the same short transaction. A corrupted
    # oversized locator must stop before the client materializes its key text.
    sizes=SC.rows(c,'SELECT source_rowid,octet_length(source_key_json),octet_length(source_key_hash),octet_length(visible_row_hash) FROM em_content.history_source_locators'+tail,tuple(args))
    check(len(sizes)<=128 and all(type(r[0]) is int and all(type(n) is int and n>=0 for n in r[1:]) for r in sizes),'locator_target_probe_changed')
    check(sum(sum(r[1:])+256 for r in sizes)<=S.MAX_BYTES,'locator_target_page_byte_budget')
    found=SC.rows(c,'SELECT library_id::text,core_run_id::text,source_run_id::text,source_rowid,source_key_json,source_key_hash,visible_row_hash FROM em_content.history_source_locators'+tail,tuple(args))
    check(len(found)<=128,'locator_unreceipted_target_rows')
    check(all(r[:3]==(b['library_id'],b['core_run_id'],b['source_run_id']) for r in found),'locator_target_scope_changed')
    check([r[3] for r in found]==[r[0] for r in sizes],'locator_target_range_changed')
    return [dict(zip(('source_rowid','source_key_json','source_key_hash','visible_row_hash'),r[3:])) for r in found]


def _page_target(c,b,page):
    actual=_target_records(c,b,page['table'],page['after'],end=page['end'],range_only=not page['exhausted'])
    check(actual==page['records'] and digest(actual)==page['page_hash'],'locator_target_readback_changed')


def _request(page):
    return {k:page[k] for k in ('table','after','end','row_count','page_hash','exhausted','source_phase_complete','source_hash_passes','source_seal_sha256')}


def sync_locator(runtime,core_plan,sources_plan,receipt_path):
    before=status_locator(runtime,core_plan,sources_plan)
    _no_pending(runtime);_receipt(runtime,receipt_path)
    check(before['stage']=='loading' and before['load_table']<2,'locator_load_stage_required')
    table=S.TABLES[before['load_table']]
    page=S.read_page(runtime.config.source,core_plan,sources_plan,table,before['load_after'])
    def load(c,b,g,p):
        check(p['stage']=='loading' and p['load_table']==before['load_table'] and p['load_after']==page['after'],'locator_load_frontier_changed')
        count=p['load_count']+page['row_count'];check(count<=b['counts'][table],'locator_source_count_exceeded')
        if page['exhausted']:check(count==b['counts'][table],'locator_source_count_incomplete')
        for r in page['records']:
            c.execute('''INSERT INTO em_content.history_source_locators(library_id,core_run_id,source_run_id,table_code,source_rowid,source_key_json,source_key_hash,visible_row_hash)
              VALUES(%s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s,%s)''',
                      (b['library_id'],b['core_run_id'],b['source_run_id'],table,r['source_rowid'],r['source_key_json'],r['source_key_hash'],r['visible_row_hash']))
        _page_target(c,b,page)
        nxt=p['load_table']+int(page['exhausted'])
        c.execute('''UPDATE em_content.history_source_locator_progress SET load_table=%s,load_after=%s,load_count=%s,input_bytes=input_bytes+%s,
          stage=%s,verify_generation=CASE WHEN %s=2 THEN generation ELSE NULL END WHERE singleton''',
                  (nxt,None if page['exhausted'] else page['end'],0 if page['exhausted'] else count,page['input_bytes'],'verifying' if nxt==2 else 'loading',nxt))
        result=_public(_progress(c,b,g));result.update(page=_request(page),source_phase_complete=True)
        return result
    return _effect(runtime,core_plan,sources_plan,receipt_path,'sync',_request(page),load)


def verify_locator(runtime,core_plan,sources_plan,receipt_path=None,expected_rows=None):
    """One independently sealed source page, one bounded target transaction.

    expected_rows is deliberately not an authority input. Runner's independent
    original-field oracle separately compares fixtures; this function binds the
    actual sealed source, current parents and actual metadata sidecar.
    """
    before=status_locator(runtime,core_plan,sources_plan)
    _no_pending(runtime)
    if before['stage']=='verified':
        fresh=S.seal_exit(runtime.config.source,core_plan,sources_plan)
        # Recheck parents and sidecar generation AFTER source exit, not only
        # before the potentially slower file hashing operation.
        with _tx(runtime,core_plan,sources_plan) as (c,b,g,p):
            check(p['stage']=='verified' and p['generation']==before['generation'] and g==before['parent_generations'],'locator_final_generation_changed')
            _check_proof(p['proof'],b,g,p['generation'])
            check(fresh['source_seal_sha256']==b['source_seal_sha256'] and fresh['source_file_sha256']==b['source_file_sha256'],'locator_fresh_source_changed')
            result=dict(p['proof'],fresh_source_exit=fresh)
        return result
    check(receipt_path is not None and before['stage']=='verifying' and before['verify_table']<2,'locator_verify_stage_required')
    _receipt(runtime,receipt_path)
    table=S.TABLES[before['verify_table']]
    page=S.read_page(runtime.config.source,core_plan,sources_plan,table,before['verify_after'])
    def verify(c,b,g,p):
        check(p['stage']=='verifying' and p['verify_table']==before['verify_table'] and p['verify_after']==page['after'],'locator_verify_frontier_changed')
        _page_target(c,b,page)
        count=p['verify_count']+page['row_count'];check(count<=b['counts'][table],'locator_verify_count_exceeded')
        if page['exhausted']:check(count==b['counts'][table],'locator_verify_count_incomplete')
        nxt=p['verify_table']+int(page['exhausted']);proof=None
        if nxt==2:
            # Only fixed two-table codes are permitted; no hidden third scope.
            check(not SC.rows(c,'SELECT 1 FROM em_content.history_source_locators WHERE table_code<>ALL(%s::text[]) LIMIT 1',(list(S.TABLES),)),'locator_unknown_table')
            proof=_proof(b,g,p['generation'],page['source_hash_passes'])
        c.execute('''UPDATE em_content.history_source_locator_progress SET verify_table=%s,verify_after=%s,verify_count=%s,stage=%s,proof=%s::jsonb WHERE singleton''',
                  (nxt,None if page['exhausted'] else page['end'],0 if page['exhausted'] else count,'verified' if nxt==2 else 'verifying',None if proof is None else canonical(proof).decode()))
        result=_public(_progress(c,b,g));result.update(page=_request(page),source_phase_complete=True)
        if proof is not None:result.update(proof)
        return result
    return _effect(runtime,core_plan,sources_plan,receipt_path,'verify',_request(page),verify)


def recover_locator(runtime,core_plan,sources_plan,receipt_path):
    """Explicit outcome lookup only. Returns not_applied without replaying it."""
    path=_receipt(runtime,receipt_path);pending=runtime.config.work_dir/'locator-pending.json'
    intent=json.loads(private_read(str(path)+'.started',limit=8*1024**2))
    marker=json.loads(private_read(pending));operation=digest(intent)
    _,binding=_context(runtime,core_plan,sources_plan)
    check(set(intent)=={'format','operation','binding_hash','request','receipt_name'} and intent['format']==VERSION
          and intent['operation'] in ('prepare','sync','verify') and intent['receipt_name']==path.name,'locator_recovery_intent_invalid')
    check(marker==dict(operation=operation,receipt_name=path.name) and intent['binding_hash']==digest(binding),'locator_recovery_binding_changed')
    fresh=S.seal_exit(runtime.config.source,core_plan,sources_plan)
    with _tx(runtime,core_plan,sources_plan,ready=False) as (c,b,g,p):
        exists=SC.rows(c,"SELECT to_regclass('em_content.history_source_locator_operations') IS NOT NULL")==[(True,)]
        if not exists:
            check(intent['operation']=='prepare','locator_schema_missing_after_effect');found=[]
            check(not SC.rows(c,"SELECT relname FROM pg_class WHERE relnamespace='em_content'::regnamespace AND relname=ANY(%s::text[])",(list(LEDGERS),)),'locator_partial_schema')
        else:
            _schema(c);_progress(c,b,g)
            sizes=SC.rows(c,'SELECT octet_length(intent_hash),octet_length(result::text) FROM em_content.history_source_locator_operations WHERE operation_hash=%s',(operation,))
            check(len(sizes)<=1 and all(r[0]==64 and type(r[1]) is int and 0<r[1]<=32768 for r in sizes),'locator_operation_metadata_budget')
            found=SC.rows(c,'SELECT intent_hash,result FROM em_content.history_source_locator_operations WHERE operation_hash=%s',(operation,))
            check(len(found)<=1,'locator_operation_duplicate')
            if found:
                check(found[0][0]==digest(intent) and type(found[0][1]) is dict,'locator_recovery_intent_changed')
                request=intent['request']
                if request:
                    check(found[0][1].get('page')==request,'locator_recovery_page_receipt_changed')
                    actual=_target_records(c,b,request['table'],request['after'],end=request['end'],range_only=not request['exhausted'])
                    check(len(actual)==request['row_count'] and digest(actual)==request['page_hash'],'locator_recovery_readback_changed')
    result=dict(operation=operation,commit='confirmed_applied' if found else 'confirmed_not_applied',replayed=False,complete=False,source_phase_complete=fresh['source_phase_complete'])
    recovered=Path(str(path)+'.recovered')
    if recovered.exists():check(json.loads(private_read(recovered))==result,'locator_recovery_receipt_changed')
    else:_write(recovered,result)
    _clear_pending(pending,operation,path.name)
    return result
