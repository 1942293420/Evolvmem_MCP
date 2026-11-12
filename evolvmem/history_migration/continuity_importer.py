"""Eight-table quarantined import; real PG SQL with the frozen transaction gate."""
from dataclasses import asdict
from .history_core import importer as C
from .batched_importer import BatchedImporter
from .continuity_plan import Plan
from . import continuity_mapping as M

check=M.check
rows=C._rows
LEDGER=('history_continuity_schema_migrations','history_continuity_runs','history_continuity_chunks','history_continuity_progress','history_continuity_links')
WATCH=M.TABLES+('history_continuity_chunks','history_continuity_links')
COUNT_TABLES=M.TABLES+('history_continuity_links',)
FIELDS=('manifest_hash','stage','next_load','next_restore','next_verify','generation','verify_generation','count_table','count_rows','count_last')
TOUCH_BODY='\nBEGIN\n UPDATE em_content.history_continuity_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'

class ContinuityImporter(C.Importer):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._source=BatchedImporter(self.factory,self.target,capacity_guard=self.capacity_guard)
    def _unsupported(self,*args,**kwargs):raise M.ImportError('continuity_manifest_api_required')
    begin_run=import_chunk=lookup_chunk=restore_links=verify=_unsupported
    def _gate(self,c):
        self._source._gate(c) # one connection: original six-table core gates and locks
        from .continuity_parent import check_parent_trigger_qualifications
        check_parent_trigger_qualifications(c) # only the core generation triggers
        for t in M.TABLES+LEDGER:c.execute('LOCK TABLE em_content.'+t+' IN SHARE ROW EXCLUSIVE MODE')
        check(rows(c,"SELECT current_setting('session_replication_role')")==[('origin',)],'continuity_origin_session_required')
        check(rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_continuity_schema_migrations')==
            [(1,'001_history_continuity_eight',self.target.library_id)],'continuity_schema_required')
        policies=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND NOT p.polpermissive,p.polroles=ARRAY[0::oid],
            pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)',
            pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)'
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND p.polname='history_continuity_quarantine' ORDER BY c.relname''',
            (self.target.owner_role,self.target.owner_role))
        check(policies==[(t,True,True,True,True,True,True,True) for t in sorted(M.TABLES)],'continuity_quarantine_required')
        private=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only' ORDER BY c.relname''',(list(LEDGER),))
        check(private==[(t,True,True,True,True,True,True,True) for t in sorted(LEDGER)],'continuity_private_ledger_required')
        triggers=rows(c,'''SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,
            t.tgqual IS NULL,t.tgattr=''::int2vector,t.tgnargs=0,octet_length(t.tgargs)=0,
            f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],
            f.prosrc=%s,fn.nspname='em_content' AND f.proname='history_continuity_touch'
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
            WHERE n.nspname='em_content' AND t.tgname='history_continuity_generation' ORDER BY c.relname''',(TOUCH_BODY,))
        check(triggers==[(t,)+(True,)*12 for t in sorted(WATCH)]
            and all(type(v) is bool for row in triggers for v in row[1:]),'continuity_generation_trigger_required')
        actual=rows(c,'''SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum''',(list(M.TABLES),))
        expected=[]
        for t in sorted(M.TABLES):
            expected.extend([(t,'library_id','uuid',True),(t,'project_id','uuid',False),(t,'source_rowid','bigint',True)])
            expected.extend((t,name,{'INTEGER':'bigint','REAL':'double precision','TEXT':'text'}[kind],not nullable) for name,kind,nullable in M.SPECS[t])
        check(actual==expected,'continuity_columns_changed')
        from .continuity_constraints import validate_constraints
        validate_constraints(c)
    def _parent(self,c,plan):
        check(type(plan) is Plan and plan.target==self.target,'trusted_continuity_plan_required');plan.require_importable()
        from .continuity_parent import checked_progress
        p=checked_progress(c,self._source,plan.parent_plan)
        check(p['stage']=='verified' and p['generation']==p['verify_generation'],'verified_core_parent_required')
    def _progress(self,c,plan):
        self._parent(c,plan)
        budgets=rows(c,'SELECT octet_length(manifest::text),octet_length(loaded_counts::text) FROM em_content.history_continuity_runs WHERE run_id=%s::uuid AND library_id=%s::uuid AND parent_run_id=%s::uuid FOR UPDATE',(plan.run_id,self.target.library_id,plan.parent_plan.run_id))
        check(len(budgets)==1 and all(type(v) is int for v in budgets[0]) and 0<=budgets[0][0]<=16*1024*1024 and 0<=budgets[0][1]<=16384,'continuity_run_metadata_budget')
        cursor_budget=rows(c,'SELECT COALESCE(octet_length(count_last::text),0) FROM em_content.history_continuity_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(cursor_budget)==1 and type(cursor_budget[0][0]) is int and 0<=cursor_budget[0][0]<=128,'continuity_progress_metadata_budget')
        run=rows(c,'''SELECT manifest_hash,manifest,loaded_counts,loaded_bytes FROM em_content.history_continuity_runs
            WHERE run_id=%s::uuid AND library_id=%s::uuid AND parent_run_id=%s::uuid FOR UPDATE''',
            (plan.run_id,self.target.library_id,plan.parent_plan.run_id))
        check(len(run)==1,'continuity_run_missing');sha,manifest,counts,size=run[0]
        check(sha==plan.sha256 and manifest==plan.document,'continuity_run_binding_changed')
        check(type(counts) is dict and set(counts)==set(M.TABLES) and all(type(v) is int and 0<=v<=plan.counts[t] for t,v in counts.items())
            and type(size) is int and 0<=size<=plan.header['input_bytes'],'continuity_progress_invalid')
        data=rows(c,'SELECT '+','.join(FIELDS)+' FROM em_content.history_continuity_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(data)==1,'continuity_progress_missing');p=dict(zip(FIELDS,data[0]));check(p['manifest_hash']==plan.sha256,'continuity_plan_conflict')
        check(p['stage'] in ('loading','restoring','verifying','counting','verified') and all(type(p[k]) is int and p[k]>=0 for k in ('next_load','next_restore','next_verify','generation','count_table','count_rows'))
            and p['next_verify']<=p['next_restore']<=p['next_load']<=plan.page_count and p['count_table']<=len(COUNT_TABLES),'continuity_progress_invalid')
        if p['stage'] not in ('loading','restoring'):
            check(type(p['verify_generation']) is int and p['generation']==p['verify_generation'],'continuity_verification_generation_changed')
            check(counts==plan.counts and size==plan.header['input_bytes'],'continuity_load_totals_changed')
        if p['stage']=='verified':self._sequence_proof(c,plan,verified=True)
        p.update(loaded_counts=counts,loaded_bytes=size);return p
    def begin(self,plan):
        with self._tx() as c:
            self._parent(c,plan);c.execute('LOCK TABLE em_content.history_continuity_runs IN EXCLUSIVE MODE')
            existing=rows(c,'SELECT run_id::text FROM em_content.history_continuity_runs')
            if existing:
                check(existing==[(plan.run_id,)],'continuity_target_run_exists');result=self._progress(c,plan)
            else:
                for t in M.TABLES:check(not rows(c,'SELECT 1 FROM em_content.'+t+' LIMIT 1'),'continuity_empty_target_required')
                c.execute('''INSERT INTO em_content.history_continuity_runs(run_id,library_id,parent_run_id,manifest_hash,manifest,loaded_counts)
                    VALUES(%s::uuid,%s::uuid,%s::uuid,%s,%s::jsonb,%s::jsonb)''',
                    (plan.run_id,self.target.library_id,plan.parent_plan.run_id,plan.sha256,M.canonical(plan.document).decode(),M.canonical({t:0 for t in M.TABLES}).decode()))
                c.execute("INSERT INTO em_content.history_continuity_progress(run_id,manifest_hash,stage) VALUES(%s::uuid,%s,'loading')",(plan.run_id,plan.sha256))
                result=self._progress(c,plan)
        return result
    def status(self,plan):
        with self._tx() as c:result=self._progress(c,plan)
        return result
    def _receipt(self,c,plan,page):
        budget=rows(c,'SELECT octet_length(row_keys::text) FROM em_content.history_continuity_chunks WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s',(plan.run_id,page['table'],page['chunk_key']))
        check(len(budget)<=1 and (not budget or type(budget[0][0]) is int and 0<=budget[0][0]<=8192),'continuity_receipt_metadata_budget')
        result=rows(c,'''SELECT input_hash,row_keys,row_count,input_bytes FROM em_content.history_continuity_chunks
            WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s''',(plan.run_id,page['table'],page['chunk_key']))
        check(len(result)<=1,'duplicate_continuity_receipt');return result[0] if result else None
    def _checked(self,c,plan,page,*,restored=True):
        receipt=self._receipt(c,plan,page)
        if not page['row_count']:check(receipt is None,'unexpected_empty_continuity_receipt');return
        check(receipt is not None and receipt[0]==page['input_hash'] and receipt[2:]==(page['row_count'],page['input_bytes']),'continuity_receipt_conflict')
        hashed,keys,count,size=receipt;t=page['table']
        check(type(keys) is list and len(keys)==count and all(type(k) is list and len(k)==len(M.KEYS[t]) and all(type(v) is int and M.INT64_MIN<=v<=M.INT64_MAX for v in k) for k in keys)
            and len({tuple(k) for k in keys})==count,'continuity_receipt_keys_invalid')
        clauses=['('+' AND '.join(M.quote(k)+'=%s' for k in M.KEYS[t])+')' for _ in keys];names=M.target_columns(t)
        condition=' FROM em_content.'+t+' WHERE library_id=%s::uuid AND ('+' OR '.join(clauses)+') LIMIT 129'
        params=(self.target.library_id,*(v for k in keys for v in k))
        lengths='+'.join('COALESCE(octet_length('+M.quote(n)+'),0)' for n,kind,_ in M.SPECS[t]
            if kind=='TEXT' and not(t=='continuity_workstreams' and n=='parent_id')) or '0'
        sizes=rows(c,'SELECT '+lengths+condition,params)
        check(len(sizes)==count and all(len(r)==1 and type(r[0]) is int and 0<=r[0]<=M.MAX_BYTES for r in sizes)
            and sum(r[0] for r in sizes)<=M.MAX_BYTES,'continuity_target_readback_budget')
        links={}
        if t=='continuity_workstreams':
            # Check escrow TEXT lengths before fetching any original parent ID.
            bounded=' FROM em_content.history_continuity_links WHERE run_id=%s::uuid AND source_rowid=ANY(%s::bigint[]) LIMIT 129'
            link_args=(plan.run_id,[k[0] for k in keys])
            lengths=rows(c,'SELECT COALESCE(octet_length(parent_id),0)'+bounded,link_args)
            check(len(lengths)==count and all(len(r)==1 and type(r[0]) is int and 0<=r[0]<=M.MAX_BYTES for r in lengths)
                and sum(r[0] for r in lengths)+sum(r[0] for r in sizes)<=M.MAX_BYTES,'continuity_link_readback_budget')
            # Fetch the original parent once. Physical link equality is checked
            # in SQL so a restored near-budget row is not materialized twice.
            predicate='w.parent_id IS NOT DISTINCT FROM l.parent_id' if restored else 'w.parent_id IS NULL'
            found_links=rows(c,'SELECT l.source_rowid,l.parent_id,(SELECT '+predicate+
                ' FROM em_content.continuity_workstreams w WHERE w.library_id=%s::uuid AND w.source_rowid=l.source_rowid)'+
                bounded.replace('history_continuity_links WHERE','history_continuity_links l WHERE'),(self.target.library_id,*link_args))
            check(all(len(r)==3 and r[2] is True for r in found_links),'continuity_parent_link_changed')
            links={r[0]:r[1] for r in found_links}
            check(set(links)=={k[0] for k in keys},'continuity_link_receipt_missing')
        projection=','.join('NULL AS "parent_id"' if t=='continuity_workstreams' and n=='parent_id' else M.quote(n) for n in names)
        found=rows(c,'SELECT '+projection+',project_id::text'+condition,params)
        check(len(found)==count,'continuity_target_missing');values=[]
        for r in found:
            check(r[-1] is None,'continuity_target_scope_changed');original=dict(zip(names,r[:-1]))
            if t=='continuity_workstreams':
                parent=links[original['source_rowid']]
                original['parent_id']=parent
            values.append(M.unmap_row(t,original))
        values=M.ordered_rows(t,values)
        check(M.chunk_hash(t,values)==hashed and len(M.canonical(values))==size,'continuity_target_digest_changed')
    def load_page(self,plan,index,args):
        page=plan.page(index);t=page['table']
        if page['row_count']:
            check(type(args) is dict and set(args)=={'table','chunk_key','rows','expected_input_hash'} and args['table']==t
                and args['chunk_key']==page['chunk_key'] and args['expected_input_hash']==page['input_hash'],'continuity_page_arguments_changed')
            source,hashed,size=M.prepare_chunk(t,args['rows'],page['input_hash'])
            check(len(source)==page['row_count'] and size==page['input_bytes'] and max(r['source_rowid'] for r in source)==page['end']
                and all(page['after'] is None or r['source_rowid']>page['after'] for r in source),'continuity_page_range_changed')
        else:check(args is None,'continuity_empty_page_arguments');source=[];size=0;hashed=None
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_load']:self._checked(c,plan,page,restored=index<p['next_restore']);result=dict(replayed=True,progress=p)
            else:
                check(p['stage']=='loading' and index==p['next_load'],'continuity_load_frontier_conflict')
                check(self._receipt(c,plan,page) is None,'unexpected_continuity_receipt')
                for row in source:
                    preserved=dict(row)
                    if t=='continuity_workstreams':preserved['parent_id']=None
                    c.execute(*M.insert_statement(t,preserved,self.target.library_id))
                    if t=='continuity_workstreams':
                        c.execute('INSERT INTO em_content.history_continuity_links(run_id,source_rowid,parent_id) VALUES(%s::uuid,%s,%s)',
                            (plan.run_id,row['source_rowid'],row['parent_id']))
                if source:
                    keys=[list(M.key(t,row)) for row in source]
                    c.execute('''INSERT INTO em_content.history_continuity_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                        VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',(plan.run_id,t,page['chunk_key'],hashed,M.canonical(keys).decode(),len(source),size))
                    self._checked(c,plan,page,restored=False)
                counts=dict(p['loaded_counts']);counts[t]+=len(source);total=p['loaded_bytes']+size
                check(counts[t]<=plan.counts[t] and total<=plan.header['input_bytes'],'continuity_manifest_total_exceeded')
                c.execute('UPDATE em_content.history_continuity_runs SET loaded_counts=%s::jsonb,loaded_bytes=%s WHERE run_id=%s::uuid',(M.canonical(counts).decode(),total,plan.run_id))
                c.execute('UPDATE em_content.history_continuity_progress SET next_load=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
                if index+1==plan.page_count:
                    check(counts==plan.counts and total==plan.header['input_bytes'],'continuity_load_totals_incomplete')
                    c.execute("UPDATE em_content.history_continuity_progress SET stage='restoring' WHERE run_id=%s::uuid",(plan.run_id,))
                result=dict(replayed=False,progress=self._progress(c,plan))
        return result
    def restore_page(self,plan,index):
        page=plan.page(index)
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_restore']:
                self._checked(c,plan,page);return dict(replayed=True,progress=p)
            check(p['stage']=='restoring' and index==p['next_restore'],'continuity_restore_frontier_conflict')
            self._checked(c,plan,page,restored=False)
            if page['row_count'] and page['table']=='continuity_workstreams':
                receipt=self._receipt(c,plan,page)
                c.execute('''UPDATE em_content.continuity_workstreams t SET parent_id=l.parent_id
                    FROM em_content.history_continuity_links l WHERE l.run_id=%s::uuid
                    AND l.source_rowid=ANY(%s::bigint[]) AND t.library_id=%s::uuid AND t.source_rowid=l.source_rowid''',
                    (plan.run_id,[k[0] for k in receipt[1]],self.target.library_id))
                c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            self._checked(c,plan,page)
            c.execute('UPDATE em_content.history_continuity_progress SET next_restore=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
            if index+1==plan.page_count:
                c.execute("UPDATE em_content.history_continuity_progress SET stage='verifying',verify_generation=generation WHERE run_id=%s::uuid",(plan.run_id,))
            result=dict(replayed=False,progress=self._progress(c,plan))
        return result
    def verify_page(self,plan,index):
        page=plan.page(index)
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('verifying','counting','verified') and index<=p['next_verify'],'continuity_verify_frontier_conflict')
            self._checked(c,plan,page)
            if index==p['next_verify']:
                c.execute('UPDATE em_content.history_continuity_progress SET next_verify=%s,stage=%s WHERE run_id=%s::uuid',
                    (index+1,'counting' if index+1==plan.page_count else 'verifying',plan.run_id))
            result=dict(readback_verified=True,progress=self._progress(c,plan))
        return result
    def count_page(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage']=='counting' and p['count_table']<len(COUNT_TABLES),'continuity_count_phase_required')
            t=COUNT_TABLES[p['count_table']];where='';args=[]
            escrow=t=='history_continuity_links';scope='run_id' if escrow else 'library_id'
            owner=plan.run_id if escrow else self.target.library_id
            wanted=plan.counts['continuity_workstreams' if escrow else t]
            if p['count_last'] is not None:
                check(type(p['count_last']) is list and len(p['count_last'])==1 and type(p['count_last'][0]) is int,'continuity_count_cursor_changed')
                where=' WHERE ('+scope+',source_rowid)>(%s::uuid,%s)';args=[owner,p['count_last'][0]]
            found=rows(c,'SELECT '+scope+'::text,'+('NULL' if escrow else 'project_id::text')+',source_rowid FROM em_content.'+t+where+' ORDER BY '+scope+',source_rowid LIMIT 128',tuple(args))
            check(all(r[0]==owner and r[1] is None and type(r[2]) is int for r in found),'continuity_target_scope_changed')
            total=p['count_rows']+len(found);check(total<=wanted,'unreceipted_continuity_target_rows');done=len(found)<128
            if done:check(total==wanted,'continuity_target_count_incomplete')
            c.execute('UPDATE em_content.history_continuity_progress SET count_table=%s,count_rows=%s,count_last=%s::jsonb WHERE run_id=%s::uuid',
                (p['count_table']+int(done),0 if done else total,None if done else M.canonical([found[-1][2]]).decode(),plan.run_id))
            result=dict(table_index=p['count_table'],rows_checked=len(found),progress=self._progress(c,plan))
        return result
    def _sequence_proof(self,c,plan,*,verified):
        from .continuity_sequences import finish
        proof={}
        for table,evidence in plan.header['sequence'].items():
            top=rows(c,'SELECT '+M.quote(M.AUTO[table])+' FROM em_content.'+table+' ORDER BY '+M.quote(M.AUTO[table])+' DESC LIMIT 1')
            maximum=max(0,top[0][0]) if top else 0
            proof[table]=finish(c,table,evidence,maximum,verified=verified)
        return proof

    def finalize(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('counting','verified') and p['count_table']==len(COUNT_TABLES) and p['next_verify']==plan.page_count,'continuity_verification_incomplete')
            check(rows(c,'SELECT run_id::text FROM em_content.history_continuity_runs ORDER BY run_id LIMIT 2')==[(plan.run_id,)],'continuity_target_run_exists')
            expected=sorted((plan.run_id,page['table'],page['chunk_key'],page['input_hash'],page['row_count'],page['input_bytes'])
                for page in plan.document['pages'] if page['row_count'])
            actual=rows(c,'''SELECT run_id::text,table_code,chunk_key,input_hash,row_count,input_bytes
                FROM em_content.history_continuity_chunks ORDER BY run_id,table_code,chunk_key LIMIT 4097''')
            check(sorted(actual)==expected,'continuity_receipt_set_changed')
            sequence_proof=self._sequence_proof(c,plan,verified=p['stage']=='verified')
            c.execute("UPDATE em_content.history_continuity_progress SET stage='verified' WHERE run_id=%s::uuid",(plan.run_id,))
        return dict(state='continuity_domain_verified',target_domain_verified=True,continuity_domain_complete=False,
            tables=len(M.TABLES),sequence_proof=sequence_proof,history_complete=False,route_activated=False)
