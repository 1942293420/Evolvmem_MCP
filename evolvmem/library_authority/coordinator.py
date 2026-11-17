"""Durable owner-authorized legacy activation, never an ordinary business tool."""
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from evolvmem.library_foundation import FoundationError
from evolvmem.library_catalog.adapter import _HELD, _identity, _uuid

MAX_EPOCH=2**63-1
HASH=re.compile(r'[0-9a-f]{64}\Z')
REFERENCE=re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,127}\Z')

def require(value,code='invalid_authority_arguments'):
    if not value:raise FoundationError(code)

def epoch(value,*,increment=False):
    require(type(value) is int and 1<=value<=MAX_EPOCH-int(increment))
    return value

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

@dataclass(frozen=True,slots=True)
class AuthorityIntent:
    identity:object
    library_id:str
    expected_route_epoch:int
    expected_policy_epoch:int
    connection_ref:str
    operation_id:str
    request_id:str
    action:str='activate'

    def __post_init__(self):
        _identity(self.identity)
        for value in (self.library_id,self.operation_id):_uuid(value)
        epoch(self.expected_route_epoch,increment=True);epoch(self.expected_policy_epoch,increment=True)
        require(self.action=='activate','unsupported_authority_action')
        require(type(self.connection_ref) is str and REFERENCE.fullmatch(self.connection_ref) is not None)
        require(type(self.request_id) is str and 1<=len(self.request_id)<=128)

    @property
    def payload_hash(self):
        value=dict(version=1,action=self.action,actor=list(_identity(self.identity)),library_id=self.library_id,
            expected_route_epoch=self.expected_route_epoch,expected_policy_epoch=self.expected_policy_epoch,
            connection_ref=self.connection_ref,operation_id=self.operation_id,request_id=self.request_id)
        return hashlib.sha256(canonical(value).encode('utf-8')).digest()

OPERATION_KEYS=set('version operation_id request_id library_id action owner_principal_id kind device_id connection_ref expected_route_epoch expected_policy_epoch target_route_epoch target_policy_epoch operation_state payload_hash plan_hash receipt_hash'.split())

@dataclass(frozen=True,slots=True)
class AuthorityOperation:
    document:object

@dataclass(frozen=True,slots=True)
class VerifiedAuthorityReceipt:
    """Trusted worker result after committed fresh readback, never CLI input."""
    operation_id:str
    library_id:str
    connection_ref:str
    target_route_epoch:int
    target_policy_epoch:int
    plan_hash:str
    receipt_text:str

    def __post_init__(self):
        for value in (self.operation_id,self.library_id):_uuid(value)
        epoch(self.target_route_epoch);epoch(self.target_policy_epoch)
        require(type(self.connection_ref) is str and REFERENCE.fullmatch(self.connection_ref) is not None)
        require(type(self.plan_hash) is str and HASH.fullmatch(self.plan_hash) is not None)
        require(type(self.receipt_text) is str and 0<len(self.receipt_text.encode('utf-8'))<=2*1024**2)

    @property
    def receipt_hash(self):return hashlib.sha256(self.receipt_text.encode('utf-8')).digest()

    @property
    def summary(self):
        return dict(state='content_applied',operation_id=self.operation_id,library_id=self.library_id,
            target_route_epoch=self.target_route_epoch,target_policy_epoch=self.target_policy_epoch,
            plan_hash=self.plan_hash,receipt_hash=self.receipt_hash.hex())

def parse_operation(rows,intent,*,optional=False):
    require(isinstance(intent,AuthorityIntent))
    if optional and (not rows or (len(rows)==1 and rows[0].get('operation_json') is None)):return None
    require(len(rows)==1 and set(rows[0])=={'operation_json'},'authority_operation_mismatch')
    d=rows[0]['operation_json']
    require(type(d) is dict and set(d)==OPERATION_KEYS,'authority_operation_mismatch')
    expected=dict(version=1,operation_id=intent.operation_id,request_id=intent.request_id,library_id=intent.library_id,
        action='activate',owner_principal_id=intent.identity.principal_id,kind='legacy',device_id=None,
        connection_ref=intent.connection_ref,expected_route_epoch=intent.expected_route_epoch,
        expected_policy_epoch=intent.expected_policy_epoch,target_route_epoch=intent.expected_route_epoch+1,
        target_policy_epoch=intent.expected_policy_epoch+1,payload_hash=intent.payload_hash.hex())
    require(type(d['version']) is int and all(d[k]==v for k,v in expected.items()),'authority_operation_mismatch')
    for name in ('expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch'):epoch(d[name])
    require(d['operation_state'] in ('pending','applying','complete','failed'),'authority_operation_mismatch')
    for name in ('plan_hash','receipt_hash'):
        require(d[name] is None or (type(d[name]) is str and HASH.fullmatch(d[name]) is not None),'authority_operation_mismatch')
    require(d['operation_state']!='complete' or (d['plan_hash'] is not None and d['receipt_hash'] is not None),'authority_operation_mismatch')
    return AuthorityOperation(MappingProxyType(dict(d)))

