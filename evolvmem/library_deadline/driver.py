"""Actual synchronous Psycopg resource registration; no background SQL."""
from dataclasses import dataclass
import math
import threading

from evolvmem.library_deadline import timeout_reuse
from evolvmem.library_foundation import FoundationError

# psycopg/libpq TransactionStatus.INTRANS.  The driver already relies on the
# enum's integer values when rejecting a non-IDLE connection at registration.
_INTRANS = 2
_SET_TIMEOUTS = ("SELECT pg_catalog.set_config('statement_timeout', %s, false), "
                 "pg_catalog.set_config('lock_timeout', %s, false)")
_CONTENT_STATIC_TIMEOUT_CONFIG = (
    "SELECT pg_catalog.set_config('statement_timeout', '10000', true), "
    "pg_catalog.set_config('idle_in_transaction_session_timeout', '15000', true)"
)
_CONTENT_TIMEOUT_CAPS = (10000, 10000)


@dataclass(frozen=True)
class _CapsRecord:
    """One successfully installed capped pair on this exact connection."""
    connection: object
    guard: object
    statement_ms: int
    lock_ms: int




def _resolved_profile(profile, raw):
    """Reject unless the connected raw session matches the attested binding.

    The pre-connect kwargs match is not enough: a connector may resolve a
    different host/database/role/CA.  Missing or unexpected metadata keeps the
    original path.  Passwords are never read or logged.
    """
    if profile is None:
        return None
    binding = profile.binding
    try:
        info = raw.info
        # Actual Psycopg 3 API: ConnectionInfo has no ssl_in_use or
        # sslrootcert attribute.  TLS is on pgconn; resolved connection
        # parameters are in ConnectionInfo.get_parameters(), which excludes
        # the password.  Never inspect/log the password.
        if getattr(raw.pgconn, 'ssl_in_use', None) is not True:
            return None
        parameters = info.get_parameters()
        if not isinstance(parameters, dict):
            return None
        if parameters.get('sslmode') != 'verify-full':
            return None
        if parameters.get('sslrootcert') != binding.sslrootcert:
            return None
        if getattr(info, 'host', None) != binding.host:
            return None
        if getattr(info, 'hostaddr', None) != binding.hostaddr:
            return None
        if int(getattr(info, 'port')) != binding.port:
            return None
        if getattr(info, 'dbname', None) != binding.database:
            return None
        if getattr(info, 'user', None) != binding.user:
            return None
        return profile
    except Exception:
        return None


def connect(guard, connector, kwargs, *, kind, statement_ms, lock_ms, profile=None,
            prepare_none=False):
    """Connect on the request thread. A late raw connection never reaches SQL.

    ``profile`` is an internal, default-off trusted fixture profile.  It is
    never derived from request arguments; without an exactly matching profile
    the connection follows the original timeout-setting path.
    """
    raw = None
    try:
        guard.check()
        state = guard._state()
        with state.lock:
            guard._check_locked(state)
            if state.sealed or len(state.resources) >= guard.MAX_RESOURCES:
                raise FoundationError('deadline_resource_limit')
        kwargs = dict(kwargs, connect_timeout=max(1, math.ceil(min(3, guard.remaining_seconds()))),
                      sslmode='verify-full', autocommit=True)
        reuse = timeout_reuse.profile_for(kind, kwargs, statement_ms=statement_ms,
                                          lock_ms=lock_ms, profile=profile)
        with guard.operation(kind=kind):
            raw = connector(**kwargs)
            if prepare_none:
                # The reused raw keeps this from its first creation; a fresh
                # raw is disarmed here, before its first SQL.  Content and
                # coordinator/default-off connects never take this path.
                raw.prepare_threshold = None
        reuse = _resolved_profile(reuse, raw)
        return GuardedConnection(raw, guard, kind=kind, statement_ms=statement_ms,
                                 lock_ms=lock_ms, profile=reuse)
    except BaseException:
        if raw is not None:
            try: raw.close()
            except BaseException:
                # This raw connection may never have reached _register(), so
                # quiesce cannot discover its unconfirmed retirement. Poison
                # the same guard, while the bare raise preserves the primary.
                guard._unhealthy.set()
        raise


