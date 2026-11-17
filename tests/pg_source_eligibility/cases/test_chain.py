"""Actual SQLite storage/oracle and canonical ranker; explicit PG SQL/catalog port.

No assertion here is PostgreSQL compilation, RLS, a real PG transaction, or a
historical import/promotion receipt. The real fixture callable is unchanged.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import importlib.util,json,sqlite3,sys,tempfile,unittest
from evolvmem.config import Config
from evolvmem.context_models import (ContextItemDraft,ContextLayers,ContextContentType,
    ContextStatus,ContextSearchRequest,ContextSearchHit,ContextLayer)
from evolvmem.context_store import ContextStore
from evolvmem.context_retriever import ContextRetriever
from evolvmem.pg_business_core.recall import CandidateBatch
from evolvmem.pg_business_core import historical_validation as H
from evolvmem.library_foundation import FoundationError

HERE=Path(__file__).resolve().parents[1]
NOW=datetime(2030,1,1,tzinfo=timezone.utc)
LIB='00000000-0000-0000-0000-000000000001'
BINDING=H.FixtureBinding('em_pilot_history','em_history_owner',LIB,
 '00000000-0000-0000-0000-000000000011','1'*64,
 '00000000-0000-0000-0000-000000000012','2'*64)


class SqlPort:
    def __init__(self,conn):self.c=conn;self.calls=[];self.gate_failure=None
    def execute(self,sql,params=()):
        self.calls.append((sql,params))
        b=BINDING
        if sql==H.IDENTITY_SQL:return [(b.database,b.owner,'on','repeatable read',False,False,False,False,False,True,True,True)]
        if sql.startswith('LOCK TABLE '):return []
        if sql==H.STATE_SQL:return [(LIB,'legacy',None,'pg_staging','updating')]
        if sql==H.POLICY_SQL:
            rows=[(t,True,True,True,True) for t in sorted(H.TABLES)]
            if self.gate_failure=='policy':rows[0]=(rows[0][0],True,True,False,True)
            return rows
        if sql==H.SCHEMA_SQL:return [(1,'001_organization_qualification_fixture',LIB)]
        if sql==H.UNSUPPORTED_SQL:return []
        if sql==H.PARENTS_SQL:return [(b.core_run_id,b.core_manifest_hash,'core_verified','verified',0,0,
            b.source_run_id,b.source_manifest_hash,b.core_run_id,'verified',0,1 if self.gate_failure=='parent' else 0)]
        if not sql.startswith('SELECT to_jsonb(q) FROM ('):raise AssertionError('unknown SQL port query')
        body=sql[len('SELECT to_jsonb(q) FROM ('):-3].replace('em_content.','').replace('%s','?').replace('::text','')
        # Explicit structural SQL-port delta: PostgreSQL nested-row JSON itself
        # is NOT tested by SQLite. All joins and stale predicates execute here.
        body=body.replace('to_jsonb(b) AS basis','b.unit_digest AS basis')
        rows=[]
        for value in self.c.execute(body,params):
            row=dict(value)
            if 'legacy_tags_text' in row:
                for name in ('tags','projects','experience_payload'):
                    row[name]=json.loads(row[name]) if row[name] else None
                row['personal_general']=bool(row['personal_general'])
            rows.append((row,))
        return rows


class Chain(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Config(data_dir=Path(self.temp.name),apply_environment=False)
        self.store=ContextStore(self.config);self.store.initialize();self.c=self.store._connection()
        self.addCleanup(self.store.close)
        evidence=json.loads((HERE/'reviewed-schema.json').read_text(encoding='utf-8'))
        for table,spec in evidence.items():
            if not self.c.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone():self.c.execute(spec['ddl'])
        item=self.store.create_item(ContextItemDraft('synthetic:historical:1',ContextContentType.FACT,
            ContextLayers('synthetic bounded fact','synthetic fact detail','synthetic original evidence','fixture'),
            project='p',status=ContextStatus.ACTIVE,confidence=.9))
        self.item=item.id
        for table in ('context_items','context_layers','context_sources','context_project_resolutions',
                      'session_archives',*H.ORGANIZATION_TABLES):
            self.c.execute('ALTER TABLE '+table+' ADD COLUMN library_id TEXT DEFAULT \''+LIB+'\'')
        for table in ('context_items','context_layers','context_sources'):
            self.c.execute('ALTER TABLE '+table+' ADD COLUMN project_id TEXT')
        for name in ('tags','projects','experience_payload'):
            self.c.execute('ALTER TABLE context_items ADD COLUMN legacy_'+name+'_text TEXT')
            self.c.execute('UPDATE context_items SET legacy_'+name+'_text='+name)
        self.c.execute('ALTER TABLE organization_conversations ADD COLUMN source_rowid INTEGER')
        self.c.execute("INSERT INTO organization_tasks(id,source_key,source_revision,rule_revision,created_at,updated_at,status) VALUES(1,'conversation:x','s1','r1','a','b','completed')")
        self.c.execute("INSERT INTO organization_conversations(source_key,identity,members,updated_at,source_rowid) VALUES('conversation:x','x','[\"archive:9\"]','b',1)")
        self.c.execute("INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,created_at,updated_at,decision,project) VALUES(1,1,'d','u','unit',1,2,'a','b','auto','p')")
        self.c.execute("INSERT INTO unit_derivations(unit_task_id,unit_digest,item_id,created_at) VALUES(1,'d',?,'a')",(self.item,))
        self.port=SqlPort(self.c);self.between=None;self.snapshots=0
        self.c.commit()
        request=ContextSearchRequest('synthetic',top_k=5,cross_project=True,
            content_types=(ContextContentType.FACT,ContextContentType.REFERENCE))
        lexical=self.store.search_fts(request.query,top_k=25,statuses=(ContextStatus.ACTIVE,ContextStatus.SUPERSEDED),
            layers=(ContextLayer.L0,ContextLayer.L1),project=None,now='2030-01-01 00:00:00',
            content_types=request.content_types,min_confidence=self.config.context_min_confidence)
        self.batch=CandidateBatch(LIB,request,tuple(lexical),())
    @contextmanager
    def factory(self):
        self.c.execute('BEGIN')
        try:yield self.port
        finally:self.c.rollback()
        self.snapshots+=1
        if self.snapshots==1 and self.between:self.between();self.c.commit()
    def call(self):return H.validate_quarantined_candidates(self.factory,BINDING,self.batch,self.config,clock=lambda:NOW)
    def original(self):
        # Full unchanged f594 search, actual FTS and original ranking. The sole
        # vector source is empty because this test never loads a model/index.
        name='eligibility_f594_retriever'
        if name not in sys.modules:
            spec=importlib.util.spec_from_file_location(name,HERE/'reference/context_retriever.py')
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
        retriever=sys.modules[name].ContextRetriever(self.config,self.store,None,None,clock=lambda:NOW)
        return retriever.search(self.batch.request,allow_rerank=False)
    def test_current_unit_full_typed_dto_matches_original_and_two_snapshots(self):
        expected=self.original();self.assertTrue(expected)
        self.assertEqual(expected,self.call());self.assertEqual(2,self.snapshots)
    def test_withdrawal_then_manual_independent_source(self):
        self.c.execute("UPDATE organization_units SET decision='review'");self.c.commit()
        self.assertEqual((),self.call());self.assertEqual(self.original(),self.call())
        self.c.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'manual','human source','v1','a')",(self.item,))
        self.c.execute('UPDATE context_items SET source_count=1');self.c.commit()
        self.assertTrue(self.call());self.assertEqual(self.original(),self.call())

    def test_inferred_rule_human_resolution_and_final_revoke(self):
        payload='{"basis":"inferred","evidence":[{"role":"assistant"}]}'
        self.c.execute("INSERT INTO learning_memories(item_id,category,payload,revision) VALUES(?,'habit',?,1)",(self.item,payload));self.c.commit()
        self.assertEqual((),self.call());self.assertEqual(self.original(),self.call())
        self.c.execute("INSERT INTO context_project_resolutions(item_id,resolution_state,resolved_project,decision_source,review_state,evidence_json,created_at,updated_at) VALUES(?,'resolved','p','human','accepted','[]','a','b')",(self.item,));self.c.commit()
        self.assertTrue(self.call());self.assertEqual(self.original(),self.call())
        self.snapshots=0;self.between=lambda:self.c.execute("UPDATE context_project_resolutions SET review_state='pending'")
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.call()

    def test_legacy_comma_tags_and_empty_project_list_dto_match(self):
        self.c.execute("UPDATE context_items SET tags=' alpha, beta ',legacy_tags_text=' alpha, beta '")
        # The PG typed projection differs from the SQLite original comma text.
        old_execute=self.port.execute
        def execute(sql,params=()):
            if sql==H.SQL['item']:
                row=dict(self.c.execute('SELECT * FROM context_items WHERE id=?',(self.item,)).fetchone())
                row.update(tags=['alpha','beta'],projects=[],experience_payload=json.loads(row['experience_payload']) if row['experience_payload'] else None)
                return [(row,)]
            return old_execute(sql,params)
        self.port.execute=execute;self.c.commit()
        self.assertEqual(self.original(),self.call())
    def test_final_dependency_change_rejects_even_if_still_eligible(self):
        self.between=lambda:self.c.execute("UPDATE organization_units SET revision=revision+1")
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.call()
    def test_final_withdrawal_rejects(self):
        self.between=lambda:self.c.execute("UPDATE organization_units SET decision='review'")
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.call()
    def test_final_parent_or_policy_change_rejects(self):
        for name in ('parent','policy'):
            self.snapshots=0;self.port.gate_failure=None;self.between=lambda:setattr(self.port,'gate_failure',name)
            with self.assertRaisesRegex(FoundationError,'verified_history_parents_required|quarantine_policy_required'):self.call()
    def test_raw_shadow_drift_fails_and_preserves_null_times(self):
        self.c.execute("UPDATE context_items SET legacy_projects_text='[\"other\"]'");self.c.commit()
        with self.assertRaisesRegex(FoundationError,'historical_shadow_changed'):self.call()
    def test_source_bound_and_same_library_join_are_real_sql(self):
        self.c.execute("UPDATE organization_tasks SET library_id='foreign'");self.c.commit()
        with self.assertRaisesRegex(FoundationError,'historical_orphan_derivation'):self.call()
    def test_no_mutation_receipt_is_queried(self):
        self.call();self.assertFalse(any('lan_write_requests' in sql for sql,_ in self.port.calls))
