"""Private six-table importer using fresh real psycopg-like owner connections.

Factories must resolve trusted private configuration; no DSN/SQL/path is accepted
from an HTTP caller. Targets remain unattached and quarantined; nonpilot targets
also require a live bound operator lease. This module never activates a route.
"""
from contextlib import contextmanager
from dataclasses import dataclass,asdict
import json
import re

from .mapping import (TABLES,KEYS,LINK_TABLES,INT64_MAX,ImportError,check,uuid_text,
    canonical,digest,ordered_rows,chunk_hash,map_row,unmap_row,target_key,target_columns,is_json_column)

MAX_ROWS=3000
MAX_CHUNK_ROWS=128
MAX_CHUNK_BYTES=4*1024*1024
MAX_RUN_BYTES=32*1024*1024
HASH=re.compile(r'[0-9a-f]{64}\Z')
NAME=re.compile(r'[a-z][a-z0-9_]{0,62}\Z')


@dataclass(frozen=True)
class Run:
    run_id:str
    source_namespace:str
    source_schema_hash:str
    source_snapshot_hash:str
    expected_counts:tuple
    source_highwaters:tuple
    project_mapping:tuple=()

    def __post_init__(self):
        uuid_text(self.run_id)
        check(self.source_namespace in ('owner','kane','public'),'source_namespace_required')
        check(all(type(v) is str and HASH.fullmatch(v) for v in (self.source_schema_hash,self.source_snapshot_hash)),'source_hash_required')
        for values in (self.expected_counts,self.source_highwaters,self.project_mapping):
            check(type(values) is tuple and all(type(v) is tuple and len(v)==2 for v in values),'immutable_mapping_required')
            check(len(dict(values))==len(values),'duplicate_mapping_key')
        counts=dict(self.expected_counts);high=dict(self.source_highwaters);projects=dict(self.project_mapping)
        check(set(counts)==set(TABLES),'all_six_counts_required')
        check(all(type(n) is int and 0<=n<=MAX_ROWS for n in counts.values()) and sum(counts.values())<=MAX_ROWS,'run_row_budget')
        check(set(high)=={'memories','context_items','context_layers'} and all(type(n) is int and 0<=n<INT64_MAX for n in high.values()),'source_highwaters_required')
        check(len(set(projects.values()))==len(projects),'duplicate_project_uuid')
        for name,value in projects.items():
            check(type(name) is str and name and '\x00' not in name,'project_mapping_name_required');uuid_text(value)

    @property
    def manifest(self):return json.loads(canonical(asdict(self)))
    @property
    def binding_hash(self):return digest(self.manifest)


@dataclass(frozen=True)
class Target:
    database:str
    owner_role:str
    login_role:str
    library_id:str
    route_epoch:int
    policy_epoch:int

    def __post_init__(self):
        from ..operator_target import permits
        check(type(self.database) is str and (re.fullmatch(r'em_pilot_[a-z0-9_]{1,48}',self.database) or permits(asdict(self))),'synthetic_target_required')
        check(all(type(x) is str and NAME.fullmatch(x) for x in (self.owner_role,self.login_role)),'trusted_roles_required')
        check(self.owner_role!=self.login_role,'nonlogin_owner_required');uuid_text(self.library_id)
        check(all(type(x) is int and x>0 for x in (self.route_epoch,self.policy_epoch)),'target_epochs_required')


@contextmanager
def transaction(factory):
    raw=None;begun=False
    try:
        raw=factory()
        check(raw.closed is False and raw.autocommit is True and raw.info.transaction_status==0,'fresh_owner_connection_required')
        raw.execute('BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE');begun=True
        raw.execute("SET LOCAL statement_timeout='3000ms'")
        raw.execute("SET LOCAL lock_timeout='1000ms'")
        raw.execute("SET LOCAL idle_in_transaction_session_timeout='5000ms'")
        yield raw
        try:raw.commit()
        except BaseException:raise ImportError('commit_outcome_unknown') from None
    except BaseException as exc:
        if raw is not None and begun:
            try:raw.rollback()
            except BaseException:pass
        if isinstance(exc,(ImportError,KeyboardInterrupt,SystemExit)):raise
        if getattr(exc,'sqlstate',None) in ('23502','23503','23505','23514'):
            raise ImportError('target_constraint_conflict') from None
        raise ImportError('import_transaction_failed') from None
    finally:
        if raw is not None:
            try:raw.close()
            except BaseException:pass


