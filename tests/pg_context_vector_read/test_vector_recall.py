import unittest
from tests.pg_context_vector_read.vector_fixture import VectorFixture
from tests.pg_context_read_slice.tests.driver_port import L
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_vector_port.model import VectorError, vector


class VectorRecallTests(unittest.TestCase):
    def setUp(self):
        self.f = VectorFixture(); self.addCleanup(self.f.close)

    def test_actual_candidate_sql_then_canonical_typed_results_under_one_pin(self):
        ident = self.f.seed('vector-reference','Synthetic reference without lexical matching.')
        self.f.mirror()
        published = []
        def publish(result):
            self.assertTrue(any(c.pinned and not c.closed for c in self.f.port.connections if c.db is None))
            self.assertTrue(all(c.closed for c in self.f.port.connections if c.db is not None))
            published.append(result)
        result = self.f.search(publish=publish)
        self.assertEqual((ident,),tuple(r.id for r in result))
        self.assertEqual([result],published)
        self.assertTrue(any('ORDER BY embedding <=>' in sql for _,sql,_ in self.f.port.calls))
        self.assertTrue(all(c.closed for c in self.f.port.connections))
        self.assertLessEqual(self.f.port.peak,2)

    def test_projection_generation_to_first_body_read_is_version_bound(self):
        ident = self.f.seed('stale-vector','Synthetic stale projection text.')
        self.f.projections[ident]['revision'] -= 1
        self.assertEqual((),self.f.search())

    def test_projection_body_changed_without_revision_cannot_enter_rank(self):
        ident = self.f.seed('stale-body','Synthetic stale projection text.')
        self.f.projections[ident]['hash'] = '0'*64
        self.assertEqual((),self.f.search())

    def test_changed_projection_with_same_document_revision_rejects_final(self):
        ident = self.f.seed('vector-change','Synthetic projection may not drift.')
        def drift():
            v = vector([.1,.9]+[0.]*766,self.f.contract)
            self.f.projections[ident].update(embedding=v,vector_hash=v.pg_digest)
        self.f.after_initial = drift
        published=[]
        with self.assertRaises((FoundationError,VectorError)):
            self.f.search(publish=published.append)
        self.assertEqual([],published)

    def test_current_projection_digest_must_match_actual_stored_vector(self):
        ident = self.f.seed('bad-proof','Synthetic invalid projection proof.')
        self.f.projections[ident]['vector_hash'] = '0'*64
        with self.assertRaisesRegex(VectorError,'recall_vector_changed'):
            self.f.search()

    def test_final_changed_vector_with_identical_distance_still_rejects(self):
        ident=self.f.seed('equal-distance','Digest protects the full vector, not just its score.')
        old=self.f.projections[ident]['embedding']
        def drift():
            values=list(old.values);values[1]=-values[1]
            changed=vector(values,self.f.contract)
            self.assertEqual(self.f.distance(old,self.f.query_vector.values),
                self.f.distance(changed,self.f.query_vector.values))
            self.assertNotEqual(old.pg_digest,changed.pg_digest)
            self.f.projections[ident].update(embedding=changed,vector_hash=changed.pg_digest)
        self.f.after_initial=drift
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.f.search()

    def test_initial_hit_is_bound_to_projection_before_materialization(self):
        ident=self.f.seed('generation-gap','The hit must match its generating projection.')
        def fault():
            changed=vector([.1,.9]+[0.]*766,self.f.contract)
            self.f.projections[ident].update(embedding=changed,vector_hash=changed.pg_digest)
        # Driver inconsistency injection, NOT a claim that real RR permits an
        # external transaction to mutate this request's fixed snapshot.
        self.f.after_candidates=fault
        with self.assertRaisesRegex(VectorError,'recall_vector_changed'):self.f.search()

    def test_final_control_permission_revocation_never_publishes(self):
        self.f.seed('revoked','Synthetic authorization cannot be cached.')
        self.f.after_initial = lambda:self.f.port.rows[L].update(permission='none')
        published=[]
        with self.assertRaises(FoundationError):
            self.f.search(publish=published.append)
        self.assertEqual([],published)


if __name__ == '__main__': unittest.main()
