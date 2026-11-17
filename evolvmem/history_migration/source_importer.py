"""Seven-table chunk writes only; no source scanner, finish or activation API.

Uses the frozen core transaction and bounded-core isolation gate. Assembly must
provide the existing budgeted, capacity-checked private connection factory.
"""
from dataclasses import dataclass,asdict
import re
from .history_core import importer as C
from .history_core.mapping import check,uuid_text,canonical,digest,ImportError
from .batched_importer import BatchedImporter
from . import source_mapping as M

Target=C.Target
transaction=C.transaction
LEDGER=('history_source_schema_migrations','history_source_runs','history_source_chunks')
MAX_RUN_ROWS=250000
MAX_RUN_BYTES=1024**3

@dataclass(frozen=True)
class SourceRun:
    run_id:str
    core_run_id:str
    core_binding_hash:str
    source_namespace:str
    source_file_sha256:str
    source_schema_hash:str
    expected_counts:tuple
    source_highwaters:tuple

    def __post_init__(self):
        uuid_text(self.run_id);uuid_text(self.core_run_id)
        check(self.run_id!=self.core_run_id,'source_run_identity_required')
        check(self.source_namespace in ('owner','kane','public'),'source_namespace_required')
        check(all(type(v) is str and C.HASH.fullmatch(v) for v in (self.core_binding_hash,self.source_file_sha256,self.source_schema_hash)),'source_hash_required')
        check(self.source_schema_hash==M.schema_hash(self.source_namespace),'reviewed_source_schema_required')
        for values in (self.expected_counts,self.source_highwaters):
            check(type(values) is tuple and all(type(p) is tuple and len(p)==2 for p in values),'immutable_mapping_required')
            check(len(dict(values))==len(values),'duplicate_mapping_key')
        counts=dict(self.expected_counts);high=dict(self.source_highwaters)
        check(set(counts)==set(M.TABLES) and all(type(v) is int and 0<=v<=MAX_RUN_ROWS for v in counts.values()) and sum(counts.values())<=MAX_RUN_ROWS,'source_run_row_budget')
        check(set(high)==set(M.AUTO) and all(v is None or type(v) is int and 0<=v<C.INT64_MAX for v in high.values()),'source_sequence_shape_required')

    @property
    def manifest(self):return __import__('json').loads(canonical(asdict(self)))


