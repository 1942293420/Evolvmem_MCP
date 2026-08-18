"""Behavioral contracts for recurring legacy-memory migration."""

import sqlite3

import pytest

from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import (
    ContextContentType,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_store import ContextStore
from evolvmem.memory_store import MemoryStore


def _create_legacy_rows(config, rows):
    ids = []
    with MemoryStore(config) as legacy:
        for row in rows:
            ids.append(legacy.add(**row))
    return ids


def _legacy_snapshot(config):
    conn = sqlite3.connect(config.db_path)
    try:
        columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(memories)"))
        rows = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
        return columns, rows
    finally:
        conn.close()


def _mapped_item(store, legacy_id):
    item_id = store.resolve_legacy_mapping(legacy_id)
    assert item_id is not None
    item = store.get_item(item_id)
    assert item is not None
    return item


@pytest.mark.parametrize("create_legacy_table", [False, True])
def test_empty_database_migration_succeeds_without_creating_items(
    test_config, create_legacy_table
):
    """Missing or empty legacy storage must be a successful no-op."""
    if create_legacy_table:
        with MemoryStore(test_config):
            pass

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()

        assert report.legacy_table_found is create_legacy_table
        assert report.scanned == 0
        assert report.created == 0
        assert report.already_migrated == 0
        assert report.duplicate_active_count == 0
        assert store.count_by_status() == {}


def test_migration_preserves_legacy_metadata_layers_sources_and_supersession(
    test_config,
):
    """Losing metadata, source evidence, or directional history breaks migration."""
    test_config.context_l0_max_chars = 32
    test_config.context_l1_max_chars = 64
    test_config.context_l2_max_chars = 80
    oversized = "  " + "x" * 100 + "\r\nlast line  "

    with MemoryStore(test_config) as legacy:
        active_id = legacy.add(
            key="  global:constraint:retention  ",
            value=oversized,
            attribute="constraint",
            tags=["safety", "durable"],
            source_session="session-active",
            importance=9.25,
            tier="pinned",
        )
        old_id = legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first.",
            attribute="decision",
            tags=["database"],
            source_session="session-old",
            importance=7.0,
        )
        new_id = legacy.replace(
            key="project:demo:decision:database",
            new_value="Use PostgreSQL for production.",
            source_session="session-new",
        )
        archived_id = legacy.add(
            key="project:demo:fact:archived",
            value="A historical archived fact.",
            attribute="fact",
            expires_at="2031-04-05 06:07:08",
        )
        deleted_id = legacy.add(
            key="project:demo:fact:deleted",
            value="A deliberately deleted fact.",
            attribute="fact",
        )
        legacy.remove(deleted_id)
        legacy._conn.execute(
            "UPDATE memories SET status='archived' WHERE id=?", (archived_id,)
        )
        legacy._conn.execute(
            "UPDATE memories SET access_count=7, last_accessed=?, created_at=?, "
            "updated_at=? WHERE id=?",
            (
                "2026-01-02 03:04:05",
                "2025-02-03 04:05:06",
                "2026-02-03 04:05:06",
                active_id,
            ),
        )
        legacy._conn.commit()

    before = _legacy_snapshot(test_config)
    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()

        assert report.scanned == 5
        assert report.created == 5
        assert report.already_migrated == 0

        active = _mapped_item(store, active_id)
        assert active.identity_key == "global:constraint:retention"
        assert active.content_type is ContextContentType.CONSTRAINT
        assert active.project == ""
        assert active.scope is ContextScope.GLOBAL
        assert active.status is ContextStatus.ACTIVE
        assert active.tier is ContextTier.PINNED
        assert active.tags == ("safety", "durable")
        assert active.importance == 9.25
        assert active.confidence == 1.0
        assert active.source_state == "none"
        assert active.source_count == 1
        assert active.success_count == active.failure_count == 0
        assert active.access_count == 7
        assert active.last_accessed == "2026-01-02 03:04:05"
        assert active.created_at == "2025-02-03 04:05:06"
        assert active.updated_at == "2026-02-03 04:05:06"
        assert active.layers is not None
        assert len(active.layers.l0) <= test_config.context_l0_max_chars
        assert len(active.layers.l1) <= test_config.context_l1_max_chars
        assert active.layers.l2 == oversized.replace("\r\n", "\n")
        assert len(active.layers.l2) > test_config.context_l2_max_chars
        assert active.layers.generator == "migrated"

        source = store._conn.execute(
            "SELECT source_kind, source_ref, extraction_version "
            "FROM context_sources WHERE item_id=?",
            (active.id,),
        ).fetchone()
        assert tuple(source) == ("migration", "session-active", "legacy-v1")

        old = _mapped_item(store, old_id)
        new = _mapped_item(store, new_id)
        assert old.status is ContextStatus.SUPERSEDED
        assert new.status is ContextStatus.ACTIVE
        assert old.supersedes is None
        assert old.superseded_by == new.id
        assert new.supersedes == old.id
        assert new.superseded_by is None
        assert old.confidence == 0.5

        archived = _mapped_item(store, archived_id)
        deleted = _mapped_item(store, deleted_id)
        assert archived.status is ContextStatus.ARCHIVED
        assert archived.expires_at == "2031-04-05 06:07:08"
        assert deleted.status is ContextStatus.DELETED

    assert _legacy_snapshot(test_config) == before


