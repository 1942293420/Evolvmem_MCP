"""LAN-owned committed physical proofs; no semantic readiness or worker threads."""
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import uuid

PROTOCOL = 'sqlite-quick-check-v1'


class PhysicalScanTimeout(TimeoutError):
    """A complete physical check was not obtained within its execution budget."""


class PhysicalScanBusy(RuntimeError):
    """SQLite lock contention prevented the bounded physical check."""


def config_fingerprint(config):
    return hashlib.sha256(repr(sorted(vars(config).items())).encode()).hexdigest()


@dataclass(frozen=True)
class InitialPhysicalBoundary:
    identity: tuple
    contract: str
    schema_digest: str
    config_file: tuple


def _initial_config_file(path):
    path = Path(path).absolute()
    try:
        before = path.stat()
    except FileNotFoundError:
        return ('missing', str(path.resolve()))
    if not path.is_file() or before.st_size > 1024 * 1024:
        raise RuntimeError('initial_config_unavailable')
    with path.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
        opened = os.fstat(stream.fileno())
    after = path.stat()
    def stamp(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                value.st_ctime_ns, value.st_mode, value.st_uid, value.st_gid)
    if len(raw) != before.st_size or stamp(before) != stamp(opened) or stamp(opened) != stamp(after):
        raise RuntimeError('initial_config_changed')
    return (str(path.resolve()), stamp(after), hashlib.sha256(raw).hexdigest())


def initial_physical_boundary(service, config_path):
    """Metadata only; never certify an uncommitted resident transaction."""
    conn = service.store._connection()
    if conn.in_transaction:
        raise RuntimeError('initial_physical_transaction')
    path = service.config.db_path
    before = path.stat()
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    definition = hashlib.sha256(repr([tuple(row) for row in conn.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]).encode()).hexdigest()
    after = path.stat()
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or conn.execute('PRAGMA schema_version').fetchone()[0] != schema
            or conn.in_transaction):
        raise RuntimeError('initial_physical_boundary_changed')
    return InitialPhysicalBoundary((str(path.resolve()), after.st_dev, after.st_ino, schema),
        config_fingerprint(service.config), definition, _initial_config_file(config_path))


_INITIAL_PROOF_SEAL = object()


@dataclass(frozen=True)
class _InitialPhysicalProof:
    source: object
    before: InitialPhysicalBoundary
    after: InitialPhysicalBoundary
    started: float
    completed: float
    generation: int
    seal: object


def issue_initial_physical_proof(*, source, before, after, started, completed, codes, generation):
    if (type(before) is not InitialPhysicalBoundary or before != after or codes != ()
            or type(generation) is not int or generation < 0
            or type(started) not in (int, float) or type(completed) not in (int, float)
            or not math.isfinite(started) or not math.isfinite(completed) or started > completed):
        return None
    return _InitialPhysicalProof(source, before, after, started, completed, generation, _INITIAL_PROOF_SEAL)


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
        if self.state == 'timeout':
            return ('quick_check_timeout',)
        if self.state == 'busy':
            return ('quick_check_busy',)
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
        self._unavailable = None
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

    def adopt_initial(self, proof, *, source, boundary, generation, ttl):
        """Accept only a same-process resident receipt; never renew its clock."""
        with self._lock:
            if (self._closed or self._boundary_failed or self._unavailable or self._running or self._record is not None
                    or (self._predecessor is not None and self._predecessor.is_alive())
                    or type(proof) is not _InitialPhysicalProof or proof.seal is not _INITIAL_PROOF_SEAL
                    or proof.source is not source or proof.generation != generation
                    or type(proof.started) not in (int, float) or type(proof.completed) not in (int, float)
                    or not math.isfinite(proof.started) or not math.isfinite(proof.completed)
                    or proof.started > proof.completed
                    or proof.before != proof.after or proof.after != boundary
                    or boundary.identity != self.identity or boundary.contract != self.contract):
                return False
            now = self._clock()
            if (not math.isfinite(now) or not math.isfinite(ttl)
                    or not 0 <= now - proof.completed < min(300., ttl)):
                return False
            self._record = (proof.completed, ())
            self._generation += 1
            return True

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
            elif self._unavailable is not None:
                state, refresh = self._unavailable, True
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

    def cancelled(self):
        with self._lock:
            return self._closed or self._boundary_failed

    def check(self, identity, contract, ttl, scanner, *, current_config, deadline=None):
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError('physical_deadline_invalid')
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
            unavailable = None
            try:
                if deadline is not None and self._clock() >= deadline:
                    raise PhysicalScanTimeout()
                result = scanner()
                codes = () if result == () else ('quick_check_failed',)
            except PhysicalScanTimeout:
                unavailable = 'timeout'
                codes = ('quick_check_timeout',)
            except PhysicalScanBusy:
                unavailable = 'busy'
                codes = ('quick_check_busy',)
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
                if deadline is not None and self._clock() >= deadline:
                    unavailable = 'timeout'
                if unavailable is not None:
                    # No successful completion stamp is issued for an overrun.
                    # Keep _running until the actual scanner has returned.
                    self._record = None
                    self._unavailable = unavailable
                    self._generation += 1
                    return (('quick_check_timeout',) if unavailable == 'timeout' else ('quick_check_busy',)), None
                # Visible immediately, before any following semantic evaluation.
                self._record = (checked, codes)
                self._unavailable = None
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
