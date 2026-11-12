"""Independent bounded delta epochs over the original inactive operator target."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import re
import hashlib
import json
from evolvmem.library_project_acl import coordinator as PROJECTS
import uuid
from . import catchup_catalog as C
from .catchup_plan import Plan, release_only_update
from .catchup_source import Snapshot, summarize
from .history_core.importer import transaction

PROGRESS=dict(core='history_batch_progress',sources='history_source_batch_progress',
    organization='history_organization_progress',capture='history_capture_progress',
    governance='history_governance_progress',knowledge='history_knowledge_progress',
    capability='history_capability_progress',continuity='history_continuity_progress',operations='history_operations_progress')

def validate_generation_triggers(tables,rows):
    prefixes=dict(core='history_batch',sources='history_source_batch',
        **{d:'history_'+d for d in PROGRESS if d not in ('core','sources')})
    for t in tables:
        prefix=prefixes[t.domain]
        matches=[r for r in rows if r[0]==t.target_name and r[1]==prefix+'_generation']
        C.check(len(matches)==1,'catchup_generation_trigger_required')
        _,name,enabled,bits,namespace,function,body,owner,security,config,definition=matches[0]
        expected='BEGIN UPDATE em_content.'+PROGRESS[t.domain]+' SET generation=generation+1; RETURN NULL; END;'
        C.check(enabled=='A' and bits==60 and namespace=='em_content' and function==prefix+'_touch'
            and owner is True and security is False and config==['search_path=pg_catalog']
            and re.sub(r'\s+','',body)==re.sub(r'\s+','',expected),'catchup_generation_trigger_changed')

def generation(c,namespace,*,baseline=False):
    domains={t.domain for t in C.tables(namespace)};result={}
    for domain,name in PROGRESS.items():
        if domain not in domains:continue
        rows=c.execute('SELECT run_id::text,generation,stage,verify_generation FROM em_content.'+name).fetchall()
        C.check(len(rows)==1,'catchup_original_progress_required')
        run,gen,stage,verified=rows[0]
        C.check(type(gen) is int and gen>=0,'catchup_generation_invalid')
        if baseline:C.check(stage=='verified' and gen==verified,'catchup_baseline_not_verified')
        result[domain]=dict(run_id=run,generation=gen)
    return result

def anchor(runtime):
    """Bind an actual original baseline or previous complete epoch, not a flag."""
    with transaction(runtime.factory) as c:
        namespace=runtime.config.source.namespace
        exists=c.execute("SELECT to_regclass('em_content.history_catchup_latest') IS NOT NULL").fetchall()==[(True,)]
        latest=c.execute('SELECT l.epoch_id::text,l.completion_hash,l.generation,e.manifest FROM em_content.history_catchup_latest l JOIN em_content.history_catchup_epochs e USING(epoch_id) WHERE l.library_id=%s::uuid AND e.state=\'complete\'',(runtime.target.library_id,)).fetchall() if exists else []
        if latest:
            C.check(len(latest)==1,'catchup_latest_ambiguous');epoch,proof,gen,manifest=latest[0]
            C.check(generation(c,namespace)==gen,'catchup_previous_generation_changed')
            return dict(kind='epoch',epoch_id=epoch,completion_hash=proof,generation=gen,source_seal=manifest['new_seal'])
        gen=generation(c,namespace,baseline=True)
        proofs={}
        for domain,name in PROGRESS.items():
            if domain in gen:
                rows=c.execute('SELECT to_jsonb(p) FROM em_content.'+name+' p').fetchall()
                proofs[domain]=C.digest(rows[0][0])
        return dict(kind='initial',base_binding=runtime.config.binding_hash,
            source_seal=runtime.config.source.receipt_sha256,generation=gen,proofs=proofs,
            completion_hash=C.digest(dict(binding=runtime.config.binding_hash,generation=gen,proofs=proofs)))

def catalog(c,namespace):
    """Actual fixed-table PostgreSQL catalog; expressions/foreign schemas fail closed."""
    tables=C.tables(namespace);by_target={t.target_name:t.name for t in tables};names=list(by_target)
    rows=c.execute('''SELECT c.relname,c.relowner=current_user::regrole,c.relrowsecurity,c.relforcerowsecurity
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) ORDER BY c.relname''',(names,)).fetchall()
    C.check(rows==[(n,True,True,True) for n in sorted(names)],'catchup_owner_quarantine_required')
    unique=[]
    rows=c.execute('''SELECT t.relname,i.relname,ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum,ord)
      LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum WHERE k.ord<=x.indnkeyatts ORDER BY k.ord),
      pg_get_expr(x.indpred,x.indrelid),pg_get_indexdef(x.indexrelid)
      FROM pg_index x JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
      JOIN pg_class i ON i.oid=x.indexrelid WHERE n.nspname='em_content' AND t.relname=ANY(%s::text[]) AND x.indisunique
      ORDER BY t.relname,i.relname''',(names,)).fetchall()
    for name,index,cols,predicate,definition in rows:
        C.check(all(type(v) is str for v in cols),'catchup_expression_index_unsupported')
        t=C.table(by_target[name]);watched=[k for k in t.target_columns if predicate and re.search(r'\b'+re.escape(k)+r'\b',predicate)]
        cols=[k for k in cols if k!='library_id']
        if 'project_id' in cols:continue # NULL project_id never conflicts
        unique.append(dict(table=t.name,index=index,columns=cols,predicate=predicate,predicate_columns=watched,definition=definition))
    foreign=[]
    rows=c.execute('''SELECT ch.relname,p.relname,pn.nspname,x.conname,
      ARRAY(SELECT a.attname FROM unnest(x.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=ch.oid AND a.attnum=k.num ORDER BY ord),
      ARRAY(SELECT a.attname FROM unnest(x.confkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=p.oid AND a.attnum=k.num ORDER BY ord),
      ARRAY(SELECT a.attname FROM unnest(x.conkey) k(num) JOIN pg_attribute a ON a.attrelid=ch.oid AND a.attnum=k.num WHERE NOT a.attnotnull ORDER BY a.attname),
      x.confdeltype::text,x.condeferrable,pg_get_constraintdef(x.oid)
      FROM pg_constraint x JOIN pg_class ch ON ch.oid=x.conrelid JOIN pg_namespace cn ON cn.oid=ch.relnamespace
      JOIN pg_class p ON p.oid=x.confrelid JOIN pg_namespace pn ON pn.oid=p.relnamespace
      WHERE x.contype='f' AND cn.nspname='em_content' AND ch.relname=ANY(%s::text[])
      ORDER BY ch.relname,x.conname''',(names,)).fetchall()
    for child,parent,schema,name,cols,pcols,nullable,action,deferred,definition in rows:
        C.check(schema=='em_content' and parent in by_target,'catchup_unreviewed_fk_target')
        pairs=[(a,b) for a,b in zip(cols,pcols) if a!='library_id']
        C.check(not any(a=='project_id' for a,b in pairs),'catchup_scoped_fk_unsupported')
        foreign.append(dict(table=by_target[child],parent=by_target[parent],name=name,
            columns=[a for a,b in pairs],parent_columns=[b for a,b in pairs],
            nullable=[k for k in nullable if k!='project_id'],delete_action=action,deferrable=deferred,definition=definition))
    triggers=c.execute('''SELECT t.relname,g.tgname,g.tgenabled::text,g.tgtype,pn.nspname,p.proname,p.prosrc,
      p.proowner=current_user::regrole,p.prosecdef,p.proconfig,pg_get_triggerdef(g.oid)
      FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid JOIN pg_namespace n ON n.oid=t.relnamespace
      JOIN pg_proc p ON p.oid=g.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
      WHERE NOT g.tgisinternal AND n.nspname='em_content' AND t.relname=ANY(%s::text[])
      ORDER BY t.relname,g.tgname''',(names,)).fetchall()
    validate_generation_triggers(tables,triggers)
    policies=c.execute('''SELECT t.relname,p.polname,p.polpermissive,p.polcmd::text,
       p.polroles::text,pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid),
       NOT p.polpermissive AND p.polcmd='*' AND p.polroles=ARRAY[0::oid]
       AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
       AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
       FROM pg_policy p JOIN pg_class t ON t.oid=p.polrelid JOIN pg_namespace n ON n.oid=t.relnamespace
       WHERE n.nspname='em_content' AND t.relname=ANY(%s::text[]) ORDER BY t.relname,p.polname''',(names,)).fetchall()
    for name in names:C.check(any(r[0]==name and r[-1] is True for r in policies),'catchup_restrictive_quarantine_required')
    external=c.execute('''SELECT cn.nspname,ch.relname,p.relname,x.conname,
      ARRAY(SELECT a.attname FROM unnest(x.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=ch.oid AND a.attnum=k.num ORDER BY ord),
      ARRAY(SELECT a.attname FROM unnest(x.confkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=p.oid AND a.attnum=k.num ORDER BY ord)
      FROM pg_constraint x JOIN pg_class ch ON ch.oid=x.conrelid JOIN pg_namespace cn ON cn.oid=ch.relnamespace
      JOIN pg_class p ON p.oid=x.confrelid JOIN pg_namespace pn ON pn.oid=p.relnamespace
      WHERE x.contype='f' AND pn.nspname='em_content' AND p.relname=ANY(%s::text[])
      AND NOT (cn.nspname='em_content' AND ch.relname=ANY(%s::text[])) ORDER BY cn.nspname,ch.relname,x.conname''',(names,names)).fetchall()
    return dict(unique=unique,foreign=foreign,triggers=[list(r) for r in triggers],policies=[list(r) for r in policies],
        external_foreign=[dict(schema=s,table=t,parent=by_target[p],name=n,columns=cols,parent_columns=pc) for s,t,p,n,cols,pc in external])

def nullable_columns(t,catalog):
    # All nullable FK fields are temporarily cleared. Source before/after values
    # are journaled first and restored before this page can receive a receipt.
    return tuple(sorted({n for f in catalog['foreign'] if f['table']==t.name for n in f['nullable']}))

def effect_order(items,catalog,projects):
    """Topological order after explicit nullable-link removal; reject hard cycles."""
    edges={i:set() for i in range(len(items))};degree=dict.fromkeys(edges,0)
    mapped=[C.table(t).mapped(row,projects) for t,row in items]
    for i,(name,row) in enumerate(items):
        t=C.table(name);nulls=set(nullable_columns(t,catalog))
        for f in catalog['foreign']:
            if f['table']!=name or nulls.intersection(f['columns']):continue
            values=tuple(mapped[i][k] for k in f['columns'])
            if any(v is None for v in values):continue
            for j,(other,_) in enumerate(items):
                if other==f['parent'] and tuple(mapped[j][k] for k in f['parent_columns'])==values and i!=j:
                    if i not in edges[j]:edges[j].add(i);degree[i]+=1
    ready=[i for i in edges if degree[i]==0];result=[]
    while ready:
        i=ready.pop(0);result.append(i)
        for j in sorted(edges[i]):
            degree[j]-=1
            if degree[j]==0:ready.append(j)
    C.check(len(result)==len(items),'catchup_nonnullable_fk_cycle');return result

def project_supplement_document(plan,old_source,new_source):
    d=plan.document;base=dict(d['project_mapping'])
    extra=[(p,c) for p in d['pages'] for c in p['changes']
        if c['table']=='context_project_registry' and c['key'][0] not in base]
    if not extra:return None
    C.check(plan.sha256=='837c333c4f4eb86006063bc404c2431a486f3c5440a8691ad9ea1eaa0e5ff5dd'
        and d['namespace']=='owner' and d['epoch_id']=='7c5b591d-148f-498e-ae6b-e88a694936b2' and len(base)==97 and C.digest(base)=='8871f133e9a508cb0f8370a10d27a2d15fbe206f420d9433e0a030f310c8bc33'
        and len(extra)==1,'catchup_project_supplement_context')
    C.check(old_source.receipt_sha256==d['old_seal'] and new_source.receipt_sha256==d['new_seal']
        and old_source.namespace==new_source.namespace=='owner','catchup_project_supplement_source')
    C.check(all(PROJECTS.project_uuid(k)==v for k,v in base.items()),'catchup_project_convention_changed')
    convention=hashlib.sha256(Path(PROJECTS.__file__).read_bytes()).hexdigest()
    C.check(convention=='cc8de6dd0c4deaa14c0b453bd168f5e6b1381193084d7a7421aa2f30a6185be6','catchup_project_convention_changed')
    page,change=extra[0];key=change['key'][0]
    C.check(page['ordinal']==280 and page['sha256']=='0f9af47db9e8799ad84b5d06417d6bc28c07b59ca68e30ef1bf1d667c1728038'
        and change['op']==change['mode']=='insert' and change['before_hash'] is None
        and C.digest(key)=='3b6cac4644a64e422820ad827f2032f2cbfe541be5fc8d7f1ace6090e41fae6a'
        and change['after_hash']=='5087c87c441574ca55797ee9549335773845cdd952e73cd65371d164a5a23054'
        and PROJECTS.project_uuid(key)=='c7336cbf-516f-5e9d-b68e-de05586cd59a'
        and d['old_summary']['context_project_registry']['count']==97
        and d['new_summary']['context_project_registry']['count']==98,'catchup_project_supplement_entry')
    return dict(format='history-catchup-project-supplement-v1',scope='registry-derived-uuid-only',
        epoch_id=d['epoch_id'],namespace='owner',plan_sha256=plan.sha256,base_binding=d['base_binding'],
        old_seal=d['old_seal'],new_seal=d['new_seal'],base_mapping_sha256=C.digest(base),
        convention_module_sha256=convention,old_registry_count=97,new_registry_count=98,
        entries=[dict(project=key,project_uuid=PROJECTS.project_uuid(key),source_row_sha256=change['after_hash'],
            page_ordinal=280,page_sha256=page['sha256'])],source_phase_complete=True,source_hash_passes=4,authority_granted=False)

def overlay_project_mapping(plan,old_source,new_source,raw):
    base=dict(plan.document['project_mapping']);expected=project_supplement_document(plan,old_source,new_source)
    if expected is None:
        C.check(raw is None,'catchup_project_supplement_unexpected');return base,None
    C.check(raw is not None,'catchup_project_supplement_required')
    C.check(type(raw) is bytes and len(raw)<=65536,'catchup_project_supplement_budget')
    from .operator_config import _pairs
    try:document=json.loads(raw,object_pairs_hook=_pairs)
    except (ValueError,TypeError):raise C.ImportError('catchup_project_supplement_invalid') from None
    C.check(C.canonical(document)==C.canonical(expected),'catchup_project_supplement_changed')
    result=dict(base)
    for entry in expected['entries']:
        C.check(entry['project'] not in result and entry['project_uuid'] not in result.values(),'catchup_project_supplement_collision')
        result[entry['project']]=entry['project_uuid']
    return result,hashlib.sha256(raw).hexdigest()

class CatchupImporter:
    def __init__(self,runtime,plan,old_source,new_source,*,project_supplement=None):
        from .operator_scope import OperatorRuntime
        C.check(type(runtime) is OperatorRuntime and type(plan) is Plan,'catchup_trusted_runtime_required')
        self.runtime=runtime;self.plan=plan;self.d=plan.document
        self.old_source=old_source;self.new_source=new_source
        self.tables=C.tables(self.d['namespace']);self.epoch=self.d['epoch_id'];self.library=runtime.target.library_id
        self.projects,self.project_supplement_sha256=overlay_project_mapping(plan,old_source,new_source,project_supplement)
        C.check(self.d['base_binding']==runtime.config.binding_hash and self.d['target']==asdict(runtime.target),'catchup_target_binding_changed')
        C.check(self.d['old_seal']==old_source.receipt_sha256 and self.d['new_seal']==new_source.receipt_sha256
            and old_source.namespace==new_source.namespace==runtime.config.source.namespace,'catchup_source_binding_changed')
    @contextmanager
    def _tx(self,*,check_catalog=True):
        self.runtime.guard()
        with transaction(self.runtime.factory) as c:
            c.execute('LOCK TABLE '+','.join('em_content.'+C.quote(t.target_name) for t in self.tables)+' IN SHARE ROW EXCLUSIVE MODE')
            for schema,table in sorted({(f['schema'],f['table']) for f in self.d['catalog'].get('external_foreign',())}):
                c.execute('LOCK TABLE '+C.quote(schema)+'.'+C.quote(table)+' IN SHARE ROW EXCLUSIVE MODE')
            if check_catalog:C.check(catalog(c,self.d['namespace'])==self.d['catalog'],'catchup_target_schema_changed')
            if c.execute("SELECT to_regclass('em_content.history_catchup_pages') IS NOT NULL").fetchall()==[(True,)]:
                from .governance_large_row import check_ledger
                check_ledger(c,'catchup')
            yield c
            self.runtime.guard()
    def prepare(self):
        from .operator_scope import sql_statements
        with self._tx() as c:
            for sql in sql_statements((Path(__file__).parent/'sql/003_history_catchup.sql').read_text()):c.execute(sql)
            from .governance_large_row import check_ledger
            check_ledger(c,'catchup')
        return dict(prepared=True,route_activated=False)
    def begin(self):
        with self._tx() as c:
            c.execute('LOCK TABLE em_content.history_catchup_epochs IN EXCLUSIVE MODE')
            prior=c.execute('SELECT plan_hash,manifest FROM em_content.history_catchup_epochs WHERE epoch_id=%s::uuid',(self.epoch,)).fetchall()
            if prior:
                C.check(prior==[(self.plan.sha256,self.d)],'catchup_epoch_binding_changed');return dict(already_registered=True)
            C.check(not c.execute("SELECT 1 FROM em_content.history_catchup_epochs WHERE library_id=%s::uuid AND state<>'complete' LIMIT 1",(self.library,)).fetchall(),'catchup_other_epoch_incomplete')
            previous=self.d['previous_proof'];gen=generation(c,self.d['namespace'],baseline=previous['kind']=='initial')
            C.check(gen==previous['generation'],'catchup_previous_generation_changed')
            C.check(previous['source_seal']==self.d['old_seal'],'catchup_previous_source_changed')
            if previous['kind']=='epoch':
                rows=c.execute('SELECT epoch_id::text,completion_hash,generation FROM em_content.history_catchup_latest WHERE library_id=%s::uuid FOR UPDATE',(self.library,)).fetchall()
                C.check(rows==[(previous['epoch_id'],previous['completion_hash'],gen)],'catchup_previous_proof_changed')
            else:
                C.check(previous['base_binding']==self.d['base_binding'],'catchup_original_binding_changed')
                C.check(not c.execute('SELECT 1 FROM em_content.history_catchup_latest WHERE library_id=%s::uuid',(self.library,)).fetchall(),'catchup_latest_already_exists')
                for domain,name in PROGRESS.items():
                    if domain in gen:C.check(C.digest(c.execute('SELECT to_jsonb(p) FROM em_content.'+name+' p').fetchall()[0][0])==previous['proofs'][domain],'catchup_original_proof_changed')
            c.execute("INSERT INTO em_content.history_catchup_epochs(epoch_id,library_id,plan_hash,manifest,state,caught_up_to,generation) VALUES(%s::uuid,%s::uuid,%s,%s::jsonb,'applying',%s,%s::jsonb)",
                (self.epoch,self.library,self.plan.sha256,C.canonical(self.d).decode(),self.d['caught_up_to'],C.canonical(gen).decode()))
            c.execute("INSERT INTO em_content.history_catchup_progress(epoch_id,phase,generation) VALUES(%s::uuid,'baseline',%s::jsonb)",(self.epoch,C.canonical(gen).decode()))
        return dict(registered=True,phase='baseline')
    def _progress(self,c):
        row=c.execute('''SELECT e.plan_hash,p.phase,p.next_page,p.verify_table,p.after_key,p.check_ordinal,p.generation
            FROM em_content.history_catchup_epochs e JOIN em_content.history_catchup_progress p USING(epoch_id)
            WHERE e.epoch_id=%s::uuid FOR UPDATE OF e,p''',(self.epoch,)).fetchall()
        C.check(len(row)==1 and row[0][0]==self.plan.sha256,'catchup_epoch_not_registered')
        _,phase,page,table,after,ordinal,gen=row[0]
        C.check(generation(c,self.d['namespace'])==gen,'catchup_generation_cas_failed')
        return dict(phase=phase,next_page=page,verify_table=table,after_key=after,check_ordinal=ordinal,generation=gen)
    def status(self):
        with self._tx() as c:p=self._progress(c)
        return dict(p,epoch_id=self.epoch,plan_hash=self.plan.sha256,route_activated=False)
    def recover(self,attempt_id):
        """Read-only receipt lookup. Never retries or repeats page effects."""
        C.core.uuid_text(attempt_id)
        with self._tx() as c:
            p=self._progress(c)
            attempts=[attempt_id]+[str(uuid.uuid5(uuid.UUID(attempt_id),'page:'+str(i))) for i in range(1,128)]
            rows=c.execute('SELECT ordinal,page_hash,row_count,after_generation,attempt_id::text FROM em_content.history_catchup_pages WHERE epoch_id=%s::uuid AND attempt_id=ANY(%s::uuid[]) ORDER BY ordinal',(self.epoch,attempts)).fetchall()
            if rows:
                for ordinal,hashed,count,gen,aid in rows:
                    C.check(hashed==self.d['pages'][ordinal]['sha256'] and p['next_page']>ordinal,'catchup_receipt_frontier_conflict')
                return dict(commit_outcome='committed',committed_pages=[r[0] for r in rows],
                    last_page=rows[-1][0],row_count=sum(r[2] for r in rows),next_page=p['next_page'],automatic_retry=False)
            return dict(commit_outcome='not_receipted',next_page=p['next_page'],automatic_retry=False,
                explicit_new_attempt_required=True)
    def _target_rows(self,c,t,*,key=None,after=None,limit=128):
        columns=t.target_columns
        select=[C.quote(n)+('::text' if n=='project_uuid' else '') for n in columns]
        where='library_id=%s::uuid';args=[self.library]
        if key is not None:
            where+=' AND '+' AND '.join(C.quote(n)+' IS NOT DISTINCT FROM %s' for n in t.target_keys);args+=list(key)
        if after is not None:
            where+=' AND ('+','.join(t.target_key_sql)+')>('+','.join('%s' for _ in after)+')';args+=list(after)
        suffix=' FROM em_content.'+C.quote(t.target_name)+' WHERE '+where+ ' ORDER BY '+','.join(t.target_key_sql)+' LIMIT '+str(limit)
        if t.byte_budget>C.MAX_BYTES:
            lengths='+'.join('COALESCE(octet_length('+C.quote(n)+'),0)' for n,kind,_ in t.module.SPECS[t.name] if kind in ('TEXT','BLOB')) or '0'
            sizes=c.execute('SELECT '+lengths+suffix,tuple(args)).fetchall()
            C.check(sum(r[0] for r in sizes)<=t.byte_budget,'catchup_target_native_byte_budget')
        rows=c.execute('SELECT '+','.join(select)+',project_id::text'+suffix,tuple(args)).fetchall()
        result=[];size=0
        for values in rows:
            C.check(values[-1] is None,'catchup_project_scope_changed')
            mapped=dict(zip(columns,values[:-1]));original=t.unmapped(mapped)
            C.check(mapped==t.mapped(original,self.projects),'catchup_target_projection_changed')
            size+=len(C.value_bytes(t,original));C.check(size<=t.byte_budget,'catchup_target_page_byte_budget');result.append(original)
        return result
    def _verify_step(self,source,p):
        phase=p['phase'];index=p['verify_table'];C.check(index<len(self.tables),'catchup_verify_frontier')
        t=self.tables[index]
        expected=source.page(t,p['after_key']);n=len(expected)
        with self._tx() as c:
            current=self._progress(c);C.check(current==p,'catchup_frontier_cas_failed')
            actual=self._target_rows(c,t,after=p['after_key'],limit=max(1,n))
            C.check(actual==expected,'catchup_independent_row_reconciliation_failed')
            summary=summarize(t,actual)
            c.execute('''INSERT INTO em_content.history_catchup_checks(epoch_id,phase,ordinal,table_code,after_key,row_count,typed_hash,keys_hash,generation)
              VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)''',(self.epoch,phase,p['check_ordinal'],t.name,
                C.canonical(p['after_key']).decode(),n,summary['typed_sha256'],summary['keys_sha256'],C.canonical(p['generation']).decode()))
            after=list(t.key(expected[-1])) if expected else None;next_index=index if expected else index+1
            next_phase=phase
            if next_index==len(self.tables):
                rows=c.execute('SELECT table_code,COALESCE(sum(row_count),0) FROM em_content.history_catchup_checks WHERE epoch_id=%s::uuid AND phase=%s GROUP BY table_code',(self.epoch,phase)).fetchall()
                counts=dict(rows);summary=self.d['old_summary' if phase=='baseline' else 'new_summary']
                C.check(counts=={k:v['count'] for k,v in summary.items()},'catchup_independent_count_reconciliation_failed')
                next_phase='applying' if phase=='baseline' else 'ready';next_index=0;after=None
            c.execute('UPDATE em_content.history_catchup_progress SET phase=%s,verify_table=%s,after_key=%s::jsonb,check_ordinal=%s WHERE epoch_id=%s::uuid',
                (next_phase,next_index,C.canonical(after).decode(),p['check_ordinal']+1,self.epoch))
        return dict(phase=next_phase,table=t.name,rows_verified=n)
    def _apply_step(self,old,new,p,attempt_id):
        ordinal=p['next_page']
        if ordinal==len(self.d['pages']):
            with self._tx() as c:
                C.check(self._progress(c)==p,'catchup_frontier_cas_failed')
                c.execute("UPDATE em_content.history_catchup_progress SET phase='verifying',verify_table=0,after_key=NULL,check_ordinal=0 WHERE epoch_id=%s::uuid",(self.epoch,))
                c.execute("UPDATE em_content.history_catchup_epochs SET state='verifying' WHERE epoch_id=%s::uuid",(self.epoch,))
            return dict(phase='verifying')
        page=self.d['pages'][ordinal];items=[];size=0
        for o in page['changes']:
            t=C.table(o['table']);before=old.row(t,o['key']);after=new.row(t,o['key'])
            C.check((C.row_hash(t.name,before) if before is not None else None)==o['before_hash']
                and (C.row_hash(t.name,after) if after is not None else None)==o['after_hash'],'catchup_source_before_after_changed')
            C.check(o.get('mode') in ('insert','delete','update','restore','replace','release_update'),'catchup_change_mode_invalid')
            if o['mode']=='release_update':
                C.check(o['op']=='update' and release_only_update(t,before,after,self.d['catalog'],self.projects),'catchup_release_update_invalid')
            size+=len(C.value_bytes(t,[before,after]));items.append((o,t,before,after))
        C.check(size<=C.changes_budget(page['changes']) and len(items)<=128,'catchup_apply_page_budget')
        deleting=[(t.name,b) for o,t,b,a in items if b is not None and o['mode'] in ('delete','replace')]
        inserting=[(t.name,a) for o,t,b,a in items if a is not None and o['mode'] in ('insert','replace')]
        delete_order=list(reversed(effect_order(deleting,self.d['catalog'],self.projects)))
        insert_order=effect_order(inserting,self.d['catalog'],self.projects)
        with self._tx() as c:
            C.check(self._progress(c)==p,'catchup_frontier_cas_failed')
            # Even an explicit attempt must first inspect the immutable receipt.
            prior=c.execute('SELECT page_hash FROM em_content.history_catchup_pages WHERE epoch_id=%s::uuid AND ordinal=%s',(self.epoch,ordinal)).fetchall()
            C.check(not prior,'catchup_page_already_receipted')
            for o,t,b,a in items:
                if b is None or o['mode'] not in ('delete','replace'):continue
                mapped=dict(t.mapped(b,self.projects),library_id=self.library,project_id=None)
                for f in self.d['catalog'].get('external_foreign',()):
                    if f['parent']!=t.name:continue
                    values=[mapped[k] for k in f['parent_columns']]
                    if any(v is None for v in values):continue
                    rows=c.execute('SELECT 1 FROM '+C.quote(f['schema'])+'.'+C.quote(f['table'])+' WHERE '+
                        ' AND '.join(C.quote(k)+' IS NOT DISTINCT FROM %s' for k in f['columns'])+' LIMIT 1',tuple(values)).fetchall()
                    C.check(not rows,'catchup_unjournaled_external_reference')
            for change_ordinal,(o,t,b,a) in enumerate(items):
                actual=self._target_rows(c,t,key=o['key'],limit=2)
                C.check(actual==([] if b is None else [b]),'catchup_before_image_cas_failed')
                c.execute('''INSERT INTO em_content.history_catchup_changes(epoch_id,ordinal,change_ordinal,table_code,source_key,operation,
                    before_hash,after_hash,before_row,after_row,before_typed_text,after_typed_text)
                    VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)''',
                    (self.epoch,ordinal,change_ordinal,t.name,C.canonical(o['key']).decode(),o['op'],o['before_hash'],o['after_hash'],
                        C.journal_json(t,b),C.journal_json(t,a),C.value_bytes(t,b).decode(),C.value_bytes(t,a).decode()))
            # Remove nullable links before deleting the exact closed set. All
            # before images were captured above, including otherwise equal rows.
            for o,t,b,a in items:
                nulls=nullable_columns(t,self.d['catalog'])
                if b is not None and o['mode']=='release_update':
                    c.execute(*t.update(a,self.projects,self.library,null_columns=nulls))
                elif b is not None and nulls:c.execute(*t.update(b,self.projects,self.library,null_columns=nulls))
            for i in delete_order:
                name,row=deleting[i];t=C.table(name)
                c.execute('DELETE FROM em_content.'+C.quote(t.target_name)+' WHERE library_id=%s::uuid AND '+
                    ' AND '.join(C.quote(k)+' IS NOT DISTINCT FROM %s' for k in t.target_keys),(self.library,*t.key(row)))
            for i in insert_order:
                name,row=inserting[i];t=C.table(name);nulls=nullable_columns(t,self.d['catalog'])
                # Existing core insert already clears supersedes links. Other
                # mappings receive NULL only for the temporary nullable links.
                temp=dict(row)
                for name in nulls:
                    if name in temp:temp[name]=None
                # Validation of temporary source NULL would reject a nonnullable
                # source/nullable target mismatch: fail before committing.
                c.execute(*t.insert(temp,self.projects,self.library))
            for o,t,b,a in items:
                if a is not None:c.execute(*t.update(a,self.projects,self.library))
            for o,t,b,a in items:
                C.check(self._target_rows(c,t,key=o['key'],limit=2)==([] if a is None else [a]),'catchup_page_readback_failed')
            gen=generation(c,self.d['namespace'])
            c.execute('''INSERT INTO em_content.history_catchup_pages(epoch_id,ordinal,attempt_id,page_hash,row_count,input_bytes,before_generation,after_generation)
                VALUES(%s::uuid,%s,%s::uuid,%s,%s,%s,%s::jsonb,%s::jsonb)''',
                (self.epoch,ordinal,attempt_id,page['sha256'],len(items),size,C.canonical(p['generation']).decode(),C.canonical(gen).decode()))
            c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            c.execute('UPDATE em_content.history_catchup_progress SET next_page=%s,generation=%s::jsonb,last_attempt=%s::uuid WHERE epoch_id=%s::uuid',
                (ordinal+1,C.canonical(gen).decode(),attempt_id,self.epoch))
        return dict(phase='applying',next_page=ordinal+1,attempt_id=attempt_id)
    def step(self,*,max_steps=8,attempt_id=None):
        C.check(type(max_steps) is int and 1<=max_steps<=128,'catchup_step_budget')
        result=None;applied=0
        # One process deadline supervises both sealed hashes and short target
        # leases; CLI never retries this operation automatically.
        with Snapshot(self.old_source,seconds=60) as old,Snapshot(self.new_source,seconds=60) as new:
            for i in range(max_steps):
                p=self.status()
                p={k:p[k] for k in ('phase','next_page','verify_table','after_key','check_ordinal','generation')}
                if p['phase'] in ('baseline','verifying'):result=self._verify_step(old if p['phase']=='baseline' else new,p)
                elif p['phase']=='applying':
                    if applied==8:break
                    aid=(attempt_id if i==0 else str(uuid.uuid5(uuid.UUID(attempt_id),'page:'+str(i)))) if attempt_id is not None else str(uuid.uuid4())
                    C.core.uuid_text(aid);applied+=1
                    result=self._apply_step(old,new,p,aid)
                else:result=dict(phase=p['phase']);break
        C.check(old.phase.proven_complete and new.phase.proven_complete,'catchup_source_phase_incomplete')
        return dict(result,source_phase_complete=True,automatic_retry=False)
    def finalize(self):
        # Completion is a separate epoch proof, after a new entry/exit seal
        # proof. Hashing never holds a target transaction or a five-second lease.
        with Snapshot(self.old_source,seconds=60) as old,Snapshot(self.new_source,seconds=60) as new:
            C.check(old.sequences()==self.d['old_sequence'] and new.sequences()==self.d['new_sequence'],
                'catchup_final_source_sequence_changed')
        C.check(old.phase.proven_complete and new.phase.proven_complete,'catchup_final_source_proof_required')
        return self._finalize_target()
    def _finalize_target(self):
        with self._tx() as c:
            p=self._progress(c);C.check(p['phase'] in ('ready','complete'),'catchup_full_reconciliation_required')
            checks=c.execute('''SELECT phase,table_code,sum(row_count)::bigint,count(*),
                encode(sha256(convert_to(string_agg(ordinal::text||':'||typed_hash||':'||keys_hash, E'\\n' ORDER BY ordinal),'UTF8')),'hex')
                FROM em_content.history_catchup_checks
                WHERE epoch_id=%s::uuid GROUP BY phase,table_code ORDER BY phase,table_code''',(self.epoch,)).fetchall()
            for phase,field in (('baseline','old_summary'),('verifying','new_summary')):
                actual={t:int(n) for ph,t,n,pages,hashed in checks if ph==phase}
                C.check(actual=={t:s['count'] for t,s in self.d[field].items()},'catchup_complete_count_mismatch')
            # Source sequence highwaters survive deleted high IDs. PostgreSQL
            # sequence restart is transactional and never regresses allocation.
            sequence_proof={}
            for t in self.tables:
                evidence=self.d['new_sequence'].get(t.name)
                if evidence is None:continue
                alias=None
                if t.domain=='core':alias='id' if t.name in ('memories','context_items') else None
                else:
                    e=t.evidence(self.d['namespace']);alias=e.get('rowid_alias')
                    if t.domain=='sources':alias='id' if t.name in t.module.AUTO else None
                    if t.domain=='organization':alias='id' if t.name=='organization_tasks' else None
                if alias is None:continue
                row=c.execute('SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+t.target_name,alias)).fetchall()
                if row==[(None,)]:continue # Original rowid-only tables have no PG identity.
                C.check(len(row)==1 and type(row[0][0]) is str,'catchup_sequence_missing');seq=row[0][0]
                C.check(re.fullmatch(r'em_content\.[a-z][a-z0-9_]{0,90}',seq),'catchup_sequence_name')
                last,called=c.execute('SELECT last_value,is_called FROM '+seq).fetchall()[0]
                maximum=c.execute('SELECT COALESCE(max('+C.quote(alias)+'),0) FROM em_content.'+C.quote(t.target_name)+' WHERE library_id=%s::uuid',(self.library,)).fetchall()[0][0]
                high=max(evidence['value'] or 0,maximum,last+int(called)-1)
                C.check(high<2**63-1,'catchup_sequence_exhausted_requires_explicit_handling')
                nxt=max(1,high+1)
                if p['phase']!='complete':c.execute('ALTER SEQUENCE '+seq+' RESTART WITH '+str(nxt))
                sequence_proof[t.name]=dict(source=evidence,next_value=nxt)
            proof=dict(format='history-catchup-proof-v1',epoch_id=self.epoch,plan_hash=self.plan.sha256,
                source_seal=self.d['new_seal'],caught_up_to=self.d['caught_up_to'],generation=p['generation'],
                summaries=self.d['new_summary'],sequences=sequence_proof,checks_sha256=C.digest(checks),
                source_sequences=self.d['new_sequence'],
                route_activated=False,original_binding=self.d['base_binding'])
            if self.project_supplement_sha256 is not None:
                proof['project_mapping_supplement_sha256']=self.project_supplement_sha256
                proof['effective_project_mapping_sha256']=C.digest(self.projects)
            hashed=C.digest(proof)
            if p['phase']=='complete':
                C.check(c.execute('SELECT completion_hash FROM em_content.history_catchup_epochs WHERE epoch_id=%s::uuid',(self.epoch,)).fetchall()==[(hashed,)],'catchup_completed_proof_changed')
                return dict(proof,completion_hash=hashed,state='complete')
            c.execute("UPDATE em_content.history_catchup_epochs SET state='complete',completion_hash=%s,generation=%s::jsonb,completed_at=clock_timestamp() WHERE epoch_id=%s::uuid",(hashed,C.canonical(p['generation']).decode(),self.epoch))
            c.execute('''INSERT INTO em_content.history_catchup_latest(library_id,epoch_id,completion_hash,caught_up_to,generation,plan_hash)
                VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s) ON CONFLICT(library_id) DO UPDATE SET
                epoch_id=excluded.epoch_id,completion_hash=excluded.completion_hash,caught_up_to=excluded.caught_up_to,generation=excluded.generation,plan_hash=excluded.plan_hash''',
                (self.library,self.epoch,hashed,self.d['caught_up_to'],C.canonical(p['generation']).decode(),self.plan.sha256))
            c.execute("UPDATE em_content.history_catchup_progress SET phase='complete' WHERE epoch_id=%s::uuid",(self.epoch,))
        return dict(proof,completion_hash=hashed,state='complete')