def _call(session,name,params):
    return session.execute('SELECT em_control.'+name+'('+','.join(['%s']*len(params))+') AS operation_json',params)

class AuthorityCoordinator:
    """Explicit phases only. Unknown commits are never automatically replayed."""
    def __init__(self,provider,projector):
        self.provider,self.projector=provider,projector

    @staticmethod
    def _guard(intent):
        require(isinstance(intent,AuthorityIntent))
        require(_HELD.get() is None,'authority_lease_scope_mismatch')

    def _write(self,name,params,intent):
        self._guard(intent);committed=False
        try:
            with self.provider.open(read_only=False) as session:
                result=parse_operation(_call(session,name,params),intent)
                session.commit();committed=True
            return result
        except FoundationError:
            if committed:raise FoundationError('control_committed_cleanup_failed') from None
            raise

    def begin(self,intent):
        self._guard(intent)
        self.projector.validate_target(intent)
        params=_identity(intent.identity)+(intent.library_id,intent.action,intent.expected_route_epoch,
            intent.expected_policy_epoch,intent.connection_ref,intent.operation_id,intent.request_id,intent.payload_hash)
        return self._write('begin_authority_change',params,intent)

    @staticmethod
    def _status(session,intent):
        return parse_operation(_call(session,'inspect_authority_operation',
            _identity(intent.identity)+(intent.operation_id,intent.payload_hash)),intent,optional=True)

    def status(self,intent):
        self._guard(intent)
        # Pending inspection takes real metadata locks; no DML, then rollback.
        with self.provider.open(read_only=False) as session:return self._status(session,intent)

    @staticmethod
    def _pending(operation):
        require(operation is not None,'authority_operation_missing')
        require(operation.document['operation_state'] in ('pending','applying'),'authority_operation_not_pending')

    @staticmethod
    def _receipt(receipt,intent,operation):
        require(isinstance(receipt,VerifiedAuthorityReceipt),'authority_receipt_mismatch')
        d=operation.document
        require((receipt.operation_id,receipt.library_id,receipt.connection_ref,receipt.target_route_epoch,receipt.target_policy_epoch)==
            (intent.operation_id,intent.library_id,intent.connection_ref,d['target_route_epoch'],d['target_policy_epoch']),
            'authority_receipt_mismatch')
        require(d['plan_hash'] is None or d['plan_hash']==receipt.plan_hash,'authority_receipt_mismatch')
        return receipt

    def project(self,intent):
        self._guard(intent);operation=self.status(intent);self._pending(operation)
        return self._receipt(self.projector.project(intent,operation),intent,operation)

    def recover(self,intent):
        self._guard(intent);operation=self.status(intent)
        self._pending(operation)
        require(operation.document['plan_hash'] is not None,'authority_projection_plan_required')
        return self._receipt(self.projector.recover(intent,operation),intent,operation)

    def finalize(self,intent):
        self._guard(intent);committed=False
        try:
            with self.provider.open(read_only=False) as session:
                operation=self._status(session,intent)
                require(operation is not None,'authority_operation_missing')
                if operation.document['operation_state']=='complete':return operation
                self._pending(operation)
                require(operation.document['plan_hash'] is not None,'authority_projection_plan_required')
                # This worker reuses THIS control transaction's plan locks.
                receipt=self._receipt(self.projector.recover_locked(session,intent,operation),intent,operation)
                confirmed=parse_operation(_call(session,'confirm_authority_projection',(intent.operation_id,
                    bytes.fromhex(receipt.plan_hash),receipt.receipt_text,receipt.receipt_hash)),intent)
                require(confirmed.document['receipt_hash']==receipt.receipt_hash.hex(),'authority_receipt_mismatch')
                result=parse_operation(_call(session,'finish_authority_change',(intent.operation_id,)),intent)
                require(result.document['operation_state']=='complete' and result.document['receipt_hash']==receipt.receipt_hash.hex(),
                    'authority_receipt_mismatch')
                session.commit();committed=True
            return result
        except FoundationError:
            if committed:raise FoundationError('control_committed_cleanup_failed') from None
            raise
