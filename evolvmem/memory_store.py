"""SQLite memory store: metadata + FTS5 full-text index + trigram Chinese substring index."""

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

from evolvmem.config import Config
from evolvmem.legacy_projection import (
    LegacyProjectionInsert,
    LegacyProjectionReplace,
    LegacyProjectionRepository,
    LegacyProjectionUpdate,
    _has_cjk,
    _now_iso,
)


class MemoryStore:
    """Owning lifecycle/transaction wrapper over the legacy projection SQL."""

    def __init__(self, config: Config):
        self.config = config
        self._conn: sqlite3.Connection | None = None
        self._repository: LegacyProjectionRepository | None = None
        self._has_trigram: bool | None = None
        self._transaction_depth = 0

    # ---- lifecycle ----

    def initialize(self) -> None:
        """Open database, create tables and indexes (idempotent)."""
        self.config.ensure_dirs()
        # check_same_thread=False: MCP server 在后台线程初始化（建连接）、
        # 主线程处理 tools/call；两边由 _init_done 门闩串行化，不会并发访问
        self._conn = sqlite3.connect(str(self.config.db_path),
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        self._create_fts_indexes()
        self._conn.commit()
        self._repository = LegacyProjectionRepository(
            self._conn, self._require_transaction
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
            self._repository = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()

    # ---- internal ----

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.execute(sql, params)
        return cur.fetchall()

    def _projection(self) -> LegacyProjectionRepository:
        if self._repository is None:
            raise RuntimeError("MemoryStore is not initialized")
        return self._repository

    def _require_transaction(self, operation: str) -> None:
        if self._transaction_depth == 0:
            raise RuntimeError(
                f"{operation} requires an active MemoryStore transaction"
            )

    def _create_tables(self) -> None:
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key             TEXT NOT NULL,
            value           TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            attribute        TEXT DEFAULT '',
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

        # --- 幂等迁移：category 列改名 attribute（2026-07-28 属性/分类重命名）---
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(memories)")}
        if "attribute" not in cols and "category" in cols:
            self._conn.execute(
                "ALTER TABLE memories RENAME COLUMN category TO attribute"
            )
            cols.discard("category")
            cols.add("attribute")

        # --- 幂等迁移：importance / tier 两列（2026-07-28 tiered injection）---
        migrated = False
        if "importance" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN importance REAL NOT NULL DEFAULT 5.0"
            )
            migrated = True
        if "tier" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'normal'"
            )
            migrated = True
        # expires_at 独立迁移：无需回填，不影响 migrated 标志
        if "expires_at" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN expires_at TEXT DEFAULT NULL"
            )
        if migrated:
            self._backfill_importance_tier()

    # attribute → 默认 importance（1-10）；pinned 类别集合
    _IMPORTANCE_BY_ATTRIBUTE = {
        "constraint": 8.0,
        "decision": 7.0,
        "preference": 6.0,
        "user_profile": 6.0,
    }
    _PINNED_CATEGORIES = ("constraint", "preference", "user_profile")

    def _backfill_importance_tier(self) -> None:
        """按 attribute 规则回填 importance/tier。仅在迁移（新增列）时调用一次。"""
        self._conn.execute(
            "UPDATE memories SET importance = CASE attribute "
            "WHEN 'constraint' THEN 8.0 "
            "WHEN 'decision' THEN 7.0 "
            "WHEN 'preference' THEN 6.0 "
            "WHEN 'user_profile' THEN 6.0 "
            "ELSE 5.0 END"
        )
        self._conn.execute(
            "UPDATE memories SET tier = 'pinned' "
            "WHERE attribute IN ('constraint', 'preference', 'user_profile')"
        )

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
        return _has_cjk(text)

    def _search_like(self, query: str, top_k: int = 20) -> list[dict]:
        """LIKE substring search - fallback when trigram is unavailable."""
        return self._projection()._search_like(query, top_k)

    # ---- write ----

    @contextmanager
    def transaction(self) -> Iterator["MemoryStore"]:
        outermost = self._transaction_depth == 0
        if outermost:
            self._conn.execute("BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield self
            if outermost:
                self._conn.commit()
        except Exception:
            if outermost:
                self._conn.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def add(self, key: str, value: str, attribute: str = "",
            tags: list[str] | None = None,
            source_session: str = "",
            supersedes: int | None = None,
            importance: float = 5.0, tier: str = "normal",
            expires_at: str | None = None) -> int:
        """Insert a new active memory. Returns the new record id."""
        request = LegacyProjectionInsert(
            key=key,
            value=value,
            attribute=attribute,
            tags=tuple(tags) if tags else (),
            source_session=source_session,
            supersedes=supersedes,
            importance=importance,
            tier=tier,
            expires_at=expires_at,
        )
        if self._transaction_depth:
            return self._projection().insert(request)
        with self.transaction():
            return self._projection().insert(request)

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
        request = LegacyProjectionReplace(
            key=key,
            new_value=new_value,
            attribute=kwargs.pop("attribute", None),
            tags=kwargs.pop("tags", None),
            source_session=kwargs.pop("source_session", ""),
            importance=kwargs.pop("importance", None),
            tier=kwargs.pop("tier", None),
            expires_at=kwargs.pop("expires_at", None),
        )
        if self._transaction_depth:
            return self._projection().replace(request)[1]
        with self.transaction():
            return self._projection().replace(request)[1]

    def remove(self, mem_id: int) -> None:
        """Soft delete: mark status as deleted."""
        if self._transaction_depth:
            self._projection().soft_delete(mem_id)
            return
        with self.transaction():
            self._projection().soft_delete(mem_id)

    def update_metadata(self, mem_id: int, importance: float | None = None,
                        tier: str | None = None) -> None:
        """Update importance/tier in place (used by batch rescoring)."""
        request = LegacyProjectionUpdate(
            legacy_id=mem_id, importance=importance, tier=tier
        )
        if self._transaction_depth:
            self._projection().update_metadata(request)
            return
        with self.transaction():
            self._projection().update_metadata(request)

    # ---- queries ----

    def get_active(self) -> list[dict]:
        """Return all status='active' and unexpired memories, ordered by updated_at descending.

        Expired memories (expires_at <= now) keep status='active' but are
        excluded here until the forgetting engine archives them.
        """
        return self._projection().get_active()

    def get_by_id(self, mem_id: int) -> dict | None:
        return self._projection().get_by_id(mem_id)

    def get_by_key(self, key: str) -> list[dict]:
        """Return all records for a given key (including history), sorted by updated_at desc."""
        return self._projection().get_by_key(key)

    def _get_active_by_key(self, key: str) -> dict | None:
        return self._projection()._get_active_by_key(key)

    def get_by_ids(self, ids: list[int]) -> list[dict]:
        """Batch fetch records by id."""
        return self._projection().get_by_ids(ids)

    def update_access(self, mem_id: int) -> None:
        """Increment access_count and update last_accessed (called on retrieval hit).

        Does NOT touch updated_at — recency ordering must reflect writes, not reads.
        """
        if self._transaction_depth:
            self._projection().update_access((mem_id,))
            return
        with self.transaction():
            self._projection().update_access((mem_id,))

    # ---- full-text search ----

    def search_fts(self, query: str, top_k: int = 20) -> list[dict]:
        """FTS5 full-text search, auto-selects trigram or unicode61 index.

        Always supplements with LIKE for CJK queries since short Chinese
        substrings (e.g. 2-char "退款") may not produce valid trigrams.
        """
        return self._projection().search_fts(query, top_k)

    def all_ids(self) -> list[int]:
        """Return ids of all non-deleted records (for USearch sync)."""
        return self._projection().all_ids()

    def count_active(self) -> int:
        """Count status='active' and unexpired memories (same scope as get_active)."""
        return self._projection().count_active()

    def get_forgetting_candidates(self, days_threshold: int,
                                  access_threshold: int,
                                  rate_limit_days: int) -> list[dict]:
        """Return candidates eligible for archival (downgrade).

        A record is a candidate when:
        - last_accessed is either NULL (never accessed) or older than days_threshold
        - access_count is at or below access_threshold
        - updated_at is either NULL or at least rate_limit_days ago (<= so same-second
          updates when threshold is 0 also qualify)
        - tier is not 'pinned' — pinned memories are durable rules/preferences
          and must never be auto-archived regardless of usage
        """
        return self._projection().get_forgetting_candidates(
            days_threshold=days_threshold,
            access_threshold=access_threshold,
            rate_limit_days=rate_limit_days,
        )

    def archive(self, mem_id: int) -> None:
        """Downgrade memory to archived."""
        if self._transaction_depth:
            self._projection().set_status(mem_id, "archived")
            return
        with self.transaction():
            self._projection().set_status(mem_id, "archived")
