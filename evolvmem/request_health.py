"""Reuse a health observation within one request, never across requests."""
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


def current_status(server, evaluate):
    observations = _observations.get()
    if observations is None:
        return evaluate()
    try:
        state = _state(server)
    except Exception:
        # Unknown observations must never authorize reuse of earlier health.
        return evaluate()
    previous = observations.get(server)
    if previous is not None and previous[0] == state:
        return previous[1]
    result = evaluate()
    # Evaluating health can itself refresh a resident vector image. Bind the
    # result to the state actually checked, without masking a concurrent write.
    observations[server] = (state, result)
    return result