def prepare_chunk(table,rows,projects,expected_input_hash):
    # Materialize only the bounded input; never consume an unbounded iterator.
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_CHUNK_ROWS,'chunk_row_budget')
    rows=ordered_rows(table,rows)
    size=len(canonical(rows));check(size<=MAX_CHUNK_BYTES,'chunk_byte_budget')
    for row in rows:map_row(table,row,projects)
    hashed=chunk_hash(table,rows)
    check(expected_input_hash is None or expected_input_hash==hashed,'input_hash_mismatch')
    return rows,hashed,size


def insert_statement(table,row,projects,library_id):
    mapped=map_row(table,row,projects)
    if table in LINK_TABLES:mapped.update(supersedes=None,superseded_by=None)
    columns=target_columns(table)
    values=[mapped[name] for name in columns]
    for i,name in enumerate(columns):
        if is_json_column(table,name):values[i]=None if values[i] is None else canonical(values[i]).decode()
    placeholders=['%s::uuid','%s::uuid']+['%s::jsonb' if is_json_column(table,name) else '%s' for name in columns]
    override=' OVERRIDING SYSTEM VALUE' if table in LINK_TABLES else ''
    sql='INSERT INTO em_content.'+table+'(library_id,project_id,'+','.join(columns)+')'+override+' VALUES('+','.join(placeholders)+')'
    return sql,(library_id,None,*values)


def _rows(c,sql,args=()):return c.execute(sql,args).fetchall()