class SourcesImporter(C.Importer):
    def __init__(self,factory,target,*,capacity_guard):
        super().__init__(factory,target,capacity_guard=capacity_guard)
        self._core=BatchedImporter(factory,target,capacity_guard=capacity_guard)

    def _unsupported(self,*args,**kwargs):raise ImportError('source_domain_completion_not_implemented')
    restore_links=verify=finalize=_unsupported

    def _gate(self,c):
        self._core._gate(c)  # existing core locks, FORCE RLS and generation proof
        for table in M.TABLES+LEDGER:c.execute('LOCK TABLE em_content.'+table+' IN SHARE ROW EXCLUSIVE MODE')
        check(C._rows(c,'SELECT device_id FROM em_security.library_state WHERE singleton')==[(None,)],'legacy_device_binding_forbidden')
        check(C._rows(c,'SELECT version,migration_name,library_id::text FROM em_content.history_source_schema_migrations')==
            [(1,'001_history_sources_seven',self.target.library_id)],'history_source_schema_required')
        policies=C._rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND NOT p.polpermissive,p.polroles=ARRAY[0::oid],
            pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)',
            pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(%s::text)||'::name)'
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND p.polname='history_source_quarantine' ORDER BY c.relname''',
            (self.target.owner_role,self.target.owner_role))
        check(policies==[(t,True,True,True,True,True,True,True) for t in sorted(M.TABLES)],'history_source_quarantine_required')
        private=C._rows(c,'''SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,c.relowner=current_user::regrole,
            p.polcmd='*' AND p.polpermissive,p.polroles=ARRAY[current_user::regrole::oid],
            pg_get_expr(p.polqual,p.polrelid)='true' AND pg_get_expr(p.polwithcheck,p.polrelid)='true',
            (SELECT count(*) FROM pg_policy a WHERE a.polrelid=c.oid)=1
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_policy p ON p.polrelid=c.oid
            WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND p.polname='history_owner_only'
            ORDER BY c.relname''',(list(LEDGER),))
        check(private==[(t,True,True,True,True,True,True,True) for t in sorted(LEDGER)],'history_source_private_ledger_required')

    def _parent(self,c,run):
        check(type(run) is SourceRun,'trusted_source_run_required')
        rows=C._rows(c,'''SELECT r.binding_hash,r.manifest,r.state,r.library_id::text,
            p.stage,p.generation=p.verify_generation FROM em_content.history_import_runs r
            JOIN em_content.history_batch_progress p USING(run_id) WHERE r.run_id=%s::uuid FOR SHARE OF r,p''',(run.core_run_id,))
        check(len(rows)==1,'verified_core_run_required')
        binding,manifest,state,lib,stage,stable=rows[0]
        check(binding==run.core_binding_hash and digest(manifest)==binding and state=='core_verified'
            and lib==self.target.library_id and stage=='verified' and stable is True,'verified_core_binding_changed')
        check(manifest.get('target')==asdict(self.target),'source_core_target_changed')
        source=manifest.get('source',{})
        check(source.get('format')=='history-retained-six-v1'
            and source.get('source',{}).get('namespace')==run.source_namespace
            and source.get('source',{}).get('file_sha256')==run.source_file_sha256,'source_core_snapshot_mismatch')

    def _run(self,c,run):
        self._parent(c,run)
        values=C._rows(c,'''SELECT binding_hash,manifest,state,loaded_counts,loaded_bytes FROM em_content.history_source_runs
            WHERE run_id=%s::uuid AND library_id=%s::uuid FOR UPDATE''',(run.run_id,self.target.library_id))
        check(len(values)==1,'source_run_missing')
        binding,manifest,state,counts,size=values[0]
        check(manifest==self.bound_manifest(run) and binding==digest(manifest) and state=='loading','source_run_binding_changed')
        check(type(counts) is dict and set(counts)==set(M.TABLES) and all(type(v) is int and 0<=v<=dict(run.expected_counts)[t] for t,v in counts.items())
            and type(size) is int and 0<=size<=MAX_RUN_BYTES,'source_progress_invalid')
        return counts,size

    def begin_run(self,run):
        check(type(run) is SourceRun,'trusted_source_run_required')
        with self._tx() as c:
            self._parent(c,run)
            c.execute('LOCK TABLE em_content.history_source_runs IN EXCLUSIVE MODE')
            existing=C._rows(c,'SELECT run_id::text FROM em_content.history_source_runs')
            if existing:
                check(existing==[(run.run_id,)],'source_target_run_exists');self._run(c,run)
                result={'replayed':True}
            else:
                for table in M.TABLES:
                    if table!='context_sources':check(not C._rows(c,'SELECT 1 FROM em_content.'+table+' LIMIT 1'),'source_target_occupied')
                # Existing migration sources are retained, never adopted or rewritten.
                check(not C._rows(c,"SELECT 1 FROM em_content.context_sources WHERE archive_id IS NOT NULL OR source_kind<>'migration' OR project_id IS NOT NULL OR library_id<>%s::uuid LIMIT 1",(self.target.library_id,)),'old_migration_sources_changed')
                bound=self.bound_manifest(run)
                c.execute('''INSERT INTO em_content.history_source_runs(run_id,library_id,core_run_id,binding_hash,manifest,loaded_counts,state)
                    VALUES(%s::uuid,%s::uuid,%s::uuid,%s,%s::jsonb,%s::jsonb,'loading')''',
                    (run.run_id,self.target.library_id,run.core_run_id,digest(bound),canonical(bound).decode(),canonical({t:0 for t in M.TABLES}).decode()))
                result={'replayed':False}
        return dict(result,source_domain_complete=False,route_activated=False)

    def _receipt(self,c,run,table,key):
        rows=C._rows(c,'''SELECT input_hash,row_keys,row_count,input_bytes FROM em_content.history_source_chunks
            WHERE run_id=%s::uuid AND table_code=%s AND chunk_key=%s''',(run.run_id,table,key))
        check(len(rows)<=1,'source_receipt_duplicate')
        return rows[0] if rows else None

    def _readback(self,c,run,table,receipt):
        hashed,keys,count,size=receipt
        check(type(keys) is list and type(count) is int and 1<=count<=128 and len(keys)==count
            and all(type(k) is list and len(k)==len(M.KEYS[table]) and all(type(v) is int for v in k) for k in keys)
            and len({tuple(k) for k in keys})==count,'invalid_source_receipt')
        clauses=['('+ ' AND '.join(k+'=%s' for k in M.KEYS[table])+')' for _ in keys]
        columns=M.columns(table)
        actual=C._rows(c,'SELECT '+','.join(columns)+',project_id::text FROM em_content.'+table+
            ' WHERE library_id=%s::uuid AND ('+' OR '.join(clauses)+')',(self.target.library_id,*(v for k in keys for v in k)))
        check(len(actual)==count,'source_target_missing')
        rows=[]
        for values in actual:
            check(values[-1] is None,'source_target_scope_changed');rows.append(M.unmap_row(table,dict(zip(columns,values[:-1]))))
        rows=M.ordered_rows(table,rows)
        check(M.chunk_hash(table,rows)==hashed and len(canonical(rows))==size,'source_target_digest_changed')
        return dict(input_hash=hashed,row_count=count,input_bytes=size,readback_verified=True,source_domain_complete=False,route_activated=False)

    def _arguments(self,run,table,chunk_key):
        check(type(run) is SourceRun and table in M.TABLES,'trusted_source_chunk_required')
        check(type(chunk_key) is str and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',chunk_key),'invalid_chunk_key')

    def lookup_chunk(self,run,table,chunk_key):
        self._arguments(run,table,chunk_key)
        with self._tx() as c:
            self._run(c,run);receipt=self._receipt(c,run,table,chunk_key)
            result=None if receipt is None else self._readback(c,run,table,receipt)
        return result

    def import_chunk(self,run,table,chunk_key,rows,expected_input_hash):
        self._arguments(run,table,chunk_key)
        check(type(expected_input_hash) is str and C.HASH.fullmatch(expected_input_hash),'expected_input_hash_required')
        rows,hashed,size=M.prepare_chunk(table,rows,expected_input_hash)
        with self._tx() as c:
            counts,total=self._run(c,run);receipt=self._receipt(c,run,table,chunk_key)
            if receipt is not None:
                check(receipt[0]==hashed,'source_chunk_input_conflict');result=self._readback(c,run,table,receipt);result['replayed']=True
            else:
                check(counts[table]+len(rows)<=dict(run.expected_counts)[table] and total+size<=MAX_RUN_BYTES,'source_manifest_budget_exceeded')
                for row in rows:c.execute(*M.insert_statement(table,row,self.target.library_id))
                keys=[list(M.key(table,row)) for row in rows]
                c.execute('''INSERT INTO em_content.history_source_chunks(run_id,table_code,chunk_key,input_hash,row_keys,row_count,input_bytes)
                    VALUES(%s::uuid,%s,%s,%s,%s::jsonb,%s,%s)''',(run.run_id,table,chunk_key,hashed,canonical(keys).decode(),len(rows),size))
                counts=dict(counts);counts[table]+=len(rows)
                c.execute('UPDATE em_content.history_source_runs SET loaded_counts=%s::jsonb,loaded_bytes=%s WHERE run_id=%s::uuid',
                    (canonical(counts).decode(),total+size,run.run_id))
                result=self._readback(c,run,table,(hashed,keys,len(rows),size));result['replayed']=False
        return result  # only after actual COMMIT returns; unknown outcome propagates