class GuardedConnection:
    def __init__(self, raw, guard, *, kind, statement_ms, lock_ms, profile=None):
        import psycopg
        # No fallback to PQcancel, whose timeout parameter is not effective.
        try:
            supported = psycopg.capabilities.has_cancel_safe(check=True)
        except Exception:
            supported = False
        if (not isinstance(raw, psycopg.Connection) or not supported
                or not callable(getattr(raw, 'cancel_safe', None))
                or getattr(raw, '_pool', None) is not None
                or raw.autocommit is not True or raw.closed
                or int(raw.info.transaction_status) != 0):
            raise FoundationError('deadline_driver_unsupported')
        self._raw, self.guard, self.kind = raw, guard, kind
        self.statement_ms, self.lock_ms = statement_ms, lock_ms
        self.protected = False
        self.retired = False
        # Per-wrapper cancellation/detach serialization and success-only
        # recycle eligibility.  A wrapper is never reused; only its raw may be.
        self._cancel_lock = threading.Lock()
        self._cancel_sticky = False
        self._faulty = False
        self._open_cursors = 0
        # At most one record, only on this dedicated nonpooled connection.
        self._reuse_profile = profile
        self._installed = None
        self._reuse_candidate = False
        self._prime_after = False
        # Minimal observer counters; no user-facing product surface.
        self.timeout_setter_calls = 0
        self.timeout_reuse_hits = 0
        guard._register(self)

    @property
    def info(self): return self._raw.info
    @property
    def closed(self): return self.retired or self._raw.closed
    @property
    def autocommit(self): return self._raw.autocommit

    def protect_authority(self):
        self.guard.check()
        if self.kind != 'control' or self.retired:
            raise FoundationError('invalid_authority_resource')
        with self.guard._state().lock:
            self.protected = True

    def cancel(self, timeout):
        # Never touch a stored raw: a retired wrapper is a noop.  Holding this
        # lock across the real cancel makes detach wait for an in-flight
        # cancel_safe, so the raw can never be handed off mid-cancel.
        with self._cancel_lock:
            if self.retired or self._raw is None:
                return
            self._cancel_sticky = True
            self._discard_reuse()
            self._raw.cancel_safe(timeout=timeout)

    def _fault(self):
        self._faulty = True
        self._discard_reuse()

    def detach(self):
        """Sever the raw from this wrapper after success-only quiescence."""
        with self._cancel_lock:
            if (self.retired or self._raw is None or self._cancel_sticky
                    or self._faulty or self._open_cursors):
                return None
            raw = self._raw
            self._raw = None
            self.retired = True
            self._discard_reuse()
            return raw

    def _discard_reuse(self):
        self._installed = None
        self._reuse_candidate = False
        self._prime_after = False

    def _approved(self, sql):
        if self._reuse_profile is None:
            return False
        return timeout_reuse.approved_sql(self.kind, sql, self._reuse_profile)

    def _can_reuse(self, remaining):
        record = self._installed
        if record is None:
            return False
        if (record.connection is not self or record.guard is not self.guard
                or record.statement_ms != self.statement_ms or record.lock_ms != self.lock_ms):
            return False
        if self.retired or self._raw.closed:
            return False
        try:
            status = int(self._raw.info.transaction_status)
        except Exception:
            return False
        if status != _INTRANS:
            return False
        return remaining >= self.statement_ms and remaining >= self.lock_ms

    def _timeouts(self):
        """Must keep this exact signature for existing fixture instrumentation.

        When the caller pre-set an eligible internal candidate, this may skip
        the setter.  In every other case, or when the record/caps/transaction
        no longer qualify, it clears the record and runs the original setter.
        Return whether this call actually installed both configured caps.
        """
        candidate = self._reuse_candidate
        self._reuse_candidate = False
        self._prime_after = False
        try:
            remaining = max(1, int(self.guard.remaining_seconds() * 1000))
            if candidate and self._can_reuse(remaining):
                self.timeout_reuse_hits += 1
                return False
        except BaseException:
            self._installed = None
            raise
        # Invalidate before the setter, not only after it: a failed setter must
        # never leave an older record usable.
        self._installed = None
        installed = (min(remaining, self.statement_ms), min(remaining, self.lock_ms))
        with self._raw.cursor() as cursor:
            cursor.execute(_SET_TIMEOUTS,
                           (str(installed[0]) + 'ms', str(installed[1]) + 'ms'))
        self.timeout_setter_calls += 1
        # Only an unstunted capped pair may prime.  Below either cap the
        # original dynamic setter is used and no record is installed.
        installed_caps = installed == (self.statement_ms, self.lock_ms)
        self._prime_after = candidate and installed_caps
        return installed_caps

    def _prime_content_static_config(self, params):
        # Only called after the original setter installed both caps and this
        # exact config succeeded. It sets the same statement cap locally and
        # leaves lock_timeout unchanged. It can seed, but never consume.
        profile = self._reuse_profile
        if (self.kind != 'content' or type(params) is not tuple or params
                or not isinstance(profile, timeout_reuse.Profile)
                or profile.binding.kind != 'content'
                or (self.statement_ms, self.lock_ms) != _CONTENT_TIMEOUT_CAPS
                or (profile.statement_ms, profile.lock_ms) != _CONTENT_TIMEOUT_CAPS
                or self._cancel_sticky
                or self.guard.remaining_seconds() * 1000 < self.statement_ms):
            return False
        self._prime_after = True
        return True

    def _after_execute(self, approved):
        prime = self._prime_after
        self._prime_after = False
        if not approved:
            self._installed = None
            return
        if not prime:
            # Reuse hit keeps the existing record; a below-cap setter has
            # already left _installed as None.
            return
        try:
            status = int(self._raw.info.transaction_status)
        except Exception:
            self._installed = None
            return
        if self.retired or self._raw.closed or status != _INTRANS:
            self._installed = None
            return
        self._installed = _CapsRecord(self, self.guard,
                                      self.statement_ms, self.lock_ms)

    def cursor(self):
        try:
            with self.guard.operation(self, kind=self.kind):
                raw = self._raw.cursor()
        except BaseException:
            self._fault()
            raise
        self._open_cursors += 1
        return GuardedCursor(self, raw)

    def execute(self, sql, params=()):
        cursor = self.cursor()
        try:
            return cursor.execute(sql, params)
        except BaseException:
            cursor.close()
            raise

    def commit(self):
        self._discard_reuse()
        try:
            with self.guard.operation(self, kind=self.kind):
                self._timeouts()
                self.guard.check()
                self._raw.commit()
        except BaseException as exc:
            # A cancellation/close/failed read cannot turn this into a definite
            # rollback. The business executor's atomic receipt is authoritative.
            # Every BaseException makes this wrapper permanently ineligible.
            self._fault()
            if isinstance(exc, Exception):
                raise FoundationError('commit_result_unknown') from None
            raise
        finally:
            self._discard_reuse()

    def rollback(self):
        # Do not start another network command after cancellation and pretend
        # it acknowledged rollback. close() retires this dedicated client; the
        # remote outcome remains unknown and content fencing orders projection.
        self._discard_reuse()
        try:
            self.guard.check()
        except FoundationError:
            self._fault()
            raise FoundationError('rollback_result_unknown') from None
        except BaseException:
            self._fault()
            raise
        try:
            with self.guard.operation(self, kind=self.kind, cleanup=True):
                self._raw.rollback()
        except BaseException:
            self._fault()
            raise
        finally:
            self._discard_reuse()

    def close(self):
        self.retire()

    def retire(self):
        self._discard_reuse()
        self.guard._state()  # never transferable to another request/thread
        if self.retired:
            return
        try:
            # Unregister first, outside the wrapper cancellation lock: joining
            # a cancelled watchdog must never wait on a lock its cancel holds.
            # A failed unregister means an active cancel may still be running,
            # so the raw is neither closed nor marked retired here; poison the
            # guard and propagate the original exception for honest state.
            try:
                self.guard._unregister(self)
            except BaseException:
                self.guard._unhealthy.set()
                raise
            with self._cancel_lock:
                if self.retired:
                    return
                try:
                    self._raw.close()
                except BaseException as exc:
                    # Never mark this wrapper retired while the physical raw's
                    # retirement is unconfirmed. Interrupts keep their identity.
                    self.guard._unhealthy.set()
                    if isinstance(exc, Exception):
                        raise FoundationError('deadline_cleanup_incomplete') from None
                    raise
                self.retired = True
        finally:
            self._discard_reuse()


