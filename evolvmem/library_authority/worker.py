"""Trusted real projector transport; original ACL validators remain unchanged."""
from dataclasses import dataclass
import hashlib
import json

from evolvmem.library_catalog.adapter import _uuid
from evolvmem.library_foundation import FoundationError
from evolvmem.library_project_acl import worker as ACL
from .coordinator import (AuthorityIntent,AuthorityOperation,VerifiedAuthorityReceipt,HASH,
    require,epoch,canonical)

LIMIT=2*1024**2
OUTER_KEYS=set('version operation_id library_id action connection_ref owner_principal_id kind device_id project_id expected_route_epoch expected_policy_epoch target_route_epoch target_policy_epoch payload_hash evaluation_at acl_plan_text acl_plan_hash'.split())
ENVELOPE_KEYS=set('version operation_id library_id connection_ref state plan_hash acl_snapshot_text readiness_text authority_receipt_text authority_receipt_hash'.split())
RECEIPT_KEYS=set('version operation_id library_id action connection_ref kind owner_principal_id device_id expected_route_epoch expected_policy_epoch target_route_epoch target_policy_epoch plan_hash registry_hash state_hash content_database projector_login readiness'.split())
READY_KEYS=set('kind enabled revision policy_epoch route_epoch namespace c_epoch c_completion_hash c_source_seal raw_generation membership_revision membership_hash membership_c_epoch membership_c_completion_hash'.split())
DOMAINS=set('core sources organization capture governance knowledge capability continuity operations'.split())

def _pairs(values):
    result={}
    for k,v in values:
        require(k not in result,'authority_document_invalid');result[k]=v
    return result

def decode(text):
    require(type(text) is str and len(text.encode('utf-8'))<=LIMIT,'authority_document_invalid')
    def reject(_):raise FoundationError('authority_document_invalid')
    try:return json.loads(text,object_pairs_hook=_pairs,parse_constant=reject)
    except (TypeError,ValueError):raise FoundationError('authority_document_invalid') from None

def _hash(value):
    require(type(value) is str and HASH.fullmatch(value) is not None,'authority_hash_invalid')
    return value

