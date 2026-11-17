"""Owner-only synthetic quarantine validation, NEVER an HTTP/product adapter.

The supplied factory is the existing trusted fixture factory. It must yield a
fresh bounded transaction executor each time; this module neither connects nor
sets roles, and it cannot create/activate a library. Actual SQL checks below are
mandatory even if the factory claims to be read-only. No publish callback.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from types import SimpleNamespace
from uuid import UUID

from evolvmem.context_models import ContextContentType
from evolvmem.context_store import ContextStore
from evolvmem.context_retrieval_inputs import _weight
from evolvmem.library_foundation import FoundationError
from .eligibility_sql import SQL
from .eligibility_read import _EligibilityQueries
from .recall import CandidateBatch, _ReadRetriever
from .recall_store import PgRecallStore, MAX_ROWS, MAX_BYTES, _proof

ORGANIZATION_TABLES = ('organization_tasks','organization_units','organization_conversations',
 'organization_context_basis','unit_derivations','learning_memories','knowledge_cleaning_reviews')
CORE_TABLES = ('memories','context_items','context_layers','legacy_memory_migrations',
 'context_project_registry','context_project_resolutions')
SOURCE_TABLES = ('session_archives','session_archive_storage','session_archive_holds',
 'conversation_history','archive_source_exclusions','context_sources','context_evidence')
TABLES = CORE_TABLES + SOURCE_TABLES + ORGANIZATION_TABLES

IDENTITY_SQL = '''SELECT current_database(),current_user::text,
 current_setting('transaction_read_only'),current_setting('transaction_isolation'),
 r.rolcanlogin,r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,
 d.datdba=current_user::regrole,
 (SELECT setting::bigint BETWEEN 1 AND 3000 FROM pg_settings WHERE name='statement_timeout'),
 (SELECT setting::bigint BETWEEN 1 AND 300 FROM pg_settings WHERE name='lock_timeout')
 FROM pg_roles r JOIN pg_database d ON d.datname=current_database() WHERE r.rolname=current_user'''
STATE_SQL = '''SELECT library_id::text,kind,project_id::text,authority_state,acl_state
 FROM em_security.library_state WHERE singleton'''
PARENTS_SQL = '''SELECT c.run_id::text,cp.manifest_hash,c.state,cp.stage,cp.generation,cp.verify_generation,
 s.run_id::text,sp.manifest_hash,s.core_run_id::text,sp.stage,sp.generation,sp.verify_generation
 FROM em_content.history_import_runs c JOIN em_content.history_batch_progress cp ON cp.run_id=c.run_id
 JOIN em_content.history_source_runs s ON s.core_run_id=c.run_id AND s.library_id=c.library_id
 JOIN em_content.history_source_batch_progress sp ON sp.run_id=s.run_id WHERE c.library_id=%s'''
SCHEMA_SQL = '''SELECT version,migration_name,library_id::text FROM em_content.history_organization_schema_migrations'''
POLICY_SQL = '''SELECT c.relname,c.relowner=current_user::regrole,c.relrowsecurity,c.relforcerowsecurity,
 EXISTS(SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid AND NOT p.polpermissive
 AND p.polcmd='*' AND p.polroles=ARRAY[0::oid]
 AND pg_get_expr(p.polqual,p.polrelid)=format('(CURRENT_USER = %L::name)',current_user)
 AND pg_get_expr(p.polwithcheck,p.polrelid)=format('(CURRENT_USER = %L::name)',current_user))
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='em_content' AND c.relname IN ('''+','.join('%s' for _ in TABLES)+') ORDER BY c.relname'
UNSUPPORTED_SQL = '''SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='em_content' AND c.relname IN ('knowledge_objects','knowledge_metadata','knowledge_qa','lan_publications')'''


@dataclass(frozen=True)
class FixtureBinding:
    database: str
    owner: str
    library_id: str
    core_run_id: str
    core_manifest_hash: str
    source_run_id: str
    source_manifest_hash: str

    def checked(self):
        if not re.fullmatch(r'em_pilot_[a-z0-9_]+',self.database): raise FoundationError('invalid_fixture_binding')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',self.owner): raise FoundationError('invalid_fixture_binding')
        try:
            for value in (self.library_id,self.core_run_id,self.source_run_id):
                if str(UUID(value))!=value:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise FoundationError('invalid_fixture_binding') from None
        if any(not re.fullmatch('[a-f0-9]{64}',v) for v in (self.core_manifest_hash,self.source_manifest_hash)):
            raise FoundationError('invalid_fixture_binding')
        return self


class _QuarantineRead:
    def __init__(self, connection, binding):
        self.connection,self.binding=connection,binding.checked()
        self.library_id=binding.library_id;self.active=True
        self.dependencies=[];self.remaining=MAX_BYTES;self.row_count=0
        self._queries={}
        self._reuse=False

    def _execute(self,sql,params=()):
        if not self.active:raise FoundationError('quarantine_scope_closed')
        try:
            result=self.connection.execute(sql,params)
            if isinstance(result,list):return result  # existing transaction executor
            # Existing guarded psycopg fixture connections return a Cursor.
            # Never fetch an unbounded stream or retain that cursor past this call.
            try:
                rows=[]
                if result.description is not None:
                    # GuardedCursor intentionally exposes only fetchone/fetchall.
                    # Stay on the actual guard for EVERY fetch, never unwrap raw.
                    for _ in range(MAX_ROWS+1):
                        row=result.fetchone()
                        if row is None:break
                        rows.append(row)
                return rows
            finally:result.close()
        except FoundationError:raise
        except Exception:raise FoundationError('quarantine_query_failed') from None

    def gate(self, *, for_write=False):
        self._queries.clear();self._reuse=False
        b=self.binding
        if self._execute(IDENTITY_SQL)!=[(b.database,b.owner,'off' if for_write else 'on','repeatable read',False,False,False,False,False,True,True,True)]:
            raise FoundationError('quarantine_owner_read_transaction_required')
        # Fixed ACCESS SHARE locks block table/policy DDL for the whole snapshot.
        self._execute('LOCK TABLE '+','.join('em_content.'+t for t in TABLES)+
            (' IN SHARE ROW EXCLUSIVE MODE' if for_write else ' IN ACCESS SHARE MODE'))
        if self._execute(STATE_SQL)!=[(b.library_id,'legacy',None,'pg_staging','updating')]:
            raise FoundationError('quarantine_library_required')
        policies=self._execute(POLICY_SQL,TABLES)
        if policies!=[(t,True,True,True,True) for t in sorted(TABLES)]:
            raise FoundationError('quarantine_policy_required')
        if self._execute(SCHEMA_SQL)!=[(1,'001_organization_qualification_fixture',b.library_id)]:
            raise FoundationError('organization_schema_required')
        if self._execute(UNSUPPORTED_SQL):raise FoundationError('unsupported_historical_domain')
        parents=self._execute(PARENTS_SQL,(b.library_id,))
        if (len(parents)!=1 or len(parents[0])!=12 or
            parents[0][:4]!=(b.core_run_id,b.core_manifest_hash,'core_verified','verified') or
            parents[0][4]!=parents[0][5] or parents[0][5] is None or
            parents[0][6:10]!=(b.source_run_id,b.source_manifest_hash,b.core_run_id,'verified') or
            parents[0][10]!=parents[0][11] or parents[0][11] is None):
            raise FoundationError('verified_history_parents_required')
        self._record('parents',(),parents)
        self._reuse=not for_write

    def _record(self,name,params,rows):
        self.row_count+=len(rows)
        weight=_weight((name,params,rows),self.remaining)
        if len(rows)>MAX_ROWS or self.row_count>4096 or weight is None:
            raise FoundationError('recall_dependency_limit')
        self.remaining-=weight;self.dependencies.append((name,params,deepcopy(rows)))

    def query(self,name,params):
        if not self.active:raise FoundationError('quarantine_scope_closed')
        if name not in SQL or not params or params[0]!=self.library_id:
            raise FoundationError('invalid_qualification_query')
        if not isinstance(params,tuple) or any(not isinstance(p,(str,int,type(None))) for p in params):
            raise FoundationError('invalid_qualification_query')
        key=(name,params)
        if self._reuse and key in self._queries:return deepcopy(self._queries[key])
        found=self._execute(SQL[name],params)
        if any(len(row)!=1 or not isinstance(row[0],dict) for row in found):
            raise FoundationError('unsupported_context_shape')
        rows=[row[0] for row in found]
        self._record(name,params,rows)
        if self._reuse:self._queries[key]=deepcopy(rows)
        return deepcopy(rows)

    def proof(self):
        if not self.active:raise FoundationError('quarantine_scope_closed')
        return _proof(self.dependencies)


@contextmanager
def _snapshot(connection, binding):
    scope=_QuarantineRead(connection,binding)
    try:
        scope.gate();yield scope
    finally:scope.active=False


class _HistoricalStore(PgRecallStore):
    """Reuse typed DTO/family/time methods; change origin and qualification reads."""
    def __init__(self,scope):
        self.scope=scope;self.lib=scope.library_id
        self.library=SimpleNamespace(library_id=self.lib,project_id=None,kind='legacy')
        self.rows,self.layers,self.sources,self.origins,self.families={},{},{},{},{}
        self.qualification=_EligibilityQueries(scope)

    def _load(self,ids):
        if not self.scope.active:raise FoundationError('quarantine_scope_closed')
        ids=tuple(dict.fromkeys(ids))
        if len(set(ids)|self.rows.keys())>MAX_ROWS:raise FoundationError('recall_dependency_limit')
        for ident in ids:
            if ident in self.rows:continue
            rows=self.scope.query('item',(self.lib,ident))
            if len(rows)>1:raise FoundationError('unsupported_context_shape')
            if not rows:continue
            row=rows[0]
            if (row['library_id']!=self.lib or row['project_id'] is not None or
                row['content_type'] not in ('reference','fact')):
                raise FoundationError('unsupported_historical_content_type')
            # Restore exact original inputs and prove typed/raw consistency.
            for typed,raw in (('tags','legacy_tags_text'),('projects','legacy_projects_text')):
                text=row[raw]
                if typed=='tags' and not text.strip():value=[]
                elif typed=='tags' and not text.lstrip().startswith(('[','{','"')):
                    value=[v.strip() for v in text.split(',') if v.strip()]
                else:value=json.loads(text)
                if value!=row[typed] or not isinstance(value,list):raise FoundationError('historical_shadow_changed')
                row[typed]=value
            raw=row['legacy_experience_payload_text']
            parsed=json.loads(raw) if raw else None
            if parsed!=row['experience_payload']:raise FoundationError('historical_shadow_changed')
            row['experience_payload']=raw
            layers=self.scope.query('layers',(self.lib,ident))
            if (tuple(r['layer'] for r in layers)!=('l0','l1','l2') or any(
                r['library_id']!=self.lib or r['item_id']!=ident or
                hashlib.sha256(r['content'].encode('utf-8')).hexdigest()!=r['content_hash'] for r in layers)):
                raise FoundationError('recall_content_hash_mismatch')
            sources=self.scope.query('sources',(self.lib,ident))
            if len(sources)!=row['source_count'] or any(s['library_id']!=self.lib or s['item_id']!=ident or s['project_id'] is not None for s in sources):
                raise FoundationError('historical_source_relation_changed')
            # Legacy namespace project strings are original applicability, not
            # device/project library authority. Do not manufacture project UUIDs.
            self.rows[ident]=row
            self.layers[ident]=tuple((r['layer'],r['content'],r['content_hash'],r['generator']) for r in layers)
            self.sources[ident]=sources

    def _item(self,row):
        if not self.scope.active:raise FoundationError('quarantine_scope_closed')
        # The typed JSON projection is checked above; DTO conversion uses the
        # original text, including legacy comma-tag whitespace and project fallback.
        original=dict(row)
        original['tags']=ContextStore._decode_tags(row['legacy_tags_text'])
        original['projects']=json.loads(row['legacy_projects_text']) or ([row['project']] if row['project'] else [])
        original['personal_general']=bool(row['personal_general'])
        return super()._item(original)

    def get_by_identity(self,identity_key,*,project,scope,include_layers=False):
        key=(identity_key,project,scope.value)
        if key not in self.families:
            ids=tuple(r['id'] for r in self.scope.query('family',(self.lib,*key)))
            self._load(ids);self.families[key]=ids
        return tuple(self._item(self.rows[i]) for i in self.families[key])

    def eligible(self,item_id):
        self._load((item_id,))
        return item_id in self.rows and self.qualification.eligible(item_id)

    def retrieval_fingerprints(self,ids):
        self._load(ids)
        # Same raw TEXT and temporal fields as the canonical SQLite fingerprint.
        return {i:(tuple((self.rows[i]['experience_payload'],*(self.rows[i][k] for k in
            ('effective_from','effective_until','occurred_at','expires_at')),l[0],l[2]) for l in self.layers[i]),None)
            for i in ids if i in self.rows}

    def proof(self):return self.scope.proof()


def validate_quarantined_candidates(factory,binding,batch,config,*,clock=None):
    """Two real fixture snapshots, same canonical ranker; result is NOT publishable.

    factory() must yield an execute(sql,params)->list[tuple] transaction executor
    from the existing bounded owner fixture. No product credential or HTTP input
    may select the factory. Complete import/activation are deliberately unclaimed.
    """
    binding.checked()
    if not isinstance(batch,CandidateBatch):raise FoundationError('invalid_recall_candidates')
    batch=batch.checked();original_config=deepcopy(config)
    if batch.library_id!=binding.library_id:raise FoundationError('recall_library_mismatch')
    if (not batch.request.content_types or any(t not in (ContextContentType.FACT,ContextContentType.REFERENCE)
            for t in batch.request.content_types) or config.context_reranker_model_dir):
        raise FoundationError('unsupported_recall_content_type')
    values=[]
    for _ in range(2):
        if config!=original_config:raise FoundationError('recall_config_changed')
        with factory() as connection:
            with _snapshot(connection,binding) as scope:
                store=_HistoricalStore(scope)
                try:
                    result=_ReadRetriever(original_config,store,None,None,clock=clock).rank_candidates(
                        batch.request,batch.lexical,batch.vector,allow_rerank=False)
                    values.append((result,store.proof()))
                except (KeyError,ValueError,TypeError,OverflowError):
                    raise FoundationError('unsupported_context_shape') from None
        # The factory must finish/close the actual transaction before final.
    if config!=original_config:raise FoundationError('recall_config_changed')
    if (values[0][1]!=values[1][1] or tuple((r.id,r.duplicate_ids) for r in values[0][0])
            !=tuple((r.id,r.duplicate_ids) for r in values[1][0])):
        raise FoundationError('recall_selected_changed')
    return values[0][0]
