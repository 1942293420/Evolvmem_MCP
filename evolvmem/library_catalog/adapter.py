from contextlib import ExitStack, contextmanager
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import re
import time
from uuid import UUID

from evolvmem.library_foundation import (AuthorizedRoute, AuthorizationSnapshot, CredentialBinding, FoundationError,
                           LibraryDescriptor, RequestIdentity)


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise FoundationError('invalid_arguments')
    return value


def _identity(identity):
    if not isinstance(identity, RequestIdentity):
        raise FoundationError('unauthorized')
    return (identity.credential_id, identity.principal_id, identity.device_id,
            identity.credential_epoch, identity.principal_epoch, identity.device_epoch)


class _ControlPinSqlRefusal(FoundationError):
    """Exact pin_library query execution saw 42501 (private transport marker)."""
    def __init__(self):
        super().__init__('control_access_denied')
        self.kind = 'pin_sql_control_access_denied'


class RecallPinRefusal(FoundationError):
    """Private pre-pin transition marker; never a new public MCP error code.

    Emitted only after the pinned transaction context exited normally, so the
    existing rollback/close failure path (control_cleanup_failed) wins over a
    transient observation. ``code`` remains the original stable public code.
    """
    def __init__(self, routes, failed_route=None):
        super().__init__('authority_changed')
        self.routes = tuple(routes)
        if failed_route is None and len(self.routes) == 1:
            failed_route = self.routes[0]
        if failed_route not in self.routes:
            failed_route = None
        self.failed_route = failed_route
        self.failed_library_id = (None if failed_route is None
                                  else failed_route.library.library_id)
        self.kind = 'recall_prepin_control_access_denied'


@dataclass(frozen=True, slots=True)
class ControlBinding:
    host: str
    port: int
    database: str
    role: str
    password_ref: str = field(repr=False)

    def __post_init__(self):
        for value in (self.database, self.role, self.password_ref):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', value):
                raise ValueError('trusted configured identifier/reference required')
        if not isinstance(self.host, str) or not self.host or any(c.isspace() for c in self.host):
            raise ValueError('invalid configured host')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('invalid configured port')


@dataclass(frozen=True, slots=True)
class ExternalDeviceBinding:
    external_device_id: str
    device_id: str


@dataclass(frozen=True, slots=True)
class LibraryListing:
    library_id: str
    kind: str
    backend: str
    permission: str
    policy_epoch: int
    route_epoch: int
    authority_state: str
    acl_state: str


