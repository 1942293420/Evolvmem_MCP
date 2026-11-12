"""Pure driver-port tests, NOT PostgreSQL/role/RLS or live projector evidence."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

from evolvmem.library_foundation import ConnectionBinding, RequestIdentity, FoundationError
from evolvmem.library_catalog import AclIntent, AclOperation, ControlBinding

ID = lambda n: '00000000-0000-0000-0000-' + str(n).zfill(12)
A, B, C, DA, DB, DC, CA, CB, CC, L, PROJECT, OP = map(ID, range(1,13))


class ControlPort:
    def __init__(self, text):
        self.text, self.held, self.commits, self.calls = text, False, 0, 0
        self.commit_unknown = False
    @contextmanager
    def open(self, *, read_only):
        assert read_only is False
        self.held = True
        try: yield self
        finally: self.held = False
    def execute(self, sql, params):
        assert 'get_acl_projection_plan' in sql and params == (OP,)
        self.calls += 1
        return ({'plan_text':self.text, 'plan_hash_hex':hashlib.sha256(self.text.encode()).hexdigest()},)
    def commit(self):
        self.commits += 1
        if self.commit_unknown: raise FoundationError('control_commit_result_unknown')


class DriverError(RuntimeError):
    sqlstate='08006'


class Engine:
    def __init__(self, state, control):
        self.state, self.control = deepcopy(state), control
        self.connections, self.applies = [], 0
        self.commit_unknown = False
        self.corrupt_readback = None
        self.bad_service_role = False
        self.bad_connection = None
    def connect(self, **kwargs):
        assert self.control.held
        assert kwargs['sslmode']=='verify-full'
        assert not self.connections or self.connections[-1].closed
        raw=Raw(self,kwargs)
        if self.bad_connection=='intrans': raw.info.transaction_status.name='INTRANS'
        if self.bad_connection=='autocommit': raw.autocommit=False
        if self.bad_connection=='closed': raw.closed=True
        self.connections.append(raw); return raw


class Raw:
    def __init__(self, engine, kwargs):
        self.engine, self.kwargs = engine, kwargs
        self.closed=False; self.autocommit=True; self.pending=None
        self.executed=[]; self.commits=0
        self.info=SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE'))
    def cursor(self): return Cursor(self)
    def rollback(self): self.pending=None; self.info.transaction_status.name='IDLE'
    def close(self): self.closed=True
    def commit(self):
        self.commits+=1
        if self.pending is not None: self.engine.state=self.pending
        self.pending=None; self.info.transaction_status.name='IDLE'
        if self.engine.commit_unknown: raise DriverError('private synthetic diagnostic')


class Cursor:
    def __init__(self, raw): self.raw=raw; self.description=None; self.rows=[]
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,params=()):
        e=self.raw.engine
        self.raw.executed.append(sql)
        if sql.startswith('BEGIN'):
            self.raw.info.transaction_status.name='INTRANS'; return
        if 'set_config' in sql: output=[{'configured':True}]
        elif 'pg_catalog.pg_roles' in sql:
            output=[dict(database='synthetic_content',session_user='projector',current_user='projector',login=True,
                superuser=e.bad_service_role,bypass_rls=False,create_role=False,create_db=False,memberships=False,
                owns_protected=False,direct_access=False)]
        elif 'inspect_acl_projection' in sql:
            state=deepcopy(e.state)
            if e.corrupt_readback and len(e.connections)>=2: e.corrupt_readback(state)
            output=[{'snapshot_json':json.dumps(state,sort_keys=True)}]
        elif 'apply_acl_projection' in sql:
            e.applies+=1
            plan=json.loads(params[0]); registry=json.loads(params[2]); state=deepcopy(e.state)
            fresh=state['applied'] is None
            state['library'].update(policy_epoch=plan['target_policy_epoch'],acl_state='ready')
            state['acls']=[dict(principal_id=a['principal_id'],permission=a['permission'],active=a['active'],projection_epoch=plan['target_policy_epoch']) for a in plan['acls']]
            state['bindings']=[dict(row,role_safe=True) for row in registry]
            state['applied']=dict(operation_id=OP,target_policy_epoch=plan['target_policy_epoch'],expected_policy_epoch=plan['expected_policy_epoch'],
                route_epoch=plan['route_epoch'],payload_hash=plan['payload_hash'],plan_hash=params[1].hex(),registry_hash=params[3].hex())
            self.raw.pending=state
            output=[{'applied_new':fresh}]
        else: raise AssertionError('unexpected SQL boundary')
        names=list(output[0]); self.description=[SimpleNamespace(name=n) for n in names]
        self.rows=[tuple(row[n] for n in names) for row in output]
    def fetchall(self): return self.rows


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('evolvmem.library_projector'),'real projector worker absent')
        self.api=importlib.import_module('evolvmem.library_projector')
        self.identity=RequestIdentity(A,CA,DA,1)
        self.intent=AclIntent(self.identity,L,B,None,1,OP,'synthetic-projector-request')
        self.operation=AclOperation(OP,L,2,'pending',1,'synthetic.p')
        self.plan=dict(version=1,operation_id=OP,library_id=L,connection_ref='synthetic.p',expected_policy_epoch=1,
            target_policy_epoch=2,route_epoch=1,kind='project',device_id=None,project_id=PROJECT,owner_principal_id=A,
            evaluation_at='2001-01-01T00:00:00.000000Z',
            payload_hash=self.intent.payload_hash.hex(),acls=[dict(principal_id=p,permission=perm,active=active,principal_active=True,principal_epoch=1)
              for p,perm,active in ((A,'admin',True),(B,'read',False))],
            credentials=[dict(principal_id=p,credential_id=c,device_id=d,credential_epoch=1,principal_epoch=1,device_epoch=1,
                credential_active=True,principal_active=True,device_active=True,expires_at=None) for p,c,d in ((A,CA,DA),(B,CB,DB))])
        self.config_bindings=tuple(ConnectionBinding('synthetic.p',L,p,c,d,'localhost',5432,'synthetic_content',role,'ref.'+role)
            for p,c,d,role in ((A,CA,DA,'runtime_a'),(B,CB,DB,'runtime_b'),(C,CC,DC,'runtime_c')))
        self.state=dict(database='synthetic_content',session_role='projector',library=dict(library_id=L,kind='project',device_id=None,
            project_id=PROJECT,policy_epoch=1,route_epoch=1,authority_state='pg_active',acl_state='ready'),
            acls=[dict(principal_id=p,permission=perm,active=True,projection_epoch=1) for p,perm in ((A,'write'),(B,'read'))],
            bindings=[dict(login_role=b.role,principal_id=b.principal_id,credential_id=b.credential_id,device_id=b.device_id,
                credential_epoch=1,expires_at=None,enabled=True,role_safe=True) for b in self.config_bindings],applied=None)
        self.make_worker()

    def make_worker(self, *, bindings=None):
        self.control=ControlPort(json.dumps(self.plan,sort_keys=True))
        self.engine=Engine(self.state,self.control)
        target=self.api.ProjectionTarget(L,'synthetic.p',ControlBinding('localhost',5432,'synthetic_content','projector','ref.projector'),
            self.config_bindings if bindings is None else bindings)
        self.worker=self.api.AclProjector(self.control,(target,),self.engine.connect,lambda _: 'synthetic-only-secret')

    def error(self, action):
        with self.assertRaises(FoundationError) as cm: action()
        self.assertNotIn('private',str(cm.exception))
        self.assertNotIn('synthetic-only-secret',str(cm.exception))
        return cm.exception.code

    def test_real_worker_uses_commit_then_new_connection_full_readback(self):
        receipt=self.worker(self.intent,self.operation)
        self.assertEqual((OP,L,2,1),(receipt.operation_id,receipt.library_id,receipt.target_policy_epoch,receipt.expected_route_epoch))
        self.assertEqual(2,len(self.engine.connections))
        self.assertTrue(all(raw.closed for raw in self.engine.connections))
        self.assertEqual(1,self.control.commits)
        self.assertGreaterEqual(self.control.calls,3)
        self.assertEqual([True,False,False],[b['enabled'] for b in self.engine.state['bindings']])
        self.assertEqual(2,len(self.engine.state['acls']))
        self.assertFalse(self.engine.state['acls'][1]['active'])

    def test_dirty_connector_result_is_rejected_before_any_sql(self):
        for bad in ('intrans','autocommit','closed'):
            self.make_worker(); self.engine.bad_connection=bad
            self.error(lambda:self.worker(self.intent,self.operation))
            self.assertEqual([],self.engine.connections[0].executed)
            self.assertEqual(0,self.engine.connections[0].commits)

    def test_missing_config_binding_refuses_before_apply(self):
        self.make_worker(bindings=self.config_bindings[:-1])
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)

    def test_missing_eligible_binding_refuses_before_apply(self):
        self.state['bindings']=self.state['bindings'][1:]
        self.make_worker(bindings=self.config_bindings[1:])
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)

    def test_duplicate_eligible_credential_refuses(self):
        duplicate=replace(self.config_bindings[0],role='runtime_duplicate')
        self.state['bindings'].append(dict(self.state['bindings'][0],login_role='runtime_duplicate'))
        self.make_worker(bindings=self.config_bindings+(duplicate,))
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)

    def test_historical_expired_credential_is_disabled_without_blocking(self):
        expiration='2000-01-01T00:00:00.000000Z'
        self.plan['acls'][1]['active']=True
        self.plan['credentials'][1]['expires_at']=expiration
        self.state['bindings'][1]['expires_at']=expiration
        self.make_worker()
        self.worker(self.intent,self.operation)
        self.assertFalse(self.engine.state['bindings'][1]['enabled'])

    def test_expiring_after_fixed_evaluation_does_not_make_plan_unrecoverable(self):
        self.plan['evaluation_at']='1999-01-01T00:00:00.000000Z'
        self.plan['acls'][1]['active']=True
        self.plan['credentials'][1]['expires_at']='2000-01-01T00:00:00.000000Z'
        self.state['bindings'][1]['expires_at']='2000-01-01T00:00:00.000000Z'
        self.make_worker()
        self.worker(self.intent,self.operation)
        # Actual access MUST still be denied by the existing SQL expiry gate;
        # that behavior is separately checked against real PG, not emulated here.
        self.assertTrue(self.engine.state['bindings'][1]['enabled'])

    def test_historical_expired_credential_needs_no_new_login(self):
        self.plan['acls'][1]['active']=True
        self.plan['credentials'][1]['expires_at']='2000-01-01T00:00:00.000000Z'
        self.state['bindings']=[row for row in self.state['bindings'] if row['credential_id']!=CB]
        self.make_worker(bindings=tuple(row for row in self.config_bindings if row.credential_id!=CB))
        self.worker(self.intent,self.operation)
        self.assertNotIn(CB,{row['credential_id'] for row in self.engine.state['bindings']})

    def test_binding_epoch_drift_refuses_even_if_disabled(self):
        self.state['bindings'][1]['credential_epoch']=2
        self.make_worker()
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)

    def test_unsafe_runtime_or_service_role_refuses(self):
        self.state['bindings'][0]['role_safe']=False; self.make_worker()
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)
        self.state['bindings'][0]['role_safe']=True; self.make_worker(); self.engine.bad_service_role=True
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual(0,self.engine.applies)

    def test_readback_extra_acl_or_binding_cannot_produce_receipt(self):
        for field in ('acls','bindings'):
            self.make_worker()
            self.engine.corrupt_readback=lambda state,field=field:state[field].append(deepcopy(state[field][0]))
            self.error(lambda:self.worker(self.intent,self.operation))
            self.assertEqual(2,len(self.engine.connections))

    def test_readback_hash_drift_refuses(self):
        self.engine.corrupt_readback=lambda state:state['applied'].update(plan_hash='0'*64)
        self.error(lambda:self.worker(self.intent,self.operation))

    def test_commit_unknown_never_retries_or_returns_receipt(self):
        self.engine.commit_unknown=True
        self.assertEqual('content_projection_commit_result_unknown',self.error(lambda:self.worker(self.intent,self.operation)))
        self.assertEqual(1,self.engine.applies); self.assertEqual(1,len(self.engine.connections))

    def test_explicit_retry_after_unknown_commit_uses_idempotent_readback(self):
        self.engine.commit_unknown=True; self.error(lambda:self.worker(self.intent,self.operation))
        self.engine.commit_unknown=False
        receipt=self.worker(self.intent,self.operation)
        self.assertEqual(OP,receipt.operation_id)
        self.assertEqual(2,self.engine.applies)

    def test_control_plan_commit_unknown_never_connects_content(self):
        self.control.commit_unknown=True
        self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual([],self.engine.connections)

    def test_wrong_route_or_payload_never_selects_connection(self):
        for key,value in (('connection_ref','untrusted.other'),('payload_hash','0'*64)):
            original=self.plan[key]; self.plan[key]=value; self.make_worker()
            self.error(lambda:self.worker(self.intent,self.operation)); self.assertEqual([],self.engine.connections)
            self.plan[key]=original

    def test_revoke_never_existing_acl_does_not_fabricate_acl_row(self):
        self.intent=replace(self.intent,target_principal_id=C)
        self.plan['payload_hash']=self.intent.payload_hash.hex()
        self.plan['acls'][1]['active']=True
        self.make_worker()
        self.worker(self.intent,self.operation)
        self.assertEqual({A,B},{a['principal_id'] for a in self.engine.state['acls']})


if __name__=='__main__': unittest.main()
