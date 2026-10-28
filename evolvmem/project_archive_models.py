"""Project archive schema: one current summary plus source-backed events."""

PROJECT_ARCHIVE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS project_archives(
        project TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', started_at TEXT, ended_at TEXT,
        status TEXT NOT NULL DEFAULT 'active', source_watermark TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS project_events(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('major_change','feature','problem','milestone')),
        occurred_at TEXT, recorded_at TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
        source_ref TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','corrected','ignored')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_project_events_timeline
        ON project_events(project,occurred_at,event_id)""",
)
