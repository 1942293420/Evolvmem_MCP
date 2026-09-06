"""Atomic, idempotent migration from legacy memories into Context Core."""

from collections.abc import Callable
from dataclasses import dataclass
import math
import re

from evolvmem.config import Config
from evolvmem.context_layers import layers_from_legacy_value
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_store import ContextStore
from evolvmem.project_models import ProjectResolutionDecision


@dataclass(frozen=True, slots=True)
class LegacyMigrationReport:
    legacy_table_found: bool
    scanned: int
    created: int
    already_migrated: int
    duplicate_active_count: int


class LegacyMemoryMigrator:
    def __init__(
        self,
        store: ContextStore,
        config: Config,
        *,
        project_decider: Callable[[dict], str] | None = None,
    ):
        self.store = store
        self.config = config
        self._project_decider = project_decider

    def migrate(self) -> LegacyMigrationReport:
        if not self.store.legacy_memory_table_exists():
            return LegacyMigrationReport(False, 0, 0, 0, 0)

        with self.store.transaction():
            scanned = self.store.legacy_memory_row_count()
            rows = self.store.iter_legacy_rows()
            already_migrated = sum(
                row["context_item_id"] is not None for row in rows
            )
            duplicate_ids = self._duplicate_active_ids(rows)
            created = 0
            prepared_rows: list[
                tuple[
                    dict,
                    int,
                    int | None,
                    ContextStatus,
                    str,
                    ContextTier,
                    str | None,
                    float,
                ]
            ] = []

            for row in rows:
                legacy_id = self.legacy_id_for(row["id"])
                status = self.status_for(row.get("status"))
                extraction_version = "legacy-v1"
                if legacy_id in duplicate_ids:
                    status = ContextStatus.CANDIDATE
                    extraction_version = "legacy-v1:duplicate-active"
                tier = self.tier_for(row.get("tier"))
                expires_at = self.optional_text(row.get("expires_at"))
                prepared_rows.append(
                    (
                        row,
                        legacy_id,
                        row["context_item_id"],
                        status,
                        extraction_version,
                        tier,
                        expires_at,
                        self.confidence_for(status, tier, expires_at),
                    )
                )

            # Release active-identity slots before inserting or promoting the
            # deterministic winner for each complete legacy identity group.
            for (
                row,
                _legacy_id,
                context_item_id,
                status,
                extraction_version,
                _tier,
                _expires_at,
                confidence,
            ) in prepared_rows:
                if context_item_id is None or status is ContextStatus.ACTIVE:
                    continue
                self.store._reconcile_legacy_item_state(
                    context_item_id,
                    status=status,
                    confidence=confidence,
                    updated_at=self.timestamps_for(row)[1],
                    extraction_version=extraction_version,
                )

            for (
                row,
                _legacy_id,
                context_item_id,
                status,
                extraction_version,
                _tier,
                _expires_at,
                confidence,
            ) in prepared_rows:
                if context_item_id is not None:
                    continue
                self.migrate_projection_row(
                    row,
                    status=status,
                    confidence=confidence,
                    extraction_version=extraction_version,
                )
                created += 1

            for (
                row,
                legacy_id,
                _context_item_id,
                status,
                extraction_version,
                _tier,
                _expires_at,
                confidence,
            ) in prepared_rows:
                item_id = self.store.resolve_legacy_mapping(legacy_id)
                if item_id is None:  # pragma: no cover - mapped in the insertion pass
                    raise RuntimeError("legacy mapping disappeared during migration")
                self.store._reconcile_legacy_item_state(
                    item_id,
                    status=status,
                    confidence=confidence,
                    updated_at=self.timestamps_for(row)[1],
                    extraction_version=extraction_version,
                )
                self.store.set_supersession_links(
                    item_id,
                    supersedes=self._mapped_link(row.get("supersedes")),
                    superseded_by=self._mapped_link(row.get("superseded_by")),
                )

        return LegacyMigrationReport(
            legacy_table_found=True,
            scanned=scanned,
            created=created,
            already_migrated=already_migrated,
            duplicate_active_count=len(duplicate_ids),
        )

    # ---- public conversion policy shared with production writes ----

    def draft_from_projection_row(
        self,
        row: dict,
        *,
        status: ContextStatus | None = None,
        confidence: float | None = None,
        decision: ProjectResolutionDecision | None = None,
    ) -> ContextItemDraft:
        """Derive the Context draft for one legacy projection row.

        Production writes pass the just-written row so Core metadata inherits
        exactly what the projection stored; the batch migrator passes explicit
        status/confidence for its duplicate-active and durability policy.
        ``decision`` is the typed-write resolution result for the row; without
        it the project falls back to the migrator's ``project_decider`` (if
        any) and otherwise stays empty.
        """
        legacy_id = self.legacy_id_for(row["id"])
        resolved_status = (
            status if status is not None else self.status_for(row.get("status"))
        )
        tier = self.tier_for(row.get("tier"))
        expires_at = self.optional_text(row.get("expires_at"))
        content_type = self.content_type_for(row)
        original_l2 = self.source_text(row.get("value")).replace("\r\n", "\n")
        return ContextItemDraft(
            identity_key=self.identity_key_for(row.get("key"), legacy_id),
            content_type=content_type,
            layers=self.layers_for(original_l2, content_type),
            project=self.project_for(row, decision=decision),
            scope=self.scope_for(content_type),
            status=resolved_status,
            tier=tier,
            tags=self.tags_for(row.get("tags")),
            importance=self.importance_for(row.get("importance")),
            confidence=(
                confidence
                if confidence is not None
                else self.confidence_for(resolved_status, tier, expires_at)
            ),
            expires_at=expires_at,
        )

    def migrate_projection_row(
        self,
        row: dict,
        *,
        status: ContextStatus | None = None,
        confidence: float | None = None,
        extraction_version: str = "legacy-v1",
    ) -> int:
        """Migrate one unmapped projection row inside the caller's transaction.

        Returns the new ContextItem id; historical timestamps, access
        telemetry, source evidence, and best-effort supersession links come
        along so a lazily migrated row is indistinguishable from a batch one.
        """
        self.store._require_transaction("migrate_projection_row")
        legacy_id = self.legacy_id_for(row["id"])
        draft = self.draft_from_projection_row(
            row, status=status, confidence=confidence
        )
        item = self.store._create_legacy_item(draft)
        original_l2 = self.source_text(row.get("value")).replace("\r\n", "\n")
        created_at, updated_at = self.timestamps_for(row)
        self.store._apply_legacy_item_metadata(
            item.id,
            access_count=self.nonnegative_int(row.get("access_count")),
            last_accessed=self.optional_text(row.get("last_accessed")),
            created_at=created_at,
            updated_at=updated_at,
            original_l2=original_l2,
        )
        self.store.record_migration_source(
            item.id,
            source_ref=self.source_text(row.get("source_session")),
            extraction_version=extraction_version,
        )
        self.store.record_legacy_mapping(legacy_id, item.id)
        self.store.set_supersession_links(
            item.id,
            supersedes=self._mapped_link(row.get("supersedes")),
            superseded_by=self._mapped_link(row.get("superseded_by")),
        )
        return item.id

    @staticmethod
    def legacy_id_for(value: object) -> int:
        try:
            legacy_id = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"legacy memory id is not an integer: {value!r}") from exc
        if legacy_id <= 0:
            raise ValueError(f"legacy memory id must be positive: {legacy_id}")
        return legacy_id

    @classmethod
    def identity_key_for(cls, value: object, legacy_id: int) -> str:
        normalized = re.sub(r"\s+", " ", cls.source_text(value)).strip()
        return normalized or f"legacy:memory:{legacy_id}:missing-key"

    @classmethod
    def content_type_for(cls, row: dict) -> ContextContentType:
        attribute = cls.source_text(row.get("attribute")).strip().lower()
        direct = {
            "constraint": ContextContentType.CONSTRAINT,
            "preference": ContextContentType.PREFERENCE,
            "user_profile": ContextContentType.USER_PROFILE,
            "decision": ContextContentType.DECISION,
        }
        if attribute in direct:
            return direct[attribute]
        if attribute == "fact":
            key = cls.source_text(row.get("key"))
            if ":progress:log:" in key:
                return ContextContentType.SESSION_SUMMARY
            return ContextContentType.FACT
        return ContextContentType.REFERENCE

    @staticmethod
    def scope_for(content_type: ContextContentType) -> ContextScope:
        if content_type in {
            ContextContentType.CONSTRAINT,
            ContextContentType.PREFERENCE,
            ContextContentType.USER_PROFILE,
        }:
            return ContextScope.GLOBAL
        return ContextScope.PROJECT

    def project_for(
        self, row: dict, *, decision: ProjectResolutionDecision | None = None
    ) -> str:
        """Project for one row: explicit decision > decider > project-free.

        An explicit typed-write ``decision`` wins (its ``resolved_project`` is
        empty for conflict/unresolved/global, keeping the row project-free).
        Without one, the optional ``project_decider`` is consulted; batch
        migration constructs no decider, so historical rows stay project-free
        until the maintenance backfill resolves them explicitly.
        """
        if decision is not None:
            return decision.resolved_project
        if self._project_decider is not None:
            return self._project_decider(row)
        return ""

    @staticmethod
    def status_for(value: object) -> ContextStatus:
        try:
            return ContextStatus(str(value).strip().lower())
        except ValueError:
            return ContextStatus.ARCHIVED

    @staticmethod
    def tier_for(value: object) -> ContextTier:
        try:
            return ContextTier(str(value).strip().lower())
        except ValueError:
            return ContextTier.NORMAL

    @staticmethod
    def importance_for(value: object) -> float:
        try:
            importance = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 5.0
        if not math.isfinite(importance) or not 1.0 <= importance <= 10.0:
            return 5.0
        return importance

    @staticmethod
    def confidence_for(
        status: ContextStatus, tier: ContextTier, expires_at: str | None
    ) -> float:
        durable = expires_at is None
        return 1.0 if durable and (
            status is ContextStatus.ACTIVE or tier is ContextTier.PINNED
        ) else 0.5

    @classmethod
    def tags_for(cls, value: object) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                tag.strip()
                for tag in cls.source_text(value).split(",")
                if tag.strip()
            )
        )

    def layers_for(
        self, original_l2: str, content_type: ContextContentType
    ) -> ContextLayers:
        if original_l2.strip():
            return layers_from_legacy_value(
                original_l2, content_type=content_type, config=self.config
            )
        return layers_from_legacy_value(
            "[empty legacy value]",
            content_type=content_type,
            config=self.config,
        )

    @classmethod
    def timestamps_for(cls, row: dict) -> tuple[str, str]:
        created = cls.optional_text(row.get("created_at"))
        updated = cls.optional_text(row.get("updated_at"))
        created_at = created or updated or "1970-01-01 00:00:00"
        updated_at = updated or created_at
        return created_at, updated_at

    @staticmethod
    def nonnegative_int(value: object) -> int:
        try:
            result = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return max(result, 0)

    @classmethod
    def optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = cls.source_text(value)
        return text or None

    @staticmethod
    def source_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else str(value)

    # ---- batch-migration internals ----

    def _duplicate_active_ids(self, rows: list[dict]) -> set[int]:
        active_by_identity: dict[tuple[str, ContextScope], list[dict]] = {}
        for row in rows:
            if self.status_for(row.get("status")) is not ContextStatus.ACTIVE:
                continue
            legacy_id = self.legacy_id_for(row["id"])
            content_type = self.content_type_for(row)
            identity = self.identity_key_for(row.get("key"), legacy_id)
            active_by_identity.setdefault(
                (identity, self.scope_for(content_type)), []
            ).append(row)

        duplicates: set[int] = set()
        for group in active_by_identity.values():
            if len(group) < 2:
                continue
            ordered = sorted(
                group,
                key=lambda row: (
                    self.source_text(row.get("updated_at")),
                    self.legacy_id_for(row["id"]),
                ),
                reverse=True,
            )
            duplicates.update(self.legacy_id_for(row["id"]) for row in ordered[1:])
        return duplicates

    def _mapped_link(self, value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            legacy_id = self.legacy_id_for(value)
        except ValueError:
            return None
        return self.store.resolve_legacy_mapping(legacy_id)
