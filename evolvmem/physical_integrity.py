"""LAN-owned committed physical proofs; no semantic readiness or worker threads."""
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

PROTOCOL = 'sqlite-quick-check-v1'


def config_fingerprint(config):
    return hashlib.sha256(repr(sorted(vars(config).items())).encode()).hexdigest()


@dataclass(frozen=True)
class PhysicalObservation:
    state: str
    checked_at: float | None
    generation: int
    needs_refresh: bool

    @property
    def codes(self):
        if self.state == 'ok':
            return ()
        if self.state in {'pending', 'expired'}:
            return ('quick_check_pending',)
        return ('quick_check_failed',)


class PhysicalProofOwner:
    def __init__(self, identity, contract, *, refresh, clock=time.monotonic):
        self.identity = tuple(identity)
        self.contract = contract
        self._refresh = refresh
        self._clock = clock
        self._lock = threading.RLock()
        self._record = None
        self._predecessor = None
        self._running = False
        self._closed = False
        self._boundary_failed = False
        self._generation = 0

    def bind_predecessor(self, worker):
        with self._lock:
            if self._running or self._record is not None:
                raise RuntimeError('physical_owner_already_started')
            self._predecessor = worker
            self._generation += 1

    def _matches(self, identity, contract):
        if tuple(identity) != self.identity or contract != self.contract:
            if not self._boundary_failed:
                self._boundary_failed = True
                self._record = None
                self._generation += 1
        return not self._boundary_failed

    def observe(self, identity, contract, ttl):
        with self._lock:
            matches = self._matches(identity, contract)
            checked = self._record[0] if self._record else None
            refresh = False
            if self._closed:
                state = 'closed'
            elif not matches:
                state = 'boundary'
            elif self._record is None:
                state, refresh = 'pending', True
            elif self._record[1]:
                state, refresh = 'failed', True
            else:
                age = self._clock() - checked
                refresh = not ttl or age < 0 or age >= ttl * .6
                state = 'ok' if ttl > 0 and 0 <= age < ttl else 'expired'
            return PhysicalObservation(state, checked, self._generation, refresh)

    def token(self, identity, contract, ttl):
        view = self.observe(identity, contract, ttl)
        return (PROTOCOL, id(self), view.generation, ttl, view.state, view.needs_refresh)

    def request_refresh(self):
        # Never call the manager while holding this lock or a service lock.
        with self._lock:
            callback = None if self._closed else self._refresh
        if callback is not None:
            callback()

    def check(self, identity, contract, ttl, scanner, *, current_config):
        with self._lock:
            if self._closed:
                raise RuntimeError('physical_owner_closed')
            if not self._matches(identity, contract):
                raise RuntimeError('physical_boundary_changed')
            if self._predecessor is not None:
                if self._predecessor.is_alive():
                    raise RuntimeError('physical_handoff_pending')
                self._predecessor = None
            if self._running:
                raise RuntimeError('physical_scan_running')
            if self._record is not None and not self._record[1]:
                age = self._clock() - self._record[0]
                if ttl > 0 and 0 <= age < ttl * .6:
                    return self._record[1], self._record[0]
            self._running = True
            generation = self._generation
        try:
            try:
                result = scanner()
                codes = () if result == () else ('quick_check_failed',)
            except Exception:
                codes = ('quick_check_failed',)
            checked = self._clock()
            try:
                after_config = current_config()
            except Exception:
                after_config = None
            with self._lock:
                if self._closed:
                    raise RuntimeError('physical_owner_closed')
                if not self._matches(identity, after_config) or generation != self._generation:
                    raise RuntimeError('physical_boundary_changed')
                # Visible immediately, before any following semantic evaluation.
                self._record = (checked, codes)
                self._generation += 1
            return codes, checked
        finally:
            with self._lock:
                self._running = False

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._generation += 1
                self._record = None
                self._refresh = None


def physical_scan(kind):
    """Fixed anonymous begin/end events; logging never changes check results."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            tag = 'unavailable'
            try:
                path = ((args[0] if args else kwargs['path']) if kind == 'readonly'
                        else args[0].config.db_path)
                stat = Path(path).stat()
                tag = hashlib.sha256(f'{stat.st_dev}:{stat.st_ino}'.encode()).hexdigest()[:16]
            except Exception:
                pass
            thread = threading.current_thread().name
            source = ('background' if thread == 'evolvmem-health' else
                      'resident_refresh' if thread == 'evolvmem-integrity-check' else
                      'resident_sync' if kind == 'resident' else 'readonly_other')
            fields = dict(scan_id=uuid.uuid4().hex, pid=os.getpid(), db_tag=tag, source=source)
            def emit(phase, **values):
                try:
                    logging.getLogger(__name__).warning('physical_scan %s',
                        json.dumps(dict(fields, phase=phase, **values), sort_keys=True))
                except Exception:
                    pass
            started = time.monotonic()
            emit('begin', monotonic=started)
            outcome = 'error'
            try:
                result = function(*args, **kwargs)
                outcome = 'ok' if result == () else 'failed'
                return result
            finally:
                finished = time.monotonic()
                emit('end', monotonic=finished, elapsed_ms=round((finished-started)*1000, 3),
                     outcome=outcome)
        return wrapped
    return decorate