def _digest(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

@dataclass(frozen=True,slots=True)
class AuthorityPlan:
    text:str
    sha256:str
    document:dict
    inner_text:str
    inner_hash:str
    inner:dict

def validate_authority_plan(text,sha256,intent,operation):
    require(isinstance(intent,AuthorityIntent) and isinstance(operation,AuthorityOperation),'authority_plan_mismatch')
    _hash(sha256);d=decode(text)
    require(_digest(text)==sha256 and type(d) is dict and set(d)==OUTER_KEYS,'authority_plan_mismatch')
    require(type(d['version']) is int and d['version']==1,'authority_plan_mismatch')
    expected=dict(operation_id=intent.operation_id,library_id=intent.library_id,action='activate',
        connection_ref=intent.connection_ref,owner_principal_id=intent.identity.principal_id,kind='legacy',device_id=None,project_id=None,
        expected_route_epoch=intent.expected_route_epoch,expected_policy_epoch=intent.expected_policy_epoch,
        target_route_epoch=intent.expected_route_epoch+1,target_policy_epoch=intent.expected_policy_epoch+1,
        payload_hash=intent.payload_hash.hex())
    require(all(d[k]==v for k,v in expected.items()),'authority_plan_mismatch')
    for key in ('expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch'):epoch(d[key])
    require(operation.document['plan_hash'] in (None,sha256),'authority_plan_changed')
    require(ACL.expiry(d['evaluation_at']) is not None,'authority_plan_mismatch')
    inner=decode(d['acl_plan_text']);_hash(d['acl_plan_hash'])
    require(_digest(d['acl_plan_text'])==d['acl_plan_hash'] and type(inner) is dict and set(inner)==ACL.PLAN_KEYS,
        'authority_inner_plan_mismatch')
    require(type(inner['version']) is int and inner['version']==2,'authority_inner_plan_mismatch')
    fields=('operation_id','library_id','connection_ref','owner_principal_id','kind','device_id','project_id',
        'expected_policy_epoch','target_policy_epoch','payload_hash','evaluation_at')
    require(all(inner[k]==d[k] for k in fields) and inner['route_epoch']==d['target_route_epoch'],'authority_inner_plan_mismatch')
    for name in ('expected_policy_epoch','target_policy_epoch','route_epoch'):epoch(inner[name])
    require(type(inner['acls']) is list and len(inner['acls'])<=256 and type(inner['credentials']) is list
        and len(inner['credentials'])<=1024,'authority_inner_plan_mismatch')
    principals={};credentials={}
    for row in inner['acls']:
        require(type(row) is dict and set(row)==ACL.ACL_KEYS,'authority_inner_plan_mismatch')
        _uuid(row['principal_id']);epoch(row['principal_epoch'])
        require(row['principal_id'] not in principals and row['permission'] in ('read','write','admin')
            and type(row['active']) is bool and type(row['principal_active']) is bool,'authority_inner_plan_mismatch')
        principals[row['principal_id']]=row
    owner=principals.get(intent.identity.principal_id)
    require(owner is not None and owner['permission']=='admin' and owner['active'] and owner['principal_active'],
        'authority_owner_scope_missing')
    for row in inner['credentials']:
        require(type(row) is dict and set(row)==ACL.CREDENTIAL_KEYS,'authority_inner_plan_mismatch')
        for name in ('principal_id','credential_id','device_id'):_uuid(row[name])
        for name in ('credential_epoch','principal_epoch','device_epoch'):epoch(row[name])
        for name in ('credential_active','principal_active','device_active'):
            require(type(row[name]) is bool,'authority_inner_plan_mismatch')
        ACL.expiry(row['expires_at'])
        require(row['credential_id'] not in credentials and row['principal_id'] in principals,'authority_inner_plan_mismatch')
        principal=principals[row['principal_id']]
        require(row['principal_epoch']==principal['principal_epoch'] and row['principal_active']==principal['principal_active'],
            'authority_inner_plan_mismatch')
        credentials[row['credential_id']]=row
    actor=credentials.get(intent.identity.credential_id)
    require(actor is not None and (actor['principal_id'],actor['device_id'],actor['credential_epoch'],actor['principal_epoch'],actor['device_epoch'])==
        (intent.identity.principal_id,intent.identity.device_id,intent.identity.credential_epoch,intent.identity.principal_epoch,intent.identity.device_epoch)
        and ACL.eligible(inner,actor),'authority_owner_scope_missing')
    ACL.validate_projects(inner,None)
    return AuthorityPlan(text,sha256,d,d['acl_plan_text'],d['acl_plan_hash'],inner)

def validate_readiness(d,plan,*,applied):
    require(type(d) is dict and set(d)==READY_KEYS and d['kind']=='history-preserved-v1','authority_readiness_mismatch')
    require(type(d['enabled']) is bool and d['enabled'] is applied,'authority_readiness_mismatch')
    epoch(d['revision']);epoch(d['membership_revision'])
    for key in ('c_epoch','membership_c_epoch'):_uuid(d[key])
    for key in ('c_completion_hash','c_source_seal','membership_hash','membership_c_completion_hash'):_hash(d[key])
    require(d['c_epoch']==d['membership_c_epoch'] and d['c_completion_hash']==d['membership_c_completion_hash'],
        'authority_readiness_mismatch')
    require(d['namespace'] in ('owner','kane','public'),'authority_readiness_mismatch')
    expected=DOMAINS-({'capture'} if d['namespace']=='public' else set())
    require(type(d['raw_generation']) is dict and set(d['raw_generation'])==expected,'authority_readiness_mismatch')
    for value in d['raw_generation'].values():
        require(type(value) is dict and set(value)=={'run_id','generation'},'authority_readiness_mismatch')
        _uuid(value['run_id']);require(type(value['generation']) is int and 0<=value['generation']<=2**63-1,'authority_readiness_mismatch')
    if applied:
        epoch(d['policy_epoch']);epoch(d['route_epoch'])
        require((d['policy_epoch'],d['route_epoch'])==(plan.document['target_policy_epoch'],plan.document['target_route_epoch']),
            'authority_readiness_mismatch')
    else:
        for name in ('policy_epoch','route_epoch'):
            require(d[name] is None or (type(d[name]) is int and d[name]==plan.document['expected_'+name]),
                'authority_readiness_mismatch')
    return d

def _envelope(value,plan,intent,state):
    require(type(value) is dict and set(value)==ENVELOPE_KEYS,'authority_application_mismatch')
    require(type(value['version']) is int and value['version']==1
        and (value['operation_id'],value['library_id'],value['connection_ref'],value['plan_hash'])==
        (intent.operation_id,intent.library_id,intent.connection_ref,plan.sha256),'authority_application_mismatch')
    require(value['state']==state,'authority_content_not_applied' if state=='applied' else 'authority_preparation_required')
    snapshot=decode(value['acl_snapshot_text']);readiness=decode(value['readiness_text'])
    validate_readiness(readiness,plan,applied=state=='applied')
    return snapshot,readiness

def prepared_registry(value,target,plan,intent):
    snapshot,_=_envelope(value,plan,intent,'prepared')
    require(value['authority_receipt_text'] is None and value['authority_receipt_hash'] is None,'authority_application_mismatch')
    ACL.inspect_shape(snapshot,target,plan.inner)
    require(snapshot['library']['policy_epoch']==plan.document['expected_policy_epoch']
        and snapshot['library']['acl_state']=='updating','authority_application_mismatch')
    return ACL.registry_for(snapshot,target,plan.inner)

def verify_application(value,target,plan,intent,*,committed=True):
    snapshot,readiness=_envelope(value,plan,intent,'applied')
    registry=ACL.registry_for(snapshot,target,plan.inner)
    registry_hash=hashlib.sha256(ACL.canonical(registry).encode()).digest()
    ACL.verify_readback(snapshot,target,plan.inner,registry,bytes.fromhex(plan.inner_hash),registry_hash)
    text=value['authority_receipt_text'];receipt=decode(text);_hash(value['authority_receipt_hash'])
    require(_digest(text)==value['authority_receipt_hash'] and type(receipt) is dict and set(receipt)==RECEIPT_KEYS,
        'authority_receipt_mismatch')
    require(type(receipt['version']) is int and receipt['version']==1,'authority_receipt_mismatch')
    for name in ('expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch'):epoch(receipt[name])
    fields=('operation_id','library_id','action','connection_ref','kind','owner_principal_id','device_id',
        'expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch')
    require(all(receipt[k]==plan.document[k] for k in fields)
        and receipt['plan_hash']==plan.sha256 and receipt['registry_hash']==registry_hash.hex()
        and receipt['state_hash']==_digest(value['acl_snapshot_text'])
        and receipt['content_database']==target.connection.database and receipt['projector_login']==target.connection.role
        and canonical(receipt['readiness'])==canonical(readiness),'authority_receipt_mismatch')
    if not committed:return None
    return VerifiedAuthorityReceipt(intent.operation_id,intent.library_id,intent.connection_ref,
        plan.document['target_route_epoch'],plan.document['target_policy_epoch'],plan.sha256,text)

class AuthorityProjector(ACL.AclProjector):
    def _plan(self,session,intent,operation):
        rows=session.execute('SELECT * FROM em_control.get_authority_projection_plan(%s)',(intent.operation_id,))
        require(len(rows)==1 and set(rows[0])=={'plan_text','plan_hash_hex'},'authority_plan_unavailable')
        return validate_authority_plan(rows[0]['plan_text'],rows[0]['plan_hash_hex'],intent,operation)

    def _target(self,intent):
        require((intent.library_id,intent.connection_ref) in self.targets,'unregistered_projection_target')
        return self.targets[(intent.library_id,intent.connection_ref)]

    def validate_target(self,intent):return self._target(intent)

    def _inspect_authority(self,raw,intent):
        rows=self._execute(raw,'SELECT em_security.inspect_authority_application(%s) AS application_json',(intent.operation_id,))
        require(len(rows)==1 and set(rows[0])=={'application_json'},'authority_application_unavailable')
        return rows[0]['application_json']

    def project(self,intent,operation):
        target=self._target(intent)
        # Persist the immutable plan before entering the independent content TX.
        with self.control_provider.open(read_only=False) as control:
            first=self._plan(control,intent,operation);control.commit()
        with self.control_provider.open(read_only=False) as control:
            plan=self._plan(control,intent,operation)
            require(plan.text==first.text and plan.sha256==first.sha256,'authority_plan_changed')
            with self._open(target,read_only=False) as raw:
                self._execute(raw,'SELECT em_security.prepare_authority_application(%s,%s) AS application_json',
                    (plan.text,bytes.fromhex(plan.sha256)))
                registry=prepared_registry(self._inspect_authority(raw,intent),target,plan,intent)
                registry_text=ACL.canonical(registry);registry_hash=hashlib.sha256(registry_text.encode()).digest()
                self._execute(raw,'SELECT em_security.apply_authority_application(%s,%s,%s,%s) AS application_json',
                    (plan.text,bytes.fromhex(plan.sha256),registry_text,registry_hash))
                verify_application(self._inspect_authority(raw,intent),target,plan,intent,committed=False)
                try:raw.commit()
                except Exception:raise FoundationError('content_projection_commit_result_unknown') from None
            # The authoritative inspector takes metadata row locks. This fresh
            # READ WRITE transaction executes only SELECT/LOCK and rolls back.
            with self._open(target,read_only=False) as raw:
                receipt=verify_application(self._inspect_authority(raw,intent),target,plan,intent)
            final=self._plan(control,intent,operation)
            require(final.text==plan.text and final.sha256==plan.sha256,'authority_plan_changed')
            return receipt

    def recover_locked(self,control,intent,operation):
        require(operation.document['plan_hash'] is not None,'authority_projection_plan_required')
        plan=self._plan(control,intent,operation);target=self._target(intent)
        with self._open(target,read_only=False) as raw:
            return verify_application(self._inspect_authority(raw,intent),target,plan,intent)

    def recover(self,intent,operation):
        # Locks are rolled back on exit. This path never calls prepare or apply.
        with self.control_provider.open(read_only=False) as control:
            return self.recover_locked(control,intent,operation)