class ControlSession:
    def __init__(self, raw, binding):
        self.raw, self.binding = raw, binding
        self.broken = False
        self._pin_sql = False

    def ensure_transaction(self):
        try:
            valid = (not self.broken and not self.raw.closed and self.raw.autocommit is True
                     and self.raw.info.transaction_status.name == 'INTRANS')
        except Exception:
            valid = False
        if not valid:
            self.broken = True
            raise FoundationError('authority_lease_lost')

    def execute(self, sql, params=(), *, outside_transaction=False):
        if not outside_transaction:
            self.ensure_transaction()
        pin_sql = self._pin_sql
        pin_refusal = False
        try:
            with self.raw.cursor() as cursor:
                try:
                    cursor.execute(sql, params)
                except Exception as exc:
                    if pin_sql and getattr(exc, 'sqlstate', None) == '42501':
                        pin_refusal = True
                    else:
                        raise
                if not pin_refusal:
                    if cursor.description is None:
                        return ()
                    names = [column.name for column in cursor.description]
                    return tuple(dict(zip(names, values)) for values in cursor.fetchall())
        except FoundationError:
            raise
        except Exception as exc:
            self.broken = True
            state = getattr(exc, 'sqlstate', None)
            code = ('control_busy' if state in ('55P03', '57014', '40P01', '40001') else
                    'control_access_denied' if state == '42501' else 'catalog_unavailable')
            raise FoundationError(code) from None
        # Only a cursor that exited normally can turn the exact pin execute
        # refusal into the private marker. A close/fetch failure above never
        # reaches this point and keeps the existing non-replayable mapping.
        if pin_refusal:
            self.broken = True
            raise _ControlPinSqlRefusal() from None

    def begin(self, *, read_only, lock_timeout_ms, statement_timeout_ms, reused=False):
        mode = 'READ ONLY' if read_only else 'READ WRITE'
        self.execute('BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED ' + mode,
                     outside_transaction=True)
        # One round trip for four independent, fixed local settings. The bounded
        # lock/statement waits are transaction-local; the idle/transaction
        # timeouts are pinned to 0. Do not silently expire the row locks during
        # content work/publication: HTTP/request deadlines must be enforced by
        # the host, not a detached DB timeout which would free authority while
        # another database can commit.
        self.execute(
            "SELECT pg_catalog.set_config('lock_timeout', %s, true), "
            "pg_catalog.set_config('statement_timeout', %s, true), "
            "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, true), "
            "pg_catalog.set_config('transaction_timeout', %s, true)",
            (str(lock_timeout_ms) + 'ms', str(statement_timeout_ms) + 'ms', '0', '0'))
        role_rows = self.execute('''
            SELECT session_user, current_user, r.rolsuper AS superuser,
                   r.rolbypassrls AS bypass_rls, r.rolcreaterole AS create_role,
                   r.rolcreatedb AS create_db,
                   r.rolcanlogin AS login_enabled,
                   (r.rolvaliduntil IS NULL OR r.rolvaliduntil > pg_catalog.now()) AS nonexpired,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS memberships,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='em_control' AND c.relkind IN ('r','p','v','m','f')
                       AND pg_catalog.has_table_privilege(r.oid,c.oid,
                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) AS direct_access,
                   (EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname='em_control'
                              AND pg_catalog.pg_has_role(session_user,n.nspowner,'MEMBER'))
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='em_control'
                          AND pg_catalog.pg_has_role(session_user,c.relowner,'MEMBER'))) AS owns_protected
            FROM pg_catalog.pg_roles r WHERE r.rolname=session_user
        ''')
        if len(role_rows) != 1:
            raise FoundationError('unsafe_control_role')
        role = role_rows[0]
        if (role.get('session_user') != self.binding.role or role.get('current_user') != self.binding.role
                or any(role.get(k) is not False for k in ('superuser', 'bypass_rls', 'create_role',
                                                        'create_db', 'memberships', 'owns_protected', 'direct_access'))):
            raise FoundationError('unsafe_control_role')
        # LOGIN/password-expiry are re-attested only for a reused service
        # segment (a fresh segment just authenticated physically).  The values
        # come from the same immediately preceding bounded role statement, so
        # there is no cached result, extra round trip or invalidated timeout
        # record.  Missing/null/non-boolean values fail closed.
        if reused and (role.get('login_enabled') is not True
                       or role.get('nonexpired') is not True):
            self.broken = True
            raise FoundationError('unsafe_control_role')

    def commit(self):
        self.ensure_transaction()
        try:
            self.raw.commit()
        except Exception:
            self.broken = True
            raise FoundationError('control_commit_result_unknown') from None


