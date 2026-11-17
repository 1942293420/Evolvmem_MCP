"""Small new candidate-only checks. They do not connect to PostgreSQL."""
import hashlib
import json
from pathlib import Path
import sys
import types
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
# Load real foundation/catalog values without EvolvMem's heavy application init.
package = types.ModuleType('evolvmem')
package.__path__ = [str(ROOT/'evolvmem')]
sys.modules['evolvmem'] = package
from evolvmem.library_foundation import ConnectionBinding, FoundationError, RequestIdentity
from evolvmem.library_catalog import AclIntent, AclOperation, ControlBinding, VerifiedProjection
from evolvmem.library_project_acl import ProjectAclIntent, ProjectAclCoordinator, project_uuid
from evolvmem.library_project_acl.worker import validate_plan, eligible, verify_project_readback, ProjectionTarget, registry_for

def uid(n): return str(UUID(int=n))

class ProjectAclChecks(unittest.TestCase):
    def setUp(self):
        self.identity = RequestIdentity(uid(1),uid(2),uid(3),1)
        self.intent = ProjectAclIntent(self.identity,uid(4),uid(5),'read',7,uid(6),'grant-1',project_uuid('ai-procurement'),'ai-procurement')
        self.operation = AclOperation(uid(6),uid(4),8,'pending',1,'device_one')
        self.plan = dict(version=2,operation_id=uid(6),library_id=uid(4),connection_ref='device_one',expected_policy_epoch=7,
            target_policy_epoch=8,route_epoch=1,kind='device',device_id=uid(3),project_id=None,owner_principal_id=uid(1),
            payload_hash=self.intent.payload_hash.hex(),evaluation_at='2026-11-01T00:00:00.000000Z',
            acls=[dict(principal_id=uid(5),permission='read',active=True,principal_active=True,principal_epoch=1)],
            credentials=[dict(principal_id=uid(5),credential_id=uid(7),device_id=uid(8),credential_epoch=1,principal_epoch=1,
                device_epoch=1,credential_active=True,principal_active=True,device_active=True,expires_at=None)],
            projects=[dict(project_id=self.intent.project_id,project_key='ai-procurement',active=True)],
            project_acls=[dict(project_id=self.intent.project_id,principal_id=uid(5),permission='read',active=True)])

    def validate(self):
        text=json.dumps(self.plan)
        return validate_plan(text,hashlib.sha256(text.encode()).hexdigest(),self.intent,self.operation)

    def test_explicit_project_and_foreign_device_are_valid(self):
        plan=self.validate()
        self.assertTrue(eligible(plan,plan['credentials'][0]))

    def test_stable_project_id_and_key_validation(self):
        self.assertEqual(project_uuid('ai-procurement'),self.intent.project_id)
        self.assertNotEqual(project_uuid('ai-procurement'),project_uuid('other'))
        with self.assertRaises(FoundationError): project_uuid(' ai-procurement ')

    def test_project_payload_binds_project_and_operation_kind(self):
        library=AclIntent(self.identity,uid(4),uid(5),'read',7,uid(6),'grant-1')
        other=ProjectAclIntent(self.identity,uid(4),uid(5),'read',7,uid(6),'grant-1',project_uuid('other'),'other')
        self.assertNotEqual(library.payload_hash,self.intent.payload_hash)
        self.assertNotEqual(other.payload_hash,self.intent.payload_hash)

    def test_project_uuid_cannot_rebind_a_key(self):
        with self.assertRaises(FoundationError):
            ProjectAclIntent(self.identity,uid(4),uid(5),'read',7,uid(6),'grant-1',uid(10),'ai-procurement')

    def test_missing_project_registration_fails(self):
        self.plan['projects']=[]
        with self.assertRaises(FoundationError): self.validate()

    def test_project_grant_without_library_ceiling_fails(self):
        self.plan['project_acls'][0]['principal_id']=uid(40)
        with self.assertRaises(FoundationError): self.validate()

    def test_duplicate_project_grants_fail(self):
        self.plan['project_acls']*=2
        with self.assertRaises(FoundationError): self.validate()

    def test_null_project_grant_fails(self):
        self.plan['project_acls'][0]['project_id']=None
        with self.assertRaises(FoundationError): self.validate()

    def test_inactive_credential_never_enabled(self):
        self.plan['credentials'][0]['device_active']=False
        self.assertFalse(eligible(self.validate(),self.plan['credentials'][0]))

    def test_all_project_projection_epochs_verified_on_library_changes(self):
        snapshot=dict(projects=[dict(self.plan['projects'][0],projection_epoch=8)],
                      project_acls=[dict(self.plan['project_acls'][0],projection_epoch=8)])
        verify_project_readback(snapshot,self.plan)
        snapshot['project_acls'][0]['projection_epoch']=7
        with self.assertRaises(FoundationError): verify_project_readback(snapshot,self.plan)

    def test_project_api_keeps_stable_request_and_hash(self):
        calls=[]
        coordinator=ProjectAclCoordinator(None)
        coordinator._mutate=lambda sql,params,**kwargs: calls.append((sql,params,kwargs))
        coordinator.begin(self.intent)
        self.assertIn('begin_project_acl_change',calls[0][0])
        self.assertEqual(calls[0][1][-3:],(self.intent.payload_hash,self.intent.project_id,self.intent.project_key))
        self.assertEqual(calls[0][1][-4],self.intent.request_id)

    def test_project_operation_uses_confirm_then_finish_after_verified_readback(self):
        events=[]
        coordinator=ProjectAclCoordinator(None)
        coordinator.begin=lambda intent: self.operation
        coordinator.confirm=lambda intent,receipt: events.append('confirmed')
        coordinator.finish=lambda intent: events.append('finished')
        receipt=VerifiedProjection(uid(6),uid(4),8,1,'device_one',bytes(32))
        coordinator.apply(self.intent,lambda intent,operation: receipt)
        self.assertEqual(events,['confirmed','finished'])

    def test_unknown_projection_commit_fences_instead_of_reopening(self):
        events=[]
        coordinator=ProjectAclCoordinator(None)
        coordinator.begin=lambda intent: self.operation
        coordinator.fail=lambda intent,code: events.append(('failed',code))
        coordinator.confirm=lambda *args: events.append('unsafe_confirm')
        coordinator.finish=lambda *args: events.append('unsafe_finish')
        def lost_reply(*args): raise FoundationError('content_projection_commit_result_unknown')
        with self.assertRaises(FoundationError): coordinator.apply(self.intent,lost_reply)
        self.assertEqual(events,[('failed','content_projection_failed')])

    def test_registered_project_mutation_with_no_project_intent_cannot_be_forged(self):
        self.plan['project_acls'][0]['active']=False
        with self.assertRaises(FoundationError): self.validate()

    def test_malformed_project_key_is_a_stable_denial(self):
        self.plan['projects'][0]['project_key']=[]
        with self.assertRaises(FoundationError): self.validate()

    def test_cross_device_binding_enabled_but_forged_identity_rejected(self):
        target=ProjectionTarget(uid(4),'device_one',ControlBinding('localhost',5432,'test_db','projector','test_secret'),
            (ConnectionBinding('device_one',uid(4),uid(5),uid(7),uid(8),'localhost',5432,'test_db','reader','test_secret'),))
        snapshot=dict(database='test_db',session_role='projector',
            library=dict(library_id=uid(4),kind='device',device_id=uid(3),project_id=None,policy_epoch=7,route_epoch=1,
                authority_state='pg_active',acl_state='updating',owner_principal_id=uid(1)),
            acls=[],projects=[],project_acls=[],applied=None,
            bindings=[dict(login_role='reader',principal_id=uid(5),credential_id=uid(7),device_id=uid(8),
                credential_epoch=1,expires_at=None,enabled=False,role_safe=True)])
        self.assertTrue(registry_for(snapshot,target,self.validate())[0]['enabled'])
        snapshot['bindings'][0]['device_id']=uid(3)
        with self.assertRaises(FoundationError): registry_for(snapshot,target,self.validate())

if __name__=='__main__': unittest.main()
