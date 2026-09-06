"""Old-shaped memory_* boundary delegating to the ContextService typed API.

The facade serves the narrow read shapes that the legacy Retriever, conflict
detector, and maintenance code need, and routes every mutation through the
service's typed methods. It never exposes a connection, arbitrary SQL, or a
transaction callback; its update_access/archive/metadata helpers are service
calls, not repository writes.
"""

from typing import TYPE_CHECKING

from evolvmem.legacy_models import (
    LegacyAccessRequest,
    LegacyAddRequest,
    LegacyHardDeleteRequest,
    LegacyRemoveRequest,
    LegacyReplaceRequest,
    LegacyStatusRequest,
    LegacyUpdateRequest,
)

if TYPE_CHECKING:
    from evolvmem.context_service import ContextService


class LegacyCompatibilityFacade:
    """Narrow old-API surface over a ContextService, selected by its mode."""

    def __init__(self, service: "ContextService") -> None:
        self._service = service

    # ---- narrow legacy reads ----

    def get_by_id(self, mem_id: int) -> dict | None:
        return self._reader().get_by_id(mem_id)

    def get_by_key(self, key: str) -> list[dict]:
        """Return all records for a given key (including history), sorted by updated_at desc."""
        return self._reader().get_by_key(key)

    def get_by_ids(self, ids: list[int]) -> list[dict]:
        """Batch fetch records by id."""
        return self._reader().get_by_ids(ids)

    def get_active(self) -> list[dict]:
        """Return all status='active' and unexpired memories, ordered by updated_at descending."""
        return self._reader().get_active()

    def search_fts(self, query: str, top_k: int = 20) -> list[dict]:
        """FTS5 full-text search over the legacy projection."""
        return self._reader().search_fts(query, top_k)

    def all_ids(self) -> list[int]:
        """Return ids of all non-deleted records (for USearch sync)."""
        return self._reader().all_ids()

    def count_active(self) -> int:
        """Count status='active' and unexpired memories (same scope as get_active)."""
        return self._reader().count_active()

    def get_forgetting_candidates(
        self, days_threshold: int, access_threshold: int, rate_limit_days: int
    ) -> list[dict]:
        """Return candidates eligible for archival (downgrade)."""
        return self._reader().get_forgetting_candidates(
            days_threshold=days_threshold,
            access_threshold=access_threshold,
            rate_limit_days=rate_limit_days,
        )

    # ---- mutations delegate to the typed service API ----

    def add(
        self,
        key: str,
        value: str,
        attribute: str = "",
        tags: list[str] | None = None,
        source_session: str = "",
        importance: float = 5.0,
        tier: str = "normal",
        expires_at: str | None = None,
    ) -> int:
        """Insert a new active memory. Returns the new legacy record id."""
        result = self._service.legacy_add(
            LegacyAddRequest(
                key=key,
                value=value,
                attribute=attribute,
                tags=tuple(tags) if tags else (),
                source_session=source_session,
                importance=importance,
                tier=tier,
                expires_at=expires_at,
            )
        )
        return result.legacy_id

    def replace(self, key: str, new_value: str, **kwargs) -> int:
        """Replace the old active row. Returns the new legacy record id."""
        tags = kwargs.pop("tags", None)
        result = self._service.legacy_replace(
            LegacyReplaceRequest(
                key=key,
                new_value=new_value,
                attribute=kwargs.pop("attribute", None),
                tags=None if tags is None else tuple(tags),
                source_session=kwargs.pop("source_session", ""),
                importance=kwargs.pop("importance", None),
                tier=kwargs.pop("tier", None),
                expires_at=kwargs.pop("expires_at", None),
            )
        )
        return result.legacy_id

    def remove(self, mem_id: int) -> None:
        """Soft delete: mark the projection and its ContextItem as deleted."""
        self._service.legacy_remove(LegacyRemoveRequest(legacy_id=mem_id))

    def update_metadata(
        self,
        mem_id: int,
        importance: float | None = None,
        tier: str | None = None,
        attribute: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Update importance/tier/attribute/tags in place on both mapped sides."""
        self._service.legacy_update(
            LegacyUpdateRequest(
                legacy_id=mem_id,
                importance=importance,
                tier=tier,
                attribute=attribute,
                tags=None if tags is None else tuple(tags),
            )
        )

    def archive(self, mem_id: int) -> None:
        """Downgrade the projection and its ContextItem to archived."""
        self._service.legacy_archive(LegacyStatusRequest(legacy_id=mem_id))

    def restore(self, mem_id: int) -> None:
        """Bring the projection and its ContextItem back to active."""
        self._service.legacy_restore(LegacyStatusRequest(legacy_id=mem_id))

    def hard_delete(self, mem_id: int) -> None:
        """Physically remove the exact mapping/projection/ContextItem triple."""
        self._service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=mem_id))

    def update_access(self, mem_id: int) -> None:
        """Increment access telemetry on both mapped sides."""
        self._service.legacy_access(LegacyAccessRequest(legacy_ids=(mem_id,)))

    # ---- internal ----

    def _reader(self):
        """The mode-selected narrow read backend owned by the service."""
        return self._service._legacy_reader()
