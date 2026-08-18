"""Independent SQLite persistence for typed, layered context items."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from evolvmem.config import Config
from evolvmem.context_layers import validate_layers
from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextRetrievalRecord,
    ContextScope,
    ContextSearchHit,
    ContextStatus,
    ContextTier,
    ContextVectorDocument,
)


def _now_iso() -> str:
    """Return a UTC timestamp whose lexical order matches chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ContextStore:
    """SQLite store that owns context tables without changing legacy memories."""

    def __init__(self, config: Config):
        self.config = config
        self._conn: sqlite3.Connection | None = None
        self._has_trigram: bool | None = None
        self._transaction_depth = 0

    # ---- lifecycle ----

    def initialize(self) -> None:
        """Open the configured database and idempotently create context schema."""
        if self._conn is not None:
            return

        self.config.ensure_dirs()
        conn = sqlite3.connect(str(self.config.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            self._create_tables()
            self._create_fts_indexes()
            conn.commit()
        except Exception:
            conn.close()
            self._conn = None
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._transaction_depth = 0

    def __enter__(self) -> "ContextStore":
        self.initialize()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["ContextStore"]:
        """Join nested writes to one outermost BEGIN IMMEDIATE transaction."""
        conn = self._connection()
        outermost = self._transaction_depth == 0
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield self
            if outermost:
                conn.commit()
        except Exception:
            if outermost:
                conn.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ContextStore is not initialized")
        return self._conn

    # ---- schema ----

    def _create_tables(self) -> None:
        self._connection().executescript(
            """
            CREATE TABLE IF NOT EXISTS context_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key TEXT NOT NULL,
                content_type TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'project',
                status TEXT NOT NULL DEFAULT 'candidate',
                tier TEXT NOT NULL DEFAULT 'normal',
                tags TEXT NOT NULL DEFAULT '',
                importance REAL NOT NULL DEFAULT 5.0,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_state TEXT NOT NULL DEFAULT 'none',
                source_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT,
                last_verified_at TEXT,
                expires_at TEXT,
                supersedes INTEGER REFERENCES context_items(id),
                superseded_by INTEGER REFERENCES context_items(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS context_layers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
                layer TEXT NOT NULL CHECK (layer IN ('l0', 'l1', 'l2')),
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                generator TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(item_id, layer)
            );

            CREATE TABLE IF NOT EXISTS session_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                adapter TEXT NOT NULL,
                external_session_id TEXT NOT NULL,
                payload_path TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'available',
                expires_at TEXT NOT NULL,
                purged_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(adapter, external_session_id)
            );

            CREATE TABLE IF NOT EXISTS context_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
                archive_id INTEGER REFERENCES session_archives(id),
                source_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                extraction_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, source_kind, source_ref)
            );

            CREATE TABLE IF NOT EXISTS context_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
                source_id INTEGER REFERENCES context_sources(id),
                outcome TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS legacy_memory_migrations (
                legacy_memory_id INTEGER PRIMARY KEY,
                context_item_id INTEGER NOT NULL REFERENCES context_items(id),
                migrated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_context_items_identity
                ON context_items(identity_key, project, scope);
            CREATE INDEX IF NOT EXISTS idx_context_items_status
                ON context_items(status);
            CREATE INDEX IF NOT EXISTS idx_context_items_project_scope_status
                ON context_items(project, scope, status);
            CREATE INDEX IF NOT EXISTS idx_context_items_expires_at
                ON context_items(expires_at);
            CREATE INDEX IF NOT EXISTS idx_context_layers_item
                ON context_layers(item_id);
            CREATE INDEX IF NOT EXISTS idx_context_sources_item
                ON context_sources(item_id);
            CREATE INDEX IF NOT EXISTS idx_context_evidence_item
                ON context_evidence(item_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_context_items_one_active_identity
                ON context_items(identity_key, project, scope)
                WHERE status = 'active';
            """
        )

    def _create_fts_indexes(self) -> None:
        conn = self._connection()
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS context_layers_fts "
            "USING fts5(content, tokenize='unicode61')"
        )

        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS context_layers_fts_trigram "
                "USING fts5(content, tokenize='trigram')"
            )
            self._has_trigram = True
        except sqlite3.OperationalError:
            self._has_trigram = False

        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS context_layers_fts_ai
            AFTER INSERT ON context_layers
            WHEN new.layer IN ('l0', 'l1')
            BEGIN
                INSERT INTO context_layers_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS context_layers_fts_ad
            AFTER DELETE ON context_layers
            WHEN old.layer IN ('l0', 'l1')
            BEGIN
                DELETE FROM context_layers_fts WHERE rowid=old.id;
            END;

            DROP TRIGGER IF EXISTS context_layers_fts_au_delete;
            DROP TRIGGER IF EXISTS context_layers_fts_au_insert;

            CREATE TRIGGER IF NOT EXISTS context_layers_fts_au
            AFTER UPDATE ON context_layers
            WHEN old.layer IN ('l0', 'l1') OR new.layer IN ('l0', 'l1')
            BEGIN
                DELETE FROM context_layers_fts
                WHERE rowid=old.id AND old.layer IN ('l0', 'l1');
                INSERT INTO context_layers_fts(rowid, content)
                SELECT new.id, new.content
                WHERE new.layer IN ('l0', 'l1');
            END;
            """
        )

        if self._has_trigram:
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS context_layers_fts_trigram_ai
                AFTER INSERT ON context_layers
                WHEN new.layer IN ('l0', 'l1')
                BEGIN
                    INSERT INTO context_layers_fts_trigram(rowid, content)
                    VALUES (new.id, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS context_layers_fts_trigram_ad
                AFTER DELETE ON context_layers
                WHEN old.layer IN ('l0', 'l1')
                BEGIN
                    DELETE FROM context_layers_fts_trigram WHERE rowid=old.id;
                END;

                DROP TRIGGER IF EXISTS context_layers_fts_trigram_au_delete;
                DROP TRIGGER IF EXISTS context_layers_fts_trigram_au_insert;

                CREATE TRIGGER IF NOT EXISTS context_layers_fts_trigram_au
                AFTER UPDATE ON context_layers
                WHEN old.layer IN ('l0', 'l1') OR new.layer IN ('l0', 'l1')
                BEGIN
                    DELETE FROM context_layers_fts_trigram
                    WHERE rowid=old.id AND old.layer IN ('l0', 'l1');
                    INSERT INTO context_layers_fts_trigram(rowid, content)
                    SELECT new.id, new.content
                    WHERE new.layer IN ('l0', 'l1');
                END;
                """
            )

    # ---- writes ----

    def create_item(self, draft: ContextItemDraft) -> ContextItem:
        """Insert one item and its L0/L1/L2 rows atomically."""
        validate_layers(draft.layers, self.config)
        if self._transaction_depth:
            return self._create_item_no_commit(draft)
        with self.transaction():
            return self._create_item_no_commit(draft)

    def _create_legacy_item(self, draft: ContextItemDraft) -> ContextItem:
        """Insert migration-derived layers with only the legacy L2 exception."""
        self._require_transaction("_create_legacy_item")
        validate_layers(draft.layers, self.config, allow_legacy_overflow=True)
        return self._create_item_no_commit(draft)

    def _create_item_no_commit(
        self,
        draft: ContextItemDraft,
        *,
        status: ContextStatus | None = None,
        supersedes: int | None = None,
    ) -> ContextItem:
        conn = self._connection()
        now = _now_iso()
        item_status = status or draft.status
        predecessor = draft.supersedes if supersedes is None else supersedes
        cursor = conn.execute(
            "INSERT INTO context_items ("
            "identity_key, content_type, project, scope, status, tier, tags, "
            "importance, confidence, expires_at, supersedes, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.identity_key,
                draft.content_type.value,
                draft.project,
                draft.scope.value,
                item_status.value,
                draft.tier.value,
                self._encode_tags(draft.tags),
                draft.importance,
                draft.confidence,
                draft.expires_at,
                predecessor,
                now,
                now,
            ),
        )
        item_id = int(cursor.lastrowid)
        self._insert_layers(item_id, draft.layers, now)
        item = self.get_item(item_id)
        if item is None:  # pragma: no cover - SQLite returned the inserted row id
            raise RuntimeError("inserted context item could not be loaded")
        return item

    def _insert_layers(self, item_id: int, layers: ContextLayers, now: str) -> None:
        rows = (
            (item_id, ContextLayer.L0.value, layers.l0),
            (item_id, ContextLayer.L1.value, layers.l1),
            (item_id, ContextLayer.L2.value, layers.l2),
        )
        self._connection().executemany(
            "INSERT INTO context_layers ("
            "item_id, layer, content, content_hash, generator, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    row_item_id,
                    layer,
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    layers.generator,
                    now,
                    now,
                )
                for row_item_id, layer, content in rows
            ),
        )

    def supersede_active(self, draft: ContextItemDraft) -> ContextItem:
        """Atomically supersede the active identity and create its successor."""
        validate_layers(draft.layers, self.config)
        if self._transaction_depth:
            return self._supersede_active_no_commit(draft)
        with self.transaction():
            return self._supersede_active_no_commit(draft)

    def _supersede_active_no_commit(self, draft: ContextItemDraft) -> ContextItem:
        conn = self._connection()
        old = conn.execute(
            "SELECT id FROM context_items "
            "WHERE identity_key=? AND project=? AND scope=? AND status='active'",
            (draft.identity_key, draft.project, draft.scope.value),
        ).fetchone()
        if old is None:
            return self._create_item_no_commit(draft)

        old_id = int(old["id"])
        now = _now_iso()
        conn.execute(
            "UPDATE context_items SET status='superseded', updated_at=? WHERE id=?",
            (now, old_id),
        )
        successor = self._create_item_no_commit(
            draft,
            status=ContextStatus.ACTIVE,
            supersedes=old_id,
        )
        conn.execute(
            "UPDATE context_items SET superseded_by=?, updated_at=? WHERE id=?",
            (successor.id, now, old_id),
        )
        return successor

    def update_access(self, item_ids: list[int]) -> None:
        """Increment access telemetry for a batch without changing updated_at."""
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        params = (_now_iso(), *item_ids)
        if self._transaction_depth:
            self._connection().execute(
                "UPDATE context_items SET access_count=access_count+1, last_accessed=? "
                f"WHERE id IN ({placeholders})",
                params,
            )
            return
        with self.transaction():
            self._connection().execute(
                "UPDATE context_items SET access_count=access_count+1, last_accessed=? "
                f"WHERE id IN ({placeholders})",
                params,
            )

    # ---- recurring legacy migration support ----

    def legacy_memory_table_exists(self) -> bool:
        row = self._connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        return row is not None

    def legacy_memory_row_count(self) -> int:
        if not self.legacy_memory_table_exists():
            return 0
        row = self._connection().execute(
            "SELECT COUNT(*) AS count FROM memories"
        ).fetchone()
        return int(row["count"])

    def iter_legacy_rows(self) -> list[dict]:
        """Read all legacy rows and their mappings without upgrading the schema."""
        if not self.legacy_memory_table_exists():
            return []

        columns = {
            row["name"]
            for row in self._connection().execute("PRAGMA table_info(memories)")
        }

        def legacy_column(name: str, default_sql: str) -> str:
            if name in columns:
                return f'm."{name}"'
            return default_sql

        attribute = (
            legacy_column("attribute", "NULL")
            if "attribute" in columns
            else legacy_column("category", "''")
        )
        legacy_id = legacy_column("id", "m.rowid")
        selections = (
            f"{legacy_id} AS id",
            legacy_column("key", "''") + " AS key",
            legacy_column("value", "''") + " AS value",
            legacy_column("status", "'archived'") + " AS status",
            f"{attribute} AS attribute",
            legacy_column("tags", "''") + " AS tags",
            legacy_column("source_session", "''") + " AS source_session",
            f"{legacy_column('access_count', '0')} AS access_count",
            f"{legacy_column('last_accessed', 'NULL')} AS last_accessed",
            f"{legacy_column('supersedes', 'NULL')} AS supersedes",
            f"{legacy_column('superseded_by', 'NULL')} AS superseded_by",
            legacy_column("created_at", "'1970-01-01 00:00:00'")
            + " AS created_at",
            legacy_column("updated_at", "'1970-01-01 00:00:00'")
            + " AS updated_at",
            f"{legacy_column('importance', '5.0')} AS importance",
            legacy_column("tier", "'normal'") + " AS tier",
            f"{legacy_column('expires_at', 'NULL')} AS expires_at",
            "migration.context_item_id AS context_item_id",
        )
        rows = self._connection().execute(
            f"SELECT {', '.join(selections)} FROM memories m "
            "LEFT JOIN legacy_memory_migrations migration "
            f"ON migration.legacy_memory_id={legacy_id} "
            "ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def iter_unmigrated_legacy_rows(self) -> list[dict]:
        """Read only legacy rows that do not yet have a ContextItem mapping."""
        return [
            {key: value for key, value in row.items() if key != "context_item_id"}
            for row in self.iter_legacy_rows()
            if row["context_item_id"] is None
        ]

    def _apply_legacy_item_metadata(
        self,
        item_id: int,
        *,
        access_count: int,
        last_accessed: str | None,
        created_at: str,
        updated_at: str,
        original_l2: str,
    ) -> None:
        """Restore metadata create_item intentionally does not expose generally.

        Exact legacy L2 evidence is authoritative even when it is blank or exceeds
        today's validation budget; ordinary ContextItemDraft writes retain the
        non-empty and bounded-layer invariants.
        """
        self._require_transaction("_apply_legacy_item_metadata")
        conn = self._connection()
        conn.execute(
            "UPDATE context_items SET access_count=?, last_accessed=?, "
            "created_at=?, updated_at=? WHERE id=?",
            (access_count, last_accessed, created_at, updated_at, item_id),
        )
        conn.execute(
            "UPDATE context_layers SET content=?, content_hash=?, created_at=?, "
            "updated_at=? WHERE item_id=? AND layer='l2'",
            (
                original_l2,
                hashlib.sha256(original_l2.encode("utf-8")).hexdigest(),
                created_at,
                updated_at,
                item_id,
            ),
        )
        conn.execute(
            "UPDATE context_layers SET created_at=?, updated_at=? "
            "WHERE item_id=? AND layer IN ('l0', 'l1')",
            (created_at, updated_at, item_id),
        )

    def record_migration_source(
        self, item_id: int, *, source_ref: str, extraction_version: str
    ) -> int:
        self._require_transaction("record_migration_source")
        cursor = self._connection().execute(
            "INSERT INTO context_sources ("
            "item_id, source_kind, source_ref, extraction_version, created_at"
            ") VALUES (?, 'migration', ?, ?, ?)",
            (item_id, source_ref, extraction_version, _now_iso()),
        )
        self._connection().execute(
            "UPDATE context_items SET source_count=("
            "SELECT COUNT(*) FROM context_sources WHERE item_id=?"
            ") WHERE id=?",
            (item_id, item_id),
        )
        return int(cursor.lastrowid)

    def record_legacy_mapping(
        self, legacy_memory_id: int, context_item_id: int
    ) -> None:
        self._require_transaction("record_legacy_mapping")
        self._connection().execute(
            "INSERT INTO legacy_memory_migrations ("
            "legacy_memory_id, context_item_id, migrated_at"
            ") VALUES (?, ?, ?)",
            (legacy_memory_id, context_item_id, _now_iso()),
        )

    def _reconcile_legacy_item_state(
        self,
        item_id: int,
        *,
        status: ContextStatus,
        confidence: float,
        updated_at: str,
        extraction_version: str,
    ) -> None:
        """Refresh mutable legacy state while preserving the mapped ContextItem."""
        self._require_transaction("_reconcile_legacy_item_state")
        conn = self._connection()
        conn.execute(
            "UPDATE context_items SET status=?, confidence=?, updated_at=? WHERE id=?",
            (status.value, confidence, updated_at, item_id),
        )
        conn.execute(
            "UPDATE context_sources SET extraction_version=? "
            "WHERE item_id=? AND source_kind='migration'",
            (extraction_version, item_id),
        )

    def resolve_legacy_mapping(self, legacy_memory_id: int) -> int | None:
        row = self._connection().execute(
            "SELECT context_item_id FROM legacy_memory_migrations "
            "WHERE legacy_memory_id=?",
            (legacy_memory_id,),
        ).fetchone()
        return None if row is None else int(row["context_item_id"])

    def set_supersession_links(
        self,
        item_id: int,
        *,
        supersedes: int | None,
        superseded_by: int | None,
    ) -> None:
        self._require_transaction("set_supersession_links")
        self._connection().execute(
            "UPDATE context_items SET supersedes=?, superseded_by=? WHERE id=?",
            (supersedes, superseded_by, item_id),
        )

    def _require_transaction(self, operation: str) -> None:
        if self._transaction_depth == 0:
            raise RuntimeError(f"{operation} requires an active ContextStore transaction")

    # ---- reads ----

    def get_item(
        self, item_id: int, *, include_layers: bool = True
    ) -> ContextItem | None:
        row = self._connection().execute(
            "SELECT * FROM context_items WHERE id=?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        layers = self._load_layers(item_id) if include_layers else None
        return self._row_to_item(row, layers)

    def get_by_identity(
        self,
        identity_key: str,
        *,
        project: str = "",
        scope: ContextScope = ContextScope.PROJECT,
    ) -> list[ContextItem]:
        rows = self._connection().execute(
            "SELECT * FROM context_items "
            "WHERE identity_key=? AND project=? AND scope=? "
            "ORDER BY updated_at DESC, id DESC",
            (identity_key, project, scope.value),
        ).fetchall()
        return [self._row_to_item(row, self._load_layers(row["id"])) for row in rows]

    def list_vector_documents(self) -> list[ContextVectorDocument]:
        rows = self._connection().execute(
            "SELECT i.id AS item_id, l.content AS l0 "
            "FROM context_items i "
            "JOIN context_layers l ON l.item_id=i.id AND l.layer='l0' "
            "WHERE i.status='active' "
            "AND (i.expires_at IS NULL OR i.expires_at > ?) "
            "ORDER BY i.id",
            (_now_iso(),),
        ).fetchall()
        return [ContextVectorDocument(item_id=row["item_id"], l0=row["l0"]) for row in rows]

    def get_retrieval_records(
        self, item_ids: list[int]
    ) -> tuple[ContextRetrievalRecord, ...]:
        """Load metadata, L0 text, and available layer names without L1/L2.

        One metadata/L0 query plus one grouped layer-name query; input-ID
        order is preserved and nonexistent IDs are omitted.
        """
        unique_ids = list(dict.fromkeys(item_ids))
        if not unique_ids:
            return ()
        placeholders = ",".join("?" for _ in unique_ids)
        params = tuple(unique_ids)
        rows = self._connection().execute(
            "SELECT i.*, l.content AS l0 FROM context_items i "
            "JOIN context_layers l ON l.item_id=i.id AND l.layer='l0' "
            f"WHERE i.id IN ({placeholders})",
            params,
        ).fetchall()
        layer_rows = self._connection().execute(
            "SELECT item_id, layer FROM context_layers "
            f"WHERE item_id IN ({placeholders}) "
            "GROUP BY item_id, layer",
            params,
        ).fetchall()
        layers_by_item: dict[int, set[str]] = {}
        for row in layer_rows:
            layers_by_item.setdefault(int(row["item_id"]), set()).add(row["layer"])
        canonical = (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)
        by_id = {int(row["id"]): row for row in rows}
        records: list[ContextRetrievalRecord] = []
        for item_id in unique_ids:
            row = by_id.get(item_id)
            if row is None:
                continue
            present = layers_by_item.get(item_id, set())
            records.append(
                ContextRetrievalRecord(
                    item=self._row_to_item(row, None),
                    l0=str(row["l0"]),
                    available_layers=tuple(
                        layer for layer in canonical if layer.value in present
                    ),
                )
            )
        return tuple(records)

    def get_layer(self, item_id: int, layer: ContextLayer) -> str | None:
        """Return the exact stored content of one layer, or None when absent."""
        row = self._connection().execute(
            "SELECT content FROM context_layers WHERE item_id=? AND layer=?",
            (item_id, layer.value),
        ).fetchone()
        return None if row is None else str(row["content"])

    def list_pinned_policy_records(
        self, *, project: str, min_confidence: float
    ) -> tuple[ContextRetrievalRecord, ...]:
        """Active, unexpired pinned policy seeds for a project plus applicable globals."""
        rows = self._connection().execute(
            "SELECT id FROM context_items "
            "WHERE status='active' AND tier='pinned' "
            "AND content_type IN ('workflow_policy', 'constraint', 'preference') "
            "AND confidence >= ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND (project = ? OR scope = 'global') "
            "ORDER BY id",
            (min_confidence, _now_iso(), project),
        ).fetchall()
        return self.get_retrieval_records([int(row["id"]) for row in rows])

    def count_by_status(self) -> dict[str, int]:
        rows = self._connection().execute(
            "SELECT status, COUNT(*) AS count FROM context_items GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def _load_layers(self, item_id: int) -> ContextLayers:
        rows = self._connection().execute(
            "SELECT layer, content, generator FROM context_layers "
            "WHERE item_id=? ORDER BY layer",
            (item_id,),
        ).fetchall()
        by_layer = {row["layer"]: row for row in rows}
        required = {member.value for member in ContextLayer}
        if set(by_layer) != required:
            raise RuntimeError(f"context item {item_id} does not have exactly L0/L1/L2")
        generators = {row["generator"] for row in rows}
        if len(generators) != 1:
            raise RuntimeError(f"context item {item_id} layers have mixed generators")
        return ContextLayers(
            l0=by_layer[ContextLayer.L0.value]["content"],
            l1=by_layer[ContextLayer.L1.value]["content"],
            l2=by_layer[ContextLayer.L2.value]["content"],
            generator=next(iter(generators)),
        )

    def _row_to_item(self, row: sqlite3.Row, layers: ContextLayers | None) -> ContextItem:
        return ContextItem(
            id=row["id"],
            identity_key=row["identity_key"],
            content_type=ContextContentType(row["content_type"]),
            project=row["project"],
            scope=ContextScope(row["scope"]),
            status=ContextStatus(row["status"]),
            tier=ContextTier(row["tier"]),
            tags=self._decode_tags(row["tags"]),
            importance=row["importance"],
            confidence=row["confidence"],
            source_state=row["source_state"],
            source_count=row["source_count"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            access_count=row["access_count"],
            last_accessed=row["last_accessed"],
            last_verified_at=row["last_verified_at"],
            expires_at=row["expires_at"],
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            layers=layers,
        )

    @staticmethod
    def _encode_tags(tags: tuple[str, ...]) -> str:
        return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_tags(value: str) -> tuple[str, ...]:
        if not value:
            return ()
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return tuple(tag for tag in value.split(",") if tag)
        if not isinstance(decoded, list):
            raise RuntimeError("stored context tags must be a JSON array")
        return tuple(str(tag) for tag in decoded)

    # ---- lexical search ----

    def search_fts(
        self,
        query: str,
        *,
        top_k: int = 20,
        statuses: tuple[ContextStatus, ...] | None = None,
        layers: tuple[ContextLayer, ...] = (ContextLayer.L0, ContextLayer.L1),
    ) -> list[ContextSearchHit]:
        if top_k <= 0 or not query.strip() or statuses == () or not layers:
            return []

        layer_sql, layer_params = self._layer_filter(layers)
        has_cjk = self._has_cjk(query)
        table = (
            "context_layers_fts_trigram"
            if self._has_trigram and has_cjk
            else "context_layers_fts"
        )
        rows = self._search_fts5(table, query, top_k, statuses, layer_sql, layer_params)
        if has_cjk:
            rows.extend(self._search_like(query, top_k, statuses, layer_sql, layer_params))

        merged: dict[int, dict[str, object]] = {}
        for row in rows:
            item_id = int(row["item_id"])
            layer = ContextLayer(row["layer"])
            score = float(row["score"])
            existing = merged.get(item_id)
            if existing is None:
                merged[item_id] = {
                    "score": score,
                    "layers": {layer},
                    "content_type": ContextContentType(row["content_type"]),
                    "project": row["project"],
                    "status": ContextStatus(row["status"]),
                }
            else:
                existing["score"] = max(float(existing["score"]), score)
                existing["layers"].add(layer)  # type: ignore[union-attr]

        hits = [
            ContextSearchHit(
                item_id=item_id,
                score=float(data["score"]),
                match_layers=tuple(
                    layer
                    for layer in (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)
                    if layer in data["layers"]
                ),
                content_type=data["content_type"],  # type: ignore[arg-type]
                project=data["project"],  # type: ignore[arg-type]
                status=data["status"],  # type: ignore[arg-type]
            )
            for item_id, data in merged.items()
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.item_id))
        return hits[:top_k]

    def _search_fts5(
        self,
        table: str,
        query: str,
        top_k: int,
        statuses: tuple[ContextStatus, ...] | None,
        layer_sql: str,
        layer_params: tuple[str, ...],
    ) -> list[dict[str, object]]:
        status_sql, status_params = self._status_filter(statuses)
        safe_query = self._sanitize_fts5_query(query)
        try:
            ranked_rows = self._connection().execute(
                "SELECT i.id AS item_id, l.layer, i.content_type, i.project, "
                "i.status, -rank AS score "
                f"FROM {table} f "
                "JOIN context_layers l ON l.id=f.rowid "
                "JOIN context_items i ON i.id=l.item_id "
                f"WHERE {table} MATCH ? AND {status_sql} AND {layer_sql} "
                "ORDER BY rank, i.id, l.layer LIMIT ?",
                (safe_query, *status_params, *layer_params, top_k * 2),
            ).fetchall()
            selected_ids = tuple(
                dict.fromkeys(row["item_id"] for row in ranked_rows)
            )[:top_k]
            if not selected_ids:
                return []
            placeholders = ",".join("?" for _ in selected_ids)
            rows = self._connection().execute(
                "SELECT i.id AS item_id, l.layer, i.content_type, i.project, "
                "i.status, -rank AS score "
                f"FROM {table} f "
                "JOIN context_layers l ON l.id=f.rowid "
                "JOIN context_items i ON i.id=l.item_id "
                f"WHERE {table} MATCH ? AND {status_sql} AND {layer_sql} "
                f"AND i.id IN ({placeholders}) "
                "ORDER BY rank, i.id, l.layer",
                (safe_query, *status_params, *layer_params, *selected_ids),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def _search_like(
        self,
        query: str,
        top_k: int,
        statuses: tuple[ContextStatus, ...] | None,
        layer_sql: str,
        layer_params: tuple[str, ...],
    ) -> list[dict[str, object]]:
        status_sql, status_params = self._status_filter(statuses)
        rows = self._connection().execute(
            "SELECT i.id AS item_id, l.layer, i.content_type, i.project, "
            "i.status, 0.0 AS score "
            "FROM context_layers l "
            "JOIN context_items i ON i.id=l.item_id "
            f"WHERE {layer_sql} AND l.content LIKE ? "
            f"AND {status_sql} "
            "ORDER BY i.id, l.layer LIMIT ?",
            (*layer_params, f"%{query}%", *status_params, top_k * 2),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _layer_filter(
        layers: tuple[ContextLayer, ...],
    ) -> tuple[str, tuple[str, ...]]:
        values = tuple(dict.fromkeys(layer.value for layer in layers))
        placeholders = ",".join("?" for _ in values)
        return f"l.layer IN ({placeholders})", values

    @staticmethod
    def _status_filter(
        statuses: tuple[ContextStatus, ...] | None,
    ) -> tuple[str, tuple[str, ...]]:
        if statuses is None:
            return "i.status != ?", (ContextStatus.DELETED.value,)
        values = tuple(status.value for status in statuses)
        placeholders = ",".join("?" for _ in values)
        return f"i.status IN ({placeholders})", values

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return bool(re.search(r"[一-鿿㐀-䶿]", text))

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        query = query.strip().strip('"')
        query = query.replace('"', '""')
        return f'"{query}"'
