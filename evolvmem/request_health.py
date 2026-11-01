"""Bounded complete health snapshots, serialized per resident server.

All business users must still serialize access to the shared store/identity.
This lock only coalesces health evaluation; it does not make SQLite transactions
or the mutable remote identity safe for parallel dispatch.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
import os
import threading
import time
from weakref import WeakKeyDictionary

from evolvmem import vector_provenance
from evolvmem.request_trace import HEALTH_STATE_FIELDS, stage, health_evaluation, health_event

# Zero disables cross-call reuse for diagnosis/rollback. Never unbounded.
def _ttl():
    try:
        return min(30.0, max(0.0, float(os.environ.get("EVOLVMEM_HEALTH_CACHE_SECONDS", "5"))))
    except ValueError:
        return 0.0

_entries = WeakKeyDictionary()
_entries_lock = threading.Lock()
_background_read = ContextVar('evolvmem_background_read', default=None)


class BackgroundReadDenied(RuntimeError):
    """Fixed, content-free rejection from a request's real background gate."""


def _check_read_binding(health, binding):
    namespace, server, service, mode, adapter = binding
    if (not health.active or health.servers.get(namespace) is not server
            or server.context_service is not service
            or server.context_mode is not mode or service._mode is not mode
            or server.adapter != adapter or service._adapter != adapter):
        raise BackgroundReadDenied('health_boundary_changed')
    if mode is None:
        raise BackgroundReadDenied('not_initialized')
    if getattr(mode, 'value', None) not in ('primary', 'shadow'):
        raise BackgroundReadDenied('context_not_enabled')


def _check_read_health(health, bindings):
    for binding in bindings:
        _check_read_binding(health, binding)
        try:
            observed = health.status(binding[0])
            gate = observed['hard_gate']
            ready = (observed.get('diagnostic') or {}).get('last_checked_ready')
        except Exception:
            raise BackgroundReadDenied('health_boundary_unavailable') from None
        if gate != 'allowed':
            # Only the fixed protocol codes can leave this boundary.
            codes = {'health_closed', 'health_boundary_unavailable',
                     'health_baseline_pending', 'health_boundary_changed',
                     'health_physical_failed', 'health_contract_invalid',
                     'health_physical_expired', 'health_evaluation_failed'}
            raise BackgroundReadDenied(gate if gate in codes else 'health_boundary_unavailable')
        if ready is not True:
            raise BackgroundReadDenied('degraded_legacy')


@contextmanager
def background_read_scope(health, tool, servers):
    """Admit only LAN experience recall using each actual namespace baseline.

    This is not a ContextServiceStatus or a health cache. The dispatcher owns
    the resident business lock throughout. All original candidate/evidence
    checks still run; the real hard gate is checked before and after dispatch.
    Semantic staleness schedules the existing worker, never foreground repair.
    """
    if tool != 'experience_recall' or _background_read.get() is not None:
        raise BackgroundReadDenied('health_boundary_unavailable')
    names = tuple(name for name, _ in servers)
    if (not names or len(names) > 2 or len(set(names)) != len(names)
            or not set(names) <= {'jiangli', 'kane', 'public'}
            or {'jiangli', 'kane'} <= set(names)):
        raise BackgroundReadDenied('health_boundary_unavailable')
    bindings = tuple((name, server, server.context_service,
                      server.context_mode, server.adapter) for name, server in servers)
    _check_read_health(health, bindings)
    token = _background_read.set((health, bindings))
    try:
        yield
        _check_read_health(health, bindings)
    finally:
        _background_read.reset(token)


def background_read_ready(service):
    """None outside the explicit scope; never authorize a different service."""
    admission = _background_read.get()
    if admission is None:
        return None
    health, bindings = admission
    for binding in bindings:
        if binding[2] is service:
            _check_read_binding(health, binding)
            return True
    raise BackgroundReadDenied('health_boundary_changed')


