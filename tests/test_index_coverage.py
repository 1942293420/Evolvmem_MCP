"""Every hot predicate must have an index.

This exists because it did not, and that cost a 39 s page. The schema declares
indexes by hand as queries slow down, and columns added later by the incremental
``ALTER TABLE`` migration have no place to declare one — ``organization_units
.knowledge_id`` is such a column, and every fact verification scanned 11820 rows
because of it.

The list below is the set of columns this application filters or joins on. A
query that filters one of them without a leading index degenerates into a full
table scan, and the cost grows with the table rather than staying constant.

When a new hot predicate is added, add it here and index it; the test then holds
the line. ``audit_query_plans.py`` in the tooling directory is the companion that
finds new ones by reading real query plans.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

REQUIRED = (
    ('organization_units', 'knowledge_id'),
    ('organization_units', 'item_id'),
    ('organization_units', 'task_id'),
    ('session_archives', 'project'),
    ('session_archives', 'state'),
    ('context_sources', 'item_id'),
    ('context_sources', 'archive_id'),
    ('context_items', 'status'),
    ('context_items', 'project'),
    ('knowledge_objects', 'item_id'),
    ('knowledge_fact_sources', 'active'),
    ('knowledge_fact_relations', 'version_id'),
    ('lan_session_uploads', 'archive_id'),
)


def leading_indexed(conn, table, column):
    """True when ``column`` is the first column of some index on ``table``."""
    for index in conn.execute('PRAGMA index_list("%s")' % table):
        name = index[1]
        info = conn.execute('PRAGMA index_info("%s")' % name).fetchall()
        if info and info[0][2] == column:
            return True
    return False


def test_hot_predicates_are_indexed():
    from evolvmem.config import Config
    from evolvmem.context_store import ContextStore

    with tempfile.TemporaryDirectory() as tmp:
        store = ContextStore(Config(data_dir=Path(tmp)))
        try:
            store.initialize()
        except TypeError:
            from evolvmem.context_models import ContextMode
            store.initialize(mode=ContextMode.SHADOW)
        conn = store._connection()
        missing = []
        for table, column in REQUIRED:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            if not leading_indexed(conn, table, column):
                missing.append('%s.%s' % (table, column))
        store.close()

    assert not missing, (
        'these filtered columns have no leading index and would scan the whole '
        'table: ' + ', '.join(missing))
