"""Request-owned sessions. Catalog/authority adapters must be supplied explicitly.

The authority lease is a security requirement, not an optional callback. Its real
implementation must serialize credential, ACL and route changes against serving.
This candidate deliberately provides no no-op or in-memory production substitute.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ContextManager, Protocol
from .models import AuthorizedRoute, FoundationError
from ._projection_conflict import (ConfirmedBatchRollback, ConfirmedPreEffectRollback,
                                   PreEffectRowConflict, ProjectionBatchConflict,
                                   ProjectionBatchDeferred, ProjectionConflictDeferred,
                                   _ProjectionApplyOperation, _ProjectionBatchApplyOperation)


@dataclass(frozen=True, slots=True)
class DatabaseRole:
    session_user: str
    current_user: str
    superuser: bool
    bypass_rls: bool
    owns_protected_objects: bool
    row_security: bool
    can_create_role: bool = False
    can_create_database: bool = False
    has_memberships: bool = False


@dataclass(frozen=True, slots=True)
class ContentSessionState:
    principal_id: str
    library_id: str
    policy_epoch: int
    route_epoch: int
    permission: str | None
    authority_state: str
    acl_state: str


@dataclass(frozen=True, slots=True)
class DatabaseCredential:
    credential_id: str
    device_id: str
    credential_epoch: int


class _ProjectionPrepareOperation:
    """Private marker: one projection preparation read under write authority.

    The original write/admin route and its rank/role/RLS/credential/project
    checks are retained; only the transaction body is REPEATABLE READ READ ONLY
    and uses the non-locking session_context. It never enables apply deferral.
    """
    __slots__ = ('_callback',)

    def __init__(self, callback):
        self._callback = callback

    def __call__(self, session):
        return self._callback(session)


@dataclass(frozen=True, slots=True)
class _RemoveReady:
    lock_keys: tuple[int, ...]
    canonical_identity: str


@dataclass(frozen=True, slots=True)
class _RemoveDenied:
    outcome: dict


class AuthorityLease(Protocol):
    def hold(self, route: AuthorizedRoute) -> ContextManager[None]:
        """Pin all identity/ACL/authority dependencies through commit/publication.

        Revocation/route updates must take the conflicting lock and may declare
        completion only after old leases drain. A fresh SELECT alone is not a
        lease. Multi-library locks require a globally stable acquisition order.
        """
        ...


class TransactionConnection(Protocol):
    def begin(self, *, read_only: bool): ...
    def inspect_role(self) -> DatabaseRole: ...
    def session_context(self) -> ContentSessionState | None: ...
    def pin_session_context(self, policy_epoch, route_epoch) -> ContentSessionState | None: ...
    def session_credential(self) -> DatabaseCredential | None: ...
    def execute(self, sql: str, params=()): ...
    def commit(self): ...
    def rollback(self): ...
    def close(self): ...


class ConnectionProvider(Protocol):
    def open(self, binding) -> TransactionConnection: ...


class LibrarySession:
    """For trusted repositories only; never expose SQL execution in an MCP schema."""
    def __init__(self, route, connection):
        self.route = route
        self._connection = connection
        self._closed = False

    def execute(self, sql, params=()):
        if self._closed:
            raise FoundationError('session_closed')
        return self._connection.execute(sql, params)


class SessionManager:
    def __init__(self, router, registry, provider: ConnectionProvider, authority: AuthorityLease):
        if not callable(getattr(authority, 'hold', None)):
            raise ValueError('an authority lease coordinator is required')
        guard = getattr(authority, 'deadline_guard', None)
        if guard is not None and getattr(provider, 'deadline_guard', None) is not guard:
            raise ValueError('content provider and authority must share the request guard')
        self.router, self.registry = router, registry
        self.provider, self.authority = provider, authority

    @contextmanager
    def keyed(self, route, lock_keys, *, _prepare_remove=None):
        """One authority lease/physical connection across fresh transactions.

        Trusted repositories only. Session locks are acquired OUTSIDE a
        transaction after a committed authorization preflight. Consequently the
        business RR snapshot begins after lock acquisition, and loss of the lock
        connection also makes a business commit on that connection impossible.
        This connection must always close; it must never return to a pool.
        The transport must still bind this entire scope to cancel/quiesce.

        The private remove preparation reuses this same lease/connection for the
        locator before the independent read-only preflight and session locks.
        An early denial retires content before exposing its exact outcome; the
        caller may publish it under the still-held authority.
        """
        prepared_mode = _prepare_remove is not None
        if prepared_mode:
            if (lock_keys is not None or not callable(_prepare_remove)
                    or getattr(route, 'tool', None) != 'memory_remove'
                    or route.action not in ('write', 'admin')):
                raise ValueError('invalid keyed remove preparation scope')
        elif (route.action not in ('write', 'admin') or not isinstance(lock_keys, tuple)
                or not 1 <= len(lock_keys) <= 4
                or any(type(k) is not int or not -(2**63) <= k < 2**63 for k in lock_keys)):
            raise ValueError('invalid keyed transaction scope')
        keys = () if prepared_mode else tuple(sorted(set(lock_keys)))
        with self.authority.hold(route):
            self.router.revalidate(route)
            binding = self.registry.resolve(route)
            try:
                connection = self.provider.open(binding)
            except FoundationError:
                raise
            except Exception:
                raise FoundationError('database_connection_failed') from None
            scope = _KeyedTransactions(self, route, binding, connection)
            failed = False
            try:
                if prepared_mode:
                    # The locator keeps its ordinary READ WRITE transaction and
                    # true pin. Only this existing transaction may resolve the
                    # scoped ID to key and compute the lock material.
                    with scope.transaction() as session:
                        prepared = _prepare_remove(session)
                    if type(prepared) is _RemoveReady:
                        if (not isinstance(prepared.lock_keys, tuple)
                                or not 1 <= len(prepared.lock_keys) <= 4
                                or any(type(k) is not int or not -(2**63) <= k < 2**63
                                       for k in prepared.lock_keys)
                                or type(prepared.canonical_identity) is not str
                                or not prepared.canonical_identity):
                            raise ValueError('invalid keyed remove preparation result')
                        # Preserve the ordinary keyed derivation: the existing
                        # lock inputs are ordered and deduplicated before use.
                        keys = tuple(sorted(set(prepared.lock_keys)))
                    elif type(prepared) is _RemoveDenied:
                        if type(prepared.outcome) is not dict or prepared.outcome != {
                                'error': 'project_scope_unavailable'}:
                            raise ValueError('invalid keyed remove preparation result')
                        # Content retires before any publication. Cleanup is
                        # attempted exactly once and a successful locator commit
                        # keeps the original delivery-failure classification.
                        scope._content_retired = True
                        try:
                            connection.close()
                        except Exception:
                            raise FoundationError('write_committed_delivery_failed') from None
                        scope._early_denial = prepared.outcome
                        yield scope
                        return
                    else:
                        raise ValueError('invalid keyed remove preparation result')
                # Preserve the independent READ ONLY preflight in a fresh
                # snapshot. Never merge it with the locator transaction.
                with scope.transaction(_preflight=True):
                    pass
                connection.acquire_session_locks(keys)
                if prepared_mode:
                    scope._canonical_identity = prepared.canonical_identity
                yield scope
            except BaseException:
                failed = True
                raise
            finally:
                scope._closed = True
                if not scope._content_retired:
                    try:
                        connection.close()  # PostgreSQL releases all session locks.
                    except Exception:
                        if not failed:
                            raise FoundationError('write_committed_delivery_failed' if scope._committed
                                                  else 'database_close_failed') from None

    @contextmanager
    def open(self, route):
        """Repository scope only; for an HTTP response use run(..., publish=...)."""
        with self.authority.hold(route):
            with self._transaction(route) as session:
                yield session

    def _projection_apply(self, operation):
        """Trusted private opt-in for one native projection apply attempt."""
        if not callable(operation):
            raise ValueError('projection operation required')
        return _ProjectionApplyOperation(operation)

    def _projection_batch_apply(self, operation):
        """Trusted private opt-in for one bounded native projection batch.

        Only this exact marker may turn a batch conflict into a deferred sentinel,
        and only after full rollback, actual IDLE verification and close.
        """
        if not callable(operation):
            raise ValueError('projection batch operation required')
        return _ProjectionBatchApplyOperation(operation)

    def _projection_prepare(self, operation):
        """Trusted private opt-in for one native projection preparation read."""
        if not callable(operation):
            raise ValueError('projection preparation operation required')
        return _ProjectionPrepareOperation(operation)

    def run(self, route, operation, *, publish=None):
        """Keep the lease through response publication, including write commit.

        publish is the trusted transport writer, not a business callback. A
        publication failure after commit does NOT imply that the write failed;
        the eventual business adapter must resolve the same request_id receipt.

        Only the private projection wrapper opts into pre-effect row-conflict
        deferral. It must be a write/admin call with no publisher. The marker is
        consumed INSIDE the authority lease, then the lease is left normally so
        every post-yield authority check and cleanup still runs. A sentinel is
        returned only after that full successful scope exit; ordinary calls
        keep the pre-existing propagation and never recover automatically.

        The separate private preparation marker also requires write/admin and
        no publisher, but it never participates in conflict deferral. It runs
        the same authority/route/role/RLS/credential/project checks in a
        read-only transaction so preparation cannot be downgraded to read
        authorization.
        """
        defer = type(operation) is _ProjectionApplyOperation
        prepare = type(operation) is _ProjectionPrepareOperation
        batch = type(operation) is _ProjectionBatchApplyOperation
        if defer and (publish is not None or route.action not in ('write', 'admin')):
            raise ValueError('invalid projection conflict deferral scope')
        if prepare and (publish is not None or route.action not in ('write', 'admin')):
            raise ValueError('invalid projection preparation scope')
        if batch and (publish is not None or route.action not in ('write', 'admin')):
            raise ValueError('invalid projection batch scope')
        callback = operation._callback if prepare or batch else operation
        deferred = False
        batch_deferred = False
        with self.authority.hold(route):
            try:
                with self._transaction(route, projection_prepare=prepare,
                                       projection_batch=batch) as session:
                    result = callback(session)
            except ConfirmedPreEffectRollback:
                if not defer:
                    # Preserve the pre-existing redacted public outcome for
                    # callers that did not opt into projection deferral.
                    raise FoundationError('database_operation_failed') from None
                deferred = True
            except ConfirmedBatchRollback:
                if not batch:
                    # A batch-only marker in an ordinary operation is fatal and
                    # never becomes the older pre-effect deferral.
                    raise FoundationError('database_operation_failed') from None
                batch_deferred = True
            if not deferred and not batch_deferred:
                try:
                    self.router.revalidate(route)
                    if publish is not None:
                        publish(result)
                except Exception:
                    if route.action != 'read':
                        raise FoundationError('write_committed_delivery_failed') from None
                    raise
        if deferred:
            raise ProjectionConflictDeferred() from None
        if batch_deferred:
            raise ProjectionBatchDeferred() from None
        return result

    def run_many(self, routes, operation, *, combine, finalize=None, publish=None):
        """Bounded sequential reads; one real control lease covers publication.

        Each library uses its own dedicated read-only connection. Optional
        final content validation runs in fresh transactions, not the earlier
        candidate snapshots. These are not concurrent reads or a distributed
        content snapshot. Never use this method for cross-database writes.
        """
        if (not isinstance(routes, tuple) or not 1 <= len(routes) <= 4
                or any(not isinstance(route, AuthorizedRoute) or route.action != 'read' for route in routes)
                or not callable(getattr(self.authority, 'hold_many', None))
                or not callable(operation) or not callable(combine)
                or (finalize is not None and not callable(finalize))):
            raise FoundationError('invalid_read_routes')
        routes = tuple(sorted(routes, key=lambda route: route.library.library_id))
        with self.authority.hold_many(routes):
            gathered = []
            for route in routes:
                with self._transaction(route) as session:
                    gathered.append((route, operation(session)))
            if finalize is not None:
                validated = []
                for route, candidates in gathered:
                    with self._transaction(route) as session:
                        validated.append((route, finalize(session, candidates)))
                gathered = validated
            result = combine(tuple(gathered))
            for route in routes:
                self.router.revalidate(route)
            if publish is not None:
                publish(result)
            return result

    @staticmethod
    def _validate_role(role, binding):
        if (not isinstance(role, DatabaseRole) or role.session_user != binding.role
                or role.current_user != binding.role or role.superuser is not False
                or role.bypass_rls is not False or role.owns_protected_objects is not False
                or role.row_security is not True or role.can_create_role is not False
                or role.can_create_database is not False or role.has_memberships is not False):
            raise FoundationError('unsafe_database_role')

    @staticmethod
    def _validate_projection(state, route):
        rank = {'read': 1, 'write': 2, 'admin': 3}
        if (not isinstance(state, ContentSessionState)
                or state.principal_id != route.identity.principal_id
                or state.library_id != route.library.library_id
                or type(state.policy_epoch) is not int or state.policy_epoch != route.library.policy_epoch
                or type(state.route_epoch) is not int or state.route_epoch != route.library.route_epoch
                or rank.get(state.permission, 0) < rank[route.action]
                or state.authority_state != 'pg_active' or state.acl_state != 'ready'):
            raise FoundationError('database_authorization_mismatch')

    @contextmanager
    def _transaction(self, route, *, projection_prepare=False, projection_batch=False):
        self.router.revalidate(route)
        binding = self.registry.resolve(route)
        try:
            connection = self.provider.open(binding)
        except FoundationError:
            raise
        except Exception:
            raise FoundationError('database_connection_failed') from None
        session = LibrarySession(route, connection)
        failed = False
        committed = False
        close_required = True
        try:
            connection.begin(read_only=projection_prepare or route.action == 'read')
            self._validate_role(connection.inspect_role(), binding)
            state = (connection.session_context() if projection_prepare or route.action == 'read'
                     else connection.pin_session_context(route.library.policy_epoch,
                                                        route.library.route_epoch))
            self._validate_projection(state, route)
            credential = connection.session_credential()
            if (not isinstance(credential, DatabaseCredential)
                    or credential.credential_id != route.identity.credential_id
                    or credential.device_id != route.identity.device_id
                    or type(credential.credential_epoch) is not int
                    or credential.credential_epoch != route.identity.credential_epoch):
                raise FoundationError('database_credential_mismatch')
            yield session
            # Fresh control-db check: never rely on this content RR snapshot to
            # detect a committed revocation. The lease closes the final race.
            self.router.revalidate(route)
            self._validate_role(connection.inspect_role(), binding)
            try:
                connection.commit()
                committed = True
            except Exception:
                raise FoundationError('commit_result_unknown' if route.action != 'read'
                                      else 'database_commit_failed') from None
        except PreEffectRowConflict:
            # No COMMIT is attempted. This exact marker is eligible only after
            # BOTH rollback and content close succeed; otherwise the cleanup
            # failure is fatal and no retry/deferral sentinel is produced.
            failed = True
            rollback_ok = close_ok = False
            try:
                connection.rollback()
                rollback_ok = True
            except Exception:
                pass
            try:
                connection.close()
                close_ok = True
            except Exception:
                pass
            close_required = False
            session._closed = True
            if not (rollback_ok and close_ok):
                raise FoundationError('projection_conflict_cleanup_failed') from None
            raise ConfirmedPreEffectRollback() from None
        except ProjectionBatchConflict:
            # Only the exact private batch operation may convert this marker.
            # Conversion requires full ROLLBACK, actual driver IDLE verification
            # and content close to all succeed. Anything less stays fatal and
            # never produces a retry/deferral sentinel.
            failed = True
            if not projection_batch:
                # A forged/ordinary/unknown marker is fatal. Keep the normal
                # finally close path so exactly one close is still attempted;
                # a close failure must never replace the primary fatal outcome.
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise FoundationError('database_operation_failed') from None
            # Keep the normal finally close armed until the explicit close
            # below actually starts, so an interrupted rollback/IDLE check
            # (BaseException) still attempts exactly one close and keeps the
            # original fault object/type.
            session._closed = True
            rolled_back = False
            try:
                connection._rollback_projection_batch()
                rolled_back = True
            except Exception:
                rolled_back = False
            close_required = False
            closed = False
            try:
                connection.close()
                closed = True
            except Exception:
                closed = False
            if not rolled_back:
                raise FoundationError('projection_batch_rollback_failed') from None
            if not closed:
                raise FoundationError('projection_batch_close_failed') from None
            raise ConfirmedBatchRollback() from None
        except BaseException:
            failed = True
            try:
                connection.rollback()
            except Exception:
                pass  # Preserve the primary/unknown-commit outcome; never retry.
            raise
        finally:
            session._closed = True
            if close_required:
                try:
                    connection.close()
                except Exception:
                    if not failed:
                        raise FoundationError('write_committed_delivery_failed' if committed and route.action != 'read'
                                              else 'database_close_failed') from None


class _KeyedTransactions:
    """Not transferable across routes or reusable outside manager.keyed()."""
    def __init__(self, manager, route, binding, connection):
        self.manager, self.route, self.binding, self.connection = manager, route, binding, connection
        self._closed = self._active = self._poisoned = self._committed = False
        self._content_retired = False
        self._early_denial = None
        self._canonical_identity = None

    @property
    def early_denial(self):
        return self._early_denial

    @property
    def canonical_identity(self):
        return self._canonical_identity

    @contextmanager
    def transaction(self, *, read_only=False, _preflight=False):
        if self._closed or self._active or self._poisoned or self._content_retired:
            raise FoundationError('session_closed')
        self._active = True
        manager, route, connection = self.manager, self.route, self.connection
        session = LibrarySession(route, connection)
        try:
            manager.router.revalidate(route)
            # Reject changed principal/device/connection routing, even if an
            # incorrectly implemented external coordinator let it drift.
            if manager.registry.resolve(route) != self.binding:
                raise FoundationError('database_authorization_mismatch')
            connection.begin(read_only=_preflight or read_only)
            manager._validate_role(connection.inspect_role(), self.binding)
            state = (connection.session_context() if _preflight or read_only else
                     connection.pin_session_context(route.library.policy_epoch, route.library.route_epoch))
            manager._validate_projection(state, route)
            credential = connection.session_credential()
            if (not isinstance(credential, DatabaseCredential)
                    or credential.credential_id != route.identity.credential_id
                    or credential.device_id != route.identity.device_id
                    or type(credential.credential_epoch) is not int
                    or credential.credential_epoch != route.identity.credential_epoch):
                raise FoundationError('database_credential_mismatch')
            yield session
            manager.router.revalidate(route)
            manager._validate_role(connection.inspect_role(), self.binding)
            try:
                connection.commit()
                if not (_preflight or read_only):
                    self._committed = True
            except Exception:
                raise FoundationError('database_commit_failed' if _preflight or read_only else 'commit_result_unknown') from None
        except BaseException:
            # Never reuse a connection after any phase failure, even a definite
            # rollback. The receipt remains indeterminate for reconciliation.
            self._poisoned = True
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            session._closed = True
            self._active = False

    def publish(self, result, callback=None):
        if self._closed or self._active or self._poisoned:
            raise FoundationError('session_closed')
        try:
            self.manager.router.revalidate(self.route)
            if callback is not None:
                callback(result)
        except Exception:
            raise FoundationError('write_committed_delivery_failed') from None
        return result


class PsycopgDirectProvider:
    """Optional psycopg3 adapter: inject psycopg.connect and a trusted vault reader.

    No package import, connection, secret resolution or filesystem access occurs
    at construction. Each request opens/closes its own connection; no pool can
    accidentally reuse another principal's session. TLS is required by default.
    """
    def __init__(self, connector, secret_resolver, *, deadline_guard=None):
        self._connector, self._secret_resolver = connector, secret_resolver
        if deadline_guard is not None:
            from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
            if not isinstance(deadline_guard, SyncRequestDeadlineGuard):
                raise ValueError('concrete synchronous deadline guard required')
        self.deadline_guard = deadline_guard

    def open(self, binding):
        secret = None
        try:
            if self.deadline_guard is not None:
                self.deadline_guard.check()
            secret = self._secret_resolver(binding.password_ref)
            if not isinstance(secret, str) or not secret:
                raise ValueError('secret reference is unavailable')
            kwargs = dict(host=binding.host, port=binding.port, dbname=binding.database,
                          user=binding.role, password=secret, connect_timeout=5,
                          autocommit=True, sslmode='verify-full')
            if self.deadline_guard is None:
                raw = self._connector(**kwargs)
            else:
                from evolvmem.library_deadline.driver import connect
                raw = connect(self.deadline_guard, self._connector, kwargs, kind='content',
                              statement_ms=10000, lock_ms=10000)
            return _PsycopgConnection(raw)
        except FoundationError:
            raise
        except Exception:
            raise FoundationError('database_connection_failed') from None
        finally:
            secret = None


class _PsycopgConnection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        try:
            with self._raw.cursor() as cursor:
                cursor.execute(sql, params)
                return tuple(cursor.fetchall()) if cursor.description is not None else ()
        except Exception:
            raise FoundationError('database_operation_failed') from None

    def acquire_session_locks(self, keys):
        # psycopg/libpq transaction status IDLE is 0. Locking inside a prior RR
        # transaction would freeze an obsolete snapshot, so fail closed.
        if (self._raw.autocommit is not True or int(self._raw.info.transaction_status) != 0
                or not isinstance(keys, tuple) or not 1 <= len(keys) <= 4
                or any(type(key) is not int or not -(2**63) <= key < 2**63 for key in keys)
                or keys != tuple(sorted(set(keys)))):
            raise FoundationError('invalid_session_lock_state')
        # This is an autocommit statement, so SET LOCAL would not cover waiting.
        # Same 10s statement bound as ordinary transactions; no blind retry.
        self.execute("SELECT pg_catalog.set_config('statement_timeout', '10000', false)")
        for key in keys:
            self.execute('SELECT pg_catalog.pg_advisory_lock(%s)', (key,))

    def begin(self, *, read_only):
        mode = 'READ ONLY' if read_only else 'READ WRITE'
        self.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ ' + mode)
        self.execute(
            "SELECT pg_catalog.set_config('statement_timeout', '10000', true), "
            "pg_catalog.set_config('idle_in_transaction_session_timeout', '15000', true)")

    def inspect_role(self):
        rows = self.execute("""
            SELECT session_user, current_user, r.rolsuper, r.rolbypassrls,
                   (EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                             WHERE n.nspname IN ('em_security','em_content')
                               AND pg_catalog.pg_has_role(session_user,c.relowner,'MEMBER'))
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
                                WHERE n.nspname IN ('em_security','em_content')
                                  AND pg_catalog.pg_has_role(session_user,n.nspowner,'MEMBER'))),
                   pg_catalog.current_setting('row_security') = 'on', r.rolcreaterole, r.rolcreatedb,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid)
              FROM pg_catalog.pg_roles r WHERE r.rolname=session_user
        """)
        if len(rows) != 1:
            raise FoundationError('unsafe_database_role')
        return DatabaseRole(*rows[0])

    def session_context(self):
        rows = self.execute('SELECT principal_id, library_id, policy_epoch, route_epoch, '
                            'permission, authority_state, acl_state FROM em_security.session_context()')
        if len(rows) != 1:
            return None
        principal, library, policy, route, permission, authority, acl = rows[0]
        return ContentSessionState(str(principal), str(library), policy, route, permission, authority, acl)

    def pin_session_context(self, policy_epoch, route_epoch):
        rows = self.execute('SELECT principal_id, library_id, policy_epoch, route_epoch, '
                            'permission, authority_state, acl_state FROM '
                            'em_security.pin_session_context(%s,%s)', (policy_epoch, route_epoch))
        if len(rows) != 1:
            return None
        principal, library, policy, route, permission, authority, acl = rows[0]
        return ContentSessionState(str(principal), str(library), policy, route, permission, authority, acl)

    def commit(self):
        self._raw.commit()  # SessionManager classifies a failed write commit as unknown.

    def session_credential(self):
        rows = self.execute('SELECT credential_id, authenticated_device_id, credential_epoch '
                            'FROM em_security.session_credential()')
        if len(rows) != 1:
            return None
        credential, device, epoch = rows[0]
        return DatabaseCredential(str(credential), str(device), epoch)

    def rollback(self):
        self._raw.rollback()

    def _rollback_projection_batch(self):
        """Full rollback plus actual libpq IDLE verification for one batch.

        A truthy return or a merely successful method call is not proof: the
        driver's real transaction status must be IDLE before close/deferral.
        """
        from psycopg.pq import TransactionStatus
        self._raw.rollback()
        if self._raw.info.transaction_status != TransactionStatus.IDLE:
            raise FoundationError('projection_batch_rollback_not_idle')

    def close(self):
        self._raw.close()