@pytest.mark.parametrize(
    "attribute,key,expected_type,expected_scope",
    [
        ("constraint", "constraint:key", ContextContentType.CONSTRAINT, ContextScope.GLOBAL),
        ("preference", "preference:key", ContextContentType.PREFERENCE, ContextScope.GLOBAL),
        ("user_profile", "profile:key", ContextContentType.USER_PROFILE, ContextScope.GLOBAL),
        ("decision", "decision:key", ContextContentType.DECISION, ContextScope.PROJECT),
        ("fact", "project:x:progress:log:2026-08-17", ContextContentType.SESSION_SUMMARY, ContextScope.PROJECT),
        ("fact", "fact:key", ContextContentType.FACT, ContextScope.PROJECT),
        ("unrecognized", "reference:key", ContextContentType.REFERENCE, ContextScope.PROJECT),
    ],
)
def test_migration_maps_legacy_attributes_to_typed_scopes(
    test_config, attribute, key, expected_type, expected_scope
):
    """Wrong attribute routing would make migrated retrieval semantically unsafe."""
    legacy_id = _create_legacy_rows(
        test_config,
        [{"key": key, "value": "A sufficiently detailed legacy value.", "attribute": attribute}],
    )[0]

    with ContextStore(test_config) as store:
        LegacyMemoryMigrator(store, test_config).migrate()
        item = _mapped_item(store, legacy_id)

    assert item.content_type is expected_type
    assert item.scope is expected_scope
    assert item.project == ""


def test_second_migration_reports_every_existing_mapping_without_new_items(test_config):
    """Repeating startup migration must not duplicate items, layers, or sources."""
    _create_legacy_rows(
        test_config,
        [
            {"key": "fact:first", "value": "The first durable fact."},
            {"key": "fact:second", "value": "The second durable fact."},
        ],
    )

    with ContextStore(test_config) as store:
        first = LegacyMemoryMigrator(store, test_config).migrate()
        second = LegacyMemoryMigrator(store, test_config).migrate()

        assert first.created == 2
        assert second.scanned == 2
        assert second.created == 0
        assert second.already_migrated == 2
        assert store._conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == 2
        assert store._conn.execute("SELECT COUNT(*) FROM context_layers").fetchone()[0] == 6
        assert store._conn.execute("SELECT COUNT(*) FROM context_sources").fetchone()[0] == 2
        assert store._conn.execute(
            "SELECT COUNT(*) FROM legacy_memory_migrations"
        ).fetchone()[0] == 2