class Importer:
    def __init__(self,factory,target,*,capacity_guard):
        check(isinstance(target,Target) and callable(factory) and callable(capacity_guard),'private_factory_and_capacity_guard_required')
        self.factory=factory;self.target=target;self.capacity_guard=capacity_guard

    def bound_manifest(self,run):
        return {'source':run.manifest,'target':asdict(self.target)}

    def _gate(self,c):
        t=self.target
        if not re.fullmatch(r'em_pilot_[a-z0-9_]{1,48}',t.database):
            from ..operator_target import gate
            gate(c,t)
        rows=_rows(c,'''SELECT current_database(),current_user,session_user,r.rolcanlogin,r.rolsuper,r.rolbypassrls,
            r.rolcreatedb,r.rolcreaterole,(d.datdba=r.oid)
            FROM pg_roles r JOIN pg_database d ON d.datname=current_database() WHERE r.rolname=current_user''')
        check(rows==[(t.database,t.owner_role,t.login_role,False,False,False,False,False,True)],'owner_connection_identity_mismatch')
        state=_rows(c,'''SELECT library_id::text,kind,project_id::text,authority_state,acl_state,route_epoch,policy_epoch
            FROM em_security.library_state WHERE singleton FOR SHARE''')
        check(state==[(t.library_id,'legacy',None,'pg_staging','updating',t.route_epoch,t.policy_epoch)],'unattached_staging_target_required')
        ledger=('history_schema_migrations','history_import_runs','history_import_chunks','history_import_links')
        # Acquire locks before metadata validation, closing an ALTER-policy TOCTOU.
        for table in TABLES+ledger:
            c.execute('LOCK TABLE em_content.'+table+' IN SHARE ROW EXCLUSIVE MODE')
        version=_rows(c,'''SELECT version,migration_name,library_id::text FROM em_content.history_schema_migrations''')
        check(version==[(1,'001_history_six_fixture',t.library_id)],'history_schema_required')
        # Quarantine must remain restrictive on every affected business table.
        policies=_rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND NOT p.polpermissive,p.polroles=ARRAY[0::oid],
            pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)',
            pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)'
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_policy p ON p.polrelid=c.oid WHERE n.nspname='em_content'
            AND p.polname='history_fixture_quarantine' ORDER BY c.relname''',(t.owner_role,t.owner_role))
        check(policies==[(table,True,True,True,True,True,True,True) for table in sorted(TABLES)],'history_quarantine_required')
        private=_rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy allpol WHERE allpol.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_policy p ON p.polrelid=c.oid WHERE n.nspname='em_content'
            AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only' ORDER BY c.relname''',(list(ledger),))
        check(private==[(table,True,True,True,True,True,True,True) for table in sorted(ledger)],'history_private_ledger_required')

    def _run(self,c,run):
        rows=_rows(c,'''SELECT binding_hash,manifest,state,library_id::text FROM em_content.history_import_runs
                       WHERE run_id=%s::uuid FOR UPDATE''',(run.run_id,))
        check(len(rows)==1,'run_not_registered')
        binding,manifest,state,lib=rows[0]
        expected=self.bound_manifest(run)
        check(binding==digest(expected) and manifest==expected and lib==self.target.library_id,'run_binding_conflict')
        return state

    @contextmanager
    def _tx(self):
        self.capacity_guard()
        with transaction(self.factory) as c:
            self._gate(c)
            yield c
            self.capacity_guard()  # Any loss of the capacity floor rolls back this batch.

    def begin_run(self,run):
        check(isinstance(run,Run),'trusted_run_required')
        with self._tx() as c:
            # Serialize an empty-target registration before a run row exists.
            c.execute('LOCK TABLE em_content.history_import_runs IN EXCLUSIVE MODE')
            existing=_rows(c,'SELECT run_id::text FROM em_content.history_import_runs')
            if existing:
                check(existing==[(run.run_id,)],'target_run_already_registered')
                self._run(c,run);return 'already_registered'
            for table in TABLES:
                check(not _rows(c,'SELECT 1 FROM em_content.'+table+' LIMIT 1'),'target_not_empty')
            c.execute('''INSERT INTO em_content.history_import_runs(run_id,library_id,binding_hash,manifest,state)
                VALUES(%s::uuid,%s::uuid,%s,%s::jsonb,'loading')''',
                (run.run_id,self.target.library_id,digest(self.bound_manifest(run)),canonical(self.bound_manifest(run)).decode()))
        return 'registered'

    def _receipt(self,c,run,table,key):
        rows=_rows(c,'''SELECT input_hash,row_keys,row_count,input_bytes FROM em_content.history_import_chunks
                       WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s''',(run.run_id,table,key))
        return rows[0] if rows else None

    def _readback(self,c,run,table,receipt,state):
        hashed,keys,count,size=receipt
        check(type(keys) is list and 1<=len(keys)<=MAX_CHUNK_ROWS and len(keys)==count and len(set(keys))==count,'invalid_chunk_receipt')
        columns=target_columns(table)
        select_columns=[name+'::text' if name=='project_uuid' else name for name in columns]
        rows=_rows(c,'SELECT '+','.join(select_columns)+',project_id::text FROM em_content.'+table+
            ' WHERE library_id=%s::uuid AND '+target_key(table)+' IN ('+','.join(['%s']*len(keys))+')',
            (self.target.library_id,*keys))
        check(len(rows)==count,'chunk_target_missing')
        links={}
        if table in LINK_TABLES:
            links={row[0]:(row[1],row[2]) for row in _rows(c,'''SELECT source_id,supersedes,superseded_by
                FROM em_content.history_import_links WHERE run_id=%s::uuid AND table_code=%s AND source_id=ANY(%s::bigint[])''',
                (run.run_id,table,keys))}
            check(set(links)==set(keys),'chunk_links_missing')
        source=[]
        for values in rows:
            check(values[-1] is None,'target_project_scope_changed')
            actual=dict(zip(columns,values[:-1]));original=unmap_row(table,actual)
            if table in LINK_TABLES:
                link=links[original['id']]
                check((actual['supersedes'],actual['superseded_by'])==((None,None) if state=='loading' else link),'target_links_changed')
                original.update(supersedes=link[0],superseded_by=link[1])
            expected=map_row(table,original,dict(run.project_mapping))
            if table in LINK_TABLES and state=='loading':expected.update(supersedes=None,superseded_by=None)
            check(actual==expected,'target_projection_changed')
            source.append(original)
        source=ordered_rows(table,source)
        check(chunk_hash(table,source)==hashed and len(canonical(source))==size,'chunk_digest_changed')
        return dict(input_hash=hashed,row_count=count,input_bytes=size,link_state=state,
                    readback_verified=True,route_activated=False)

    def lookup_chunk(self,run,table,chunk_key):
        check(table in TABLES,'unsupported_table')
        with self._tx() as c:
            state=self._run(c,run);receipt=self._receipt(c,run,table,chunk_key)
            return None if receipt is None else self._readback(c,run,table,receipt,state)

    def import_chunk(self,run,table,chunk_key,rows,expected_input_hash):
        check(type(chunk_key) is str and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',chunk_key),'invalid_chunk_key')
        check(type(expected_input_hash) is str and HASH.fullmatch(expected_input_hash),'expected_input_hash_required')
        rows,hashed,size=prepare_chunk(table,rows,dict(run.project_mapping),expected_input_hash)
        with self._tx() as c:
            state=self._run(c,run);prior=self._receipt(c,run,table,chunk_key)
            if prior is not None:
                check(prior[0]==hashed,'chunk_input_conflict')
                return self._readback(c,run,table,prior,state)
            check(state=='loading','run_not_loading')
            totals=_rows(c,'''SELECT COALESCE(sum(row_count) FILTER(WHERE table_code=%s),0),COALESCE(sum(input_bytes),0)
                             FROM em_content.history_import_chunks WHERE run_id=%s::uuid''',(table,run.run_id))[0]
            check(totals[0]+len(rows)<=dict(run.expected_counts)[table],'source_count_exceeded')
            check(totals[1]+size<=MAX_RUN_BYTES,'run_byte_budget')
            for row in rows:
                c.execute(*insert_statement(table,row,dict(run.project_mapping),self.target.library_id))
                if table in LINK_TABLES:
                    c.execute('''INSERT INTO em_content.history_import_links(run_id,table_code,source_id,supersedes,superseded_by)
                        VALUES(%s::uuid,%s,%s,%s,%s)''',(run.run_id,table,row['id'],row['supersedes'],row['superseded_by']))
            keys=[row[KEYS[table]] for row in rows]
            c.execute('''INSERT INTO em_content.history_import_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                         VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',
                (run.run_id,table,chunk_key,hashed,canonical(keys).decode(),len(rows),size))
            result=self._readback(c,run,table,(hashed,keys,len(rows),size),state)
        return result

    def _verify_all(self,c,run,state):
        results=[];expected=dict(run.expected_counts)
        chunks=_rows(c,'''SELECT table_code,input_hash,row_keys,row_count,input_bytes FROM em_content.history_import_chunks
                         WHERE run_id=%s::uuid ORDER BY table_code,chunk_key LIMIT 3001''',(run.run_id,))
        check(len(chunks)<=MAX_ROWS,'receipt_budget')
        for table in TABLES:
            selected=[row[1:] for row in chunks if row[0]==table]
            keys=[key for row in selected for key in row[1]]
            check(len(keys)==len(set(keys))==expected[table],'import_count_or_overlap_mismatch')
            actual=_rows(c,'SELECT '+target_key(table)+' FROM em_content.'+table+' WHERE library_id=%s::uuid LIMIT 3001',
                         (self.target.library_id,))
            check(len(actual)==expected[table] and {row[0] for row in actual}==set(keys),'unreceipted_target_rows')
            for receipt in selected:results.append(self._readback(c,run,table,receipt,state))
        return results

    def restore_links(self,run):
        with self._tx() as c:
            state=self._run(c,run)
            self._verify_all(c,run,state)
            if state!='loading':return 'already_restored'
            for table in LINK_TABLES:
                c.execute('UPDATE em_content.'+table+''' t SET supersedes=l.supersedes,superseded_by=l.superseded_by
                    FROM em_content.history_import_links l WHERE l.run_id=%s::uuid AND l.table_code=%s
                    AND t.library_id=%s::uuid AND t.id=l.source_id''',(run.run_id,table,self.target.library_id))
            # Force the stricter target self-FKs before issuing a receipt/state transition.
            c.execute('SET CONSTRAINTS ALL IMMEDIATE')
            self._verify_all(c,run,'links_restored')
            c.execute("UPDATE em_content.history_import_runs SET state='links_restored' WHERE run_id=%s::uuid",(run.run_id,))
        return 'links_restored'

    def finalize(self,run):
        with self._tx() as c:
            state=self._run(c,run);check(state in ('links_restored','core_verified'),'links_not_restored')
            self._verify_all(c,run,state)
            next_ids={}
            for table in LINK_TABLES:
                maximum=_rows(c,'SELECT max(id) FROM em_content.'+table+' WHERE library_id=%s::uuid',(self.target.library_id,))[0][0]
                sequence='em_content.'+table+'_id_seq'
                check(_rows(c,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+table,'id'))==[(sequence,)],'identity_sequence_changed')
                last,called=_rows(c,'SELECT last_value,is_called FROM '+sequence)[0]
                next_value=max(1,dict(run.source_highwaters)[table]+1,(maximum if maximum is not None else 0)+1,last+int(called))
                check(next_value<=INT64_MAX,'identity_sequence_exhausted')
                if state!='core_verified':
                    # ALTER RESTART is transactional; setval() is deliberately not used.
                    c.execute('ALTER SEQUENCE '+sequence+' RESTART WITH '+str(next_value))
                else:check(last>=max(dict(run.source_highwaters)[table]+1,(maximum or 0)+1),'identity_sequence_regressed')
                next_ids[table]=next_value
            layer_max=_rows(c,'SELECT max(legacy_layer_id) FROM em_content.context_layers WHERE library_id=%s::uuid',(self.target.library_id,))[0][0]
            check(dict(run.source_highwaters)['context_layers']>=max(0,layer_max or 0),'layer_highwater_below_imported_id')
            c.execute("UPDATE em_content.history_import_runs SET state='core_verified',verified_at=COALESCE(verified_at,clock_timestamp()) WHERE run_id=%s::uuid",(run.run_id,))
        return dict(state='core_verified',next_identity=next_ids,tables=6,history_complete=False,route_activated=False)

    def verify(self,run):
        with self._tx() as c:
            state=self._run(c,run);self._verify_all(c,run,state)
        return dict(state=state,tables=6,history_complete=False,route_activated=False)
