from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from tests.pg_context_vector_read.vector_fixture import VectorFixture
from tests.pg_context_read_slice.tests.driver_port import L, TOKEN
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_business_core.recall import CandidateBatch
from evolvmem.pg_vector_port.model import VectorError
from evolvmem.pg_vector_port.runtime import VectorPort, BorrowedEmbeddingProvider


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.f = VectorFixture(); self.addCleanup(self.f.close)

    def test_complete_dto_matches_independent_original_sqlite_vector_ranking(self):
        a = self.f.seed('a','The first ordinary reference.',component=.9)
        b = self.f.seed('b','The second ordinary reference.',component=.3)
        self.f.mirror(); request = self.f.request('nonlexicalquery')
        vectors = tuple({'id':i,'distance':self.f.distance(self.f.projections[i]['embedding'],self.f.query_vector.values)} for i in (a,b))
        expected = self.f.original_result(CandidateBatch(L,request,(),vectors))
        self.assertTrue(expected)
        self.assertEqual(expected,self.f.search(request=request))

    def test_final_raw_l2_content_change_without_revision_rejects(self):
        ident = self.f.seed('body','The supported current body.')
        self.f.after_initial = lambda:self.f.port.databases['content_0'][1].execute(
            "UPDATE context_layers SET content='unproved mutation' WHERE item_id=? AND layer='l2'",(ident,))
        with self.assertRaises(FoundationError): self.f.search()

    def test_final_projection_removed_or_dimension_changed_rejects(self):
        ident = self.f.seed('projection','Projection state is current.')
        self.f.after_initial = lambda:self.f.projections[ident].update(state='removed')
        with self.assertRaisesRegex(VectorError,'recall_vector_changed'):self.f.search()

    def test_final_model_query_prefix_drift_rejects(self):
        self.f.seed('model','Model registration is current.')
        self.f.after_initial = lambda:setattr(self.f,'registered_model',replace(self.f.contract,query_prefix='different'))
        with self.assertRaisesRegex(VectorError,'registered_vector_model_required'):self.f.search()

    def test_actual_engine_config_change_during_encode_rejects_before_ann(self):
        self.f.seed('engine','Loaded engine must attest itself.')
        other = deepcopy(self.f.config); other.embedding_model_filename = 'another-model.gguf'
        def encode(text):
            self.f.engine.config=other
            return self.f.query_vector.values
        self.f.engine.encode_query=encode
        with self.assertRaisesRegex(VectorError,'embedding_contract_changed'):self.f.search()
        self.assertFalse(any('ORDER BY embedding <=>' in sql for _,sql,_ in self.f.port.calls))

    def test_source_domain_relation_even_empty_remains_unsupported(self):
        self.f.seed('domain','Unsupported history cannot silently become independent.')
        self.f.port.databases['content_0'][1].execute('CREATE TABLE unit_derivations(id)')
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_qualification_domain'):self.f.search()

    def test_receipt_invisible_or_absent_remains_unsupported(self):
        self.f.seed('receipt','A vector is not current-source eligibility.')
        self.f.port.databases['content_0'][1].execute('DELETE FROM lan_write_requests')
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_origin'):self.f.search()

    def test_selected_effective_until_crosses_without_commit_rejects(self):
        ident = self.f.seed('time','Temporal predicates are rerun.')
        stamp = (self.f.now+timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        self.f.port.databases['content_0'][1].execute('UPDATE context_items SET effective_until=? WHERE id=?',(stamp,ident))
        self.f.after_initial = lambda:setattr(self.f,'now',self.f.now+timedelta(seconds=2))
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.f.search()

    def test_historical_request_explicitly_rejected_not_silently_active_only(self):
        self.f.seed('historical','Only current active projections are implemented.')
        request=replace(self.f.request(),as_of='2029-01-01 00:00:00')
        with self.assertRaisesRegex(FoundationError,'unsupported_vector_history'):
            self.f.search(request=request)

    def test_final_nonempty_supported_source_mutation_rejects(self):
        ident=self.f.seed('source','Supported source bytes are independently checked.')
        db=self.f.port.databases['content_0'][1]
        db.execute("INSERT INTO context_sources(library_id,item_id,source_kind,source_ref,extraction_version) VALUES(?,?,'migration','synthetic-source','legacy-write.v1')",(L,ident))
        db.execute('UPDATE context_items SET source_count=1 WHERE id=?',(ident,))
        self.assertTrue(self.f.search())
        self.f.after_initial=lambda:db.execute("UPDATE context_sources SET source_ref='changed-source'")
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.f.search()

    def test_same_absolute_guard_allows_activation_then_two_sequential_reads(self):
        from evolvmem.pg_business_core.vector_recall import PgVectorContextRecall
        ident=self.f.seed('sequential','Sequential leases keep the request deadline.')
        with self.f.host.request_scope():
            identity=self.f.host.authenticate(TOKEN);state=self.f.host._state()
            route=state.router.plan(identity,'context_read',library_id=L)
            port=VectorPort(state.manager,(route,),self.f.contract,embedding_provider=BorrowedEmbeddingProvider(self.f.engine))
            guard_state=port.guard._state();deadline=guard_state.deadline
            read=PgVectorContextRecall(port,self.f.config,clock=lambda:self.f.now)
            first=read.search(L,self.f.request(),candidate_limit=20)
            second=read.search(L,self.f.request(),candidate_limit=20)
            self.assertEqual((ident,),tuple(r.id for r in first))
            self.assertEqual(first,second)
            self.assertIs(port.guard._state(),guard_state)
            self.assertEqual(deadline,guard_state.deadline)

    def test_native_query_error_cancels_and_closes_without_publish(self):
        self.f.seed('sql-error','SQL failures cannot become empty success.')
        def fail(raw,sql,params):
            if 'ORDER BY embedding <=>' in sql:raise OSError('synthetic private SQL error')
        self.f.port.on_sql=fail;published=[]
        with self.assertRaises(FoundationError) as caught:self.f.search(publish=published.append)
        self.assertNotIn('private',str(caught.exception))
        self.assertEqual([],published)
        self.assertTrue(all(c.closed for c in self.f.port.connections))


if __name__=='__main__':unittest.main()