def test_migration_reconciles_legacy_replace_created_after_prior_migration(test_config):
    """Scanning only unmapped rows would collide with the mapped active predecessor."""
    with MemoryStore(test_config) as legacy:
        old_legacy_id = legacy.add(
            key="project:demo:decision:database",
            value="Use SQLite first.",
            attribute="decision",
            source_session="session-old",
        )

    with ContextStore(test_config) as store:
        first = LegacyMemoryMigrator(store, test_config).migrate()
        old_context_id = store.resolve_legacy_mapping(old_legacy_id)
        assert first.created == 1
        assert old_context_id is not None

    with MemoryStore(test_config) as legacy:
        new_legacy_id = legacy.replace(
            key="project:demo:decision:database",
            new_value="Use PostgreSQL for production.",
            source_session="session-new",
        )
    before = _legacy_snapshot(test_config)

    with ContextStore(test_config) as store:
        second = LegacyMemoryMigrator(store, test_config).migrate()
        old = _mapped_item(store, old_legacy_id)
        new = _mapped_item(store, new_legacy_id)
        item_count = store._conn.execute(
            "SELECT COUNT(*) FROM context_items"
        ).fetchone()[0]
        third = LegacyMemoryMigrator(store, test_config).migrate()

        assert second.scanned == 2
        assert second.created == 1
        assert second.already_migrated == 1
        assert second.duplicate_active_count == 0
        assert old.id == old_context_id
        assert old.status is ContextStatus.SUPERSEDED
        assert old.supersedes is None
        assert old.superseded_by == new.id
        assert new.status is ContextStatus.ACTIVE
        assert new.supersedes == old.id
        assert new.superseded_by is None
        assert item_count == 2
        assert third.created == 0
        assert third.already_migrated == 2
        assert store._conn.execute(
            "SELECT COUNT(*) FROM context_items"
        ).fetchone()[0] == item_count

    assert _legacy_snapshot(test_config) == before


def test_migration_reconciles_new_active_duplicate_against_existing_mapping(test_config):
    """Duplicate selection must include mapped rows, not only newly unmapped rows."""
    with MemoryStore(test_config) as legacy:
        older_legacy_id = legacy.add(
            key="  project:x:fact:duplicate  ",
            value="Older duplicate value.",
            source_session="older-session",
        )
        legacy._conn.execute(
            "UPDATE memories SET updated_at='2026-01-01 00:00:00' WHERE id=?",
            (older_legacy_id,),
        )
        legacy._conn.commit()

    with ContextStore(test_config) as store:
        LegacyMemoryMigrator(store, test_config).migrate()
        older_context_id = store.resolve_legacy_mapping(older_legacy_id)
        assert older_context_id is not None

    with MemoryStore(test_config) as legacy:
        newer_legacy_id = legacy.add(
            key="project:x:fact:duplicate",
            value="Newer duplicate value.",
            source_session="newer-session",
        )
        legacy._conn.execute(
            "UPDATE memories SET updated_at='2026-01-02 00:00:00' WHERE id=?",
            (newer_legacy_id,),
        )
        legacy._conn.commit()
    before = _legacy_snapshot(test_config)

    with ContextStore(test_config) as store:
        second = LegacyMemoryMigrator(store, test_config).migrate()
        older = _mapped_item(store, older_legacy_id)
        newer = _mapped_item(store, newer_legacy_id)
        item_count = store._conn.execute(
            "SELECT COUNT(*) FROM context_items"
        ).fetchone()[0]
        third = LegacyMemoryMigrator(store, test_config).migrate()
        versions = dict(
            store._conn.execute(
                "SELECT m.legacy_memory_id, s.extraction_version "
                "FROM legacy_memory_migrations m "
                "JOIN context_sources s ON s.item_id=m.context_item_id"
            ).fetchall()
        )

        assert second.created == 1
        assert second.already_migrated == 1
        assert second.duplicate_active_count == 1
        assert older.id == older_context_id
        assert older.status is ContextStatus.CANDIDATE
        assert newer.status is ContextStatus.ACTIVE
        assert older.supersedes is older.superseded_by is None
        assert newer.supersedes is newer.superseded_by is None
        assert versions == {
            older_legacy_id: "legacy-v1:duplicate-active",
            newer_legacy_id: "legacy-v1",
        }
        assert item_count == 2
        assert third.created == 0
        assert third.already_migrated == 2
        assert third.duplicate_active_count == 1
        assert store._conn.execute(
            "SELECT COUNT(*) FROM context_items"
        ).fetchone()[0] == item_count

    assert _legacy_snapshot(test_config) == before


