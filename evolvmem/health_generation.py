"""Durable semantic-health generations; unrelated intake remains writable.

This contract covers every SQLite input of ContextService.evaluated_status:
projection/mapping/layers, vector document eligibility, unit backing, inferred
rule confirmation and derived rollup currency. Track whole rows deliberately,
including raw layer content updates that do not refresh content_hash.
Physical integrity, database identity, schema, config and vector files remain
independent request_health guards. Older or changed schemas use its original
conservative all-commit token until startup explicitly prepares this contract.
LAN startup installs it; standalone databases are not implicitly upgraded.
"""

VERSION = 1
TABLES = (
    'memories', 'context_items', 'context_layers', 'context_sources',
    'legacy_memory_migrations', 'learning_memories', 'unit_derivations',
    'organization_tasks', 'organization_units', 'organization_conversations',
    'context_project_resolutions', 'context_project_rollups',
)


def _triggers():
    for table in TABLES:
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'health_generation_{table}_{action.lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {action} ON {table} '
                         'BEGIN UPDATE context_health_generation '
                         'SET generation=generation+1 WHERE id=1; END')


def prepare(store):
    """Install after namespace bootstrap, never while serving a read request."""
    with store.transaction():
        conn = store._connection()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES) <= tables:
            return  # An older/incomplete namespace retains conservative checks.
        conn.execute('CREATE TABLE IF NOT EXISTS context_health_generation ('
                     'id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, '
                     'generation INTEGER NOT NULL CHECK(generation>=0), schema_version INTEGER NOT NULL)')
        existing = {row[0]: row[1] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        for name, sql in _triggers():
            if existing.get(name) == sql:
                continue
            conn.execute(f'DROP TRIGGER IF EXISTS {name}')
            conn.execute(sql)
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        conn.execute('INSERT INTO context_health_generation VALUES(1,?,0,?) '
                     'ON CONFLICT(id) DO UPDATE SET version=excluded.version, '
                     'schema_version=excluded.schema_version '
                     'WHERE version!=excluded.version OR schema_version!=excluded.schema_version',
                     (VERSION, schema))


def observed(store, schema):
    """Return a trusted generation, or None for the conservative fallback.

    Schema cookies invalidate the cached trigger verification. Counter data is
    always read afresh, including commits made by another native process.
    """
    conn = store._connection()
    try:
        row = conn.execute('SELECT version,generation,schema_version '
                           'FROM context_health_generation WHERE id=1').fetchone()
        if (row is None or row[0] != VERSION or row[2] != schema
                or type(row[1]) is not int or row[1] < 0):
            return None
        cached = getattr(store, '_health_generation_contract', None)
        key = (id(conn), schema)
        if cached is None or cached[0] != key:
            definitions = {r[0]: r[1] for r in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
            valid = all(definitions.get(name) == sql for name, sql in _triggers())
            store._health_generation_contract = (key, valid)
        else:
            valid = cached[1]
        return row[1] if valid else None
    except Exception:
        return None
