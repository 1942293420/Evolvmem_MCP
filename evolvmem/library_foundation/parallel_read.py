"""Bounded, request-owned parallel reads under one real control authority lease.

Trusted synchronous repositories only. Workers never receive the parent's guard,
control connection, router, context or publication callback. All content
transactions are READ ONLY and end with ROLLBACK. This is not a distributed
snapshot; final content qualification must be supplied by the caller.
"""
from contextlib import contextmanager
from contextvars import Context
import math
import queue
import threading
import time

from .models import AuthorizedRoute, FoundationError
from .sessions import (SessionManager, LibrarySession, DatabaseCredential, PsycopgDirectProvider)
from evolvmem.library_catalog.adapter import ControlAuthorityLease
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard


class ParallelReadBudget:
    """Host-owned admission, not a pool or an unbounded task queue.

    A request reserves its entire branch capacity atomically. Unsettled workers
    keep their slots, and a failed cleanup permanently poisons this budget.
    A replacement host must not reuse this budget or its connections.
    """
    def __init__(self, max_workers=16):
        if type(max_workers) is not int or not 1 <= max_workers <= 64:
            raise ValueError('bounded worker capacity required')
        self._limit = max_workers
        self._lock = threading.Lock()
        self._groups = set()
        self._used = 0
        self._closed = self._unhealthy = False

    def reserve(self, count):
        if type(count) is not int or not 1 <= count <= 3:
            raise ValueError('bounded request branch count required')
        with self._lock:
            if self._closed or self._unhealthy:
                raise FoundationError('parallel_read_unavailable')
            if self._used + count > self._limit:
                raise FoundationError('parallel_read_busy')
            group = _Reservation(self, count)
            self._groups.add(group)
            self._used += count
            return group

    def _release(self, group):
        with self._lock:
            if group in self._groups:
                self._groups.remove(group)
                self._used -= group.capacity

    def _poison(self):
        with self._lock:
            self._unhealthy = True

    def close(self, timeout_seconds=2.0):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 <= timeout_seconds <= 5):
            raise ValueError('bounded cleanup timeout required')
        with self._lock:
            self._closed = True
            groups = tuple(self._groups)
        for group in groups:
            group.cancel()
        deadline = time.monotonic() + timeout_seconds
        complete = all([group.wait(max(0., deadline-time.monotonic())) for group in groups])
        if not complete:
            self._poison()
        return complete


class _Reservation:
    def __init__(self, budget, capacity):
        self.budget, self.capacity = budget, capacity
        self._condition = threading.Condition()
        self._handles = set()
        self._running = 0
        self._cancelled = self._released = False

    def check(self):
        with self._condition:
            if self._cancelled or self._released:
                raise FoundationError('parallel_read_cancelled')

    def cancel(self):
        with self._condition:
            self._cancelled = True
            handles = tuple(self._handles)
            self._condition.notify_all()
        for handle in handles:
            handle.cancel()

    def add(self, handle):
        with self._condition:
            self._handles.add(handle)
            cancelled = self._cancelled
        if cancelled:
            handle.cancel()

    def remove(self, handle):
        with self._condition:
            self._handles.discard(handle)

    def start(self, work, finished):
        with self._condition:
            self.check()
            if self._running >= self.capacity:
                raise FoundationError('parallel_read_capacity')
            self._running += 1
        def run():
            try:
                result = Context().run(work)  # Never inherit held control ContextVar.
            finally:
                with self._condition:
                    self._running -= 1
                    release = self._released and self._running == 0
                    self._condition.notify_all()
                if release:
                    self.budget._release(self)
            # Signal completion only after the branch slot becomes available.
            # Otherwise the parent can observe done and race the old slot count
            # while scheduling the fourth library.
            finished(result)
        try:
            threading.Thread(target=run, name='evolvmem-library-read', daemon=True).start()
        except BaseException:
            with self._condition:
                self._running -= 1
                self._condition.notify_all()
            raise FoundationError('parallel_read_unavailable') from None

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def release(self):
        with self._condition:
            self._released = True
            complete = self._running == 0
        if complete:
            self.budget._release(self)


@contextmanager
def _read_transaction(route, binding, provider):
    """Private branch capability: invoked only after parent hold_many has pinned.

    There is deliberately no pretend/no-op worker AuthorityLease. The parent's
    real control lease and final revalidation supply control authority; this
    branch verifies actual role, content ACL projection and credential binding.
    """
    connection = provider.open(binding)
    session = LibrarySession(route, connection)
    failed = False
    try:
        connection.begin(read_only=True)
        SessionManager._validate_role(connection.inspect_role(), binding)
        SessionManager._validate_projection(connection.session_context(), route)
        credential = connection.session_credential()
        if (not isinstance(credential, DatabaseCredential)
                or credential.credential_id != route.identity.credential_id
                or credential.device_id != route.identity.device_id
                or type(credential.credential_epoch) is not int
                or credential.credential_epoch != route.identity.credential_epoch):
            raise FoundationError('database_credential_mismatch')
        yield session
        SessionManager._validate_role(connection.inspect_role(), binding)
    except BaseException:
        failed = True
        raise
    finally:
        session._closed = True
        rollback_failed = close_failed = False
        try:
            connection.rollback()
        except Exception:
            rollback_failed = True
        try:
            connection.close()
        except Exception:
            close_failed = True
        if close_failed:
            # Preserve primary failures but always poison the guard/budget.
            provider.deadline_guard._unhealthy.set()
            if not failed:
                raise FoundationError('database_close_failed') from None
        elif rollback_failed and not failed:
            # A read-only connection with confirmed retirement has no commit
            # outcome to replay. Preserve failed rollback as request failure,
            # but ordinary deadline cancellation must not poison the host.
            raise FoundationError('database_rollback_failed') from None


