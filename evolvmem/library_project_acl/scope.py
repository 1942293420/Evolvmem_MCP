"""Resolve a requested canonical key using the actual role-bound content session."""
from dataclasses import dataclass
from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_foundation.sessions import LibrarySession
from .coordinator import project_uuid

@dataclass(frozen=True,slots=True)
class ProjectScope:
    library_id: str
    project_id: str | None
    project_key: str | None
    policy_epoch: int
    route_epoch: int
    identity: RequestIdentity
    permission: str

    def require(self,session):
        if not isinstance(session,LibrarySession) or session._closed:
            raise FoundationError('project_scope_session_required')
        route=session.route
        if (route.identity!=self.identity or route.library.library_id!=self.library_id
                or route.library.policy_epoch!=self.policy_epoch or route.library.route_epoch!=self.route_epoch
                or route.action!=self.permission):
            raise FoundationError('project_scope_changed')

def resolve_project_scope(session,project_key):
    if not isinstance(session,LibrarySession) or session._closed:
        raise FoundationError('project_scope_session_required')
    route=session.route
    if project_key is None:
        if route.action!='read':
            raise FoundationError('project_key_required')
        expected_id=None  # SQL grants this explicit unclassified read to actual owner only.
    else:
        expected_id=project_uuid(project_key)
    if route.action not in ('read','write','admin'):
        raise FoundationError('invalid_project_permission')
    rows=session.execute('SELECT project_id::text,project_key,policy_epoch,route_epoch '
                         'FROM em_security.resolve_project_scope(%s,%s,%s)',
                         (route.library.library_id,project_key,route.action))
    if (len(rows)!=1 or len(rows[0])!=4 or type(rows[0][2]) is not int or type(rows[0][3]) is not int
            or tuple(rows[0])!=(expected_id,project_key,
            route.library.policy_epoch,route.library.route_epoch)):
        raise FoundationError('project_scope_unavailable')
    scope=ProjectScope(route.library.library_id,expected_id,project_key,
                       route.library.policy_epoch,route.library.route_epoch,route.identity,route.action)
    scope.require(session)
    return scope
