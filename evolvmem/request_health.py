"""Reuse a health observation within one request, never across requests."""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from evolvmem import vector_provenance

_observations = ContextVar('evolvmem_request_health', default=None)


@contextmanager
def request_health_scope():
    token = _observations.set({})
    try:
        yield
    finally:
        _observations.reset(token)


def _stamp(path):
    try:
        info = Path(path).stat()
        return info.st_ino, info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def _state(server):
    service = server.context_service
    connection = service.store._connection()
    config = server.config
    paths = (config.db_path, str(config.db_path) + '-wal',
             config.context_vector_path, config.vector_path,
             vector_provenance.path(config))
    index = service.vector_index
    return (
        id(service), id(connection), connection.total_changes,
        connection.execute('PRAGMA data_version').fetchone()[0],
        connection.execute('PRAGMA schema_version').fetchone()[0],
        connection.in_transaction,
        tuple((name, repr(value)) for name, value in vars(config).items()),
        server.context_mode, server.adapter,
        bool(getattr(service.embedding_engine, 'is_loaded', False)),
        id(index), index.count(), index.is_dirty(),
        tuple(_stamp(path) for path in paths),
    )


# Health reuse is bounded in time as well as by state: a fingerprint cannot see
# every conceivable degradation, so an answer is never reused for longer than this
# even when the state looks unchanged.
_REUSE_TTL_S = 5.0


def current_status(server, evaluate):
    observations = _observations.get()
    try:
        state = _state(server)
    except Exception:
        # Unknown observations must never authorize reuse of earlier health.
        return evaluate()
    # Cross-request reuse: _state already covers writes from this connection and,
    # through PRAGMA data_version, from every other one. The evaluation is a pure
    # function of that state, so an unchanged state means an unchanged answer.
    # The TTL is a belt-and-braces bound for anything the fingerprint cannot see.
    cached = getattr(server, '_health_state_cache', None)
    if cached is not None and cached[0] == state:
        if (time.monotonic() - cached[2]) <= _REUSE_TTL_S:
            if observations is not None:
                observations[server] = (state, cached[1])
            return cached[1]
    result = evaluate()
    # Bind the result to the state actually checked, without masking a concurrent
    # write: a change inside evaluate() makes the next call's state differ.
    server._health_state_cache = (state, result, time.monotonic())
    if observations is not None:
        observations[server] = (state, result)
    return result
