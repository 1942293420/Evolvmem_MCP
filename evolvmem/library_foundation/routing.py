"""Fail-closed authentication and routing, independent of SQLite/MCP bootstraps."""
import hashlib
import hmac
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from .models import (AuthorizedRoute, AuthorizationSnapshot, CredentialBinding,
                     FoundationError, RequestIdentity, _uuid)


class Catalog(Protocol):
    """Adapter must obtain an internally consistent current control-db snapshot."""
    def credential_for_digest(self, digest: str) -> CredentialBinding | None: ...
    def authorization_snapshot(self, identity: RequestIdentity, library_id: str) -> AuthorizationSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class RecallWitness:
    """One decoded, actually-authorized library observed in ACL ``updating``."""
    identity: RequestIdentity
    tool: str
    library: object
    permission: str | None

    @property
    def library_id(self):
        return self.library.library_id


class RecallTransientWitness(FoundationError):
    """Private all-routes witness; no route tuple escapes while it exists."""
    def __init__(self, witnesses, tool, library_ids, routes=()):
        super().__init__('library_unavailable')
        self.witnesses = tuple(witnesses)
        self.tool = tool
        self.library_ids = tuple(library_ids)
        # Private values from the same fully validated observation. They are
        # never returned as a partial route tuple while a witness exists.
        self.routes = tuple(routes)
        self.kind = 'recall_acl_updating'




