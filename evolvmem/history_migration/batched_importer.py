"""Bounded real-SQL adapter over the frozen six-table importer.

Requires the independent ledger/ALWAYS triggers and the same quarantined,
unattached legacy gate. A private operator separately supplies formal authority.
Each public operation is one bounded owner transaction. No route activation API.
"""
import hashlib
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from .history_core import importer as F
from .history_core import mapping as M
from .retained_chain import Plan

CORE_SHA='38fc1bc9ad2a8fa7982624afb8ff6b21df448edeb74cdeee38c280c99712cc0b'
TOUCH_BODY='\nBEGIN\n UPDATE em_content.history_batch_progress SET generation=generation+1;\n RETURN NULL;\nEND;\n'
FIELDS=('manifest_hash','stage','next_load','next_restore','next_verify','loaded_counts','loaded_bytes',
        'generation','verify_generation','count_table','count_rows','count_last')
rows=F._rows
check=M.check
WATCHED_TABLES=M.TABLES+('history_import_chunks','history_import_links')


class Budget:
    def __init__(self,seconds):
        check(type(seconds) in (int,float) and 0<seconds<=60,'batch_deadline_invalid')
        self.deadline=time.monotonic()+seconds;self.owner=(os.getpid(),threading.get_ident())
    def check(self):
        check(self.owner==(os.getpid(),threading.get_ident()),'batch_owner_changed')
        check(time.monotonic()<self.deadline,'batch_deadline_exceeded')


class BudgetConnection:
    """Original 3s SQL limit plus immediate same-budget check at every effect.

    No background cancellation or hard kernel deadline is claimed. A blocked
    driver still needs the caller's bounded connect timeout/process supervisor.
    Rollback/close always remain available after expiry.
    """
    def __init__(self,raw,budget):self.raw=raw;self.budget=budget
    def __getattr__(self,name):return getattr(self.raw,name)
    def execute(self,*args,**kwargs):
        self.budget.check();result=self.raw.execute(*args,**kwargs);self.budget.check();return result
    def commit(self):self.budget.check();return self.raw.commit()


@contextmanager
def batch_budget(port,seconds):
    check(not getattr(port,'_batch_open',False),'batch_already_open')
    budget=Budget(seconds);original=port.factory;port._batch_open=True
    def factory():
        budget.check();raw=original()
        try:budget.check();return BudgetConnection(raw,budget)
        except BaseException:
            raw.close();raise
    port.factory=factory
    try:yield budget
    finally:port.factory=original;port._batch_open=False


def same_generation(progress):
    check(progress['verify_generation'] is not None and progress['generation']==progress['verify_generation'],
          'verification_generation_changed')