class ControlProvider:
    """One non-owner psycopg3 connection per operation/lease, bounded SQL waits."""
    def __init__(self, binding, connector, secret_resolver, *, lock_timeout_ms=500, statement_timeout_ms=2000,
                 deadline_guard=None, control_request_reuse=False):
        if not isinstance(binding, ControlBinding):
            raise ValueError('trusted ControlBinding required')
        if (type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 5000
                or type(statement_timeout_ms) is not int or not lock_timeout_ms < statement_timeout_ms <= 30000):
            raise ValueError('invalid bounded control timeout')
        self.binding, self.connector, self.secret_resolver = binding, connector, secret_resolver
        self.lock_timeout_ms, self.statement_timeout_ms = lock_timeout_ms, statement_timeout_ms
        if deadline_guard is not None:
            from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
            if not isinstance(deadline_guard, SyncRequestDeadlineGuard):
                raise ValueError('concrete synchronous deadline guard required')
        if type(control_request_reuse) is not bool:
            raise ValueError('exact control request reuse opt-in required')
        self._reuse = None
        if control_request_reuse:
            # Opt-in only with the concrete synchronous guard; the per-request
            # store is owned by this provider instance and closed by its host.
            from evolvmem.library_deadline.control_reuse import ControlReuseStore
            if deadline_guard is None:
                raise ValueError('control request reuse requires a concrete deadline guard')
            self._reuse = ControlReuseStore(deadline_guard)
        self.deadline_guard = deadline_guard

    def close_reuse(self):
        """Physical close of the one idle raw; call before guard scope exits."""
        if self._reuse is not None:
            self._reuse.close()

    @contextmanager
    def open(self, *, read_only=True):
        from evolvmem.library_deadline import control_reuse
        raw, session, failed = None, None, False
        token = None
        store = self._reuse
        reused = False
        try:
            try:
                if self.deadline_guard is not None:
                    self.deadline_guard.check()
                secret = self.secret_resolver(self.binding.password_ref)
                if not isinstance(secret, str) or not secret:
                    raise ValueError('unavailable secret')
                kwargs = dict(host=self.binding.host, port=self.binding.port,
                    dbname=self.binding.database, user=self.binding.role, password=secret,
                    autocommit=True, connect_timeout=3, sslmode='verify-full')
                if self.deadline_guard is None:
                    raw = self.connector(**kwargs)
                else:
                    from evolvmem.library_deadline.driver import connect
                    connector = self.connector
                    connect_kwargs = dict(kind='control',
                                          statement_ms=self.statement_timeout_ms,
                                          lock_ms=self.lock_timeout_ms)
                    if store is not None:
                        # Resolve the current secret once, before the connect.
                        # Checkout happens INSIDE driver.connect's guarded
                        # operation: if connect rejects first, the idle raw
                        # stays in the store for the request finally to close.
                        token = control_reuse.fingerprint(self.binding, secret)
                        source = {'reused': False}

                        def connector(**ignored):
                            idle = store.take(token)
                            if idle is not None:
                                source['reused'] = True
                                return idle
                            return self.connector(**ignored)

                        # Keyword present only for the opt-in path so the
                        # default-off driver call signature is unchanged.
                        connect_kwargs['prepare_none'] = True
                    raw = connect(self.deadline_guard, connector, kwargs, **connect_kwargs)
                    if store is not None:
                        reused = source['reused']
            except FoundationError:
                raise
            except Exception:
                raise FoundationError('catalog_unavailable') from None
            finally:
                secret = None
            session = ControlSession(raw, self.binding)
            session.begin(read_only=read_only, lock_timeout_ms=self.lock_timeout_ms,
                          statement_timeout_ms=self.statement_timeout_ms, reused=reused)
            yield session
        except BaseException:
            failed = True
            raise
        finally:
            if raw is not None:
                self._finish(raw, session, failed=failed, store=store, token=token)

    def _finish(self, raw, session, *, failed, store, token):
        """Always retire unless a fully successful borrow was recycled."""
        cleanup_error = None
        rolled_back = False
        try:
            raw.rollback()  # read/pin transactions have no committed effects
            rolled_back = True
        except BaseException as exc:
            cleanup_error = exc
        recycled = False
        if (cleanup_error is None and store is not None and not failed
                and rolled_back and session is not None and not session.broken):
            try:
                recycled = self._recycle(raw, store, token)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            if not recycled:
                raw.close()  # physical, even if the recycle path raised
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is None:
            return
        if failed:
            # The body's BaseException is already propagating; never replace it.
            return
        if isinstance(cleanup_error, Exception):
            raise FoundationError('control_cleanup_failed') from None
        raise cleanup_error

    @staticmethod
    def _recycle(wrapper, store, token):
        """Success-only recycle of this wrapper's raw; never a replay path."""
        guard = wrapper.guard
        try:
            guard.check()
        except FoundationError:
            return False
        if (wrapper.closed or wrapper._faulty or wrapper._cancel_sticky
                or wrapper._open_cursors):
            return False
        raw = wrapper._raw
        try:
            state = guard._state()
            with state.lock:
                # Recheck expiry/cancel and confirm rollback left an IDLE
                # physical session before releasing authority protection.
                guard._check_locked(state)
                if int(raw.info.transaction_status) != 0:
                    return False
                wrapper.protected = False
        except FoundationError:
            return False
        # Ordinary driver cursor: bounded timeout setter runs, and the reset
        # cursor is closed explicitly by the guard wrapper.
        with wrapper.cursor() as cursor:
            cursor.execute('DISCARD ALL')
        if int(raw.info.transaction_status) != 0:
            return False
        try:
            # Unregister first: a cancelled guard joins the watchdog here, so
            # an already-started direct cancel_safe has finished before detach.
            guard._unregister(wrapper)
        except BaseException:
            wrapper._fault()
            raise
        if wrapper._cancel_sticky or wrapper._faulty:
            return False
        detached = wrapper.detach()
        if detached is None:
            return False
        return store.put_after_guard_check(token, detached)