class Authenticator:
    def __init__(self, catalog: Catalog, *, clock=time.time):
        self.catalog, self.clock = catalog, clock

    def authenticate(self, token, *, claimed_device_id=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise FoundationError('unauthorized')
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        try:
            binding = self.catalog.credential_for_digest(digest)
        except Exception:
            raise FoundationError('catalog_unavailable') from None
        if (not isinstance(binding, CredentialBinding) or not binding.usable(self.clock())
                or not hmac.compare_digest(binding.token_digest, digest)):
            raise FoundationError('unauthorized')
        if claimed_device_id is not None and claimed_device_id != binding.device_id:
            raise FoundationError('device_binding_mismatch')
        return RequestIdentity(binding.principal_id, binding.credential_id, binding.device_id, binding.epoch,
                               binding.principal_epoch, binding.device_epoch)


TOOL_ACTIONS = MappingProxyType({
    **dict.fromkeys(('memory_search', 'context_search', 'context_read', 'memory_status',
                     'context_status', 'capability_search', 'experience_recall',
                     'context_project_recall', 'project_archive_get', 'conversation_read',
                     'continuity_find', 'continuity_list', 'continuity_resume',
                     'session_archive_status'), 'read'),
    **dict.fromkeys(('memory_add', 'memory_replace', 'memory_remove', 'memory_publish',
                     'session_archive_upload', 'session_archive_retry', 'session_archive_assign',
                     'experience_record', 'continuity_begin', 'continuity_checkpoint',
                     'context_record_outcome', 'context_confirm', 'context_sweep'), 'write'),
    'library_acl_update': 'admin',
})
_RANK = {'read': 1, 'write': 2, 'admin': 3}
_FORBIDDEN = frozenset({'dsn', 'connection_ref', 'principal_id', 'credential_id', 'db_role',
                        'owner', 'backend', 'space', 'authenticated_user', 'policy_epoch',
                        'route_epoch', 'writer_backend', 'authenticated_device_id', 'credential_epoch',
                        'principal_epoch', 'device_epoch', 'device_id'})


class AuthorityRouter:
    def __init__(self, catalog: Catalog, *, implemented_tools=None, clock=time.time):
        self.catalog, self.clock = catalog, clock
        configured = implemented_tools or {}
        if set(configured) - {'postgres', 'sqlite'}:
            raise ValueError('unknown backend capability map')
        self.implemented_tools = MappingProxyType({k: frozenset(v) for k, v in configured.items()})
        if any(set(v) - set(TOOL_ACTIONS) for v in self.implemented_tools.values()):
            raise ValueError('unknown tool capability')

    def plan(self, identity, tool, *, library_id, expected_route_epoch=None,
             expected_policy_epoch=None, request_fields=None):
        return self._planned_route(
            identity, tool, library_id,
            lambda selected_id: self.catalog.authorization_snapshot(identity, selected_id),
            expected_route_epoch=expected_route_epoch,
            expected_policy_epoch=expected_policy_epoch,
            request_fields=request_fields)

    def _validate_plan_request(self, identity, tool, library_id, request_fields=None):
        if not isinstance(identity, RequestIdentity):
            raise FoundationError('unauthorized')
        if not isinstance(tool, str) or tool not in TOOL_ACTIONS:
            raise FoundationError('unknown_tool')
        if request_fields is not None and (not isinstance(request_fields, dict) or _FORBIDDEN.intersection(request_fields)):
            raise FoundationError('routing_override_forbidden')
        try:
            _uuid(library_id)
        except (ValueError, TypeError, AttributeError):
            raise FoundationError('invalid_library_selection') from None

    def _planned_route(self, identity, tool, library_id, resolve, *,
                       expected_route_epoch=None, expected_policy_epoch=None,
                       request_fields=None, recall_transition=False):
        self._validate_plan_request(identity, tool, library_id, request_fields)
        try:
            snapshot = resolve(library_id)
        except Exception:
            raise FoundationError('catalog_unavailable') from None
        return self._validated_route(identity, tool, library_id, snapshot,
                                     expected_route_epoch=expected_route_epoch,
                                     expected_policy_epoch=expected_policy_epoch,
                                     recall_transition=recall_transition)

    def _validated_route(self, identity, tool, library_id, snapshot, *,
                         expected_route_epoch=None, expected_policy_epoch=None,
                         recall_transition=False):
        """Validate one decoded catalog snapshot and bind it to the request."""
        if not isinstance(identity, RequestIdentity):
            raise FoundationError('unauthorized')
        if not isinstance(tool, str) or tool not in TOOL_ACTIONS:
            raise FoundationError('unknown_tool')
        if snapshot is None:
            raise FoundationError('library_access_denied')
        if not isinstance(snapshot, AuthorizationSnapshot):
            raise FoundationError('catalog_unavailable')
        credential, library = snapshot.credential, snapshot.library
        if (not credential.usable(self.clock()) or credential.principal_id != identity.principal_id
                or credential.credential_id != identity.credential_id or credential.device_id != identity.device_id
                or credential.epoch != identity.credential_epoch
                or credential.principal_epoch != identity.principal_epoch
                or credential.device_epoch != identity.device_epoch):
            raise FoundationError('unauthorized')
        action = TOOL_ACTIONS[tool]
        if library.library_id != library_id or _RANK.get(snapshot.permission, 0) < _RANK[action]:
            raise FoundationError('library_access_denied')
        expected_authority = {'postgres': 'pg_active', 'sqlite': 'sqlite_active'}[library.backend]
        if library.acl_state == 'updating':
            # A transient ACL projection is observable only through the private
            # recall opt-in, after every identity/permission/library check above.
            if (not recall_transition or library.backend != 'postgres'
                    or action != 'read' or tool not in ('context_search', 'experience_recall')
                    or tool not in self.implemented_tools.get(library.backend, ())
                    or not library.active or library.authority_state != 'pg_active'):
                raise FoundationError('library_unavailable')
            return RecallWitness(identity, tool, library, snapshot.permission)
        if not library.active or library.authority_state != expected_authority or library.acl_state != 'ready':
            raise FoundationError('library_unavailable')
        if expected_route_epoch is not None and (type(expected_route_epoch) is not int or expected_route_epoch != library.route_epoch):
            raise FoundationError('route_epoch_changed')
        if expected_policy_epoch is not None and (type(expected_policy_epoch) is not int or expected_policy_epoch != library.policy_epoch):
            raise FoundationError('policy_epoch_changed')
        if action != 'read' and library.writer_backend != library.backend:
            raise FoundationError('write_authority_mismatch')
        if tool not in self.implemented_tools.get(library.backend, ()):
            raise FoundationError('library_backend_unsupported')
        return AuthorizedRoute(identity, library, tool, action)

    def revalidate(self, route):
        current = self.plan(route.identity, route.tool, library_id=route.library.library_id,
                            expected_route_epoch=route.library.route_epoch,
                            expected_policy_epoch=route.library.policy_epoch)
        if current != route:
            raise FoundationError('route_binding_changed')
        return current

    def plan_many(self, identity, tool, library_ids, *, recall_transition=False):
        if (tool not in ('memory_search', 'context_search', 'experience_recall') or not isinstance(library_ids, (list, tuple))
                or not 1 <= len(library_ids) <= 4 or any(not isinstance(x, str) for x in library_ids)
                or len(set(library_ids)) != len(library_ids)):
            raise FoundationError('invalid_library_selection')
        transition = bool(recall_transition and tool in ('context_search', 'experience_recall'))
        # The standalone path and its adapter port stay unchanged. Opted-in
        # recall always uses one preflight so cleanup precedes any witness.
        if len(library_ids) == 1 and not transition:
            return (self.plan(identity, tool, library_id=library_ids[0]),)
        preflight = getattr(self.catalog, 'authorization_preflight', None)
        if not callable(preflight):
            # Older/non-ControlCatalog adapters keep the per-library port.
            if transition:
                values = tuple(self._planned_route(identity, tool, library_id=value,
                    resolve=lambda selected: self.catalog.authorization_snapshot(identity, selected),
                    recall_transition=True) for value in library_ids)
                witnesses = tuple(value for value in values if isinstance(value, RecallWitness))
                if witnesses:
                    raise RecallTransientWitness(witnesses, tool, library_ids,
                        tuple(value for value in values if not isinstance(value, RecallWitness)))
                return values
            return tuple(self.plan(identity, tool, library_id=value) for value in library_ids)
        # Match plan()'s original order: identity and the first UUID are checked
        # before any control connection, then each later ID is resolved in turn.
        self._validate_plan_request(identity, tool, library_ids[0])
        try:
            with preflight(identity) as resolve:
                values = tuple(self._planned_route(identity, tool, library_id, resolve,
                                                   recall_transition=transition)
                               for library_id in library_ids)
        except FoundationError:
            raise
        except Exception:
            raise FoundationError('catalog_unavailable') from None
        # The preflight context has exited (and therefore cleaned up) before a
        # witness can be observed or repeated by the caller. Nothing partial is
        # returned while any requested library is updating.
        witnesses = tuple(value for value in values if isinstance(value, RecallWitness))
        if witnesses:
            raise RecallTransientWitness(witnesses, tool, library_ids,
                tuple(value for value in values if not isinstance(value, RecallWitness)))
        return values


class ConnectionRegistry:
    def __init__(self, bindings):
        entries = {}
        for binding in bindings:
            key = (binding.connection_ref, binding.principal_id, binding.credential_id,
                   binding.device_id, binding.library_id)
            if key in entries:
                raise ValueError('duplicate trusted connection binding')
            entries[key] = binding
        self._bindings = MappingProxyType(entries)

    def resolve(self, route):
        if route.library.backend != 'postgres':
            raise FoundationError('library_backend_unsupported')
        key = (route.library.connection_ref, route.identity.principal_id, route.identity.credential_id,
               route.identity.device_id, route.library.library_id)
        binding = self._bindings.get(key)
        if binding is None:
            raise FoundationError('connection_binding_unavailable')
        return binding
