"""New native qualification checks only: synthetic SQL port, no PG/service."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from uuid import UUID

ROOT=Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT',str(Path(__file__).resolve().parents[2])))
OVERLAY=os.environ.get('EVOLVMEM_TEST_OVERLAY')
PATHS=([Path(OVERLAY)/'evolvmem'] if OVERLAY else [])+[ROOT/'evolvmem']
package=ModuleType('evolvmem');package.__path__=[str(p) for p in PATHS];sys.modules['evolvmem']=package
business=ModuleType('evolvmem.pg_business_core');business.__path__=[str(p/'pg_business_core') for p in PATHS]
sys.modules[business.__name__]=business

from evolvmem.library_foundation import FoundationError,RequestIdentity,LibraryDescriptor,AuthorizedRoute
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.context_models import ContextContentType
from evolvmem.pg_business_core.eligibility_read import PgEligibilityRead
from evolvmem.pg_business_core.native_qualification_sql import READ_CONTEXT,BATCH,SUMMARY_CONTENT
from evolvmem.source_qualification import rollup_content_signature,summary_current

uid=lambda n:str(UUID(int=n))
LIB=uid(1);PROJECT=project_uuid('project-a');NOW=datetime(2026,11,2,tzinfo=timezone.utc)
HASH=hashlib.sha256(b'fixed synthetic source').hexdigest()

def meta(item,**kwargs):
    return dict(library_id=LIB,project_id=PROJECT,item_id=item,revision=1,**kwargs)

def source(item,kind='migration',ref='synthetic',ident=None):
    ident=ident or item*10
    return dict(library_id=LIB,project_id=PROJECT,item_id=item,id=ident,archive_id=None,
        source_kind=kind,source_ref=ref,extraction_version='legacy-write.v1',created_at='2026-10-01 00:00:00',
        qualification=None if kind=='migration' else meta(item,source_id=ident,state='available',
            verification='verified',source_revision=1,content_hash=HASH,source_kind=kind,source_ref=ref,
            extraction_version='legacy-write.v1',archive_id=None))

def envelope(ident,kind='fact'):
    item=dict(id=ident,library_id=LIB,project_id=PROJECT,revision=1,project='project-a',projects=[],scope='project',
        content_type=kind,status='active',success_count=0,experience_payload=None,source_count=1,
        created_at='2026-10-01 00:00:00',updated_at='2026-10-01 00:00:00',expires_at=None,
        effective_from=None,effective_until=None)
    row=dict(item=item,item_l1_hash=HASH,learning=[],decision=[],resolution=[],registry=[meta(ident,project='project-a',project_uuid=PROJECT,status='active')],
        sources=[source(ident)],derivations=[],evidence=[],rollup=[])
    return row

def make(rows,*,ready=True,content=None):
    calls=[];stored=deepcopy(rows);content=deepcopy(content or {})
    class Connection:
        def execute(self,sql,params=()):
            calls.append((sql,params))
            if sql==READ_CONTEXT:return [(1,'native-v1',7,1,2)] if ready else []
            if sql==BATCH:return [(i,deepcopy(stored[i])) for i in sorted(params[2]) if i in stored]
            if sql==SUMMARY_CONTENT:return [content[i] for i in sorted(params[2]) if i in content]
            raise AssertionError('unexpected_SQL')
    identity=RequestIdentity(uid(2),uid(3),uid(4),1)
    lib=LibraryDescriptor(LIB,'device','postgres','target',7,1,'pg_active','ready','postgres',
        device_id=uid(5),owner_principal_id=uid(3),active=True)
    session=LibrarySession(AuthorizedRoute(identity,lib,'context_search','read'),Connection())
    scope=ProjectScope(LIB,PROJECT,'project-a',7,1,identity,'read')
    obj=PgEligibilityRead(session,scope,clock=lambda:NOW)
    return obj,calls,stored,session

def experience():
    payload=dict(project='project-a',problem='PostgreSQL shared lock recall latency',conditions={'database':'postgres'},
        steps=['Move expensive scans outside the shared lock'],rationale='Bound critical section',result='Low latency',
        applicability=['Concurrent recall'],exclusions=[],transferable=True,parent_experience_id=None)
    row=envelope(1,'experience');row['item'].update(success_count=1,experience_payload=payload)
    row['sources']=[source(1,'tool_result')]
    row['evidence']=[meta(1,evidence_id='ev-1',source_id=10,item_revision=1,item_content_hash=HASH,source_revision=1,
        source_hash=HASH,state='active',outcome='success',verification='verified',role='tool',
        task_id='task-1',event_id='event-1',event_order=1,conditions={'database':'postgres'},verification_level='technical')]
    return row


class NativeQualificationTests(unittest.TestCase):
    def test_all_context_types_use_canonical_verdict(self):
        rows={i:envelope(i,kind.value) for i,kind in enumerate(ContextContentType,1)}
        for i,r in rows.items():
            if r['item']['content_type']=='experience':
                # Unverified is refused despite a positive counter/payload.
                r['item'].update(success_count=5,experience_payload={'solution':'guess'})
        q,calls,_,_=make(rows)
        expected={i for i,r in rows.items() if r['item']['content_type']!='experience'}
        self.assertEqual(q.eligible_ids(tuple(rows)),expected)
        self.assertEqual(sum(sql==BATCH for sql,_ in calls),1)

    def test_source_exclusion_withdrawal_and_human_decisions(self):
        inferred=json.dumps(dict(basis='inferred',evidence=[{'role':'assistant'}]))
        for mode,expected in [('inferred',False),('human',True),('human_stale',False),('withdrawn',False),('excluded',False),('manual',True),('manual_changed',False)]:
            with self.subTest(mode=mode):
                r=envelope(1);r['learning']=[meta(1,category='project_convention',payload=inferred)]
                if mode in ('human','human_stale'):
                    r['decision']=[meta(1,decision='accepted',item_revision=1,item_content_hash=HASH,actor_principal_id=uid(3))]
                    if mode=='human_stale':r['item_l1_hash']='0'*64
                if mode=='withdrawn':r['decision']=[meta(1,decision='withdrawn',actor_principal_id=uid(3))]
                if mode in ('manual','excluded','manual_changed'):
                    r['sources']=[source(1,'manual')]
                    if mode=='excluded':r['sources'][0]['qualification']['state']='excluded'
                    if mode=='manual_changed':r['sources'][0]['source_ref']='rewritten'
                q,*_=make({1:r});self.assertEqual(q.eligible(1),expected)

    def test_experience_requires_current_bound_success_evidence(self):
        for mutation in ('none','source_hash','item_revision','source_revision','role','state','source_excluded','zero_success','body_hash','source_ref'):
            with self.subTest(mutation=mutation):
                r=experience()
                if mutation in ('source_hash','item_revision','source_revision','role','state'):
                    r['evidence'][0][mutation]={'source_hash':'0'*64,'item_revision':2,'source_revision':2,'role':'assistant','state':'withdrawn'}[mutation]
                if mutation=='source_excluded':r['sources'][0]['qualification']['state']='excluded'
                if mutation=='zero_success':r['item']['success_count']=0
                if mutation=='body_hash':r['item_l1_hash']='0'*64
                if mutation=='source_ref':r['sources'][0]['source_ref']='rewritten'
                q,*_=make({1:r});self.assertEqual(q.eligible(1),mutation=='none')

    def test_unit_withdrawal_and_basis_versions(self):
        base=envelope(1);base['sources']=[source(1,'organization',ref='unit:one')]
        d=meta(1,unit_task_id='task-1',unit_digest=HASH,source_id=10,source_key='unit:one',source_revision=1,
            status='active',decision='auto',disposition='keep',project='project-a',projects=[],knowledge_scope='project',
            conversation_archives=[],basis_source_id=None,basis_source_revision=None,basis_content_hash=None,basis_source=None,basis_descriptor=None)
        base['derivations']=[d]
        q,*_=make({1:base});self.assertTrue(q.eligible(1))
        withdrawn=deepcopy(base);withdrawn['derivations'][0]['disposition']='withdrawn'
        q,*_=make({1:withdrawn});self.assertFalse(q.eligible(1))
        withdrawn['sources'].append(source(1,ident=11));withdrawn['item']['source_count']=2
        q,*_=make({1:withdrawn});self.assertTrue(q.eligible(1))
        stale=deepcopy(base);stale['derivations'][0].update(basis_source_id=99,basis_source_revision=1,basis_content_hash=HASH)
        q,*_=make({1:stale})
        with self.assertRaisesRegex(FoundationError,'basis_reconciliation_required'):q.eligible(1)

    def test_summary_uses_current_sources_time_and_content_signature(self):
        a=envelope(1);summary=envelope(2,'project_summary');summary['sources']=[source(2,'context_reference','1')]
        text='synthetic L1';content={1:(1,'project-a','fact',text,hashlib.sha256(text.encode()).hexdigest())}
        signature=rollup_content_signature([(1,'project-a','fact',text)])
        summary['rollup']=[meta(2,source_set_hash=signature,status='active')]
        for change,expected in [('none',True),('expired',False),('content_changed',False),('future',False),('missing',False)]:
            with self.subTest(change=change):
                rows={1:deepcopy(a),2:deepcopy(summary)};bodies=deepcopy(content)
                if change=='expired':rows[1]['item']['expires_at']='2026-11-01 00:00:00'
                if change=='future':rows[1]['item']['effective_from']='2026-11-03 00:00:00'
                if change=='missing':del rows[1]
                if change=='content_changed':bodies[1]=(1,'project-a','fact','new',hashlib.sha256(b'new').hexdigest())
                q,calls,*_=make(rows,content=bodies);self.assertEqual(q.eligible(2),expected)
        # Pure canonical fallback for older ID signatures is unchanged.
        self.assertTrue(summary_current(summary['item'],(1,),lambda:[a['item']],now='2026-11-02 00:00:00',
            backed=lambda _:True,unconfirmed=lambda _:False,signature=lambda:'legacy-id-hash',content_signature=lambda:None))

    def test_cache_is_snapshot_local_and_fresh_proof_detects_changes(self):
        q,calls,stored,session=make({1:envelope(1)})
        self.assertEqual(q.eligible_ids((1,99)),{1});before=q.proof()
        count=len(calls);self.assertTrue(q.eligible(1));self.assertEqual(before,q.proof());self.assertEqual(len(calls),count)
        # Each independent final snapshot observes current dependencies, including
        # newly appearing IDs and decisions, but unrelated records are irrelevant.
        changed=deepcopy(stored);changed[99]=envelope(99)
        final,*_=make(changed);final.eligible_ids((1,99));self.assertNotEqual(before,final.proof())
        changed=deepcopy(stored);changed[1]['item']['revision']+=1
        final,*_=make(changed);final.eligible_ids((1,99));self.assertNotEqual(before,final.proof())
        unrelated=deepcopy(stored);unrelated[2]=envelope(2)
        final,*_=make(unrelated);final.eligible_ids((1,99));self.assertEqual(before,final.proof())
        original_connection=session._connection
        session._connection=object()
        with self.assertRaisesRegex(FoundationError,'qualification_snapshot_changed'):q.proof()
        session._connection=original_connection
        session._closed=True
        for method in (q.proof,lambda:q.eligible(1)):
            with self.assertRaisesRegex(FoundationError,'project_scope_session_required'):method()

    def test_readiness_scope_and_limits_fail_closed(self):
        with self.assertRaisesRegex(FoundationError,'recall_domain_not_ready'):make({},ready=False)
        for change in ('project','source_count','overflow'):
            with self.subTest(change=change):
                r=envelope(1)
                if change=='project':r['item']['project_id']=uid(999)
                if change=='source_count':r['item']['source_count']=2
                if change=='overflow':r['sources']*=65
                q,*_=make({1:r})
                with self.assertRaises(FoundationError):q.eligible(1)
        q,*_=make({})
        for ids in ((True,),tuple(range(1,242)),(0,)):
            with self.assertRaisesRegex(FoundationError,'invalid_qualification_ids'):q.eligible_ids(ids)

    def test_512_dependencies_but_only_240_input_candidates(self):
        q,calls,*_=make({i:envelope(i) for i in range(1,514)})
        self.assertEqual(len(q.eligible_ids(tuple(range(1,241)))),240)
        self.assertEqual(len(q.eligible_ids(tuple(range(241,481)))),240)
        self.assertEqual(len(q.eligible_ids(tuple(range(481,513)))),32)
        self.assertEqual(len(q.rows),512)
        with self.assertRaisesRegex(FoundationError,'recall_dependency_limit'):q.eligible(513)
        with self.assertRaisesRegex(FoundationError,'invalid_qualification_ids'):q.eligible_ids(tuple(range(1,242)))

    def test_case_contract_and_later_task_outcomes_revoke_old_success(self):
        for kind in ('experience','playbook'):
            for outcome in ('failure','contradicted','inapplicable'):
                with self.subTest(kind=kind,outcome=outcome):
                    r=experience();r['item']['content_type']=kind
                    later=deepcopy(r['evidence'][0]);later.update(evidence_id='ev-2',event_id='event-2',event_order=2,outcome=outcome)
                    r['evidence'].append(later)
                    q,*_=make({1:r});self.assertFalse(q.eligible(1));self.assertEqual(q.cases((1,)),())
        r=experience();q,*_=make({1:r});case=q.cases((1,))[0]
        self.assertEqual(case['verification'][0]['conditions'],{'database':'postgres'})
        self.assertEqual(case['verification'][0]['source']['digest'],HASH)
        self.assertEqual(case['success_count'],1)
        self.assertEqual(case['problem'],r['item']['experience_payload']['problem'])
        withdrawn=deepcopy(r['evidence'][0]);withdrawn.update(evidence_id='ev-2',event_order=2,revision=2,state='withdrawn')
        r['evidence'].append(withdrawn)
        q,*_=make({1:r});self.assertFalse(q.eligible(1))


if __name__=='__main__':unittest.main(verbosity=2)
