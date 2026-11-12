"""Three-table quarantined import; real PG SQL with the frozen transaction gate."""
from dataclasses import asdict
from .history_core import importer as C
from .source_complete import CompleteImporter
from .organization_complete import Importer as OrganizationImporter
from .governance_plan import Plan
from . import governance_mapping as M

check=M.check
rows=C._rows
LEDGER=('history_governance_schema_migrations','history_governance_runs','history_governance_chunks','history_governance_progress')
WATCH=M.TARGET_TABLES+('history_governance_chunks',)
FIELDS=('manifest_hash','stage','next_load','next_verify','generation','verify_generation','count_table','count_rows','count_last')
TOUCH_BODY='\nBEGIN\n UPDATE em_content.history_governance_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'

class GovernanceImporter(C.Importer):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._source=CompleteImporter(self.factory,self.target,capacity_guard=self.capacity_guard)
        self._organization=OrganizationImporter(self.factory,self.target,capacity_guard=self.capacity_guard)
    def _unsupported(self,*args,**kwargs):raise M.ImportError('governance_manifest_api_required')
    begin_run=import_chunk=lookup_chunk=restore_links=verify=_unsupported
    def _gate(self,c):
        self._organization._gate(c) # original core/source/organization locks; same connection
        for t in M.TARGET_TABLES+LEDGER:c.execute('LOCK TABLE em_content.'+t+' IN SHARE ROW EXCLUSIVE MODE')
        from .governance_large_row import check_ledger
        check_ledger(c,'governance')
        check(rows(c,"SELECT current_setting('session_replication_role')")==[('origin',)],'governance_origin_session_required')
        check(rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_governance_schema_migrations')==
            [(1,'001_history_governance_twenty_six',self.target.library_id)],'governance_schema_required')
        policies=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND NOT p.polpermissive,p.polroles=ARRAY[0::oid],
            pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)',
            pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)'
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND p.polname='history_governance_quarantine' ORDER BY c.relname''',
            (self.target.owner_role,self.target.owner_role))
        check(policies==[(t,True,True,True,True,True,True,True) for t in sorted(M.TARGET_TABLES)],'governance_quarantine_required')
        private=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only' ORDER BY c.relname''',(list(LEDGER),))
        check(private==[(t,True,True,True,True,True,True,True) for t in sorted(LEDGER)],'governance_private_ledger_required')
        triggers=rows(c,'''SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,
            t.tgqual IS NULL,t.tgattr=''::int2vector,t.tgnargs=0,octet_length(t.tgargs)=0,
            f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],
            f.prosrc=%s,fn.nspname='em_content' AND f.proname='history_governance_touch'
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
            WHERE n.nspname='em_content' AND t.tgname='history_governance_generation' ORDER BY c.relname''',(TOUCH_BODY,))
        check(triggers==[(t,)+(True,)*12 for t in sorted(WATCH)]
            and all(type(v) is bool for row in triggers for v in row[1:]),'governance_generation_trigger_required')
        actual=rows(c,'''SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum''',(list(M.TARGET_TABLES),))
        expected=[]
        for t in sorted(M.TABLES,key=M.target_table):
            expected.extend([(M.target_table(t),'library_id','uuid',True),(M.target_table(t),'project_id','uuid',False),(M.target_table(t),'source_rowid','bigint',True)])
            expected.extend((M.target_table(t),name,{'INTEGER':'bigint','REAL':'double precision','TEXT':'text'}[kind],not nullable) for name,kind,nullable in M.SPECS[t])
        check(actual==expected,'governance_columns_changed')
        from .governance_constraints import check_constraints
        check_constraints(c)
    def _parent(self,c,plan):
        check(type(plan) is Plan and plan.target==self.target,'trusted_governance_plan_required');plan.require_importable()
        p=self._source._progress(c,plan.parent_plan)
        check(p['stage']=='verified' and p['generation']==p['verify_generation'],'verified_source_parent_required')
        org=self._organization._progress_org(c,plan.organization_plan,p['generation'])
        check(org['stage']=='verified' and org['generation']==org['verify_generation'] and org['count_table']==7 and org['next_verify']==len(plan.organization_plan.document['pages']) and type(org['proof']) is dict,'verified_organization_parent_required')
        proof=org['proof'];next_id=proof.get('next_task_id')
        check(type(next_id) is int and plan.organization_plan.document['task_highwater']<next_id<=M.INT64_MAX,'governance_organization_sequence_proof')
        expected=dict(state='organization_domain_verified',manifest_hash=plan.organization_plan.sha256,
            source_sha256=plan.header['source']['file_sha256'],generation=org['generation'],parent_generation=p['generation'],
            parent_plan_hash=plan.parent_plan.sha256,next_task_id=next_id,route_activated=False,history_complete=False)
        check(proof==expected,'governance_organization_completion_proof')
        from .organization_complete import check_identity_sequences
        check_identity_sequences(c)
        check(rows(c,'SELECT last_value FROM em_content.organization_tasks_id_seq')[0][0]>=next_id,'governance_organization_sequence_regressed')
    def _progress(self,c,plan):
        self._parent(c,plan)
        budgets=rows(c,'SELECT octet_length(manifest::text),octet_length(loaded_counts::text) FROM em_content.history_governance_runs WHERE run_id=%s::uuid AND library_id=%s::uuid AND parent_run_id=%s::uuid FOR UPDATE',(plan.run_id,self.target.library_id,plan.parent_plan.run_id))
        check(len(budgets)==1 and all(type(v) is int for v in budgets[0]) and 0<=budgets[0][0]<=16*1024*1024 and 0<=budgets[0][1]<=16384,'governance_run_metadata_budget')
        cursor_budget=rows(c,'SELECT COALESCE(octet_length(count_last::text),0) FROM em_content.history_governance_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(cursor_budget)==1 and type(cursor_budget[0][0]) is int and 0<=cursor_budget[0][0]<=128,'governance_progress_metadata_budget')
        run=rows(c,'''SELECT manifest_hash,manifest,loaded_counts,loaded_bytes FROM em_content.history_governance_runs
            WHERE run_id=%s::uuid AND library_id=%s::uuid AND parent_run_id=%s::uuid FOR UPDATE''',
            (plan.run_id,self.target.library_id,plan.parent_plan.run_id))
        check(len(run)==1,'governance_run_missing');sha,manifest,counts,size=run[0]
        check(sha==plan.sha256 and manifest==plan.document,'governance_run_binding_changed')
        check(type(counts) is dict and set(counts)==set(M.TABLES) and all(type(v) is int and 0<=v<=plan.counts[t] for t,v in counts.items())
            and type(size) is int and 0<=size<=plan.header['input_bytes'],'governance_progress_invalid')
        data=rows(c,'SELECT '+','.join(FIELDS)+' FROM em_content.history_governance_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(data)==1,'governance_progress_missing');p=dict(zip(FIELDS,data[0]));check(p['manifest_hash']==plan.sha256,'governance_plan_conflict')
        check(p['stage'] in ('loading','verifying','counting','verified') and all(type(p[k]) is int and p[k]>=0 for k in ('next_load','next_verify','generation','count_table','count_rows'))
            and p['next_verify']<=p['next_load']<=plan.page_count and p['count_table']<=len(M.TABLES),'governance_progress_invalid')
        if p['stage']!='loading':
            check(type(p['verify_generation']) is int and p['generation']==p['verify_generation'],'governance_verification_generation_changed')
            check(counts==plan.counts and size==plan.header['input_bytes'],'governance_load_totals_changed')
        if p['stage']=='verified':self._verify_sequence_highwaters(c,plan)
        p.update(loaded_counts=counts,loaded_bytes=size);return p
    def begin(self,plan):
        with self._tx() as c:
            self._parent(c,plan);c.execute('LOCK TABLE em_content.history_governance_runs IN EXCLUSIVE MODE')
            existing=rows(c,'SELECT run_id::text FROM em_content.history_governance_runs')
            if existing:
                check(existing==[(plan.run_id,)],'governance_target_run_exists');result=self._progress(c,plan)
            else:
                for t in M.TABLES:check(not rows(c,'SELECT 1 FROM em_content.'+M.target_table(t)+' LIMIT 1'),'governance_empty_target_required')
                c.execute('''INSERT INTO em_content.history_governance_runs(run_id,library_id,parent_run_id,manifest_hash,manifest,loaded_counts)
                    VALUES(%s::uuid,%s::uuid,%s::uuid,%s,%s::jsonb,%s::jsonb)''',
                    (plan.run_id,self.target.library_id,plan.parent_plan.run_id,plan.sha256,M.canonical(plan.document).decode(),M.canonical({t:0 for t in M.TABLES}).decode()))
                c.execute("INSERT INTO em_content.history_governance_progress(run_id,manifest_hash,stage) VALUES(%s::uuid,%s,'loading')",(plan.run_id,plan.sha256))
                result=self._progress(c,plan)
        return result
    def status(self,plan):
        with self._tx() as c:result=self._progress(c,plan)
        return result
    def _receipt(self,c,plan,page):
        budget=rows(c,'SELECT octet_length(row_keys::text) FROM em_content.history_governance_chunks WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s',(plan.run_id,page['table'],page['chunk_key']))
        check(len(budget)<=1 and (not budget or type(budget[0][0]) is int and 0<=budget[0][0]<=8192),'governance_receipt_metadata_budget')
        result=rows(c,'''SELECT input_hash,row_keys,row_count,input_bytes FROM em_content.history_governance_chunks
            WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s''',(plan.run_id,page['table'],page['chunk_key']))
        check(len(result)<=1,'duplicate_governance_receipt');return result[0] if result else None
    def _checked(self,c,plan,page):
        receipt=self._receipt(c,plan,page)
        if not page['row_count']:check(receipt is None,'unexpected_empty_governance_receipt');return
        check(receipt is not None and receipt[0]==page['input_hash'] and receipt[2:]==(page['row_count'],page['input_bytes']),'governance_receipt_conflict')
        hashed,keys,count,size=receipt;t=page['table']
        check(type(keys) is list and len(keys)==count and all(type(k) is list and len(k)==len(M.KEYS[t]) and all(type(v) is int and M.INT64_MIN<=v<=M.INT64_MAX for v in k) for k in keys)
            and len({tuple(k) for k in keys})==count,'governance_receipt_keys_invalid')
        clauses=['('+' AND '.join(k+'=%s' for k in M.KEYS[t])+')' for _ in keys];names=M.target_columns(t)
        condition=' FROM em_content.'+M.target_table(t)+' WHERE library_id=%s::uuid AND ('+' OR '.join(clauses)+') LIMIT 129'
        params=(self.target.library_id,*(v for k in keys for v in k))
        lengths='+'.join('COALESCE(octet_length("'+n+'"),0)' for n,kind,_ in M.SPECS[t] if kind=='TEXT') or '0'
        sizes=rows(c,'SELECT '+lengths+condition,params)
        check(len(sizes)==count and all(len(r)==1 and type(r[0]) is int and 0<=r[0]<=M.MAX_BYTES for r in sizes)
            and sum(r[0] for r in sizes)<=M.MAX_BYTES,'governance_target_readback_budget')
        found=rows(c,'SELECT '+','.join(chr(34)+n+chr(34) for n in names)+',project_id::text'+condition,params)
        check(len(found)==count,'governance_target_missing');values=[]
        for r in found:
            check(r[-1] is None,'governance_target_scope_changed');values.append(M.unmap_row(t,dict(zip(names,r[:-1]))))
        values=M.ordered_rows(t,values)
        check(M.chunk_hash(t,values)==hashed and len(M.canonical(values))==size,'governance_target_digest_changed')
    def load_page(self,plan,index,args):
        page=plan.page(index);t=page['table']
        if page['row_count']:
            check(type(args) is dict and set(args)=={'table','chunk_key','rows','expected_input_hash'} and args['table']==t
                and args['chunk_key']==page['chunk_key'] and args['expected_input_hash']==page['input_hash'],'governance_page_arguments_changed')
            source,hashed,size=M.prepare_chunk(t,args['rows'],page['input_hash'])
            check(len(source)==page['row_count'] and size==page['input_bytes'] and max(r['source_rowid'] for r in source)==page['end']
                and all(page['after'] is None or r['source_rowid']>page['after'] for r in source),'governance_page_range_changed')
        else:check(args is None,'governance_empty_page_arguments');source=[];size=0;hashed=None
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_load']:self._checked(c,plan,page);result=dict(replayed=True,progress=p)
            else:
                check(p['stage']=='loading' and index==p['next_load'],'governance_load_frontier_conflict')
                check(self._receipt(c,plan,page) is None,'unexpected_governance_receipt')
                for row in source:c.execute(*M.insert_statement(t,row,self.target.library_id))
                if source:
                    keys=[list(M.key(t,row)) for row in source]
                    c.execute('''INSERT INTO em_content.history_governance_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                        VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',(plan.run_id,t,page['chunk_key'],hashed,M.canonical(keys).decode(),len(source),size))
                    self._checked(c,plan,page)
                counts=dict(p['loaded_counts']);counts[t]+=len(source);total=p['loaded_bytes']+size
                check(counts[t]<=plan.counts[t] and total<=plan.header['input_bytes'],'governance_manifest_total_exceeded')
                c.execute('UPDATE em_content.history_governance_runs SET loaded_counts=%s::jsonb,loaded_bytes=%s WHERE run_id=%s::uuid',(M.canonical(counts).decode(),total,plan.run_id))
                c.execute('UPDATE em_content.history_governance_progress SET next_load=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
                if index+1==plan.page_count:
                    check(counts==plan.counts and total==plan.header['input_bytes'],'governance_load_totals_incomplete')
                    c.execute("UPDATE em_content.history_governance_progress SET stage='verifying',verify_generation=generation WHERE run_id=%s::uuid",(plan.run_id,))
                result=dict(replayed=False,progress=self._progress(c,plan))
        return result
    def verify_page(self,plan,index):
        page=plan.page(index)
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('verifying','counting','verified') and index<=p['next_verify'],'governance_verify_frontier_conflict')
            self._checked(c,plan,page)
            if index==p['next_verify']:
                c.execute('UPDATE em_content.history_governance_progress SET next_verify=%s,stage=%s WHERE run_id=%s::uuid',
                    (index+1,'counting' if index+1==plan.page_count else 'verifying',plan.run_id))
            result=dict(readback_verified=True,progress=self._progress(c,plan))
        return result
    def count_page(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage']=='counting' and p['count_table']<len(M.TABLES),'governance_count_phase_required')
            t=M.TABLES[p['count_table']];where='';args=[]
            if p['count_last'] is not None:
                check(type(p['count_last']) is list and len(p['count_last'])==1 and type(p['count_last'][0]) is int,'governance_count_cursor_changed')
                where=' WHERE (library_id,source_rowid)>(%s::uuid,%s)';args=[self.target.library_id,p['count_last'][0]]
            found=rows(c,'SELECT library_id::text,project_id::text,source_rowid FROM em_content.'+M.target_table(t)+where+' ORDER BY library_id,source_rowid LIMIT 128',tuple(args))
            check(all(r[0]==self.target.library_id and r[1] is None and type(r[2]) is int for r in found),'governance_target_scope_changed')
            total=p['count_rows']+len(found);check(total<=plan.counts[t],'unreceipted_governance_target_rows');done=len(found)<128
            if done:check(total==plan.counts[t],'governance_target_count_incomplete')
            c.execute('UPDATE em_content.history_governance_progress SET count_table=%s,count_rows=%s,count_last=%s::jsonb WHERE run_id=%s::uuid',
                (p['count_table']+int(done),0 if done else total,None if done else M.canonical([found[-1][2]]).decode(),plan.run_id))
            result=dict(table_index=p['count_table'],rows_checked=len(found),progress=self._progress(c,plan))
        return result
    def finalize(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('counting','verified') and p['count_table']==len(M.TABLES) and p['next_verify']==plan.page_count,'governance_verification_incomplete')
            check(rows(c,'SELECT run_id::text FROM em_content.history_governance_runs ORDER BY run_id LIMIT 2')==[(plan.run_id,)],'governance_target_run_exists')
            expected=sorted((plan.run_id,page['table'],page['chunk_key'],page['input_hash'],page['row_count'],page['input_bytes'])
                for page in plan.document['pages'] if page['row_count'])
            actual=rows(c,'''SELECT run_id::text,table_code,chunk_key,input_hash,row_count,input_bytes
                FROM em_content.history_governance_chunks ORDER BY run_id,table_code,chunk_key LIMIT 4097''')
            check(sorted(actual)==expected,'governance_receipt_set_changed')
            self._restore_sequences(c,plan)
            c.execute("UPDATE em_content.history_governance_progress SET stage='verified' WHERE run_id=%s::uuid",(plan.run_id,))
        return dict(state='governance_domain_verified',target_domain_verified=True,governance_domain_complete=False,
            tables=len(M.TABLES),history_complete=False,route_activated=False)

    def _verify_sequence_highwaters(self,c,plan):
        for table,column in M.AUTO.items():
            last,called=rows(c,'SELECT last_value,is_called FROM em_content.'+table+'_'+column+'_seq')[0]
            value=plan.header['sequence_evidence'][table]['value'] or 0
            check(type(last) is int and type(called) is bool and ((value<M.INT64_MAX and last+int(called)>value) or (value==M.INT64_MAX and last==value and called)),'governance_sequence_regressed')
    def _restore_sequences(self,c,plan):
        from .governance_constraints import check_sequences
        check_sequences(c)
        for table,column in M.AUTO.items():
            sequence=table+'_'+column+'_seq'
            value=plan.header['sequence_evidence'][table]['value'] or 0
            last,called=rows(c,'SELECT last_value,is_called FROM em_content.'+sequence)[0]
            check(type(last) is int and type(called) is bool,'governance_target_sequence_invalid')
            next_value,exhausted=sequence_restart(value,last,called)
            c.execute('ALTER SEQUENCE em_content.'+sequence+' RESTART WITH '+str(next_value))
            if exhausted:check(rows(c,'SELECT nextval(%s::regclass)',('em_content.'+sequence,))==[(M.INT64_MAX,)],'governance_exhaustion_restore_failed')
            check(rows(c,'SELECT last_value,is_called FROM em_content.'+sequence)==[(next_value,exhausted)],'governance_sequence_restore_failed')

def sequence_restart(value,last,called):
    check(type(value) is int and 0<=value<=M.INT64_MAX and type(last) is int and 1<=last<=M.INT64_MAX and type(called) is bool,'governance_target_sequence_invalid')
    exhausted=value==M.INT64_MAX or last==M.INT64_MAX and called
    return (M.INT64_MAX,True) if exhausted else (max(1,value+1,last+int(called)),False)
