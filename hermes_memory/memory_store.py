"""SQLite memory store: metadata + FTS5 full-text index + trigram Chinese substring index."""

import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_memory.config import Config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """SQLite memory store with FTS5 and trigram dual indexing."""

    def __init__(self, config: Config):
        self.config = config
        self._conn: sqlite3.Connection | None = None
        self._has_trigram: bool | None = None

    # ---- lifecycle ----

    def initialize(self) -> None:
        """Open database, create tables and indexes (idempotent)."""
        self.config.ensure_dirs()
        self._conn = sqlite3.connect(str(self.config.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        self._create_fts_indexes()
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()

    # ---- internal ----

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.execute(sql, params)
        return cur.fetchall()

    def _create_tables(self) -> None:
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key             TEXT NOT NULL,
            value           TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            category        TEXT DEFAULT '',
            tags            TEXT DEFAULT '',
            source_session  TEXT DEFAULT '',
            access_count    INTEGER DEFAULT 0,
            last_accessed   TEXT DEFAULT NULL,
            supersedes      INTEGER REFERENCES memories(id),
            superseded_by   INTEGER REFERENCES memories(id),
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
        CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
        CREATE INDEX IF NOT EXISTS idx_memories_key_status
            ON memories(key, status);
        """)

    def _create_fts_indexes(self) -> None:
        # Check if trigram tokenizer is available
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_trigram "
                "USING fts5(value, tags, content=memories, content_rowid=id, "
                "tokenize='trigram')"
            )
            self._has_trigram = True
        except sqlite3.OperationalError:
            self._has_trigram = False

        # Standard FTS5 index (unicode61 tokenizer)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
            "USING fts5(value, tags, content=memories, content_rowid=id, "
            "tokenize='unicode61')"
        )

        # Triggers to keep FTS5 indexes in sync with memories table
        self._conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, value, tags) VALUES (new.id, new.value, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, value, tags)
                VALUES ('delete', old.id, old.value, old.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, value, tags)
                VALUES ('delete', old.id, old.value, old.tags);
            INSERT INTO memories_fts(rowid, value, tags)
                VALUES (new.id, new.value, new.tags);
        END;
        """)

        # Triggers for trigram FTS5 index (if available)
        if self._has_trigram:
            self._conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS memories_ai_trigram AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts_trigram(rowid, value, tags) VALUES (new.id, new.value, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_ad_trigram AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts_trigram(memories_fts_trigram, rowid, value, tags)
                    VALUES ('delete', old.id, old.value, old.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_au_trigram AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts_trigram(memories_fts_trigram, rowid, value, tags)
                    VALUES ('delete', old.id, old.value, old.tags);
                INSERT INTO memories_fts_trigram(rowid, value, tags)
                    VALUES (new.id, new.value, new.tags);
            END;
            """)

    def _has_cjk(self, text: str) -> bool:
        return bool(re.search(r'[一-鿿㐀-䶿]', text))

    def _search_like(self, query: str, top_k: int = 20) -> list[dict]:
        """LIKE substring search - fallback when trigram is unavailable."""
        pattern = f"%{query}%"
        rows = self._execute(
            "SELECT *, rank FROM ("
            "  SELECT m.*, 1.0 as rank FROM memories m "
            "  WHERE m.value LIKE ? AND m.status != 'deleted'"
            "  UNION ALL"
            "  SELECT m.*, 0.5 as rank FROM memories m "
            "  WHERE m.tags LIKE ? AND m.status != 'deleted'"
            ") ORDER BY rank DESC LIMIT ?",
            (pattern, pattern, top_k),
        )
        return [dict(r) for r in rows]

    # ---- write ----

    def _insert_row(self, key: str, value: str, category: str,
                    tag_str: str, source_session: str,
                    supersedes: int | None) -> int:
        """Insert a row into memories and return its id. Does NOT commit."""
        now = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO memories (key, value, status, category, tags, "
            "source_session, supersedes, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (key, value, category, tag_str, source_session, supersedes, now, now),
        )
        return cur.lastrowid

    def add(self, key: str, value: str, category: str = "",
            tags: list[str] | None = None,
            source_session: str = "",
            supersedes: int | None = None) -> int:
        """Insert a new active memory. Returns the new record id."""
        tag_str = ",".join(tags) if tags else ""
        new_id = self._insert_row(key, value, category, tag_str,
                                  source_session, supersedes)
        self._conn.commit()
        return new_id

    def add_if_changed(self, key: str, value: str, **kwargs) -> int | None:
        """Only insert if value differs from current active. Returns None if skipped."""
        existing = self._get_active_by_key(key)
        if existing and existing["value"] == value:
            return None
        return self.add(key=key, value=value, **kwargs)

    def replace(self, key: str, new_value: str, **kwargs) -> int:
        """Replace old active with new value. Old marked as superseded, new as active.

        Wrapped in a single explicit transaction so no dual-active window exists:
        at no point can two records with the same key both be 'active'.
        """
        old = self._get_active_by_key(key)
        if old is None:
            return self.add(key=key, value=new_value, **kwargs)

        old_id = old["id"]
        tag_str = ",".join(kwargs.pop("tags", [])) if "tags" in kwargs else ""

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            new_id = self._insert_row(
                key, new_value,
                kwargs.pop("category", ""),
                tag_str,
                kwargs.pop("source_session", ""),
                old_id,
            )
            now = _now_iso()
            self._conn.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, "
                "updated_at=? WHERE id=?",
                (new_id, now, old_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_id

    def remove(self, mem_id: int) -> None:
        """Soft delete: mark status as deleted."""
        self._conn.execute(
            "UPDATE memories SET status='deleted', updated_at=? WHERE id=?",
            (_now_iso(), mem_id),
        )
        self._conn.commit()

    # ---- queries ----

    def get_active(self) -> list[dict]:
        """Return all status='active' memories, ordered by updated_at descending."""
        rows = self._execute(
            "SELECT * FROM memories WHERE status='active' "
            "ORDER BY updated_at DESC"
        )
        return [dict(r) for r in rows]

    def get_by_id(self, mem_id: int) -> dict | None:
        rows = self._execute("SELECT * FROM memories WHERE id=?", (mem_id,))
        return dict(rows[0]) if rows else None

    def get_by_key(self, key: str) -> list[dict]:
        """Return all records for a given key (including history), sorted by updated_at desc."""
        rows = self._execute(
            "SELECT * FROM memories WHERE key=? ORDER BY updated_at DESC",
            (key,),
        )
        return [dict(r) for r in rows]

    def _get_active_by_key(self, key: str) -> dict | None:
        rows = self._execute(
            "SELECT * FROM memories WHERE key=? AND status='active'",
            (key,),
        )
        return dict(rows[0]) if rows else None

    def get_by_ids(self, ids: list[int]) -> list[dict]:
        """Batch fetch records by id."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return [dict(r) for r in rows]

    def update_access(self, mem_id: int) -> None:
        """Increment access_count and update last_accessed (called on retrieval hit)."""
        self._conn.execute(
            "UPDATE memories SET access_count = access_count + 1, "
            "last_accessed = ?, updated_at = ? WHERE id = ?",
            (_now_iso(), _now_iso(), mem_id),
        )
        self._conn.commit()

    # ---- full-text search ----

    def search_fts(self, query: str, top_k: int = 20) -> list[dict]:
        """FTS5 full-text search, auto-selects trigram or unicode61 index.

        Always supplements with LIKE for CJK queries since short Chinese
        substrings (e.g. 2-char "退款") may not produce valid trigrams.
        """
        if self._has_trigram and self._has_cjk(query):
            results = self._search_fts5("memories_fts_trigram", query, top_k)
        else:
            results = self._search_fts5("memories_fts", query, top_k)

        # Supplement with LIKE for CJK queries regardless of tokenizer
        if self._has_cjk(query):
            like_results = self._search_like(query, top_k)
            seen = {r["id"] for r in results}
            for r in like_results:
                if r["id"] not in seen:
                    results.append(r)
        return results

    def _search_fts5(self, table: str, query: str, top_k: int) -> list[dict]:
        safe_query = self._sanitize_fts5_query(query)
        try:
            rows = self._execute(
                f"SELECT m.*, rank FROM {table} f "
                "JOIN memories m ON m.id = f.rowid "
                f"WHERE {table} MATCH ? AND m.status != 'deleted' "
                "ORDER BY rank LIMIT ?",
                (safe_query, top_k),
            )
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def _sanitize_fts5_query(self, query: str) -> str:
        """Sanitize FTS5 query to avoid syntax errors."""
        query = query.strip().strip('"')
        # Escape FTS5 special characters
        query = query.replace('"', '""')
        return f'"{query}"'

    def all_ids(self) -> list[int]:
        """Return ids of all non-deleted records (for USearch sync)."""
        rows = self._execute(
            "SELECT id FROM memories WHERE status != 'deleted'"
        )
        return [r["id"] for r in rows]

    def count_active(self) -> int:
        rows = self._execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE status='active'"
        )
        return rows[0]["cnt"]

    def get_forgetting_candidates(self, days_threshold: int,
                                  access_threshold: int,
                                  rate_limit_days: int) -> list[dict]:
        """Return candidates eligible for archival (downgrade)."""
        rows = self._execute(
            "SELECT * FROM memories WHERE status='active' "
            "AND (last_accessed IS NULL OR last_accessed < datetime('now', ?)) "
            "AND access_count <= ? "
            "AND (updated_at IS NULL OR updated_at < datetime('now', ?))",
            (f"-{days_threshold} days", access_threshold,
             f"-{rate_limit_days} days"),
        )
        return [dict(r) for r in rows]

    def archive(self, mem_id: int) -> None:
        """Downgrade memory to archived."""
        self._conn.execute(
            "UPDATE memories SET status='archived', updated_at=? WHERE id=?",
            (_now_iso(), mem_id),
        )
        self._conn.commit()
