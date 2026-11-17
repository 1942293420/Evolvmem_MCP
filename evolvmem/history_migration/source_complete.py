"""Bounded seven-table readback/reconciliation on the original transaction gate."""
import hashlib
from pathlib import Path
from . import source_importer as I
from . import source_mapping as M
from .source_plan import Plan

check=M.check
rows=I.C._rows
FIELDS=('manifest_hash','stage','next_load','next_verify','generation','verify_generation','count_table','count_rows','count_last')
TOUCH_BODY='\nBEGIN\n UPDATE em_content.history_source_batch_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'
WATCH=M.TABLES+('history_source_chunks',)

class CompleteImporter(I.SourcesImporter):
    def __init__(self,*args,**kwargs):
        check(hashlib.sha256(Path(I.__file__).read_bytes()).hexdigest()=='de2b1d06904314ceadcfd4bf6c0fc48342c87f7222936d001cbf020378d27069','frozen_source_importer_changed')
        super().__init__(*args,**kwargs)
    def _manifest_required(self,*args,**kwargs):raise M.ImportError('source_manifest_api_required')
    begin_run=import_chunk=lookup_chunk=restore_links=verify=_manifest_required
    def _gate(self,c):
        super()._gate(c)
        ledger=['history_source_batch_schema_migrations','history_source_batch_progress']
        for t in ledger:c.execute('LOCK TABLE em_content.'+t+' IN SHARE ROW EXCLUSIVE MODE')
        check(rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_source_batch_schema_migrations')==
            [(1,'002_history_sources_bounded',self.target.library_id)],'source_batch_schema_required')
        private=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only' ORDER BY c.relname''',(ledger,))
        check(private==[(t,True,True,True,True,True,True,True) for t in sorted(ledger)],'source_batch_private_ledger_required')
        triggers=rows(c,'''SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,
            f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],
            f.prosrc=%s,fn.nspname='em_content' AND f.proname='history_source_batch_touch'
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
            WHERE n.nspname='em_content' AND t.tgname='history_source_batch_generation' ORDER BY c.relname''',(TOUCH_BODY,))
        check(triggers==[(t,True,True,True,True,True,True,True,True) for t in sorted(WATCH)],'source_generation_trigger_required')
    def _progress(self,c,plan):
        check(type(plan) is Plan,'trusted_source_plan_required');counts,size=self._run(c,plan.run)
        data=rows(c,'SELECT '+','.join(FIELDS)+' FROM em_content.history_source_batch_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(data)==1,'source_batch_run_missing');p=dict(zip(FIELDS,data[0]));check(p['manifest_hash']==plan.sha256,'source_plan_conflict')
        if p['stage']!='loading':check(p['verify_generation'] is not None and p['generation']==p['verify_generation'],'source_verification_generation_changed')
        p.update(loaded_counts=counts,loaded_bytes=size);return p
    def begin(self,plan):
        check(type(plan) is Plan,'trusted_source_plan_required');plan.require_importable();run=plan.run
        with self._tx() as c:
            self._parent(c,run);c.execute('LOCK TABLE em_content.history_source_runs IN EXCLUSIVE MODE')
            existing=rows(c,'SELECT run_id::text FROM em_content.history_source_runs')
            if existing:
                check(existing==[(run.run_id,)],'source_target_run_exists');result=self._progress(c,plan)
            else:
                for table in M.TABLES:check(not rows(c,'SELECT 1 FROM em_content.'+table+' LIMIT 1'),'source_complete_empty_target_required')
                bound=self.bound_manifest(run)
                c.execute('''INSERT INTO em_content.history_source_runs(run_id,library_id,core_run_id,binding_hash,manifest,loaded_counts,state)
                    VALUES(%s::uuid,%s::uuid,%s::uuid,%s,%s::jsonb,%s::jsonb,'loading')''',
                    (run.run_id,self.target.library_id,run.core_run_id,M.digest(bound),M.canonical(bound).decode(),M.canonical({t:0 for t in M.TABLES}).decode()))
                c.execute("INSERT INTO em_content.history_source_batch_progress(run_id,manifest_hash,stage) VALUES(%s::uuid,%s,'loading')",(run.run_id,plan.sha256))
                result=self._progress(c,plan)
        return result
    def status(self,plan):
        with self._tx() as c:result=self._progress(c,plan)
        return result
    def _checked(self,c,plan,page):
        receipt=self._receipt(c,plan.run,page['table'],page['chunk_key'])
        if not page['row_count']:
            check(receipt is None,'unexpected_empty_source_receipt');return
        check(receipt is not None and receipt[0]==page['input_hash'] and receipt[2:]==(page['row_count'],page['input_bytes']),'source_page_receipt_conflict')
        self._readback(c,plan.run,page['table'],receipt)
    def load_page(self,plan,index,args):
        page=plan.page(index);table=page['table']
        if page['row_count']:
            check(type(args) is dict and set(args)=={'table','chunk_key','rows','expected_input_hash'} and args['table']==table
                and args['chunk_key']==page['chunk_key'] and args['expected_input_hash']==page['input_hash'],'source_page_arguments_changed')
            source,hashed,size=M.prepare_chunk(table,args['rows'],page['input_hash'])
            check(len(source)==page['row_count'] and size==page['input_bytes'],'source_page_size_changed')
        else:check(args is None,'source_empty_page_arguments');source=[];size=0;hashed=None
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_load']:
                self._checked(c,plan,page);result=dict(replayed=True,progress=p)
            else:
                check(p['stage']=='loading' and index==p['next_load'],'source_load_frontier_conflict')
                check(self._receipt(c,plan.run,table,page['chunk_key']) is None,'unexpected_source_receipt')
                for row in source:c.execute(*M.insert_statement(table,row,self.target.library_id))
                if source:
                    keys=[list(M.key(table,row)) for row in source]
                    c.execute('''INSERT INTO em_content.history_source_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                        VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',(plan.run_id,table,page['chunk_key'],hashed,M.canonical(keys).decode(),len(source),size))
                    self._checked(c,plan,page)
                counts=dict(p['loaded_counts']);counts[table]+=len(source);total=p['loaded_bytes']+size
                check(counts[table]<=plan.counts[table] and total<=plan.header['input_bytes'],'source_manifest_total_exceeded')
                c.execute('UPDATE em_content.history_source_runs SET loaded_counts=%s::jsonb,loaded_bytes=%s WHERE run_id=%s::uuid',(M.canonical(counts).decode(),total,plan.run_id))
                c.execute('UPDATE em_content.history_source_batch_progress SET next_load=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
                if index+1==plan.page_count:
                    check(counts==plan.counts and total==plan.header['input_bytes'],'source_load_totals_incomplete')
                    c.execute("UPDATE em_content.history_source_batch_progress SET stage='verifying',verify_generation=generation WHERE run_id=%s::uuid",(plan.run_id,))
                result=dict(replayed=False,progress=self._progress(c,plan))
        return result
    def verify_page(self,plan,index):
        page=plan.page(index)
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('verifying','counting','verified') and index<=p['next_verify'],'source_verify_frontier_conflict')
            self._checked(c,plan,page)
            if index==p['next_verify']:
                c.execute('UPDATE em_content.history_source_batch_progress SET next_verify=%s,stage=%s WHERE run_id=%s::uuid',
                    (index+1,'counting' if index+1==plan.page_count else 'verifying',plan.run_id))
            result=dict(readback_verified=True,progress=self._progress(c,plan))
        return result
    def count_page(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage']=='counting' and p['count_table']<7,'source_count_phase_required')
            table=M.TABLES[p['count_table']];keys=M.KEYS[table];where='';args=[]
            if p['count_last'] is not None:
                check(type(p['count_last']) is list and len(p['count_last'])==len(keys) and all(type(n) is int for n in p['count_last']),'source_count_cursor_changed')
                where=' WHERE (library_id,'+','.join(keys)+')>(%s::uuid,'+','.join(['%s']*len(keys))+')';args=[self.target.library_id,*p['count_last']]
            # Reconcile the entire isolated table, including misplaced rows.
            # The previous page proved its keys belong to this library, so the
            # next tuple boundary is (trusted library, last verified key).
            found=rows(c,'SELECT library_id::text,project_id::text,'+','.join(keys)+' FROM em_content.'+table+where+' ORDER BY library_id,'+','.join(keys)+' LIMIT 128',tuple(args))
            check(all(r[0]==self.target.library_id and r[1] is None and all(type(k) is int for k in r[2:]) for r in found),'source_target_scope_changed')
            total=p['count_rows']+len(found);check(total<=plan.counts[table],'unreceipted_source_target_rows');done=len(found)<128
            if done:check(total==plan.counts[table],'source_target_count_incomplete')
            c.execute('UPDATE em_content.history_source_batch_progress SET count_table=%s,count_rows=%s,count_last=%s::jsonb WHERE run_id=%s::uuid',
                (p['count_table']+int(done),0 if done else total,None if done else M.canonical(list(found[-1][2:])).decode(),plan.run_id))
            result=dict(table_index=p['count_table'],rows_checked=len(found),progress=self._progress(c,plan))
        return result
    def finalize(self,plan):
        plan.require_importable()
        with self._tx() as c:
            p=self._progress(c,plan);check(p['stage'] in ('counting','verified') and p['count_table']==7 and p['next_verify']==plan.page_count,'source_verification_incomplete')
            next_ids={}
            for table in M.AUTO:
                top=rows(c,'SELECT id FROM em_content.'+table+' WHERE library_id=%s::uuid ORDER BY id DESC LIMIT 1',(self.target.library_id,))
                maximum=max(0,top[0][0]) if top else 0;high=plan.highwaters[table]
                check(type(high) is int and high>=maximum,'source_highwater_below_target')
                sequence='em_content.'+table+'_id_seq'
                check(rows(c,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+table,'id'))==[(sequence,)],'source_identity_sequence_changed')
                last,called=rows(c,'SELECT last_value,is_called FROM '+sequence)[0]
                value=max(1,high+1,maximum+1,last+int(called));check(value<=M.INT64_MAX,'source_identity_sequence_exhausted')
                if p['stage']!='verified':c.execute('ALTER SEQUENCE '+sequence+' RESTART WITH '+str(value))
                else:check(last>=max(high+1,maximum+1),'source_identity_sequence_regressed')
                next_ids[table]=value
            c.execute("UPDATE em_content.history_source_batch_progress SET stage='verified' WHERE run_id=%s::uuid",(plan.run_id,))
        # A target transaction alone cannot attest a fresh sealed-source phase.
        # Only the runner joins this proof with a successful phase-end SHA.
        return dict(state='source_domain_verified',target_domain_verified=True,source_domain_complete=False,next_identity=next_ids,
            tables=7,history_complete=False,route_activated=False)
