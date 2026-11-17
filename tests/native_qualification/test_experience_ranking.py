"""Small golden checks for the one scorer shared by SQLite and native PG."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
# Adjacent portable fixture loads only domain modules; it never runs a database.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_native_qualification import experience,make
from evolvmem.context_models import ContextMatchType
from evolvmem.experience_ranking import rank_cases,successful_verifications,task_verdicts


class ExperienceRankingTests(unittest.TestCase):
    def case(self):
        q,*_=make({1:experience()})
        return q.cases((1,))[0]

    def rank(self,cases,**options):
        args=dict(project='project-a',query='PostgreSQL lock',constraints={'database':'postgres'},
            semantic={},vector_weight=.5,reranker_min_score=.5,max_chars=6000)
        args.update(options)
        return rank_cases(cases,**args)

    def test_golden_score_preview_and_budget(self):
        case=self.case();result=self.rank([case])
        self.assertEqual(len(result['results']),1)
        preview=result['results'][0]
        self.assertEqual(preview['score'],.95)
        self.assertEqual(preview['verification'],[{'level':'technical','conditions':{'database':'postgres'},
            'source':{'id':10,'kind':'tool_result'}}])
        self.assertFalse(preview['adaptation_required'])
        self.assertNotIn('evidence',preview);self.assertNotIn('sources',preview)
        self.assertEqual(self.rank([case],max_chars=1),{'results':[],'used_chars':0})
        many=[dict(case,id=i) for i in range(1,6)]
        self.assertEqual([v['id'] for v in self.rank(many)['results']],[1,2,3])

    def test_conditions_transferability_inapplicable_and_signal_gates(self):
        case=self.case()
        self.assertEqual(self.rank([case],constraints={'database':'sqlite'})['results'],[])
        changed=deepcopy(case);changed['verification'][0]['conditions']={'database':'sqlite'}
        self.assertEqual(self.rank([changed])['results'],[])
        changed=deepcopy(case);changed['transferable']=False
        self.assertEqual(self.rank([changed],project='project-b')['results'],[])
        transferred=self.rank([case],project='project-b')['results'][0]
        self.assertTrue(transferred['adaptation_required']);self.assertEqual(transferred['score'],.85)
        changed=deepcopy(case);changed['evidence'].append(dict(outcome='inapplicable',conditions_json='{"database":"postgres"}'))
        self.assertEqual(self.rank([changed])['results'],[])
        self.assertEqual(self.rank([case],query='服务检查')['results'],[])
        hit=SimpleNamespace(match_types=(ContextMatchType.VECTOR,),score_components=SimpleNamespace(relevance=.6),rerank_score=None)
        self.assertEqual(self.rank([case],query='unrelated',semantic={1:hit})['results'][0]['score'],.73)
        hit.score_components.relevance=.1
        self.assertEqual(self.rank([case],query='unrelated',semantic={1:hit})['results'],[])

    def test_shared_latest_task_rule_excludes_later_negative_outcomes(self):
        case=self.case();first=case['evidence'][0]
        for outcome in ('failure','contradicted','inapplicable'):
            with self.subTest(outcome=outcome):
                later=dict(first,id=2,outcome=outcome)
                events=[first,later]
                self.assertEqual(task_verdicts(events),{'task-1':outcome})
                self.assertEqual(successful_verifications(events,case['sources']),[])


if __name__=='__main__':unittest.main(verbosity=2)
