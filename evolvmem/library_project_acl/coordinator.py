"""Trusted project authorization workflow; request bodies cannot mint identity."""
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID, uuid5

from evolvmem.library_foundation import FoundationError
from evolvmem.library_catalog import AclIntent, ControlAclCoordinator
from evolvmem.library_catalog.adapter import _identity, _uuid

PROJECT_NAMESPACE = UUID('78894b09-38bb-5d79-bd18-b90c295bdbe3')

def project_uuid(project_key):
    # Keys come from verified project registration, not fuzzy display-name guesses.
    if (not isinstance(project_key,str) or not 1 <= len(project_key) <= 256
            or project_key != project_key.strip() or any(ord(c)<32 or ord(c)==127 for c in project_key)):
        raise FoundationError('invalid_project_key')
    return str(uuid5(PROJECT_NAMESPACE,project_key))

@dataclass(frozen=True,slots=True)
class ProjectAclIntent(AclIntent):
    project_id: str
    project_key: str

    def __post_init__(self):
        AclIntent.__post_init__(self)
        _uuid(self.project_id)
        if self.project_id != project_uuid(self.project_key):
            raise FoundationError('project_registration_conflict')

    @property
    def payload_hash(self):
        payload=dict(version=2,scope='project',actor=list(_identity(self.identity)),library=self.library_id,
            project=self.project_id,project_key=self.project_key,target=self.target_principal_id,
            permission=self.permission,expected_epoch=self.expected_policy_epoch,
            operation=self.operation_id,request=self.request_id)
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).digest()

class ProjectAclCoordinator(ControlAclCoordinator):
    """Both operation kinds share the same epoch, failure fence and receipt path."""
    def begin(self,intent):
        if not isinstance(intent,ProjectAclIntent):
            return super().begin(intent)
        return self._mutate('SELECT * FROM em_control.begin_project_acl_change(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            _identity(intent.identity)+(intent.library_id,intent.target_principal_id,intent.permission,
                intent.expected_policy_epoch,intent.operation_id,intent.request_id,intent.payload_hash,
                intent.project_id,intent.project_key),expected=self._expected(intent))
