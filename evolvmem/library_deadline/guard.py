"""Synchronous request cancellation; never a remote-COMMIT outcome oracle."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import socket
import threading
import time

from evolvmem.library_catalog.adapter import RequestDeadlineGuard
from evolvmem.library_foundation import FoundationError


@dataclass
class _Request:
    owner: int
    deadline: float
    lock: object = field(default_factory=threading.RLock)
    wake: object = field(default_factory=threading.Event)
    stopped: bool = False
    cancelled: bool = False
    sealed: bool = False
    output_started: bool = False
    active: int = 0
    resources: list = field(default_factory=list)
    http_socket: object = None
    watchdog: object = None


class _CancellationHandle:
    """Only signal cancellation cross-thread; never execute SQL or close resources."""
    def __init__(self, state):
        self._request = state

    def cancel(self):
        state = self._request
        with state.lock:
            if not state.stopped:
                state.cancelled = True
                state.wake.set()


class SyncRequestDeadlineGuard(RequestDeadlineGuard):
    CANCEL_SECONDS = .25
    MAX_RESOURCES = 4

    def __init__(self, timeout_seconds=30.0, *, absolute_deadline=None):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
            raise ValueError('finite request deadline required')
        self._timeout = float(timeout_seconds)
        if (absolute_deadline is not None and
                (type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline))):
            raise ValueError('finite absolute deadline required')
        self._absolute_deadline = absolute_deadline
        self._current = ContextVar('evolvmem_request_deadline', default=None)
        self._unhealthy = threading.Event()

    @property
    def timeout_seconds(self):
        return self._timeout

    def _state(self):
        state = self._current.get()
        if state is None or state.owner != threading.get_ident():
            raise FoundationError('deadline_scope_required')
        return state

    @contextmanager
    def enforce(self):
        if self._current.get() is not None:
            self.check()
            yield
            self.check()
            return
        if self._unhealthy.is_set():
            raise FoundationError('deadline_cleanup_incomplete')
        deadline = time.monotonic() + self._timeout
        if self._absolute_deadline is not None:
            deadline = min(deadline, self._absolute_deadline)
        state = _Request(threading.get_ident(), deadline)
        token = self._current.set(state)
        state.watchdog = threading.Thread(target=self._watch, args=(state,), daemon=True,
                                          name='evolvmem-deadline-cancel')
        state.watchdog.start()
        failed = False
        try:
            yield
            self.check()
        except BaseException:
            failed = True
            raise
        finally:
            try:
                try:
                    self.quiesce(cancel=failed)
                except BaseException:
                    self._unhealthy.set()
                    if not failed:
                        raise
            finally:
                with state.lock:
                    state.stopped = True
                    state.wake.set()
                try:
                    try:
                        self._join(state)
                    except FoundationError:
                        if not failed:
                            raise
                finally:
                    self._current.reset(token)

    def _join(self, state):
        # A wedged cancellation implementation is a hard fault, not permission
        # to spawn more requests or to wait forever holding an authority lease.
        state.watchdog.join(self.MAX_RESOURCES * self.CANCEL_SECONDS + .25)
        if state.watchdog.is_alive():
            self._unhealthy.set()
            raise FoundationError('deadline_cleanup_incomplete')

    def _watch(self, state):
        state.wake.wait(max(0.0, state.deadline - time.monotonic()))
        with state.lock:
            if state.stopped:
                return
            state.cancelled = True
            targets = tuple(r for r in state.resources if not r.retired and not r.protected)
            output = state.http_socket
        if output is not None:
            try:
                output.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for resource in targets:
            try:
                resource.cancel(self.CANCEL_SECONDS)
            except Exception:
                # Cancellation failure does not mean rollback. The request
                # thread must unwind/retire the dedicated connection itself.
                pass

    def cancel(self):
        state = self._state()
        with state.lock:
            state.cancelled = True
            state.wake.set()

    def check(self):
        state = self._state()
        with state.lock:
            expired = time.monotonic() >= state.deadline
            if expired:
                state.cancelled = True
                state.wake.set()
            if state.cancelled:
                raise FoundationError('request_deadline_exceeded')

    def remaining_seconds(self):
        self.check()
        return max(0.0, self._state().deadline - time.monotonic())

    def absolute_deadline(self):
        self.check()
        return self._state().deadline

    def cancellation_handle(self):
        self.check()
        return _CancellationHandle(self._state())

    def begin_authority(self):
        """New sequential lease segment, never a new request deadline."""
        self.check()
        state = self._state()
        with state.lock:
            self._check_locked(state)
            if self._unhealthy.is_set():
                raise FoundationError('deadline_cleanup_incomplete')
            if state.active or state.resources or state.output_started:
                raise FoundationError('deadline_previous_scope_active')
            state.sealed = False

    def bind_socket(self, connection):
        self.check()
        if not isinstance(connection, socket.socket):
            raise TypeError('real request socket required')
        state = self._state()
        with state.lock:
            self._check_locked(state)
            if state.http_socket is not None and state.http_socket is not connection:
                raise FoundationError('deadline_socket_already_bound')
            state.http_socket = connection

    def _register(self, resource):
        self.check()
        state = self._state()
        with state.lock:
            self._check_locked(state)
            if state.sealed or len(state.resources) >= self.MAX_RESOURCES:
                raise FoundationError('deadline_resource_limit')
            state.resources.append(resource)

    def _unregister(self, resource):
        state = self._state()
        with state.lock:
            if resource in state.resources:
                state.resources.remove(resource)
            cancelled = state.cancelled
        if cancelled:
            self._join(state)  # cancel_safe must finish before raw.close()

    @contextmanager
    def operation(self, resource=None, *, kind='content', cleanup=False):
        state = self._state()
        if not cleanup:
            self.check()
        with state.lock:
            if not cleanup:
                self._check_locked(state)
            if not cleanup and state.sealed and kind != 'control':
                raise FoundationError('deadline_scope_quiesced')
            if resource is not None and (resource not in state.resources or resource.retired):
                raise FoundationError('deadline_resource_retired')
            if kind == 'output':
                state.output_started = True
            state.active += 1
        try:
            yield
            if not cleanup:
                self.check()
        finally:
            with state.lock:
                state.active -= 1

    @staticmethod
    def _check_locked(state):
        if time.monotonic() >= state.deadline:
            state.cancelled = True
            state.wake.set()
        if state.cancelled:
            raise FoundationError('request_deadline_exceeded')

    def quiesce(self, *, cancel):
        state = self._state()
        if cancel:
            self.cancel()
        with state.lock:
            state.sealed = True
            if state.active:
                raise FoundationError('deadline_work_still_active')
            targets = tuple(r for r in state.resources if r.kind == 'content' and not r.retired)
            cancelled = state.cancelled
        if cancelled:
            self._join(state)
        for resource in targets:
            resource.retire()
        # Control connections belong to the outer authority context and must
        # remain pinned until after this local quiescence boundary.
