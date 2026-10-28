"""Development-capability taxonomy and additive persistence schema."""

CAPABILITY_CATEGORIES = (
    "frontend",
    "backend",
    "business_logic",
    "database",
    "architecture",
    "quality",
    "delivery",
)

CAPABILITY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS capability_categories(
        id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS capability_memories(
        memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL REFERENCES capability_categories(id),
        title TEXT NOT NULL, content TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]',
        source_project TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
        occurred_at TEXT, fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','pending','ignored','archived')),
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_capability_recall
        ON capability_memories(category,status,memory_id)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS capability_memories_fts USING fts5(
        title,content,keywords,content='capability_memories',content_rowid='memory_id',
        tokenize='unicode61')""",
    """CREATE TABLE IF NOT EXISTS capability_category_suggestions(
        suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL, reason TEXT NOT NULL, source_memory_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
        created_at TEXT NOT NULL, reviewed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS capability_intake(
        intake_id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, content TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]',
        suggested_category TEXT NOT NULL DEFAULT '', source_project TEXT NOT NULL DEFAULT '',
        source_ref TEXT NOT NULL DEFAULT '', occurred_at TEXT, fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','assigned','ignored')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_restructure_state(
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_restructure_map(
        legacy_item_id INTEGER PRIMARY KEY, target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL, outcome TEXT NOT NULL, updated_at TEXT NOT NULL)""",
)