class GuardedCursor:
    def __init__(self, connection, raw):
        self.connection, self.raw = connection, raw
    @property
    def description(self): return self.raw.description
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def execute(self, sql, params=()):
        connection = self.connection
        try:
            # Approval is inside this try: a hashing/encoding failure must
            # invalidate the previous record before it propagates.
            static_config = type(sql) is str and sql == _CONTENT_STATIC_TIMEOUT_CONFIG
            approved = not static_config and connection._approved(sql)
            with connection.guard.operation(connection, kind=connection.kind):
                # Internal eligibility token, derived from the exact SQL and
                # the immutable profile; never a caller-supplied free boolean.
                connection._reuse_candidate = approved
                installed_caps = connection._timeouts()
                connection.guard.check()
                self.raw.execute(sql, params)
                connection.guard.check()
                seeded = (static_config and installed_caps
                          and connection._prime_content_static_config(params))
                connection._after_execute(approved or seeded)
        except BaseException:
            connection._fault()
            raise
        return self
    def fetchall(self):
        connection = self.connection
        try:
            with connection.guard.operation(connection, kind=connection.kind):
                return self.raw.fetchall()
        except BaseException:
            connection._fault()
            raise
    def fetchone(self):
        connection = self.connection
        try:
            with connection.guard.operation(connection, kind=connection.kind):
                return self.raw.fetchone()
        except BaseException:
            connection._fault()
            raise
    def close(self):
        connection = self.connection
        if self.raw is None:
            return
        try:
            connection.guard._state()
            self.raw.close()
        except BaseException:
            # A failed close may leave transaction state uncertain.  An
            # ordinary successful close after every query must retain the
            # record, otherwise reuse would never survive normal cursor use.
            connection._fault()
            raise
        finally:
            self.raw = None
            connection._open_cursors -= 1
