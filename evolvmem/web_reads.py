"""Independent SQLite snapshots for audited, side-effect-free workbench reads."""
from contextlib import contextmanager
import sqlite3

from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore

# Some GETs (notably project-memory) refresh a persisted projection. Keep those
# on the ordinary service until their write/read boundary is explicitly split.
ROUTES = frozenset('/api/knowledge/' + route for route in (
    'projects', 'rules', 'directory', 'equivalences', 'equivalences/candidates',
    'organization', 'organization/guidance', 'organization/settings',
    'organization/groups', 'organization/groups/detail',
    'organization/state', 'organization/units', 'organization/detail',
    'organization/context', 'organization/progress',
    'organization/groups/recent-analysis', 'organization/groups/analysis',
))


@contextmanager
def snapshot(config):
    store = ContextStore(config)
    conn = sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    store._conn = conn
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        # No initialization, migrations, models or background workers. This
        # facade owns only this request's connection and consistent read view.
        yield ContextService(config, store=store)
    finally:
        store.close()
