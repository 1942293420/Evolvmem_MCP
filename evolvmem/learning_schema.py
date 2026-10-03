"""Additive learning tables; existing memories and evidence remain authoritative."""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS learning_memories (
        item_id INTEGER PRIMARY KEY REFERENCES context_items(id) ON DELETE CASCADE,
        category TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS learning_project_types (
        project TEXT PRIMARY KEY REFERENCES context_project_registry(project), family TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS learning_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), framework TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS learning_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE NOT NULL,
        topic TEXT NOT NULL, instruction TEXT NOT NULL, trigger_text TEXT NOT NULL,
        rationale TEXT NOT NULL, exceptions TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL,
        sources TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL, reason TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS learning_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, framework TEXT NOT NULL, rules TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS learning_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, target TEXT NOT NULL,
        input_ids TEXT NOT NULL, result_ids TEXT NOT NULL, status TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
)
