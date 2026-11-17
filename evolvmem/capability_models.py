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

CAPABILITY_CATEGORY_LABELS = {
    "frontend": "前端",
    "backend": "后端",
    "business_logic": "业务逻辑",
    "database": "数据库与数据处理",
    "architecture": "架构与技术设计",
    "quality": "测试与问题治理",
    "delivery": "部署与工程协作",
}

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
    """CREATE TABLE IF NOT EXISTS capability_sources(
        source_id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id INTEGER NOT NULL REFERENCES capability_memories(memory_id),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('context','unit','reference','manual')),
        source_key TEXT NOT NULL, source_ref TEXT NOT NULL,
        source_revision TEXT NOT NULL DEFAULT '', context_item_id INTEGER, task_id INTEGER,
        unit_digest TEXT NOT NULL DEFAULT '', unit_revision INTEGER,
        projects TEXT NOT NULL DEFAULT '[]', knowledge_scope TEXT NOT NULL DEFAULT 'project',
        evidence_status TEXT NOT NULL DEFAULT 'unverified',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(memory_id,source_kind,source_key))""",
    """CREATE TABLE IF NOT EXISTS capability_assessments(
        assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id INTEGER NOT NULL REFERENCES capability_memories(memory_id),
        source_id INTEGER NOT NULL, source_binding_revision INTEGER NOT NULL,
        source_revision TEXT NOT NULL, unit_revision INTEGER, unit_text_hash TEXT NOT NULL,
        policy_version TEXT NOT NULL, assessment_key TEXT NOT NULL,
        value TEXT NOT NULL CHECK(value IN ('reusable','reference','review','ignore')),
        verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK(verification_status='unverified'),
        score INTEGER NOT NULL, details TEXT NOT NULL,
        legacy_active_preserved INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(source_id,assessment_key), UNIQUE(source_id,revision))""",
    """CREATE INDEX IF NOT EXISTS idx_capability_assessment_memory
        ON capability_assessments(memory_id,source_id,assessment_id)""",
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
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_restructure_state(
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_restructure_map(
        legacy_item_id INTEGER PRIMARY KEY, target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL, outcome TEXT NOT NULL, updated_at TEXT NOT NULL)""",
)
