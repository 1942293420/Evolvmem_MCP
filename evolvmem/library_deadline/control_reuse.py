"""Private per-request reuse of ONE raw control connection.

This is not a pool: at most one idle raw is retained, and only for the same
ControlProvider instance, the same concrete SyncRequestDeadlineGuard, the same
admitted request-state object and the same request owner thread.  Nothing
survives the request: the host closes the store in request_scope's finally,
before guard.enforce() exits, and a new request always performs a new physical
authentication.

The stored object is the raw psycopg connection only.  Every borrow still
constructs a NEW GuardedConnection (new registration/cancellation target) and
a NEW ControlSession (new BEGIN, role check, pin authority, rollback).
"""
import hashlib
import hmac
import threading

from evolvmem.library_foundation import FoundationError


def fingerprint(binding, secret):
    """In-memory only comparison token; never stored in an error or repr."""
    payload = '\x1f'.join((str(binding.host), str(int(binding.port)),
                           str(binding.database), str(binding.role))).encode('utf-8')
    payload += b'\x00' + secret.encode('utf-8')
    return hashlib.sha256(payload).digest()


def matches(expected, actual):
    if type(expected) is not bytes or type(actual) is not bytes:
        return False
    return hmac.compare_digest(expected, actual)


class ControlReuseStore:
    """At most one detached raw, owned by exactly one admitted request scope."""

    def __init__(self, guard):
        # Concrete guard and a real opt-in are required by ControlProvider.
        self._guard = guard
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._state = None
        self._raw = None
        self._token = None
        self._closed = False

    def _same_request_locked(self):
        """Pin/compare the EXACT admitted state object for this thread."""
        if self._closed or threading.get_ident() != self._owner:
            return False
        if self._guard._unhealthy.is_set():
            return False
        try:
            state = self._guard._state()
        except FoundationError:
            return False
        if self._state is None:
            # First borrow inside this provider's request pins that scope.
            self._state = state
            return True
        return self._state is state

    @staticmethod
    def _physically_usable(raw):
        try:
            return (raw is not None and not raw.closed and raw.autocommit is True
                    and int(raw.info.transaction_status) == 0)
        except Exception:
            return False

    def _retire(self, raw):
        if raw is None:
            return
        try:
            raw.close()
        except BaseException as exc:
            # An unconfirmed physical retirement must not be hidden.
            self._guard._unhealthy.set()
            if isinstance(exc, Exception):
                raise FoundationError('control_cleanup_failed') from None
            raise

    def _retire_preserving(self, raw):
        """Last-resort physical close that never replaces the primary error."""
        if raw is None:
            return
        try:
            self._retire(raw)
        except BaseException:
            pass  # _retire already poisoned the guard on unconfirmed close

    def take(self, token):
        """Remove the idle raw when it is still the same identity/request."""
        raw, stored, usable = None, None, False
        try:
            with self._lock:
                raw, stored = self._raw, self._token
                self._raw = self._token = None
                # Pin/compare the admitted scope even when no idle raw is
                # present.  The popped raw is still owned here until return.
                current = self._same_request_locked()
                usable = (current and raw is not None
                          and matches(stored, token) and self._physically_usable(raw))
        except BaseException:
            # Interruption between unlinking and returning: nobody else can
            # own this raw, so close it here and keep the original exception.
            self._retire_preserving(raw)
            raise
        if raw is None:
            return None
        if not usable:
            self._retire(raw)
            return None
        return raw

    def put(self, token, raw):
        """Retain a detached raw only for this exact request; close otherwise.

        The wrapper is already detached/retired, so its own close cannot
        dispose of this raw: every exit before it is safely in self._raw must
        guarantee a physical close here.
        """
        keep = False
        try:
            with self._lock:
                keep = (self._raw is None and not self._closed
                        and self._same_request_locked()
                        and self._physically_usable(raw))
                if keep:
                    self._token = bytes(token)
                    self._raw = raw
        except BaseException:
            self._retire_preserving(raw)
            raise
        if keep:
            return True
        self._retire(raw)
        return False

    def put_after_guard_check(self, token, raw):
        """Re-check expiry/cancel under the pinned request lock before keeping."""
        try:
            try:
                state = self._guard._state()
            except FoundationError:
                self._retire(raw)
                return False
            if state is not self._state:
                self._retire(raw)
                return False
            with state.lock:
                try:
                    self._guard._check_locked(state)
                except FoundationError:
                    self._retire(raw)
                    return False
        except BaseException:
            # Any interruption before self.put() must not leak the detached raw.
            self._retire_preserving(raw)
            raise
        return self.put(token, raw)

    def close(self):
        """Physical cleanup of the single idle raw; idempotent."""
        with self._lock:
            raw, self._raw, self._token = self._raw, None, None
            self._closed = True
        self._retire(raw)