@dataclass(slots=True)
class _Held:
    catalog: object
    route: object
    session: ControlSession
    routes: tuple = ()


_HELD = ContextVar('evolvmem_control_authority_lease', default=None)


def _credential(row):
    expiration = row['expires_at']
    if isinstance(expiration, datetime):
        if expiration.tzinfo is None:
            raise ValueError('timezone-aware expiry required')
        expiration = expiration.timestamp()
    digest = row['credential_digest']
    if isinstance(digest, (bytes, bytearray, memoryview)):
        digest = bytes(digest).hex()
    return CredentialBinding(credential_id=str(row['credential_id']), principal_id=str(row['principal_id']),
        device_id=str(row['device_id']), token_digest=digest, epoch=row['credential_epoch'],
        active=row['credential_active'], principal_active=row['principal_active'], device_active=row['device_active'],
        expires_at=expiration, principal_epoch=row['principal_epoch'], device_epoch=row['device_epoch'])


def _snapshot(row):
    optional_uuid = lambda value: None if value is None else str(value)
    library = LibraryDescriptor(library_id=str(row['library_id']), kind=row['kind'], backend=row['backend'],
        connection_ref=row['connection_ref'], policy_epoch=row['policy_epoch'], route_epoch=row['route_epoch'],
        authority_state=row['authority_state'], acl_state=row['acl_state'], writer_backend=row['write_backend'],
        project_id=optional_uuid(row['project_id']), device_id=optional_uuid(row['library_device_id']),
        owner_principal_id=optional_uuid(row['owner_principal_id']), active=row['library_active'])
    return AuthorizationSnapshot(_credential(row), library, row['permission'])


_AUTHORIZATION_SQL = 'SELECT * FROM em_control.resolve_library(%s,%s,%s,%s,%s,%s,%s)'


def _decode_snapshot(rows):
    if not rows:
        return None
    try:
        if len(rows) != 1:
            raise ValueError('ambiguous authorization')
        return _snapshot(rows[0])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise FoundationError('catalog_unavailable') from None


