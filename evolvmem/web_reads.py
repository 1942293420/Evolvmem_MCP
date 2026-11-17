"""Independent SQLite snapshots for audited, side-effect-free workbench reads."""
from contextlib import contextmanager
import sqlite3

from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore

READ_POSTS = frozenset('/api/knowledge/' + name for name in (
    'preview', 'extraction/preview', 'cleaning/preview',
    'history/organization/preview', 'organization/groups/preview',
    'skills/cleaning/preview',
)) | {'/api/projects/suggest_display_names', '/api/memories/organize_suggest'}


def is_preview(path, body):
    return path in READ_POSTS or (path == '/api/knowledge/organize'
                                 and isinstance(body, dict) and not body.get('apply'))


class ReadService(ContextService):
    # The legacy projection is already in this database; never initialize a
    # second connection, run migrations or perform a serving-mode transition.
    def _legacy_reader(self):
        return self.store.legacy_projection()


@contextmanager
def snapshot(config, *, source=None):
    store = ContextStore(config)
    conn = sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    store._conn = conn
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        # No initialization, migrations, models or background workers. This
        # facade owns only this request's connection and consistent read view.
        reader = ReadService(config, store=store)
        if source is not None:
            # Search retains its configured model/index, but binds database
            # retrieval to this request's snapshot. Search never writes usage.
            reader.embedding_engine = source.embedding_engine
            reader.vector_index = source.vector_index
            if source.retriever is not None:
                from evolvmem.context_retriever import ContextRetriever
                reader.retriever = ContextRetriever(config, store, source.vector_index, source.embedding_engine)
        yield reader
    finally:
        store.close()