def test_recurring_migration_rolls_back_reconciliation_when_new_mapping_fails(
    test_config,
):
    """A failed recurring mapping must not strand the mapped predecessor inactive."""
    with MemoryStore(test_config) as legacy:
        old_legacy_id = legacy.add(key="fact:rollback-replace", value="Old value.")

    with ContextStore(test_config) as store:
        LegacyMemoryMigrator(store, test_config).migrate()
        old_context_id = store.resolve_legacy_mapping(old_legacy_id)
        assert old_context_id is not None

    with MemoryStore(test_config) as legacy:
        new_legacy_id = legacy.replace(
            key="fact:rollback-replace", new_value="New value."
        )
    before = _legacy_snapshot(test_config)

    with ContextStore(test_config) as store:
        store._conn.executescript(
            "CREATE TRIGGER abort_recurring_mapping "
            "BEFORE INSERT ON legacy_memory_migrations BEGIN "
            "SELECT RAISE(ABORT, 'synthetic recurring mapping failure'); END;"
        )
        store._conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError, match="synthetic recurring mapping failure"
        ):
            LegacyMemoryMigrator(store, test_config).migrate()

        old = store.get_item(old_context_id)
        assert old is not None
        assert old.status is ContextStatus.ACTIVE
        assert old.superseded_by is None
        assert store.resolve_legacy_mapping(new_legacy_id) is None
        assert store._conn.execute(
            "SELECT COUNT(*) FROM context_items"
        ).fetchone()[0] == 1
        assert store._conn.execute(
            "SELECT COUNT(*) FROM context_layers"
        ).fetchone()[0] == 3
        assert store._conn.execute(
            "SELECT COUNT(*) FROM context_sources"
        ).fetchone()[0] == 1

    assert _legacy_snapshot(test_config) == before


def test_mapping_failure_rolls_back_item_layers_source_and_mapping(test_config):
    """A partial row migration must never survive a failed atomic transaction."""
    legacy_id = _create_legacy_rows(
        test_config,
        [{"key": "fact:rollback", "value": "This migration must roll back atomically."}],
    )[0]

    with ContextStore(test_config) as store:
        store._conn.executescript(
            "CREATE TRIGGER abort_legacy_mapping "
            "BEFORE INSERT ON legacy_memory_migrations BEGIN "
            "SELECT RAISE(ABORT, 'synthetic mapping failure'); END;"
        )
        store._conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="synthetic mapping failure"):
            LegacyMemoryMigrator(store, test_config).migrate()

        for table in (
            "context_items",
            "context_layers",
            "context_sources",
            "legacy_memory_migrations",
        ):
            assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert store._conn.execute(
            "SELECT status FROM memories WHERE id=?", (legacy_id,)
        ).fetchone()[0] == "active"


def test_duplicate_active_normalized_identities_keep_newest_active(test_config):
    """Duplicate active history must retain both values without violating uniqueness."""
    with MemoryStore(test_config) as legacy:
        older_id = legacy.add(
            key="  project:x:fact:duplicate  ",
            value="Older duplicate value.",
            source_session="older-session",
        )
        newer_id = legacy.add(
            key="project:x:fact:duplicate",
            value="Newer duplicate value.",
            source_session="newer-session",
        )
        legacy._conn.execute(
            "UPDATE memories SET updated_at='2026-01-01 00:00:00' WHERE id=?",
            (older_id,),
        )
        legacy._conn.execute(
            "UPDATE memories SET updated_at='2026-01-02 00:00:00' WHERE id=?",
            (newer_id,),
        )
        legacy._conn.commit()

    before = _legacy_snapshot(test_config)
    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        older = _mapped_item(store, older_id)
        newer = _mapped_item(store, newer_id)

        assert report.duplicate_active_count == 1
        assert older.identity_key == newer.identity_key == "project:x:fact:duplicate"
        assert older.status is ContextStatus.CANDIDATE
        assert newer.status is ContextStatus.ACTIVE
        assert older.layers.l2 == "Older duplicate value."
        assert newer.layers.l2 == "Newer duplicate value."
        versions = dict(
            store._conn.execute(
                "SELECT m.legacy_memory_id, s.extraction_version "
                "FROM legacy_memory_migrations m "
                "JOIN context_sources s ON s.item_id=m.context_item_id"
            ).fetchall()
        )
        assert versions == {
            older_id: "legacy-v1:duplicate-active",
            newer_id: "legacy-v1",
        }

    assert _legacy_snapshot(test_config) == before