class ControlCatalog:
    def __init__(self, provider):
        self.provider = provider

    def _query(self, sql, params, *, identity=None, library_id=None):
        held = _HELD.get()
        if held is not None:
            if (held.catalog is not self or identity != held.route.identity
                    or (library_id is not None and library_id not in
                        {route.library.library_id for route in held.routes or (held.route,)})):
                raise FoundationError('authority_lease_scope_mismatch')
            # A failed/lost pin must never silently open an unlocked substitute.
            held.session.ensure_transaction()
            return held.session.execute(sql, params)
        with self.provider.open() as session:
            return session.execute(sql, params)

    def credential_for_digest(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise FoundationError('invalid_arguments')
        rows = self._query('SELECT * FROM em_control.auth_binding(%s)', (bytes.fromhex(digest),))
        if not rows:
            return None
        try:
            if len(rows) != 1:
                raise ValueError('ambiguous credential')
            return _credential(rows[0])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise FoundationError('catalog_unavailable') from None

    def authorization_snapshot(self, identity, library_id):
        rows = self._query(_AUTHORIZATION_SQL, _identity(identity) + (_uuid(library_id),),
                           identity=identity, library_id=library_id)
        return _decode_snapshot(rows)

    @contextmanager
    def authorization_preflight(self, identity):
        """Yield a sequential resolver backed by one short read-only session.

        The router keeps the original per-library validation order. A held
        authority lease must stay on its already-scoped session, so that branch
        resolves through the existing single-snapshot scope checks instead.
        """
        identity_fields = _identity(identity)
        if _HELD.get() is not None:
            yield lambda library_id: self.authorization_snapshot(identity, library_id)
            return
        body_failed = False
        try:
            with ExitStack() as stack:
                try:
                    session = stack.enter_context(self.provider.open())
                except Exception:
                    raise FoundationError('catalog_unavailable') from None

                def resolve(library_id):
                    rows = session.execute(_AUTHORIZATION_SQL,
                                           identity_fields + (_uuid(library_id),))
                    return _decode_snapshot(rows)

                try:
                    yield resolve
                except BaseException:
                    body_failed = True
                    raise
        except FoundationError:
            # Preserve an already-validated denial/failure from the body, but
            # normalize provider open/cleanup failures to the preflight error.
            if body_failed:
                raise
            raise FoundationError('catalog_unavailable') from None
        except Exception:
            if body_failed:
                raise
            raise FoundationError('catalog_unavailable') from None

    def resolve_external_device(self, identity, external_device_id):
        if not isinstance(external_device_id, str) or not 1 <= len(external_device_id) <= 256:
            raise FoundationError('device_binding_mismatch')
        rows = self._query('SELECT * FROM em_control.resolve_external_device(%s,%s,%s,%s,%s,%s,%s)',
                           _identity(identity) + (external_device_id,), identity=identity)
        if (len(rows) != 1 or str(rows[0].get('device_id')) != identity.device_id
                or rows[0].get('external_device_id') != external_device_id):
            raise FoundationError('device_binding_mismatch')
        return ExternalDeviceBinding(external_device_id, identity.device_id)

    def list_libraries(self, identity, *, limit=100, after_library_id=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FoundationError('invalid_arguments')
        if after_library_id is not None:
            _uuid(after_library_id)
        rows = self._query('SELECT * FROM em_control.list_libraries(%s,%s,%s,%s,%s,%s,%s,%s)',
            _identity(identity) + (limit, after_library_id), identity=identity)
        try:
            if len(rows) > limit:
                raise ValueError('unbounded catalog result')
            return tuple(LibraryListing(str(r['library_id']), r['kind'], r['backend'], r['permission'],
                r['policy_epoch'], r['route_epoch'], r['authority_state'], r['acl_state']) for r in rows)
        except (KeyError, TypeError, ValueError):
            raise FoundationError('catalog_unavailable') from None


class RequestDeadlineGuard(ABC):
    """Host's enforced full-request deadline, not a boolean readiness flag.

    Enforce cancellation across connection establishment, content execution,
    commit and HTTP output. quiesce stops local business/output work and retires
    resources before normal authority cleanup; no detached business worker may
    continue. It does NOT prove a remote COMMIT rolled back after a lost reply.
    Preserve unknown outcomes for the atomic result receipt; never blindly
    repeat the effect. Same-transaction content row fencing orders ACL projection
    against a still-running remote write. Network failure can independently
    release control locks. No Python watchdog promises a hard wall-clock bound
    for arbitrary synchronous driver/OS stalls or waits forever to hide them.
    """
    @property
    @abstractmethod
    def timeout_seconds(self): ...

    @abstractmethod
    def enforce(self): ...  # context manager; starts watchdog/cancellation scope

    @abstractmethod
    def check(self): ...

    @abstractmethod
    def begin_authority(self):
        """Rearm a sequential lease only after full prior cleanup, preserving deadline."""

    @abstractmethod
    def quiesce(self, *, cancel): ...


class ControlAuthorityLease:
    def __init__(self, catalog, *, deadline_guard):
        if (not isinstance(deadline_guard, RequestDeadlineGuard)
                or isinstance(deadline_guard.timeout_seconds, bool)
                or not isinstance(deadline_guard.timeout_seconds, (int, float))
                or not 0 < deadline_guard.timeout_seconds <= 300):
            raise ValueError('enforced bounded complete-request deadline adapter required')
        self.catalog = catalog
        self.deadline_guard = deadline_guard
        if getattr(catalog.provider, 'deadline_guard', None) is not deadline_guard:
            raise ValueError('control provider and authority must share the request guard')

    @contextmanager
    def hold(self, route):
        with self.hold_many((route,)):
            yield

    @contextmanager
    def hold_many(self, routes):
        if (not isinstance(routes, tuple) or not 1 <= len(routes) <= 4
                or any(not isinstance(route, AuthorizedRoute) or not isinstance(route.library, LibraryDescriptor)
                       or not isinstance(route.identity, RequestIdentity) for route in routes)
                or any(route.identity != routes[0].identity for route in routes)
                or len({route.library.library_id for route in routes}) != len(routes)
                or (len(routes) > 1 and any(route.action != 'read' for route in routes))):
            raise FoundationError('invalid_authority_routes')
        routes = tuple(sorted(routes, key=lambda route: route.library.library_id))
        with self.deadline_guard.enforce():
            self.deadline_guard.check()
            self.deadline_guard.begin_authority()
            with self._hold_routes(routes):
                failed = False
                try:
                    self.deadline_guard.check()
                    yield
                    self.deadline_guard.check()
                except BaseException:
                    failed = True
                    raise
                finally:
                    # Local work/publication ends before normal lock cleanup.
                    # An unacknowledged remote COMMIT remains unknown; content
                    # fencing, not indefinite quiesce, orders ACL projection.
                    try:
                        self.deadline_guard.quiesce(cancel=failed)
                    except Exception:
                        if not failed:
                            raise
                        # Preserve commit_result_unknown (or the original
                        # failure); cleanup failure never makes replay safe.

    @contextmanager
    def _hold(self, route):
        with self._hold_routes((route,)):
            yield

    @staticmethod
    def _pin(session, route):
        params = _identity(route.identity) + (route.library.library_id,
            route.library.policy_epoch, route.library.route_epoch, route.action)
        try:
            if isinstance(session, ControlSession):
                session._pin_sql = True
            try:
                rows = session.execute('SELECT * FROM em_control.pin_library(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', params)
            finally:
                if isinstance(session, ControlSession):
                    session._pin_sql = False
            if len(rows) != 1:
                raise FoundationError('authority_changed')
            pinned = _snapshot(rows[0])
            if (pinned.library != route.library or pinned.credential.credential_id != route.identity.credential_id
                    or pinned.credential.principal_id != route.identity.principal_id
                    or pinned.credential.device_id != route.identity.device_id
                    or pinned.credential.epoch != route.identity.credential_epoch
                    or pinned.credential.principal_epoch != route.identity.principal_epoch
                    or pinned.credential.device_epoch != route.identity.device_epoch
                    or not pinned.credential.usable(time.time())
                    or {'read': 1, 'write': 2, 'admin': 3}.get(pinned.permission, 0)
                       < {'read': 1, 'write': 2, 'admin': 3}.get(route.action, 4)):
                raise FoundationError('authority_changed')
        except FoundationError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError):
            raise FoundationError('authority_changed') from None

    @contextmanager
    def _hold_routes(self, routes):
        if _HELD.get() is not None:
            raise FoundationError('authority_lease_already_held')
        entered = False
        body_failed = False
        refusal = None
        recall_routes = all(route.action == 'read' and route.tool in
                            ('context_search', 'experience_recall') for route in routes)
        try:
            with self.catalog.provider.open(read_only=False) as session:
                for route in routes:
                    self.deadline_guard.check()
                    try:
                        self._pin(session, route)
                    except FoundationError as exc:
                        # Only the exact pre-yield 42501 raised by the pin SQL
                        # execute (never cursor/fetch/cleanup/BEGIN/other SQL)
                        # is a candidate transition. Keep the value; exit this
                        # context normally so rollback/close must succeed before
                        # the host may observe again.
                        if isinstance(exc, _ControlPinSqlRefusal) and recall_routes:
                            refusal = RecallPinRefusal(routes, failed_route=route)
                            break
                        raise
                if refusal is None:
                    session.raw.protect_authority()
                    token = _HELD.set(_Held(self.catalog, routes[0], session, routes))
                    entered = True
                    try:
                        yield
                        session.ensure_transaction()
                        session.execute('SELECT 1 AS alive')
                        session.ensure_transaction()
                    except BaseException:
                        body_failed = True
                        raise
                    finally:
                        _HELD.reset(token)
        except FoundationError as exc:
            if entered and not body_failed and any(route.action != 'read' for route in routes):
                raise FoundationError('commit_result_unknown') from None
            if not entered and exc.code == 'control_busy':
                raise FoundationError('authority_lease_busy') from None
            if not entered and exc.code == 'control_access_denied':
                raise FoundationError('authority_changed') from None
            raise
        # Reached only after the provider context retired/rolled back normally.
        if refusal is not None:
            raise refusal
