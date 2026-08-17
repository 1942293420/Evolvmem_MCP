"""Narrow bootstrap boundary for the opt-in Context Core foundation."""

from dataclasses import dataclass

from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator, LegacyMigrationReport
from evolvmem.context_store import ContextStore


@dataclass(frozen=True, slots=True)
class ContextCoreBootstrapReport:
    migration: LegacyMigrationReport


class ContextCore:
    """Own ContextStore setup and the optional one-time legacy migration."""

    def __init__(self, config: Config):
        self.config = config
        self.store = ContextStore(config)

    def initialize(
        self, *, migrate_legacy: bool = True
    ) -> ContextCoreBootstrapReport:
        try:
            self.store.initialize()
            if migrate_legacy:
                migration = LegacyMemoryMigrator(self.store, self.config).migrate()
            else:
                migration = LegacyMigrationReport(
                    legacy_table_found=self.store.legacy_memory_table_exists(),
                    scanned=0,
                    created=0,
                    already_migrated=0,
                    duplicate_active_count=0,
                )
            return ContextCoreBootstrapReport(migration=migration)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "ContextCore":
        self.initialize()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
