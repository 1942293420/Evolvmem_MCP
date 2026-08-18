"""Legacy memories-table projection SQL on a borrowed SQLite connection."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sqlite3


def _now_iso() -> str:
    """Return UTC now in SQLite-compatible datetime format for correct comparison."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[一-鿿㐀-䶿]", text))


@dataclass(frozen=True, slots=True)
class LegacyProjectionInsert:
    """One legacy projection row to insert as active."""

    key: str
    value: str
    attribute: str = ""
    tags: tuple[str, ...] = ()
    source_session: str = ""
    supersedes: int | None = None
    importance: float = 5.0
    tier: str = "normal"
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyProjectionReplace:
    """Replace the active row for a key; None fields inherit the old row."""

    key: str
    new_value: str
    attribute: str | None = None
    tags: tuple[str, ...] | None = None
    source_session: str = ""
    importance: float | None = None
    tier: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyProjectionUpdate:
    """In-place metadata edit; None fields keep their stored values."""

    legacy_id: int
    importance: float | None = None
    tier: str | None = None


class LegacyProjectionRepository:
    """Legacy memories projection on a connection owned by someone else.

    The repository has no lifecycle, commit, or transaction powers: every
    write requires the owner's active transaction, so projection changes
    commit or roll back with the owner's outer boundary.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        require_transaction: Callable[[str], None],
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if not callable(require_transaction):
            raise TypeError("require_transaction must be callable")
        connection.row_factory = sqlite3.Row
        self._conn = connection
        self._require_owner_transaction = require_transaction
        self._has_trigram: bool | None = None

    # ---- narrow reads used by legacy consumers ----

    def get_by_id(self, legacy_id: int) -> dict | None:
        rows = self._execute("SELECT * FROM memories WHERE id=?", (legacy_id,))
        return dict(rows[0]) if rows else None

    def get_by_key(self, key: str) -> list[dict]:
        """Return all records for a given key (including history), sorted by updated_at desc."""
        rows = self._execute(
            "SELECT * FROM memories WHERE key=? ORDER BY updated_at DESC",
            (key,),
        )
        return [dict(row) for row in rows]

    def get_by_ids(self, ids: list[int]) -> list[dict]:
        """Batch fetch records by id."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return [dict(row) for row in rows]

    def get_active(self) -> list[dict]:
        """Return all status='active' and unexpired memories, ordered by updated_at descending.

        Expired memories (expires_at <= now) keep status='active' but are
        excluded here until the forgetting engine archives them.
        """
        rows = self._execute(
            "SELECT * FROM memories WHERE status='active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY updated_at DESC",
            (_now_iso(),),
        )
        return [dict(row) for row in rows]

    def search_fts(self, query: str, top_k: int = 20) -> list[dict]:
        """FTS5 full-text search, auto-selects trigram or unicode61 index.

        Always supplements with LIKE for CJK queries since short Chinese
        substrings (e.g. 2-char "退款") may not produce valid trigrams.
        """
        if self._has_trigram_index() and _has_cjk(query):
            results = self._search_fts5("memories_fts_trigram", query, top_k)
        else:
            results = self._search_fts5("memories_fts", query, top_k)

        # Supplement with LIKE for CJK queries regardless of tokenizer
        if _has_cjk(query):
            like_results = self._search_like(query, top_k)
            seen = {row["id"] for row in results}
            for row in like_results:
                if row["id"] not in seen:
                    results.append(row)
        return results

    def all_ids(self) -> list[int]:
        """Return ids of all non-deleted records (for USearch sync)."""
        rows = self._execute("SELECT id FROM memories WHERE status != 'deleted'")
        return [row["id"] for row in rows]

    def count_active(self) -> int:
        """Count status='active' and unexpired memories (same scope as get_active)."""
        rows = self._execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE status='active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (_now_iso(),),
        )
        return rows[0]["cnt"]

    def get_forgetting_candidates(self, **thresholds: object) -> list[dict]:
        """Return candidates eligible for archival (downgrade).

        A record is a candidate when:
        - last_accessed is either NULL (never accessed) or older than days_threshold
        - access_count is at or below access_threshold
        - updated_at is either NULL or at least rate_limit_days ago (<= so same-second
          updates when threshold is 0 also qualify)
        - tier is not 'pinned' — pinned memories are durable rules/preferences
          and must never be auto-archived regardless of usage
        """
        rows = self._execute(
            "SELECT * FROM memories WHERE status='active' "
            "AND tier != 'pinned' "
            "AND (last_accessed IS NULL OR last_accessed <= datetime('now', ?)) "
            "AND access_count <= ? "
            "AND (updated_at IS NULL OR updated_at <= datetime('now', ?))",
            (
                f"-{thresholds['days_threshold']} days",
                thresholds["access_threshold"],
                f"-{thresholds['rate_limit_days']} days",
            ),
        )
        return [dict(row) for row in rows]

    def get_expired_ids(self, now: str) -> list[int]:
        """Return active IDs whose expires_at has passed, in row order."""
        rows = self._execute(
            "SELECT id FROM memories WHERE status='active' "
            "AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY id",
            (now,),
        )
        return [row["id"] for row in rows]

    # ---- transaction-required projection primitives ----

    def insert(self, request: LegacyProjectionInsert) -> int:
        """Insert a new active projection row and return its legacy id."""
        self._require_owner_transaction("insert")
        expires_at = request.expires_at
        if expires_at and len(expires_at) == 10:
            expires_at += " 00:00:00"
        tag_str = ",".join(request.tags) if request.tags else ""
        return self._insert_row(
            request.key,
            request.value,
            request.attribute,
            tag_str,
            request.source_session,
            request.supersedes,
            importance=request.importance,
            tier=request.tier,
            expires_at=expires_at,
        )

    def replace(self, request: LegacyProjectionReplace) -> tuple[int | None, int]:
        """Supersede the active row for a key and insert its successor.

        Returns (superseded id or None, new id); omitted fields inherit the
        old row so the replacement keeps legacy metadata by default.
        """
        self._require_owner_transaction("replace")
        old = self._get_active_by_key(request.key)
        if old is None:
            new_id = self.insert(
                LegacyProjectionInsert(
                    key=request.key,
                    value=request.new_value,
                    attribute=request.attribute or "",
                    tags=request.tags or (),
                    source_session=request.source_session,
                    importance=5.0 if request.importance is None else request.importance,
                    tier="normal" if request.tier is None else request.tier,
                    expires_at=request.expires_at,
                )
            )
            return None, new_id

        old_id = int(old["id"])
        attribute = request.attribute if request.attribute is not None else old["attribute"]
        tag_str = ",".join(request.tags) if request.tags is not None else old["tags"]
        importance = (
            request.importance if request.importance is not None else old["importance"]
        )
        tier = request.tier if request.tier is not None else old["tier"]
        expires_at = (
            request.expires_at if request.expires_at is not None else old["expires_at"]
        )
        if expires_at and len(expires_at) == 10:
            expires_at += " 00:00:00"

        new_id = self._insert_row(
            request.key,
            request.new_value,
            attribute,
            tag_str,
            request.source_session,
            old_id,
            importance=importance,
            tier=tier,
            expires_at=expires_at,
        )
        self._conn.execute(
            "UPDATE memories SET status='superseded', superseded_by=?, "
            "updated_at=? WHERE id=?",
            (new_id, _now_iso(), old_id),
        )
        return old_id, new_id

    def soft_delete(self, legacy_id: int) -> bool:
        """Mark one row deleted; a nonexistent id is a compatible no-op."""
        return self.set_status(legacy_id, "deleted")

    def set_status(self, legacy_id: int, status: str) -> bool:
        """Set one row's status; returns False when the id does not exist."""
        self._require_owner_transaction("set_status")
        cursor = self._conn.execute(
            "UPDATE memories SET status=?, updated_at=? WHERE id=?",
            (status, _now_iso(), legacy_id),
        )
        return cursor.rowcount > 0

    def update_metadata(self, request: LegacyProjectionUpdate) -> bool:
        """Update importance/tier in place; None fields stay unchanged."""
        self._require_owner_transaction("update_metadata")
        updated = False
        if request.importance is not None:
            cursor = self._conn.execute(
                "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                (request.importance, _now_iso(), request.legacy_id),
            )
            updated = cursor.rowcount > 0
        if request.tier is not None:
            cursor = self._conn.execute(
                "UPDATE memories SET tier=?, updated_at=? WHERE id=?",
                (request.tier, _now_iso(), request.legacy_id),
            )
            updated = cursor.rowcount > 0 or updated
        return updated

    def update_access(self, legacy_ids: tuple[int, ...]) -> tuple[int, ...]:
        """Increment access_count once per existing id without touching updated_at."""
        self._require_owner_transaction("update_access")
        if not legacy_ids:
            return ()
        placeholders = ",".join("?" for _ in legacy_ids)
        existing = tuple(
            sorted(
                int(row["id"])
                for row in self._execute(
                    f"SELECT id FROM memories WHERE id IN ({placeholders})",
                    tuple(legacy_ids),
                )
            )
        )
        if not existing:
            return ()
        existing_placeholders = ",".join("?" for _ in existing)
        self._conn.execute(
            "UPDATE memories SET access_count = access_count + 1, "
            f"last_accessed = ? WHERE id IN ({existing_placeholders})",
            (_now_iso(), *existing),
        )
        return existing

    def hard_delete(self, legacy_id: int) -> bool:
        """Physically remove one row; FTS triggers clean up its index entries."""
        self._require_owner_transaction("hard_delete")
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE id=?", (legacy_id,)
        )
        return cursor.rowcount > 0

    # ---- internal ----

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def _insert_row(self, key: str, value: str, attribute: str,
                    tag_str: str, source_session: str,
                    supersedes: int | None,
                    importance: float = 5.0, tier: str = "normal",
                    expires_at: str | None = None) -> int:
        """Insert a row into memories and return its id. Does NOT commit."""
        now = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO memories (key, value, status, attribute, tags, "
            "source_session, supersedes, importance, tier, expires_at, "
            "created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, value, attribute, tag_str, source_session, supersedes,
             importance, tier, expires_at, now, now),
        )
        return int(cur.lastrowid)

    def _get_active_by_key(self, key: str) -> dict | None:
        rows = self._execute(
            "SELECT * FROM memories WHERE key=? AND status='active'",
            (key,),
        )
        return dict(rows[0]) if rows else None

    def _has_trigram_index(self) -> bool:
        if self._has_trigram is None:
            row = self._conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='memories_fts_trigram'"
            ).fetchone()
            self._has_trigram = row is not None
        return self._has_trigram

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
        return [dict(row) for row in rows]

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
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize FTS5 query to avoid syntax errors."""
        query = query.strip().strip('"')
        # Escape FTS5 special characters
        query = query.replace('"', '""')
        return f'"{query}"'