class BatchedImporter(F.Importer):
    def __init__(self,factory,target,*,capacity_guard):
        check(hashlib.sha256(Path(F.__file__).read_bytes()).hexdigest()==CORE_SHA,'frozen_importer_changed')
        super().__init__(factory,target,capacity_guard=capacity_guard)

    def _legacy_refused(self,*args,**kwargs):raise M.ImportError('bounded_manifest_api_required')
    begin_run=import_chunk=lookup_chunk=restore_links=verify=_legacy_refused

    def _gate(self,c):
        super()._gate(c)  # lock all six targets BEFORE RLS/owner/policy validation
        ledger=['history_batch_schema_migrations','history_batch_progress']
        for table in ledger:c.execute('LOCK TABLE em_content.'+table+' IN SHARE ROW EXCLUSIVE MODE')
        check(rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_batch_schema_migrations')==
            [(1,'001_history_bounded_chain',self.target.library_id)],'history_batch_schema_required')
        private=rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only'
            ORDER BY c.relname''',(ledger,))
        check(private==[(t,True,True,True,True,True,True,True) for t in sorted(ledger)],'history_batch_private_ledger_required')
        triggers=rows(c,'''SELECT c.relname,t.tgenabled='A',t.tgtype=60,NOT t.tgisinternal,
            f.proowner=current_user::regrole,NOT f.prosecdef,f.proconfig=ARRAY['search_path=pg_catalog']::text[],
            f.prosrc=%s,fn.nspname='em_content' AND f.proname='history_batch_touch'
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
            WHERE n.nspname='em_content' AND t.tgname='history_batch_generation' ORDER BY c.relname''',(TOUCH_BODY,))
        check(triggers==[(t,True,True,True,True,True,True,True,True) for t in sorted(WATCHED_TABLES)],'history_generation_trigger_required')

    def _progress(self,c,plan):
        self._run(c,plan)
        value=rows(c,'SELECT '+','.join(FIELDS)+' FROM em_content.history_batch_progress WHERE run_id=%s::uuid FOR UPDATE',(plan.run_id,))
        check(len(value)==1,'batch_run_missing');p=dict(zip(FIELDS,value[0]))
        check(p['manifest_hash']==plan.sha256,'batch_manifest_conflict')
        if p['stage'] in ('verifying','counting','verified'):same_generation(p)
        return p

    def begin(self,plan):
        check(type(plan) is Plan,'trusted_plan_required');plan.require_importable()
        with self._tx() as c:
            c.execute('LOCK TABLE em_content.history_import_runs IN EXCLUSIVE MODE')
            existing=rows(c,'SELECT run_id::text FROM em_content.history_import_runs')
            if existing:
                check(existing==[(plan.run_id,)],'target_run_already_registered');return self._progress(c,plan)
            for table in M.TABLES:check(not rows(c,'SELECT 1 FROM em_content.'+table+' LIMIT 1'),'target_not_empty')
            bound=self.bound_manifest(plan)
            c.execute('''INSERT INTO em_content.history_import_runs(run_id,library_id,binding_hash,manifest,state)
                VALUES(%s::uuid,%s::uuid,%s,%s::jsonb,'loading')''',
                (plan.run_id,self.target.library_id,M.digest(bound),M.canonical(bound).decode()))
            c.execute('''INSERT INTO em_content.history_batch_progress(run_id,manifest_hash,stage,loaded_counts)
                VALUES(%s::uuid,%s,'loading',%s::jsonb)''',
                (plan.run_id,plan.sha256,M.canonical({t:0 for t in M.TABLES}).decode()))
            return self._progress(c,plan)

    def status(self,plan):
        with self._tx() as c:return self._progress(c,plan)

    def _page(self,plan,index):
        check(type(plan) is Plan and type(index) is int and 0<=index<plan.page_count,'plan_page_required')
        return plan.page(index)

    def _checked_receipt(self,c,plan,page,state):
        receipt=self._receipt(c,plan,page['table'],page['chunk_key'])
        if not page['row_count']:
            check(receipt is None,'unexpected_empty_page_receipt');return None
        check(receipt is not None and receipt[0]==page['input_hash'] and receipt[2:]==(page['row_count'],page['input_bytes']),
            'manifest_receipt_conflict')
        return self._readback(c,plan,page['table'],receipt,state)

    def load_page(self,plan,index,args):
        page=self._page(plan,index);table=page['table']
        if page['row_count']:
            check(type(args) is dict and set(args)=={'table','chunk_key','rows','expected_input_hash'} and
                args['table']==table and args['chunk_key']==page['chunk_key'] and args['expected_input_hash']==page['input_hash'],'page_arguments_changed')
            source,hashed,size=F.prepare_chunk(table,args['rows'],dict(plan.project_mapping),page['input_hash'])
            check(len(source)==page['row_count'] and size==page['input_bytes'],'page_size_changed')
        else:check(args is None,'empty_page_arguments');source=[];hashed=None;size=0
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_load']:
                state='links_restored' if index<p['next_restore'] else 'loading'
                self._checked_receipt(c,plan,page,state);return dict(replayed=True,index=index,progress=p)
            check(p['stage']=='loading' and index==p['next_load'],'load_frontier_conflict')
            check(self._receipt(c,plan,table,page['chunk_key']) is None,'unexpected_page_receipt')
            for row in source:
                c.execute(*F.insert_statement(table,row,dict(plan.project_mapping),self.target.library_id))
                if table in M.LINK_TABLES:
                    c.execute('''INSERT INTO em_content.history_import_links(run_id,table_code,source_id,supersedes,superseded_by)
                        VALUES(%s::uuid,%s,%s,%s,%s)''',(plan.run_id,table,row['id'],row['supersedes'],row['superseded_by']))
            if source:
                keys=[row[M.KEYS[table]] for row in source]
                c.execute('''INSERT INTO em_content.history_import_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                    VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',
                    (plan.run_id,table,page['chunk_key'],hashed,M.canonical(keys).decode(),len(source),size))
                self._checked_receipt(c,plan,page,'loading')
            counts=dict(p['loaded_counts']);counts[table]+=len(source);total=p['loaded_bytes']+size
            check(counts[table]<=plan.counts[table] and total<=plan.header['input_bytes'],'manifest_total_exceeded')
            last=index+1==plan.page_count
            if last:check(counts==plan.counts and total==plan.header['input_bytes'],'load_totals_incomplete')
            c.execute('''UPDATE em_content.history_batch_progress SET next_load=%s,loaded_counts=%s::jsonb,
                loaded_bytes=%s,stage=%s WHERE run_id=%s::uuid''',
                (index+1,M.canonical(counts).decode(),total,'restoring' if last else 'loading',plan.run_id))
            result=dict(replayed=False,index=index,progress=self._progress(c,plan))
        return result

    def restore_page(self,plan,index):
        page=self._page(plan,index);table=page['table']
        with self._tx() as c:
            p=self._progress(c,plan)
            if index<p['next_restore']:
                self._checked_receipt(c,plan,page,'links_restored');return dict(replayed=True,index=index,progress=p)
            check(p['stage']=='restoring' and index==p['next_restore'],'restore_frontier_conflict')
            self._checked_receipt(c,plan,page,'loading')
            if page['row_count'] and table in M.LINK_TABLES:
                receipt=self._receipt(c,plan,table,page['chunk_key'])
                c.execute('UPDATE em_content.'+table+''' t SET supersedes=l.supersedes,superseded_by=l.superseded_by
                    FROM em_content.history_import_links l WHERE l.run_id=%s::uuid AND l.table_code=%s
                    AND l.source_id=ANY(%s::bigint[]) AND t.library_id=%s::uuid AND t.id=l.source_id''',
                    (plan.run_id,table,receipt[1],self.target.library_id))
                c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            self._checked_receipt(c,plan,page,'links_restored')
            c.execute('UPDATE em_content.history_batch_progress SET next_restore=%s WHERE run_id=%s::uuid',(index+1,plan.run_id))
            if index+1==plan.page_count:
                c.execute("UPDATE em_content.history_import_runs SET state='links_restored' WHERE run_id=%s::uuid",(plan.run_id,))
                c.execute("UPDATE em_content.history_batch_progress SET stage='verifying',verify_generation=generation WHERE run_id=%s::uuid",(plan.run_id,))
            result=dict(replayed=False,index=index,progress=self._progress(c,plan))
        return result

    def verify_page(self,plan,index):
        page=self._page(plan,index)
        with self._tx() as c:
            p=self._progress(c,plan);same_generation(p)
            check(index<=p['next_verify'] and p['stage'] in ('verifying','counting','verified'),'verify_frontier_conflict')
            self._checked_receipt(c,plan,page,'links_restored')
            if index==p['next_verify']:
                c.execute('UPDATE em_content.history_batch_progress SET next_verify=%s,stage=%s WHERE run_id=%s::uuid',
                    (index+1,'counting' if index+1==plan.page_count else 'verifying',plan.run_id))
            result=dict(index=index,readback_verified=True,progress=self._progress(c,plan))
        return result

    def count_page(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);same_generation(p)
            check(p['stage']=='counting','count_phase_required')
            if p['count_table']==6:return dict(count_complete=True,progress=p)
            table=M.TABLES[p['count_table']];key=M.target_key(table)
            where='' if p['count_last'] is None else ' AND '+key+'>%s'
            args=(self.target.library_id,) if p['count_last'] is None else (self.target.library_id,p['count_last'])
            found=rows(c,'SELECT '+key+' FROM em_content.'+table+' WHERE library_id=%s::uuid'+where+' ORDER BY '+key+' LIMIT 128',args)
            total=p['count_rows']+len(found);check(total<=plan.counts[table],'unreceipted_target_rows')
            done=len(found)<128
            if done:check(total==plan.counts[table],'target_count_incomplete')
            c.execute('UPDATE em_content.history_batch_progress SET count_table=%s,count_rows=%s,count_last=%s::jsonb WHERE run_id=%s::uuid',
                (p['count_table']+int(done),0 if done else total,None if done else M.canonical(found[-1][0]).decode(),plan.run_id))
            result=dict(table_index=p['count_table'],rows_checked=len(found),count_complete=done and p['count_table']==5,
                progress=self._progress(c,plan))
        return result

    def finalize(self,plan):
        with self._tx() as c:
            p=self._progress(c,plan);same_generation(p)
            check(p['stage'] in ('counting','verified') and p['count_table']==6 and p['next_verify']==plan.page_count,'verification_incomplete')
            next_ids={}
            for table in ('memories','context_items','context_layers'):
                key=M.target_key(table)
                top=rows(c,'SELECT '+key+' FROM em_content.'+table+' WHERE library_id=%s::uuid ORDER BY '+key+' DESC LIMIT 1',(self.target.library_id,))
                maximum=max(0,top[0][0]) if top else 0
                check(plan.highwaters[table]>=maximum,'source_highwater_below_imported_id')
                if table not in M.LINK_TABLES:continue
                sequence='em_content.'+table+'_id_seq'
                check(rows(c,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+table,'id'))==[(sequence,)],'identity_sequence_changed')
                last,called=rows(c,'SELECT last_value,is_called FROM '+sequence)[0]
                value=max(1,plan.highwaters[table]+1,maximum+1,last+int(called));check(value<=M.INT64_MAX,'identity_sequence_exhausted')
                if p['stage']!='verified':c.execute('ALTER SEQUENCE '+sequence+' RESTART WITH '+str(value))
                else:check(last>=max(plan.highwaters[table]+1,maximum+1),'identity_sequence_regressed')
                next_ids[table]=value
            c.execute("UPDATE em_content.history_import_runs SET state='core_verified',verified_at=COALESCE(verified_at,clock_timestamp()) WHERE run_id=%s::uuid",(plan.run_id,))
            c.execute("UPDATE em_content.history_batch_progress SET stage='verified' WHERE run_id=%s::uuid",(plan.run_id,))
        return dict(state='core_verified',next_identity=next_ids,tables=6,history_complete=False,route_activated=False)
