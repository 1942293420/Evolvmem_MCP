"""Atomic, idempotent migration from legacy memories into Context Core."""

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


@dataclass(frozen=True, slots=True)
class LegacyMigrationReport:
    legacy_table_found: bool
    scanned: int
    created: int
    already_migrated: int
    duplicate_active_count: int


class LegacyMemoryMigrator:
    def __init__(self, store: ContextStore, config: Config):
        self.store = store
        self.config = config

    def migrate(self) -> LegacyMigrationReport:
        if not self.store.legacy_memory_table_exists():
            return LegacyMigrationReport(False, 0, 0, 0, 0)

        with self.store.transaction():
            scanned = self.store.legacy_memory_row_count()
            rows = self.store.iter_unmigrated_legacy_rows()
            already_migrated = scanned - len(rows)
            duplicate_ids = self._duplicate_active_ids(rows)
            created = 0

            for row in rows:
                legacy_id = self._legacy_id(row["id"])
                content_type = self._content_type(row)
                status = self._status(row.get("status"))
                extraction_version = "legacy-v1"
                if legacy_id in duplicate_ids:
                    status = ContextStatus.CANDIDATE
                    extraction_version = "legacy-v1:duplicate-active"

                original_l2 = self._source_text(row.get("value")).replace("\r\n", "\n")
                layers = self._layers(original_l2, content_type)
                tier = self._tier(row.get("tier"))
                expires_at = self._optional_text(row.get("expires_at"))
                draft = ContextItemDraft(
                    identity_key=self._identity_key(row.get("key"), legacy_id),
                    content_type=content_type,
                    layers=layers,
                    project="",
                    scope=self._scope(content_type),
                    status=status,
                    tier=tier,
                    tags=self._tags(row.get("tags")),
                    importance=self._importance(row.get("importance")),
                    confidence=self._confidence(status, tier, expires_at),
                    expires_at=expires_at,
                )
                item = self.store.create_item(draft)
                created_at, updated_at = self._timestamps(row)
                self.store.apply_legacy_item_metadata(
                    item.id,
                    access_count=self._nonnegative_int(row.get("access_count")),
                    last_accessed=self._optional_text(row.get("last_accessed")),
                    created_at=created_at,
                    updated_at=updated_at,
                    original_l2=original_l2,
                )
                self.store.record_migration_source(
                    item.id,
                    source_ref=self._source_text(row.get("source_session")),
                    extraction_version=extraction_version,
                )
                self.store.record_legacy_mapping(legacy_id, item.id)
                created += 1

            for row in rows:
                item_id = self.store.resolve_legacy_mapping(
                    self._legacy_id(row["id"])
                )
                if item_id is None:  # pragma: no cover - inserted in the first pass
                    raise RuntimeError("legacy mapping disappeared during migration")
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

    def _duplicate_active_ids(self, rows: list[dict]) -> set[int]:
        active_by_identity: dict[tuple[str, ContextScope], list[dict]] = {}
        for row in rows:
            if self._status(row.get("status")) is not ContextStatus.ACTIVE:
                continue
            legacy_id = self._legacy_id(row["id"])
            content_type = self._content_type(row)
            identity = self._identity_key(row.get("key"), legacy_id)
            active_by_identity.setdefault(
                (identity, self._scope(content_type)), []
            ).append(row)

        duplicates: set[int] = set()
        for group in active_by_identity.values():
            if len(group) < 2:
                continue
            ordered = sorted(
                group,
                key=lambda row: (
                    self._source_text(row.get("updated_at")),
                    self._legacy_id(row["id"]),
                ),
                reverse=True,
            )
            duplicates.update(self._legacy_id(row["id"]) for row in ordered[1:])
        return duplicates

    @staticmethod
    def _legacy_id(value: object) -> int:
        try:
            legacy_id = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"legacy memory id is not an integer: {value!r}") from exc
        if legacy_id <= 0:
            raise ValueError(f"legacy memory id must be positive: {legacy_id}")
        return legacy_id

    @classmethod
    def _identity_key(cls, value: object, legacy_id: int) -> str:
        normalized = re.sub(r"\s+", " ", cls._source_text(value)).strip()
        return normalized or f"legacy:memory:{legacy_id}:missing-key"

    @classmethod
    def _content_type(cls, row: dict) -> ContextContentType:
        attribute = cls._source_text(row.get("attribute")).strip().lower()
        direct = {
            "constraint": ContextContentType.CONSTRAINT,
            "preference": ContextContentType.PREFERENCE,
            "user_profile": ContextContentType.USER_PROFILE,
            "decision": ContextContentType.DECISION,
        }
        if attribute in direct:
            return direct[attribute]
        if attribute == "fact":
            key = cls._source_text(row.get("key"))
            if ":progress:log:" in key:
                return ContextContentType.SESSION_SUMMARY
            return ContextContentType.FACT
        return ContextContentType.REFERENCE

    @staticmethod
    def _scope(content_type: ContextContentType) -> ContextScope:
        if content_type in {
            ContextContentType.CONSTRAINT,
            ContextContentType.PREFERENCE,
            ContextContentType.USER_PROFILE,
        }:
            return ContextScope.GLOBAL
        return ContextScope.PROJECT

    @staticmethod
    def _status(value: object) -> ContextStatus:
        try:
            return ContextStatus(str(value).strip().lower())
        except ValueError:
            return ContextStatus.ARCHIVED

    @staticmethod
    def _tier(value: object) -> ContextTier:
        try:
            return ContextTier(str(value).strip().lower())
        except ValueError:
            return ContextTier.NORMAL

    @staticmethod
    def _importance(value: object) -> float:
        try:
            importance = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 5.0
        if not math.isfinite(importance) or not 1.0 <= importance <= 10.0:
            return 5.0
        return importance

    @staticmethod
    def _confidence(
        status: ContextStatus, tier: ContextTier, expires_at: str | None
    ) -> float:
        durable = expires_at is None
        return 1.0 if durable and (
            status is ContextStatus.ACTIVE or tier is ContextTier.PINNED
        ) else 0.5

    @classmethod
    def _tags(cls, value: object) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                tag.strip()
                for tag in cls._source_text(value).split(",")
                if tag.strip()
            )
        )

    def _layers(
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
    def _timestamps(cls, row: dict) -> tuple[str, str]:
        created = cls._optional_text(row.get("created_at"))
        updated = cls._optional_text(row.get("updated_at"))
        created_at = created or updated or "1970-01-01 00:00:00"
        updated_at = updated or created_at
        return created_at, updated_at

    def _mapped_link(self, value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            legacy_id = self._legacy_id(value)
        except ValueError:
            return None
        return self.store.resolve_legacy_mapping(legacy_id)

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        try:
            result = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return max(result, 0)

    @classmethod
    def _optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = cls._source_text(value)
        return text or None

    @staticmethod
    def _source_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else str(value)
