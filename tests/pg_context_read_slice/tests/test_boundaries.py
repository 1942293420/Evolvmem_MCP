from dataclasses import replace
from pathlib import Path
import importlib.util
import sys
import unittest

from tests.pg_context_read_slice.tests.fixture import Fixture, ContextRetriever
from tests.pg_context_read_slice.tests.driver_port import L, L2
from evolvmem.context_retrieval_inputs import read_scope, current_inputs
from evolvmem.library_foundation import FoundationError


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.item=self.f.add('boundary-ref','Synthetic reference: test current source and output boundaries.')
        self.f.mirror();self.batch=self.f.candidates(self.f.request())
        self.assertTrue(self.batch.lexical)

    def test_original_full_search_and_optimized_sqlite_scope_match_exact_dto(self):
        path=Path(__file__).parents[1]/'baseline/context_retriever.py'
        spec=importlib.util.spec_from_file_location('original_slice_ranker',path)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        conn=self.f.oracle._connection();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        try:
            with read_scope(self.f.oracle):
                self.assertIsNotNone(current_inputs(self.f.oracle))
                old=module.ContextRetriever(self.f.config,self.f.oracle,None,None,clock=lambda:self.f.now)
                new=ContextRetriever(self.f.config,self.f.oracle,None,None,clock=lambda:self.f.now)
                old._scoped_vector_candidates=lambda *a,**kw:[]
                new._scoped_vector_candidates=lambda *a,**kw:[]
                expected=old.search(self.batch.request)
                self.assertTrue(expected)
                self.assertEqual(expected,new.search(self.batch.request))
        finally:conn.rollback();conn.execute('PRAGMA query_only=OFF')
        self.assertIsNone(current_inputs(self.f.oracle))
        self.assertEqual(expected,self.f.recall(self.batch))

    def test_search_reads_dynamic_reranker_state_once_like_original(self):
        class Engine:
            def __init__(self):self.reads=0;self.calls=0
            @property
            def reranker_loaded(self):self.reads+=1;return self.reads==1
            def rerank(self,query,texts):self.calls+=1;return [.95]*len(texts)
        engine=Engine()
        retriever=ContextRetriever(self.f.config,self.f.oracle,None,engine,clock=lambda:self.f.now)
        retriever._scoped_vector_candidates=lambda *a,**kw:[]
        result=retriever.search(self.batch.request)
        self.assertTrue(result)
        self.assertEqual(1,engine.reads)
        self.assertEqual(1,engine.calls)
        self.assertEqual(.95,result[0].rerank_score)

    def test_configuration_drift_between_snapshots_rejects(self):
        def changed():self.f.config.context_score_relevance_weight=.01
        with self.assertRaisesRegex(FoundationError,'recall_config_changed'):
            self.f.recall(self.batch,between=changed)

    def test_configured_reranking_is_explicitly_unsupported(self):
        self.f.config.context_reranker_model_dir='synthetic-not-a-real-model'
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_reranking'):
            self.f.recall(self.batch)

    def test_unsupported_domain_cannot_be_treated_as_empty(self):
        self.f.port.databases['content_0'][1].execute('CREATE TABLE learning_memories(item_id,payload)')
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_qualification_domain'):
            self.f.recall(self.batch)

    def test_library_binding_rejects_before_content_reads(self):
        before=len(self.f.port.calls)
        with self.assertRaisesRegex(FoundationError,'recall_library_mismatch'):
            self.f.recall(replace(self.batch,library_id=L2))
        self.assertFalse(any('FROM em_content.context_items' in s for _,s,_ in self.f.port.calls[before:]))

    def test_revocation_after_first_snapshot_blocks_final_and_publish(self):
        published=[]
        def revoke():self.f.port.rows[L]['permission']=None
        with self.assertRaises(FoundationError):
            self.f.recall(self.batch,between=revoke,publish=published.append)
        self.assertEqual([],published)
        self.assertTrue(all(c.closed for c in self.f.port.connections))

    def test_disabled_library_after_first_snapshot_is_not_served(self):
        def disable():self.f.port.rows[L]['library_active']=False
        with self.assertRaises(FoundationError):self.f.recall(self.batch,between=disable)

    def test_one_publish_has_live_control_pin_and_all_content_closed(self):
        seen=[]
        def publish(value):
            self.assertTrue(value)
            self.assertTrue(any(c.db is None and c.pinned and not c.closed for c in self.f.port.connections))
            self.assertTrue(all(c.closed for c in self.f.port.connections if c.db is not None))
            seen.append(value)
        result=self.f.recall(self.batch,publish=publish)
        self.assertEqual([result],seen)
        self.assertTrue(all(c.closed for c in self.f.port.connections))

    def test_publication_failure_never_retries(self):
        count=[]
        def broken(value):count.append(1);raise OSError('synthetic output failure')
        with self.assertRaises(OSError):self.f.recall(self.batch,publish=broken)
        self.assertEqual([1],count)
        self.assertTrue(all(c.closed for c in self.f.port.connections))

    def test_whole_family_nonmatching_current_winner_suppresses_old_hit(self):
        other=self.f.add('not-matching','A different phrase with no query token.')
        db=self.f.port.databases['content_0'][1]
        db.execute('UPDATE context_items SET identity_key=(SELECT identity_key FROM context_items WHERE id=?) WHERE id=?',
            (self.item['context_id'],other['context_id']))
        self.assertEqual((),self.f.recall(self.batch))

    def test_whole_family_cap_refuses_partial_proof(self):
        db=self.f.port.databases['content_0'][1]
        columns=[r[1] for r in db.execute('PRAGMA table_info(context_items)')]
        values=db.execute('SELECT * FROM context_items').fetchone()
        for i in range(1000,1513):
            row=list(values);row[columns.index('id')]=i
            db.execute('INSERT INTO context_items VALUES('+','.join('?' for _ in columns)+')',row)
        with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):self.f.recall(self.batch)

    def test_unrelated_commit_does_not_change_fixed_candidate_result(self):
        expected=self.f.recall(self.batch)
        def unrelated():
            self.f.port.databases['content_0'][1].execute("INSERT INTO vector_outbox(library_id,index_kind,entity_id,action,entity_revision,model_contract) VALUES(?,'context',999,'upsert',1,'synthetic-other')",(L,))
        self.assertEqual(expected,self.f.recall(self.batch,between=unrelated))

    def test_another_principals_private_receipt_is_not_origin_authority(self):
        self.f.port.databases['content_0'][1].execute("UPDATE lan_write_requests SET principal_id='00000000-0000-0000-0000-000000000099'")
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_origin'):self.f.recall(self.batch)

    def test_old_migration_source_is_not_supported_new_write_evidence(self):
        db=self.f.port.databases['content_0'][1]
        db.execute("INSERT INTO context_sources(library_id,item_id,source_kind,source_ref,extraction_version) VALUES(?,?,'migration','synthetic-old','legacy-v1')",(L,self.item['context_id']))
        db.execute('UPDATE context_items SET source_count=1')
        with self.assertRaisesRegex(FoundationError,'unsupported_context_evidence'):self.f.recall(self.batch)


if __name__=='__main__':unittest.main()