def test_duplicate_active_timestamp_tie_keeps_highest_legacy_id_active(test_config):
    """Equal timestamps must resolve by descending ID rather than scan order."""
    with MemoryStore(test_config) as legacy:
        lower_id = legacy.add(key="fact:tied", value="Lower ID value.")
        higher_id = legacy.add(key="fact:tied", value="Higher ID value.")
        legacy._conn.execute(
            "UPDATE memories SET updated_at='2026-01-01 00:00:00' "
            "WHERE id IN (?, ?)",
            (lower_id, higher_id),
        )
        legacy._conn.commit()

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        lower = _mapped_item(store, lower_id)
        higher = _mapped_item(store, higher_id)

    assert report.duplicate_active_count == 1
    assert lower.status is ContextStatus.CANDIDATE
    assert higher.status is ContextStatus.ACTIVE


def test_old_schema_missing_optional_columns_uses_deterministic_defaults(test_config):
    """Migration must not depend on MemoryStore upgrading older legacy schemas."""
    test_config.ensure_dirs()
    conn = sqlite3.connect(test_config.db_path)
    conn.execute(
        "CREATE TABLE memories ("
        "id INTEGER PRIMARY KEY, key TEXT, value TEXT, status TEXT, attribute TEXT, "
        "tags TEXT, source_session TEXT, access_count INTEGER, last_accessed TEXT, "
        "supersedes INTEGER, superseded_by INTEGER, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO memories VALUES "
        "(41, 'old:constraint:key', 'Old schema value.', 'active', 'constraint', "
        "'legacy,old', 'old-session', 3, '2020-02-03 04:05:06', NULL, NULL, "
        "'2019-01-01 00:00:00', '2020-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        item = _mapped_item(store, 41)

    assert report.created == 1
    assert item.importance == 5.0
    assert item.tier is ContextTier.NORMAL
    assert item.expires_at is None
    assert item.scope is ContextScope.GLOBAL


def test_invalid_historical_metadata_is_archived_without_discarding_row(test_config):
    """Malformed metadata must fall back conservatively while retaining evidence."""
    legacy_id = _create_legacy_rows(
        test_config,
        [
            {
                "key": "  odd   legacy   key  ",
                "value": "  Unusual legacy evidence.  ",
                "attribute": "mystery",
            }
        ],
    )[0]
    conn = sqlite3.connect(test_config.db_path)
    conn.execute(
        "UPDATE memories SET status='mystery', tier='urgent', importance='invalid' "
        "WHERE id=?",
        (legacy_id,),
    )
    conn.commit()
    conn.close()

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        item = _mapped_item(store, legacy_id)

    assert report.created == 1
    assert item.identity_key == "odd legacy key"
    assert item.content_type is ContextContentType.REFERENCE
    assert item.status is ContextStatus.ARCHIVED
    assert item.tier is ContextTier.NORMAL
    assert item.importance == 5.0
    assert item.layers.l2 == "  Unusual legacy evidence.  "


def test_whitespace_only_legacy_value_is_migrated_with_exact_l2(test_config):
    """Malformed blank evidence must remain traceable instead of being discarded."""
    original = " \r\n\t "
    legacy_id = _create_legacy_rows(
        test_config,
        [{"key": "fact:blank", "value": original, "source_session": "blank-session"}],
    )[0]
    before = _legacy_snapshot(test_config)

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        item = _mapped_item(store, legacy_id)

    assert report.created == 1
    assert item.layers.l0 == "reference: [empty legacy value]"
    assert item.layers.l1 == "[empty legacy value]"
    assert item.layers.l2 == " \n\t "
    assert _legacy_snapshot(test_config) == before


def test_schema_creation_rolls_back_with_an_injected_migration_failure(
    test_config, monkeypatch
):
    """Bootstrap DDL must share the migration transaction's rollback boundary."""
    with MemoryStore(test_config) as legacy:
        legacy_id = legacy.add(key="fact:atomic", value="Legacy value kept intact.")

    store = ContextStore(test_config)
    store.initialize(create_schema=False)

    def fail_mapping(legacy_memory_id, context_item_id):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(store, "record_legacy_mapping", fail_mapping)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        with store.transaction():
            store.create_schema_in_transaction()
            LegacyMemoryMigrator(store, test_config).migrate()
    store.close()

    context_schema_objects = {
        "context_items",
        "context_layers",
        "session_archives",
        "context_sources",
        "context_evidence",
        "legacy_memory_migrations",
        "context_layers_fts",
        "context_layers_fts_trigram",
        "context_layers_fts_ai",
        "context_layers_fts_ad",
        "context_layers_fts_au",
        "context_layers_fts_trigram_ai",
        "context_layers_fts_trigram_ad",
        "context_layers_fts_trigram_au",
    }
    conn = sqlite3.connect(test_config.db_path)
    try:
        survivors = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
        assert survivors & context_schema_objects == set()
        assert conn.execute(
            "SELECT status FROM memories WHERE id=?", (legacy_id,)
        ).fetchone()[0] == "active"
    finally:
        conn.close()


def test_whitespace_only_legacy_value_bounds_sentinels_at_minimum_limits(test_config):
    """Blank-value sentinels must obey retrieval budgets while L2 stays exact."""
    test_config.context_l0_max_chars = 1
    test_config.context_l1_max_chars = 1
    test_config.context_l2_max_chars = 1
    original = " \r\n\t "
    legacy_id = _create_legacy_rows(
        test_config,
        [{"key": "fact:tight-blank", "value": original}],
    )[0]
    before = _legacy_snapshot(test_config)

    with ContextStore(test_config) as store:
        report = LegacyMemoryMigrator(store, test_config).migrate()
        item = _mapped_item(store, legacy_id)

    assert report.created == 1
    assert item.layers.l0 == "…"
    assert item.layers.l1 == "…"
    assert 0 < len(item.layers.l0) <= test_config.context_l0_max_chars
    assert 0 < len(item.layers.l1) <= test_config.context_l1_max_chars
    assert item.layers.l2 == " \n\t "
    assert _legacy_snapshot(test_config) == before


# ---- public conversion policy reused by production writes ----


def test_public_conversion_policy_freezes_the_legacy_mapping_rules(test_config):
    """ContextService must reuse one conversion policy, never a private copy."""
    with ContextStore(test_config) as store:
        migrator = LegacyMemoryMigrator(store, test_config)

        assert migrator.content_type_for({"attribute": "constraint"}) is ContextContentType.CONSTRAINT
        assert migrator.content_type_for({"attribute": "preference"}) is ContextContentType.PREFERENCE
        assert migrator.content_type_for({"attribute": "user_profile"}) is ContextContentType.USER_PROFILE
        assert migrator.content_type_for({"attribute": "decision"}) is ContextContentType.DECISION
        assert (
            migrator.content_type_for(
                {"attribute": "fact", "key": "project:x:progress:log:1"}
            )
            is ContextContentType.SESSION_SUMMARY
        )
        assert migrator.content_type_for({"attribute": "fact", "key": "fact:key"}) is ContextContentType.FACT
        assert migrator.content_type_for({"attribute": "unrecognized"}) is ContextContentType.REFERENCE

        assert migrator.scope_for(ContextContentType.CONSTRAINT) is ContextScope.GLOBAL
        assert migrator.scope_for(ContextContentType.PREFERENCE) is ContextScope.GLOBAL
        assert migrator.scope_for(ContextContentType.USER_PROFILE) is ContextScope.GLOBAL
        assert migrator.scope_for(ContextContentType.DECISION) is ContextScope.PROJECT

        assert migrator.status_for("ACTIVE") is ContextStatus.ACTIVE
        assert migrator.status_for("bogus") is ContextStatus.ARCHIVED
        assert migrator.tier_for("PINNED") is ContextTier.PINNED
        assert migrator.tier_for("bogus") is ContextTier.NORMAL
        assert migrator.importance_for("7.5") == 7.5
        assert migrator.importance_for(0.5) == 5.0
        assert migrator.importance_for(None) == 5.0
        assert migrator.confidence_for(ContextStatus.ACTIVE, ContextTier.NORMAL, None) == 1.0
        assert (
            migrator.confidence_for(
                ContextStatus.ACTIVE, ContextTier.NORMAL, "2031-01-01 00:00:00"
            )
            == 0.5
        )
        assert migrator.confidence_for(ContextStatus.ARCHIVED, ContextTier.PINNED, None) == 1.0
        assert migrator.confidence_for(ContextStatus.ARCHIVED, ContextTier.NORMAL, None) == 0.5
        assert migrator.project_for({"key": "anything"}) == ""
        assert migrator.tags_for("b, a, b, ,c") == ("b", "a", "c")
        assert migrator.timestamps_for({"created_at": "2026-01-01 00:00:00"}) == (
            "2026-01-01 00:00:00",
            "2026-01-01 00:00:00",
        )
        assert migrator.timestamps_for({}) == (
            "1970-01-01 00:00:00",
            "1970-01-01 00:00:00",
        )
        assert migrator.identity_key_for("  a\n b  ", 7) == "a b"
        assert migrator.identity_key_for("   ", 7) == "legacy:memory:7:missing-key"
        layers = migrator.layers_for("some legacy value", ContextContentType.FACT)
        assert layers.l2 == "some legacy value"
        assert migrator.layers_for("", ContextContentType.FACT).l2 == "[empty legacy value]"


def test_draft_from_projection_row_derives_core_metadata_from_the_stored_row(test_config):
    """Production writes inherit exactly what the projection stored."""
    legacy_id = _create_legacy_rows(
        test_config,
        [
            {
                "key": "decision:database",
                "value": "Use SQLite first.",
                "attribute": "decision",
                "tags": ["storage"],
                "importance": 8.0,
                "tier": "pinned",
                "expires_at": "2031-01-02 03:04:05",
            }
        ],
    )[0]

    with ContextStore(test_config) as store:
        migrator = LegacyMemoryMigrator(store, test_config)
        row = store.legacy_projection().get_by_id(legacy_id)

        draft = migrator.draft_from_projection_row(row, confidence=0.9)
        assert draft.identity_key == "decision:database"
        assert draft.content_type is ContextContentType.DECISION
        assert draft.status is ContextStatus.ACTIVE
        assert draft.tier is ContextTier.PINNED
        assert draft.tags == ("storage",)
        assert draft.importance == 8.0
        assert draft.confidence == 0.9
        assert draft.expires_at == "2031-01-02 03:04:05"
        assert draft.layers.l2 == "Use SQLite first."

        default = migrator.draft_from_projection_row(row)
        # expiring rows are not durable, so the policy default is 0.5
        assert default.confidence == 0.5


def test_migrate_projection_row_runs_inside_the_caller_transaction(test_config):
    """One-row migration preserves history and composes with the full migrator."""
    with MemoryStore(test_config) as legacy:
        legacy_id = legacy.add(
            key="alpha", value="historical value", source_session="s-1"
        )
        legacy._conn.execute(
            "UPDATE memories SET access_count=4, created_at=?, updated_at=? WHERE id=?",
            ("2025-01-02 03:04:05", "2026-01-02 03:04:05", legacy_id),
        )
        legacy._conn.commit()

    with ContextStore(test_config) as store:
        migrator = LegacyMemoryMigrator(store, test_config)
        row = store.legacy_projection().get_by_id(legacy_id)

        with pytest.raises(RuntimeError):
            migrator.migrate_projection_row(row)

        with store.transaction():
            context_id = migrator.migrate_projection_row(row)

        assert store.resolve_legacy_mapping(legacy_id) == context_id
        item = store.get_item(context_id)
        assert item.status is ContextStatus.ACTIVE
        assert item.layers.l2 == "historical value"
        assert item.access_count == 4
        assert item.created_at == "2025-01-02 03:04:05"
        assert item.updated_at == "2026-01-02 03:04:05"
        source = store._conn.execute(
            "SELECT source_kind, source_ref, extraction_version "
            "FROM context_sources WHERE item_id=?",
            (context_id,),
        ).fetchone()
        assert tuple(source) == ("migration", "s-1", "legacy-v1")

        report = LegacyMemoryMigrator(store, test_config).migrate()
        assert report.created == 0
        assert report.already_migrated == 1
