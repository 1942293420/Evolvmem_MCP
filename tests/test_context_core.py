"""End-to-end contract for the opt-in Context Core foundation."""

import pytest

from evolvmem import ContextCore
from evolvmem.context_migration import LegacyMemoryMigrator, LegacyMigrationReport
from evolvmem.context_vector_sync import ContextVectorSynchronizer
from evolvmem.embedding import EmbeddingEngine
from evolvmem.memory_store import MemoryStore


def test_context_core_bootstraps_idempotent_migration_without_changing_legacy(
    test_config, monkeypatch,
):
    """Skipping migration, loading a model, or rewriting legacy rows breaks bootstrap."""
    def unexpected_model_construction(*args, **kwargs):
        raise AssertionError("ContextCore must not instantiate EmbeddingEngine")

    def unexpected_vector_rebuild(*args, **kwargs):
        raise AssertionError("ContextCore must not rebuild the context vector cache")

    monkeypatch.setattr(EmbeddingEngine, "__init__", unexpected_model_construction)
    monkeypatch.setattr(
        ContextVectorSynchronizer, "rebuild_active_l0", unexpected_vector_rebuild
    )

    legacy_values = {
        "project:demo:decision:database": "Use SQLite for the local prototype.",
        "global:preference:language": "Prefer concise Chinese explanations.",
    }
    with MemoryStore(test_config) as legacy:
        legacy_ids = {
            key: legacy.add(key=key, value=value)
            for key, value in legacy_values.items()
        }

    assert not test_config.model_path.exists()
    assert not test_config.context_vector_path.exists()

    core = ContextCore(test_config)
    try:
        first = core.initialize(migrate_legacy=True)

        assert first.migration == LegacyMigrationReport(
            legacy_table_found=True,
            scanned=2,
            created=2,
            already_migrated=0,
            duplicate_active_count=0,
        )
        assert core.store.config is test_config
        assert core.store.count_by_status() == {"active": 2}
        assert not test_config.model_path.exists()
        assert not test_config.context_vector_path.exists()
        assert not test_config.context_vector_path.with_suffix(
            ".usearch.dirty"
        ).exists()

        second = core.initialize(migrate_legacy=True)

        assert second.migration == LegacyMigrationReport(
            legacy_table_found=True,
            scanned=2,
            created=0,
            already_migrated=2,
            duplicate_active_count=0,
        )
        assert core.store.count_by_status() == {"active": 2}
    finally:
        core.close()

    with MemoryStore(test_config) as legacy:
        assert {
            key: legacy.get_by_id(legacy_id)["value"]
            for key, legacy_id in legacy_ids.items()
        } == legacy_values

    with ContextCore(test_config) as reopened:
        assert reopened.store.count_by_status() == {"active": 2}
        assert reopened.initialize().migration.created == 0


def test_context_manager_closes_store_when_bootstrap_fails(
    test_config, monkeypatch,
):
    """A migration failure during __enter__ must not leak the owned connection."""
    def fail_migration(self):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(LegacyMemoryMigrator, "migrate", fail_migration)
    core = ContextCore(test_config)

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        with core:
            pass

    with pytest.raises(RuntimeError, match="ContextStore is not initialized"):
        core.store.count_by_status()


def test_initialize_closes_store_when_migration_fails(test_config, monkeypatch):
    """A direct bootstrap failure must close the store opened by initialize."""
    def fail_migration(self):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(LegacyMemoryMigrator, "migrate", fail_migration)
    core = ContextCore(test_config)

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        core.initialize()

    assert core.store._conn is None