class ParallelReadCoordinator:
    CLEANUP_SECONDS = 1.5

    def __init__(self, manager, *, content_connector, secret_resolver, budget, max_parallel=3):
        if (not isinstance(manager, SessionManager)
                or not isinstance(manager.authority, ControlAuthorityLease)
                or not isinstance(manager.authority.deadline_guard, SyncRequestDeadlineGuard)
                or not isinstance(budget, ParallelReadBudget)
                or not callable(content_connector) or not callable(secret_resolver)
                or type(max_parallel) is not int or not 1 <= max_parallel <= 3):
            raise ValueError('real authority, request guard and bounded read dependencies required')
        self.manager, self.budget = manager, budget
        self._connector, self._secrets, self._parallel = content_connector, secret_resolver, max_parallel

    def run_many(self, routes, operation, *, combine, finalize=None, publish=None):
        if (not isinstance(routes, tuple) or not 1 <= len(routes) <= 4
                or any(not isinstance(r, AuthorizedRoute) or r.action != 'read'
                       or r.library.backend != 'postgres' for r in routes)
                or len({r.library.library_id for r in routes}) != len(routes)
                or any(r.identity != routes[0].identity or r.tool != routes[0].tool for r in routes)
                or not callable(operation) or not callable(combine)
                or (finalize is not None and not callable(finalize))
                or (publish is not None and not callable(publish))):
            raise FoundationError('invalid_read_routes')
        routes = tuple(sorted(routes, key=lambda r: r.library.library_id))
        manager, guard = self.manager, self.manager.authority.deadline_guard
        group = self.budget.reserve(min(self._parallel, len(routes)))
        try:
            with manager.authority.hold_many(routes):
                try:
                    # Resolve/revalidate the ENTIRE set before any content opens.
                    for route in routes:
                        manager.router.revalidate(route)
                    bindings = tuple(manager.registry.resolve(route) for route in routes)
                    deadline = guard.absolute_deadline()
                    gathered = self._phase(routes, bindings, operation, None, deadline, group)
                    for route in routes:
                        manager.router.revalidate(route)
                    if finalize is not None:
                        gathered = self._phase(routes, bindings, finalize, gathered, deadline, group)
                    group.check()
                    result = combine(tuple(zip(routes, gathered)))
                    for route in routes:
                        manager.router.revalidate(route)
                    guard.check()
                    group.check()
                    if publish is not None:
                        publish(result)  # Parent thread, real control lease still held.
                    return result
                finally:
                    group.cancel()
                    if not group.wait(self.CLEANUP_SECONDS):
                        self.budget._poison()
                        guard._unhealthy.set()
                        raise FoundationError('parallel_read_cleanup_incomplete')
        finally:
            group.release()

    def _phase(self, routes, bindings, callback, inputs, deadline, group):
        guard = self.manager.authority.deadline_guard
        done = queue.Queue(maxsize=len(routes))
        results = [None] * len(routes)
        next_index = active = completed = 0
        def worker(index):
            child = SyncRequestDeadlineGuard(300., absolute_deadline=deadline)
            handle = None
            value = error = None
            try:
                with child.enforce():
                    handle = child.cancellation_handle()
                    group.add(handle)
                    group.check()
                    provider = PsycopgDirectProvider(self._connector, self._secrets, deadline_guard=child)
                    with _read_transaction(routes[index], bindings[index], provider) as session:
                        value = callback(session) if inputs is None else callback(session, inputs[index])
                    child.check()
            except BaseException as exc:
                error = exc if isinstance(exc, FoundationError) else FoundationError('parallel_read_failed')
            finally:
                if handle is not None:
                    group.remove(handle)
                if child._unhealthy.is_set():
                    self.budget._poison()
                    error = FoundationError('parallel_read_cleanup_incomplete')
            return (index, value if error is None else None, error)
        while completed < len(routes):
            guard.check()
            group.check()
            while next_index < len(routes) and active < group.capacity:
                index = next_index
                group.start(lambda i=index: worker(i), done.put)
                next_index += 1
                active += 1
            try:
                index, value, error = done.get(timeout=min(.02, guard.remaining_seconds()))
            except queue.Empty:
                continue
            active -= 1
            completed += 1
            if error is not None:
                group.cancel()
                raise error
            results[index] = value
        # Join this bounded phase before starting any fresh transaction.
        if not group.wait(self.CLEANUP_SECONDS):
            self.budget._poison()
            raise FoundationError('parallel_read_cleanup_incomplete')
        guard.check()
        return tuple(results)
