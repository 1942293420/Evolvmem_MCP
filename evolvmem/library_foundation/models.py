"""Immutable values, not a deserializer for untrusted MCP identity fields."""
from dataclasses import dataclass, field
import math
import re
from uuid import UUID


class FoundationError(RuntimeError):
    """A stable public error; never includes driver exceptions or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('canonical UUID required')


def _epoch(value):
    if type(value) is not int or value < 1:
        raise ValueError('positive integer epoch required')


def _reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{0,127}', value):
        raise ValueError('server reference required; URI/SQL is not accepted')


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    credential_id: str
    principal_id: str
    device_id: str
    token_digest: str = field(repr=False)
    epoch: int = 1
    active: bool = True
    principal_active: bool = True
    device_active: bool = True
    expires_at: float | None = None
    principal_epoch: int = 1
    device_epoch: int = 1

    def __post_init__(self):
        for value in (self.credential_id, self.principal_id, self.device_id):
            _uuid(value)
        _epoch(self.epoch)
        _epoch(self.principal_epoch)
        _epoch(self.device_epoch)
        if not re.fullmatch(r'[0-9a-f]{64}', self.token_digest):
            raise ValueError('SHA-256 digest required')
        if any(type(x) is not bool for x in (self.active, self.principal_active, self.device_active)):
            raise ValueError('boolean activation state required')
        if self.expires_at is not None and (type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at)):
            raise ValueError('finite expiration required')

    def usable(self, now):
        return (self.active and self.principal_active and self.device_active
                and (self.expires_at is None or now < self.expires_at))


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Mint only after credential authentication; never construct from a request body."""
    principal_id: str
    credential_id: str
    device_id: str
    credential_epoch: int
    principal_epoch: int = 1
    device_epoch: int = 1

    def __post_init__(self):
        for value in (self.principal_id, self.credential_id, self.device_id):
            _uuid(value)
        _epoch(self.credential_epoch)
        _epoch(self.principal_epoch)
        _epoch(self.device_epoch)


@dataclass(frozen=True, slots=True)
class LibraryDescriptor:
    library_id: str
    kind: str
    backend: str
    connection_ref: str
    policy_epoch: int
    route_epoch: int
    authority_state: str
    acl_state: str
    writer_backend: str
    project_id: str | None = None
    device_id: str | None = None
    owner_principal_id: str | None = None
    active: bool = False

    def __post_init__(self):
        _uuid(self.library_id)
        for value in (self.project_id, self.device_id, self.owner_principal_id):
            if value is not None:
                _uuid(value)
        _epoch(self.policy_epoch)
        _epoch(self.route_epoch)
        _reference(self.connection_ref)
        if type(self.active) is not bool:
            raise ValueError('boolean library activation state required')
        if self.kind not in ('project', 'device', 'legacy'):
            raise ValueError('invalid library kind')
        if self.backend not in ('postgres', 'sqlite') or self.writer_backend not in ('postgres', 'sqlite', 'none'):
            raise ValueError('invalid backend')
        if (self.authority_state not in ('sqlite_active', 'pg_staging', 'verify', 'freeze_and_reconcile', 'pg_active')
                or self.acl_state not in ('ready', 'updating', 'failed')):
            raise ValueError('invalid authority state')
        if self.kind == 'project' and (self.project_id is None or self.device_id is not None):
            raise ValueError('project library requires its fixed project')
        if self.kind == 'device' and (self.device_id is None or self.owner_principal_id is None or self.project_id is not None):
            raise ValueError('device library requires its fixed owner/device')


@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot:
    credential: CredentialBinding
    library: LibraryDescriptor
    permission: str | None

    def __post_init__(self):
        if (not isinstance(self.credential, CredentialBinding)
                or not isinstance(self.library, LibraryDescriptor)
                or (self.permission is not None and
                    (not isinstance(self.permission, str) or self.permission not in ('read', 'write', 'admin')))):
            raise ValueError('invalid catalog authorization projection')


@dataclass(frozen=True, slots=True)
class AuthorizedRoute:
    identity: RequestIdentity
    library: LibraryDescriptor
    tool: str
    action: str


@dataclass(frozen=True, slots=True)
class ConnectionBinding:
    """Loaded from trusted server configuration; contains only a secret reference."""
    connection_ref: str
    library_id: str
    principal_id: str
    credential_id: str
    device_id: str
    host: str
    port: int
    database: str
    role: str
    password_ref: str = field(repr=False)

    def __post_init__(self):
        _uuid(self.library_id)
        _uuid(self.principal_id)
        _uuid(self.credential_id)
        _uuid(self.device_id)
        for value in (self.connection_ref, self.password_ref, self.database, self.role):
            _reference(value)
        if not isinstance(self.host, str) or not self.host or len(self.host) > 253 or any(c.isspace() for c in self.host):
            raise ValueError('invalid configured host')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('invalid configured port')
