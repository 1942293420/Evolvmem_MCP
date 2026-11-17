"""The only replacement is the explicitly labelled fixed-query SQL port."""
import unittest
from evolvmem.pg_business_core.eligibility_read import PgEligibilityRead, _EligibilityQueries
from evolvmem.library_foundation import FoundationError


class Port:
    def __init__(self):
        self.library_id='00000000-0000-0000-0000-000000000001'
        self.active=True;self.calls=[]
        self.rows={
          'backing': [dict(item_id=1,task_exists=2,item_exists=1,source_key='conversation:x',
             status='completed',decision='auto',disposition='keep',project='p',projects='["p"]',
             knowledge_scope='project',item_project='p',item_projects='["p"]',item_scope='project')],
          'basis': [], 'conversation':[{'members':'["archive:9"]'}],
          'sources':[{'source_kind':'session','archive_id':9,'source_ref':'archive:9'}],
          'eligibility':[dict(content_type='fact',status='active',success_count=0,
             experience_payload='{}',category='habit',learning_payload='{"basis":"inferred","evidence":[{"role":"assistant"}]}')],
          'human':[], 'manual':[]}
    def query(self,name,params):
        if not self.active:raise FoundationError('quarantine_scope_closed')
        self.calls.append((name,params));return self.rows[name]


class PgRead(unittest.TestCase):
    def setUp(self):self.p=Port();self.q=_EligibilityQueries(self.p)
    def test_current_unit_then_human_confirmation(self):
        self.assertFalse(self.q.eligible(1))
        self.p.rows['human']=[{'present':1}]
        self.assertTrue(self.q.eligible(1))
        self.assertNotIn('conversation',[n for n,_ in self.p.calls])
    def test_withdrawal_same_archive_and_independent_source(self):
        self.p.rows['backing'][0]['decision']='review'
        self.assertFalse(self.q.currently_backed(1))
        self.p.rows['sources'].append({'source_kind':'manual','archive_id':None,'source_ref':'manual'})
        self.assertTrue(self.q.currently_backed(1))
    def test_orphan_does_not_become_no_derivations_true(self):
        self.p.rows['backing'][0]['task_exists']=None
        with self.assertRaisesRegex(FoundationError,'historical_orphan_derivation'):self.q.eligible(1)
    def test_basis_stale_requires_reconciliation_even_when_auto(self):
        self.p.rows['basis']=[{'stale':True}]
        with self.assertRaisesRegex(FoundationError,'basis_reconciliation_required'):self.q.eligible(1)
    def test_production_cannot_borrow_quarantine_port(self):
        with self.assertRaisesRegex(FoundationError,'read_session_required'):PgEligibilityRead(self.p)
    def test_raw_text_malformed_behavior_and_explicit_unsupported(self):
        self.p.rows['eligibility'][0]['learning_payload']='not json'
        self.assertTrue(self.q.eligible(1))
        self.p.rows['eligibility'][0]['content_type']='experience'
        with self.assertRaisesRegex(FoundationError,'unsupported_historical_content_type'):self.q.eligible(1)
    def test_closed_scope_and_no_cross_snapshot_verdict_cache(self):
        self.p.rows['eligibility'][0]['learning_payload']=''
        self.assertTrue(self.q.eligible(1));self.p.active=False
        with self.assertRaisesRegex(FoundationError,'scope_closed'):self.q.eligible(1)