class _Entry:
    def __init__(self):
        self.lock = threading.RLock()
        self.snapshot = None
        self.database_identity = None


def _entry(server):
    with _entries_lock:
        return _entries.setdefault(server, _Entry())


@contextmanager
def request_health_scope():
    # Complete health cache remains unchanged. Explicit read admission must
    # never be inherited by another adapter request, including nested calls.
    token = _background_read.set(None)
    try:
        yield
    finally:
        _background_read.reset(token)


def _stamp(path):
    try:
        info = Path(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def _index_state(index):
    # count() refreshes shared native handles. Observe resident metadata only.
    native = getattr(index, "_index", None)
    return (id(index), id(native), len(native) if native is not None else None,
            getattr(index, "_dim", None), getattr(index, "_owns_dirty_marker", None),
            bool(index.is_dirty()) if index is not None else None)


@stage("health_state")
def _state(server):
    service = server.context_service
    connection = service.store._connection()
    config = server.config
    schema = connection.execute("PRAGMA schema_version").fetchone()[0]
    db_stamp = _stamp(config.db_path)
    identity = (id(connection), str(Path(config.db_path).resolve()),
                None if db_stamp is None else db_stamp[:2])
    entry = _entry(server)
    if (entry.database_identity is not None and entry.database_identity[0] == id(connection)
            and entry.database_identity != identity):
        raise RuntimeError("health_database_replaced")
    entry.database_identity = identity
    from evolvmem.health_generation import observed
    generation = observed(service.store, schema)
    if generation is None:
        health_event('generation_fallback')
        changes = connection.total_changes
        data_version = connection.execute("PRAGMA data_version").fetchone()[0]
        database_files = (db_stamp, _stamp(str(config.db_path) + "-wal"))
    else:
        changes, data_version = ("health_generation", generation), None
        # Checkpoint/archive/telemetry writes change these mtimes without
        # changing semantic health. The validated generation replaces them;
        # physical integrity and file identity retain independent guards.
        database_files = (identity,)
    paths = (config.context_vector_path, config.vector_path,
             vector_provenance.path(config),
             str(config.context_vector_path) + ".dirty",
             str(config.vector_path) + ".dirty",
             config.context_vector_path.with_suffix(".recovery.json"))
    return (
        id(service), id(connection), changes, data_version, schema,
        connection.in_transaction,
        tuple((name, repr(value)) for name, value in vars(config).items()),
        server.context_mode, server.adapter, service._mode, service._adapter,
        id(service.embedding_engine),
        bool(getattr(service.embedding_engine, "is_loaded", False)),
        _index_state(service.vector_index),
        _index_state(service._legacy_vector),
        service._integrity_cache_state(),
        database_files + tuple(_stamp(path) for path in paths),
    )


def current_status(server, evaluate):
    entry = _entry(server)
    with entry.lock:
        try:
            state = _state(server)
        except Exception:
            health_event('state_unavailable')
            entry.snapshot = None
            return None
        previous = entry.snapshot
        now = time.monotonic()
        if (previous is not None and previous[0] == state
                and 0 <= now - previous[1] < _ttl() and not state[5]):
            health_event('cache_hit')
            return deepcopy(previous[2])
        entry.snapshot = None
        for attempt in range(2):
            started = time.perf_counter()
            try:
                result = evaluate()
                elapsed_ms = (time.perf_counter() - started) * 1000
                after = _state(server)
            except Exception:
                elapsed_ms = (time.perf_counter() - started) * 1000
                result, after = None, state
            health_evaluation(elapsed_ms, getattr(result, 'ready', None),
                              [name for name, before_value, after_value in
                               zip(HEALTH_STATE_FIELDS, state, after) if before_value != after_value])
            if after == state:
                entry.snapshot = (state, time.monotonic(), deepcopy(result))
                return deepcopy(result)
            # A shared vector reader can refresh its resident native handle
            # while inspecting health. Discard that observation and recheck
            # the new generation once; continuous writers remain fail-closed.
            state = after
        return None
