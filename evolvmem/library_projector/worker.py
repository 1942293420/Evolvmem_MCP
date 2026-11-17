"""Concrete ACL projector for ControlAclCoordinator.apply; no HTTP interface."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from types import MappingProxyType
from uuid import UUID

from evolvmem.library_foundation import ConnectionBinding, FoundationError
from evolvmem.library_catalog import AclIntent, AclOperation, ControlBinding, VerifiedProjection

PLAN_KEYS = set('version operation_id library_id connection_ref expected_policy_epoch target_policy_epoch route_epoch kind device_id project_id owner_principal_id payload_hash evaluation_at acls credentials'.split())
ACL_KEYS = set('principal_id permission active principal_active principal_epoch'.split())
CREDENTIAL_KEYS = set('principal_id credential_id device_id credential_epoch principal_epoch device_epoch credential_active principal_active device_active expires_at'.split())
BINDING_KEYS = set('login_role principal_id credential_id device_id credential_epoch expires_at enabled role_safe'.split())
LIBRARY_KEYS = set('library_id kind device_id project_id policy_epoch route_epoch authority_state acl_state'.split())


def require(condition, code='invalid_projection_plan'):
    if not condition:
        raise FoundationError(code)


def uuid_value(value):
    try: valid = isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): valid = False
    require(valid)


def epoch(value):
    require(type(value) is int and 1 <= value < 9223372036854775807)


def expiry(value):
    if value is None: return None
    require(isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z',value) is not None)
    try: return datetime.fromisoformat(value[:-1]+'+00:00')
    except ValueError: raise FoundationError('invalid_projection_plan') from None


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False)


def no_duplicate_keys(pairs):
    result={}
    for key,value in pairs:
        require(key not in result)
        result[key]=value
    return result


def decode(text):
    require(isinstance(text,str) and len(text.encode('utf-8')) <= 2097152)
    try: return json.loads(text,object_pairs_hook=no_duplicate_keys)
    except (ValueError,TypeError): raise FoundationError('invalid_projection_document') from None


@dataclass(frozen=True,slots=True)
class ProjectionTarget:
    """Trusted server configuration; never deserialize it from a request."""
    library_id: str
    connection_ref: str
    connection: ControlBinding
    runtime_bindings: tuple[ConnectionBinding,...]

    def __post_init__(self):
        uuid_value(self.library_id)
        require(isinstance(self.connection_ref,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}',self.connection_ref) is not None)
        require(isinstance(self.connection,ControlBinding),'invalid_projector_configuration')
        require(isinstance(self.runtime_bindings,tuple) and len(self.runtime_bindings)<=2048,'invalid_projector_configuration')
        roles=set()
        for binding in self.runtime_bindings:
            require(isinstance(binding,ConnectionBinding),'invalid_projector_configuration')
            require(binding.library_id==self.library_id and binding.connection_ref==self.connection_ref
                and binding.host==self.connection.host and binding.port==self.connection.port
                and binding.database==self.connection.database and binding.role!=self.connection.role
                and binding.role not in roles,'invalid_projector_configuration')
            roles.add(binding.role)


def validate_plan(text, hash_hex, intent, operation):
    require(isinstance(hash_hex,str) and re.fullmatch(r'[0-9a-f]{64}',hash_hex) is not None)
    require(isinstance(text,str) and hmac.compare_digest(hashlib.sha256(text.encode()).hexdigest(),hash_hex),'projection_plan_hash_mismatch')
    plan=decode(text)
    require(isinstance(plan,dict) and set(plan)==PLAN_KEYS and type(plan['version']) is int and plan['version']==1)
    require(isinstance(intent,AclIntent) and isinstance(operation,AclOperation),'invalid_projection_operation')
    require(operation.operation_state in ('pending','applying','failed'),'invalid_projection_operation')
    require((plan['operation_id'],plan['library_id'],plan['expected_policy_epoch'],plan['target_policy_epoch'],plan['route_epoch'],plan['connection_ref']) ==
        (intent.operation_id,intent.library_id,intent.expected_policy_epoch,operation.target_policy_epoch,operation.expected_route_epoch,operation.connection_ref))
    require(operation.operation_id==intent.operation_id and operation.library_id==intent.library_id
            and operation.target_policy_epoch==intent.expected_policy_epoch+1)
    require(plan['payload_hash']==intent.payload_hash.hex(),'projection_payload_mismatch')
    require(expiry(plan['evaluation_at']) is not None)
    for key in ('operation_id','library_id','owner_principal_id'): uuid_value(plan[key])
    for key in ('expected_policy_epoch','target_policy_epoch','route_epoch'): epoch(plan[key])
    require(plan['kind'] in ('device','project','legacy'))
    for key in ('device_id','project_id'):
        if plan[key] is not None: uuid_value(plan[key])
    require((plan['kind']=='project' and plan['project_id'] is not None and plan['device_id'] is None)
        or(plan['kind']=='device' and plan['device_id'] is not None and plan['project_id'] is None)
        or(plan['kind']=='legacy' and plan['device_id'] is None and plan['project_id'] is None))
    require(isinstance(plan['acls'],list) and len(plan['acls'])<=256)
    require(isinstance(plan['credentials'],list) and len(plan['credentials'])<=1024)
    acls={}
    for row in plan['acls']:
        require(isinstance(row,dict) and set(row)==ACL_KEYS)
        uuid_value(row['principal_id']); epoch(row['principal_epoch'])
        require(row['principal_id'] not in acls and row['permission'] in ('read','write','admin')
            and type(row['active']) is bool and type(row['principal_active']) is bool)
        require(plan['kind']!='device' or not row['active'] or row['principal_id']==plan['owner_principal_id'])
        acls[row['principal_id']]=row
    credentials={}
    for row in plan['credentials']:
        require(isinstance(row,dict) and set(row)==CREDENTIAL_KEYS)
        for key in ('principal_id','credential_id','device_id'): uuid_value(row[key])
        for key in ('credential_epoch','principal_epoch','device_epoch'): epoch(row[key])
        for key in ('credential_active','principal_active','device_active'): require(type(row[key]) is bool)
        expiry(row['expires_at'])
        require(row['credential_id'] not in credentials and row['principal_id'] in acls)
        acl=acls[row['principal_id']]
        require(acl['principal_active']==row['principal_active'] and acl['principal_epoch']==row['principal_epoch'])
        credentials[row['credential_id']]=row
    return plan


def eligible(plan,credential):
    acl=next(a for a in plan['acls'] if a['principal_id']==credential['principal_id'])
    return (acl['active'] and acl['principal_active'] and credential['credential_active']
        and credential['principal_active'] and credential['device_active']
        and (credential['expires_at'] is None or expiry(credential['expires_at'])>expiry(plan['evaluation_at']))
        and(plan['kind']!='device' or credential['device_id']==plan['device_id']))


def inspect_shape(snapshot,target,plan):
    require(isinstance(snapshot,dict) and set(snapshot)=={'database','session_role','library','acls','bindings','applied'},'invalid_projection_inventory')
    require(snapshot['database']==target.connection.database and snapshot['session_role']==target.connection.role,'projection_connection_mismatch')
    library=snapshot['library']
    require(isinstance(library,dict) and set(library)==LIBRARY_KEYS,'invalid_projection_inventory')
    require(all(library[key]==plan[key] for key in ('library_id','kind','device_id','project_id'))
        and library['route_epoch']==plan['route_epoch'] and library['authority_state']=='pg_active'
        and library['acl_state'] in ('ready','updating','failed')
        and library['policy_epoch'] in (plan['expected_policy_epoch'],plan['target_policy_epoch']),'projection_route_changed')
    require(isinstance(snapshot['bindings'],list) and len(snapshot['bindings'])<=2048
        and isinstance(snapshot['acls'],list) and len(snapshot['acls'])<=256,'invalid_projection_inventory')


def registry_for(snapshot,target,plan):
    inspect_shape(snapshot,target,plan)
    configured={binding.role:binding for binding in target.runtime_bindings}
    actual={}
    credentials={c['credential_id']:c for c in plan['credentials']}
    registry=[]; covered={}
    for row in snapshot['bindings']:
        require(isinstance(row,dict) and set(row)==BINDING_KEYS,'registry_incomplete')
        role=row['login_role']
        require(isinstance(role,str) and role in configured and role not in actual and row['role_safe'] is True,'registry_incomplete')
        binding=configured[role]
        require((row['principal_id'],row['credential_id'],row['device_id'])==
            (binding.principal_id,binding.credential_id,binding.device_id),'binding_metadata_drift')
        epoch(row['credential_epoch']); expiry(row['expires_at'])
        require(type(row['enabled']) is bool,'registry_incomplete')
        desired=False
        credential=credentials.get(row['credential_id'])
        if credential is not None:
            require(all(row[key]==credential[key] for key in ('principal_id','device_id','credential_epoch','expires_at')),'binding_metadata_drift')
            desired=eligible(plan,credential)
            if desired:
                covered[row['credential_id']]=covered.get(row['credential_id'],0)+1
        new={key:value for key,value in row.items() if key!='role_safe'}
        new['enabled']=desired
        registry.append(new); actual[role]=row
    require(set(actual)==set(configured),'registry_incomplete')
    require(all(covered.get(c['credential_id'])==1 for c in plan['credentials'] if eligible(plan,c)),'eligible_binding_missing_or_duplicate')
    require(all(isinstance(a,dict) and set(a)=={'principal_id','permission','active','projection_epoch'} for a in snapshot['acls']),'invalid_projection_inventory')
    require({a['principal_id'] for a in snapshot['acls']} <= {a['principal_id'] for a in plan['acls']},'acl_plan_incomplete')
    return sorted(registry,key=lambda row:row['login_role'])


def verify_readback(snapshot,target,plan,registry,plan_hash,registry_hash):
    inspect_shape(snapshot,target,plan)
    # Validate shapes before sorting/reading any untrusted result fields.
    require(registry_for(snapshot,target,plan)==registry,'projection_readback_mismatch')
    require(snapshot['library']['policy_epoch']==plan['target_policy_epoch'] and snapshot['library']['acl_state']=='ready','projection_readback_mismatch')
    expected_acl=sorted([dict(principal_id=a['principal_id'],permission=a['permission'],active=a['active'],
        projection_epoch=plan['target_policy_epoch']) for a in plan['acls']],key=lambda row:row['principal_id'])
    expected_bindings=[dict(row,role_safe=True) for row in registry]
    require(sorted(snapshot['acls'],key=lambda row:row['principal_id'])==expected_acl,'projection_readback_mismatch')
    require(sorted(snapshot['bindings'],key=lambda row:row['login_role'])==expected_bindings,'projection_readback_mismatch')
    # Revalidate metadata and expiry, including disabled and out-of-ACL bindings.
    expected_applied=dict(operation_id=plan['operation_id'],target_policy_epoch=plan['target_policy_epoch'],
        expected_policy_epoch=plan['expected_policy_epoch'],route_epoch=plan['route_epoch'],payload_hash=plan['payload_hash'],
        plan_hash=plan_hash.hex(),registry_hash=registry_hash.hex())
    require(snapshot['applied']==expected_applied,'projection_readback_mismatch')


class AclProjector:
    def __init__(self,control_provider,targets,connector,secret_resolver):
        entries={}
        for target in targets:
            require(isinstance(target,ProjectionTarget),'invalid_projector_configuration')
            key=(target.library_id,target.connection_ref)
            require(key not in entries,'invalid_projector_configuration')
            entries[key]=target
        self.control_provider=control_provider
        self.targets=MappingProxyType(entries)
        self.connector,self.secret_resolver=connector,secret_resolver

    @staticmethod
    def _execute(raw,sql,params=(),*,begin=False):
        if not begin:
            require(not raw.closed and raw.autocommit is True and raw.info.transaction_status.name=='INTRANS','projection_transaction_lost')
        try:
            with raw.cursor() as cursor:
                cursor.execute(sql,params)
                if cursor.description is None: return ()
                names=[column.name for column in cursor.description]
                return tuple(dict(zip(names,row)) for row in cursor.fetchall())
        except FoundationError: raise
        except Exception as exc:
            state=getattr(exc,'sqlstate',None)
            code='projection_busy' if state in ('55P03','57014','40001','40P01') else 'projection_denied' if state in ('42501','23505','22023') else 'projection_io_error'
            raise FoundationError(code) from None

    @contextmanager
    def _open(self,target,*,read_only):
        raw=None; failed=False; fresh=False
        try:
            try:
                secret=self.secret_resolver(target.connection.password_ref)
                require(isinstance(secret,str) and bool(secret),'projection_secret_unavailable')
                raw=self.connector(host=target.connection.host,port=target.connection.port,dbname=target.connection.database,
                    user=target.connection.role,password=secret,sslmode='verify-full',connect_timeout=3,autocommit=True)
            except Exception:
                raise FoundationError('projection_connection_unavailable') from None
            finally: secret=None
            try:
                fresh=(raw.closed is False and raw.autocommit is True and raw.info.transaction_status.name=='IDLE')
            except Exception:
                fresh=False
            require(fresh,'invalid_projection_connection')
            mode='READ ONLY' if read_only else 'READ WRITE'
            self._execute(raw,'BEGIN ISOLATION LEVEL READ COMMITTED '+mode,begin=True)
            self._execute(raw,"SELECT set_config('lock_timeout','500ms',true)")
            self._execute(raw,"SELECT set_config('statement_timeout','5000ms',true)")
            rows=self._execute(raw,'''SELECT current_database() AS database,session_user,current_user,r.rolcanlogin AS login,
                r.rolsuper AS superuser,r.rolbypassrls AS bypass_rls,r.rolcreaterole AS create_role,r.rolcreatedb AS create_db,
                EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS memberships,
                EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname IN('em_security','em_content') AND pg_catalog.pg_has_role(r.oid,c.relowner,'MEMBER')) AS owns_protected,
                EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname IN('em_security','em_content') AND c.relkind IN('r','p','v','m','f')
                    AND pg_catalog.has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) AS direct_access
                FROM pg_catalog.pg_roles r WHERE r.rolname=session_user''')
            require(len(rows)==1,'unsafe_projector_role'); role=rows[0]
            require(role.get('database')==target.connection.database and role.get('session_user')==target.connection.role
                and role.get('current_user')==target.connection.role and role.get('login') is True
                and all(role.get(key) is False for key in ('superuser','bypass_rls','create_role','create_db','memberships','owns_protected','direct_access')),
                'unsafe_projector_role')
            yield raw
        except BaseException:
            failed=True; raise
        finally:
            if raw is not None:
                cleanup=False
                if fresh:
                    try: raw.rollback()
                    except Exception: cleanup=True
                try: raw.close()
                except Exception: cleanup=True
                if cleanup and not failed: raise FoundationError('projection_cleanup_failed') from None

    @staticmethod
    def _plan(session,intent,operation):
        rows=session.execute('SELECT * FROM em_control.get_acl_projection_plan(%s)',(intent.operation_id,))
        require(len(rows)==1,'projection_plan_unavailable')
        row=rows[0]
        plan=validate_plan(row.get('plan_text'),row.get('plan_hash_hex'),intent,operation)
        return plan,row['plan_text'],bytes.fromhex(row['plan_hash_hex'])

    def _inspect(self,raw,operation_id):
        rows=self._execute(raw,'SELECT em_security.inspect_acl_projection(%s) AS snapshot_json',(operation_id,))
        require(len(rows)==1,'projection_inventory_unavailable')
        return decode(rows[0].get('snapshot_json'))

    def __call__(self,intent,operation):
        require(isinstance(intent,AclIntent) and isinstance(operation,AclOperation),'invalid_projection_operation')
        # Persist an immutable plan first; default provider cleanup rolls back.
        with self.control_provider.open(read_only=False) as control:
            first_plan,first_text,first_hash=self._plan(control,intent,operation)
            require((intent.library_id,operation.connection_ref) in self.targets,'unregistered_projection_target')
            control.commit()
        with self.control_provider.open(read_only=False) as control:
            plan,plan_text,plan_hash=self._plan(control,intent,operation)
            require(plan_hash==first_hash and plan_text==first_text,'projection_plan_changed')
            target=self.targets[(intent.library_id,operation.connection_ref)]
            with self._open(target,read_only=False) as connection:
                inventory=self._inspect(connection,intent.operation_id)
                registry=registry_for(inventory,target,plan)
                registry_text=canonical(registry); registry_hash=hashlib.sha256(registry_text.encode()).digest()
                rows=self._execute(connection,'SELECT em_security.apply_acl_projection(%s,%s,%s,%s) AS applied_new',
                    (plan_text,plan_hash,registry_text,registry_hash))
                require(len(rows)==1 and type(rows[0].get('applied_new')) is bool,'invalid_projection_apply_result')
                try: connection.commit()
                except Exception: raise FoundationError('content_projection_commit_result_unknown') from None
            # A genuinely new connection after commit and close is mandatory.
            with self._open(target,read_only=True) as connection:
                snapshot=self._inspect(connection,intent.operation_id)
                verify_readback(snapshot,target,plan,registry,plan_hash,registry_hash)
            _,final_text,final_hash=self._plan(control,intent,operation)
            require(final_hash==plan_hash and final_text==plan_text,'projection_plan_changed')
            receipt_hash=hashlib.sha256(canonical(snapshot).encode()).digest()
        return VerifiedProjection(intent.operation_id,intent.library_id,operation.target_policy_epoch,
                                  operation.expected_route_epoch,operation.connection_ref,receipt_hash)
