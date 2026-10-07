"""Focused recovery for the two real recall blockages, without new services.

Two independent, opt-in repairs live here:

* **Projection status drift.** ``auto_organization._mark_outputs_stale``
  downgrades a superseded revision's ``context_items`` to ``candidate`` but
  never mirrors that status into the legacy ``memories`` projection, so the
  formal primary gate reports ``projection_lag_nonzero`` forever. Only the
  provable shape is repaired: an item that (a) is owned *only* by superseded
  organization tasks, (b) is a Context ``candidate``, (c) whose mapped legacy
  row is still ``active``, and (d) carries no human decision source. Content
  type is never a criterion, so session summaries and atomic knowledge are
  treated alike. The repair is one transaction, idempotent, and only ever
  moves legacy ``active`` -> ``candidate``. Every other divergence is
  *diagnosed* and left alone: a candidate is never re-activated here.

* **Stuck Context vector dirty marker.** A resident service whose optional
  embedding engine was not loaded when a legal write landed keeps
  ``context_vector_dirty`` until something rebuilds the cache. A bounded,
  low-frequency loop reuses the shared engine and the existing atomic staging
  rebuild, and only clears the marker when the SQLite snapshot it encoded is
  still the current one.

Neither path adds a service, a dependency, or a second model. SQLite stays the
truth; vector files stay disposable caches.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import ClassVar

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.cutover_checks import (
    _duplicate_active_quarantined_items,
    duplicate_active_legacy_ids,
)
from evolvmem.cutover_vector import rebuild_context_vector_atomically
from evolvmem.legacy_projection import LegacyProjectionRepository
from evolvmem.vector_index import VectorIndex

# ---- projection status drift -------------------------------------------------

_SCHEMA = "evolvmem.recall_recovery"
_VERSION = 1

# ---- projection status classification ----------------------------------------
#
# Only a *mapped* candidate Context item whose legacy row is still ``active``
# can be a projection status mismatch. Two divergences are legitimate and are
# excluded exactly the way the formal primary gate excludes them:
#
# * the migrator's duplicate-active policy (the losing row of an active
#   identity group keeps its documented quarantine), and
# * a migration source carrying the ``legacy-v1:duplicate-active`` marker.
#
# Exactly two shapes are provable enough to repair:
#
# * ``A`` a superseded automatic output: the item is owned by superseded
#   organization tasks and by no live task, so the downgrade is the
#   ``auto_organization`` stale-output policy; and
# * ``B`` a retracted auto-organization summary: the exact unassign shape —
#   ``session_summary``, empty project, a pending automatic ``unresolved``
#   decision from ``auto-organization.v1``, an owning organization unit, and
#   the matching organization source. Anything else (a plain
#   ``project-resolver.v1`` decision, a ``conflict``, an item without an
#   organization origin, or a non-summary) is never repaired.

_RESOLVER_VERSION = "auto-organization.v1"
_EXTRACTION_VERSION = "auto-organization.v1"

_CLASSIFY_SQL = f"""
SELECT
    i.id AS item_id,
    m.id AS legacy_id,
    m.status AS legacy_status,
    (SELECT COUNT(*) FROM organization_units u
        JOIN organization_tasks t ON t.id = u.task_id
        WHERE u.item_id = i.id AND t.status = 'superseded') AS superseded_owner,
    (SELECT COUNT(*) FROM organization_units u
        JOIN organization_tasks t ON t.id = u.task_id
        WHERE u.item_id = i.id AND t.status != 'superseded') AS live_owner,
    EXISTS(
        SELECT 1 FROM context_project_resolutions r
        WHERE r.item_id = i.id AND r.decision_source = 'human'
    ) AS human,
    (
        i.content_type = 'session_summary'
        AND i.project = ''
        AND EXISTS(
            SELECT 1 FROM organization_units u WHERE u.item_id = i.id
        )
        AND EXISTS(
            SELECT 1 FROM context_sources s
            WHERE s.item_id = i.id
              AND s.source_kind = 'organization'
              AND s.extraction_version = '{_EXTRACTION_VERSION}'
        )
        AND EXISTS(
            SELECT 1 FROM context_project_resolutions r
            WHERE r.item_id = i.id
              AND r.decision_source = 'automatic'
              AND r.resolution_state = 'unresolved'
              AND r.review_state = 'pending'
              AND r.resolver_version = '{_RESOLVER_VERSION}'
        )
    ) AS retracted
FROM context_items i
JOIN legacy_memory_migrations map ON map.context_item_id = i.id
JOIN memories m ON m.id = map.legacy_memory_id
WHERE i.status = 'candidate'
ORDER BY i.id
"""

# The eligibility re-check, re-read inside the writer's transaction. It
# re-asserts every predicate the classification used, so a decision that landed
# between the scan and the write can never be overwritten. Shape B additionally
# requires the owning organization unit and the matching organization source.
_ELIGIBLE_ITEM_SQL = f"""
SELECT i.id AS item_id, m.id AS legacy_id
FROM context_items i
JOIN legacy_memory_migrations map ON map.context_item_id = i.id
JOIN memories m ON m.id = map.legacy_memory_id
WHERE i.id = ?
  AND i.status = 'candidate'
  AND m.status = 'active'
  AND NOT EXISTS(
        SELECT 1 FROM context_project_resolutions r
        WHERE r.item_id = i.id AND r.decision_source = 'human'
  )
  AND (
        (
            EXISTS(
                SELECT 1 FROM organization_units u
                JOIN organization_tasks t ON t.id = u.task_id
                WHERE u.item_id = i.id AND t.status = 'superseded'
            )
            AND NOT EXISTS(
                SELECT 1 FROM organization_units u
                JOIN organization_tasks t ON t.id = u.task_id
                WHERE u.item_id = i.id AND t.status != 'superseded'
            )
        )
        OR (
            i.content_type = 'session_summary'
            AND i.project = ''
            AND EXISTS(
                SELECT 1 FROM organization_units u WHERE u.item_id = i.id
            )
            AND EXISTS(
                SELECT 1 FROM context_sources s
                WHERE s.item_id = i.id
                  AND s.source_kind = 'organization'
                  AND s.extraction_version = '{_EXTRACTION_VERSION}'
            )
            AND EXISTS(
                SELECT 1 FROM context_project_resolutions r
                WHERE r.item_id = i.id
                  AND r.decision_source = 'automatic'
                  AND r.resolution_state = 'unresolved'
                  AND r.review_state = 'pending'
                  AND r.resolver_version = '{_RESOLVER_VERSION}'
            )
        )
  )
"""


def _duplicate_active_legacy(store: ContextStore) -> tuple[frozenset[int], frozenset[int]]:
    """(legacy ids, item ids) the formal gate treats as legitimate quarantine.

    Reuses the shared policy instead of re-deriving it: the migrator's
    duplicate-active computation over the legacy rows, and the documented
    migration-source marker on the mapped items.
    """
    rows = store.iter_legacy_rows()
    duplicate_legacy = duplicate_active_legacy_ids(rows)
    mapped_item_ids = sorted(
        {int(row["context_item_id"]) for row in rows
         if row["context_item_id"] is not None}
    )
    quarantined = _duplicate_active_quarantined_items(store, mapped_item_ids)
    return frozenset(duplicate_legacy), frozenset(quarantined)


@dataclass(frozen=True, slots=True)
class ProjectionStatusReport:
    """Content-free outcome of one diagnosis or repair of the status drift."""

    SCHEMA: ClassVar[str] = _SCHEMA
    VERSION: ClassVar[int] = _VERSION
    KIND: ClassVar[str] = "projection_status"

    status: str
    eligible_count: int
    eligible_item_ids: tuple[int, ...] = ()
    updated_item_ids: tuple[int, ...] = ()
    legacy_updated: int = 0
    superseded_count: int = 0
    retracted_count: int = 0
    human_confirmed_count: int = 0
    live_owner_count: int = 0
    other_drift_count: int = 0
    error_code: str = ""

    _COUNTS = ("eligible_count", "legacy_updated", "superseded_count",
               "retracted_count", "human_confirmed_count", "live_owner_count",
               "other_drift_count")

    def __post_init__(self) -> None:
        for name in self._COUNTS:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "eligible_item_ids", tuple(int(i) for i in self.eligible_item_ids))
        object.__setattr__(self, "updated_item_ids", tuple(int(i) for i in self.updated_item_ids))
        if self.legacy_updated != len(self.updated_item_ids):
            raise ValueError("legacy_updated must equal the reported updated ids")
        if self.eligible_count != len(self.eligible_item_ids):
            raise ValueError("eligible_count must equal the reported eligible ids")
        if self.eligible_count != self.superseded_count + self.retracted_count:
            raise ValueError("eligible_count must equal the two provable shapes")

    @property
    def unattributed(self) -> int:
        """Mapped mismatches that no provable shape explains: reported only."""
        return self.human_confirmed_count + self.live_owner_count + self.other_drift_count

    def public_dict(self) -> dict:
        """Counts, IDs, and error codes only — never Layer content."""
        return {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "kind": self.KIND,
            "status": self.status,
            "eligible_count": self.eligible_count,
            "eligible_item_ids": list(self.eligible_item_ids),
            "updated_item_ids": list(self.updated_item_ids),
            "legacy_updated": self.legacy_updated,
            "superseded_count": self.superseded_count,
            "retracted_count": self.retracted_count,
            "human_confirmed_count": self.human_confirmed_count,
            "live_owner_count": self.live_owner_count,
            "other_drift_count": self.other_drift_count,
            "error_code": self.error_code,
        }


def _validate(config: Config, store: ContextStore) -> None:
    """Reject a mismatched pair before any read or write can go to the wrong DB."""
    if not isinstance(store, ContextStore):
        raise TypeError("store must be a ContextStore instance")
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
    if store.config.db_path.resolve() != config.db_path.resolve():
        raise ValueError("store and config must address the same database")


def classify_projection_status_drift(store: ContextStore) -> ProjectionStatusReport:
    """Read-only classification of every *legitimate* mapped candidate mismatch.

    A candidate Context item with no legacy row is not drift at all, and the
    two documented legitimate divergences — the migrator's duplicate-active
    policy and the migration ``legacy-v1:duplicate-active`` marker — are
    excluded exactly as the formal primary gate excludes them. They are not
    eligible, not unattributed, and not counted anywhere.
    """
    duplicate_legacy, quarantined_items = _duplicate_active_legacy(store)
    superseded: list[int] = []
    retracted: list[int] = []
    human_confirmed = 0
    live_owner = 0
    other_drift = 0
    for row in store._connection().execute(_CLASSIFY_SQL):
        if row["legacy_status"] != "active":
            # Already candidate/superseded/deleted: one-sided, never re-activated.
            continue
        item_id = int(row["item_id"])
        legacy_id = row["legacy_id"]
        if item_id in quarantined_items or legacy_id in duplicate_legacy:
            # Legitimate duplicate-active quarantine: the formal gate does not
            # count it as a mismatch, so this module must not either.
            continue
        if row["retracted"]:
            # The item's own automatic resolution withdrew it. An explicit
            # retraction outranks task ownership, so it is repaired even while
            # the task that produced it is still open.
            retracted.append(item_id)
        elif row["human"]:
            human_confirmed += 1
        elif row["superseded_owner"] and not row["live_owner"]:
            superseded.append(item_id)
        elif row["live_owner"]:
            # Still owned by a live task with no retraction: a current output
            # may legitimately be candidate while its projection is active.
            live_owner += 1
        else:
            other_drift += 1
    eligible = tuple(superseded + retracted)
    return ProjectionStatusReport(
        status="drift" if eligible else ("unattributed_drift" if
                                         (human_confirmed or live_owner or other_drift)
                                         else "clean"),
        eligible_count=len(eligible),
        eligible_item_ids=eligible,
        superseded_count=len(superseded),
        retracted_count=len(retracted),
        human_confirmed_count=human_confirmed,
        live_owner_count=live_owner,
        other_drift_count=other_drift,
    )


def _set_legacy_candidate_in_transaction(store: ContextStore,
                                         item_ids: Iterable[int]) -> list[int]:
    """Move the eligible items' legacy rows, re-checking every predicate.

    The re-check runs inside the caller's write transaction, so a project
    decision or human confirmation that landed after the scan wins and the
    item is skipped instead of overwritten. Callers must already hold the
    transaction.
    """
    conn = store._connection()
    repository = store.legacy_projection()
    if not isinstance(repository, LegacyProjectionRepository):
        # The projection writer must stay the store-owned, transaction-guarded
        # repository; anything else would commit outside our boundary.
        raise TypeError("store.legacy_projection() must return the store repository")
    updated: list[int] = []
    for item_id in item_ids:
        row = conn.execute(_ELIGIBLE_ITEM_SQL, (int(item_id),)).fetchone()
        if row is None:
            continue
        if repository.set_status(int(row["legacy_id"]), "candidate"):
            updated.append(int(row["item_id"]))
    return updated


def diagnose_projection_status(
    config: Config, store: ContextStore
) -> ProjectionStatusReport:
    """Read-only diagnosis: counts, item ids, and error codes only.

    A missing or unreadable schema degrades to ``status='failed'`` with an
    error code instead of raising, so a probe can always print a verdict. A
    non-Config or non-ContextStore argument is still a programming error.
    """
    _validate(config, store)
    try:
        return classify_projection_status_drift(store)
    except Exception as exc:
        return ProjectionStatusReport(
            status="failed", eligible_count=0, error_code=exc.__class__.__name__,
        )


def apply_projection_status_recovery(
    config: Config, store: ContextStore
) -> ProjectionStatusReport:
    """Repair the provable mapped status drift, one transaction, idempotently.

    The scan is a plain read; every class of drift this module cannot prove is
    only reported. Each candidate is then re-checked inside the single write
    transaction and its legacy row is the only thing written, always
    ``active`` -> ``candidate``. Human-confirmed items, items still owned by a
    live task, items with no mapped legacy row, and unattributed drift stay
    exactly as they are, and no candidate is ever re-activated.
    """
    _validate(config, store)
    scan = classify_projection_status_drift(store)
    with store.transaction():
        updated = _set_legacy_candidate_in_transaction(store, scan.eligible_item_ids)
    if updated:
        status = "applied"
    elif scan.unattributed:
        status = "unattributed_drift"
    else:
        status = "clean"
    return ProjectionStatusReport(
        status=status,
        eligible_count=scan.eligible_count,
        eligible_item_ids=scan.eligible_item_ids,
        updated_item_ids=tuple(updated),
        legacy_updated=len(updated),
        superseded_count=scan.superseded_count,
        retracted_count=scan.retracted_count,
        human_confirmed_count=scan.human_confirmed_count,
        live_owner_count=scan.live_owner_count,
        other_drift_count=scan.other_drift_count,
    )


# ---- stuck Context vector dirty marker ---------------------------------------


@dataclass(frozen=True, slots=True)
class VectorSourceSnapshot:
    """The exact active L0 set a rebuild is allowed to declare synchronized."""

    item_ids: tuple[int, ...]
    content_digest: str
    document_count: int


@dataclass(frozen=True, slots=True)
class VectorRecoveryReport:
    """Content-free outcome of one context-vector recovery attempt."""

    SCHEMA: ClassVar[str] = _SCHEMA
    VERSION: ClassVar[int] = _VERSION
    KIND: ClassVar[str] = "context_vector_recovery"

    status: str
    dirty: bool
    attempted: bool
    document_count: int = 0
    reason_codes: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "kind": self.KIND,
            "status": self.status,
            "dirty": bool(self.dirty),
            "attempted": bool(self.attempted),
            "document_count": int(self.document_count),
            "reason_codes": list(self.reason_codes),
        }


def snapshot_vector_documents(store: ContextStore) -> VectorSourceSnapshot:
    """Snapshot the active L0 ids and a digest of their content."""
    documents = store.list_vector_documents()
    digest = hashlib.sha256()
    ids: list[int] = []
    for document in documents:
        ids.append(int(document.item_id))
        digest.update(str(int(document.item_id)).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(document.l0.encode("utf-8"))
        digest.update(b"\x1e")
    return VectorSourceSnapshot(
        item_ids=tuple(ids),
        content_digest=digest.hexdigest(),
        document_count=len(documents),
    )


def source_snapshot_unchanged(store: ContextStore, snapshot: VectorSourceSnapshot) -> bool:
    """True only when the active L0 truth is byte-for-byte the snapshot."""
    try:
        current = snapshot_vector_documents(store)
    except Exception:
        return False
    return (
        current.content_digest == snapshot.content_digest
        and current.item_ids == snapshot.item_ids
    )


def recover_context_vector(
    config: Config,
    store: ContextStore,
    index: VectorIndex,
    embedding_engine,
    *,
    force: bool = False,
) -> VectorRecoveryReport:
    """Rebuild one Context vector cache behind a truth-snapshot guard.

    The rebuild reuses the atomic stager: a new image is built beside the
    formal file and swapped only after exact verification. The dirty marker is
    cleared only when the encoded snapshot is still the current SQLite truth;
    a mid-rebuild write or an encode failure keeps the marker and the previous
    readable image so the next bounded attempt can retry.
    """
    _validate(config, store)
    if index.path != config.context_vector_path.resolve():
        return VectorRecoveryReport(
            status="failed",
            dirty=True,
            attempted=False,
            reason_codes=("index_path_mismatch",),
        )
    try:
        dirty = bool(index.is_dirty())
    except Exception:
        dirty = True
    if not dirty and not force:
        return VectorRecoveryReport(
            status="clean", dirty=False, attempted=False
        )
    engine_loaded = embedding_engine is not None and bool(
        getattr(embedding_engine, "is_loaded", False)
    )
    if not engine_loaded:
        # Leave the durable retry marker exactly where it is.
        try:
            index.mark_dirty()
            index.preserve_dirty()
        except Exception:
            pass
        return VectorRecoveryReport(
            status="unavailable",
            dirty=True,
            attempted=False,
            reason_codes=("engine_unavailable",),
        )

    snapshot = snapshot_vector_documents(store)
    stage = rebuild_context_vector_atomically(
        config,
        store,
        embedding_engine,
        commit_guard=lambda: source_snapshot_unchanged(store, snapshot),
    )
    if stage.status == "staged":
        # When this object is not backed by the shared cache, release its stale
        # in-memory image so the next read loads the swapped file. A shared
        # cache object needs nothing here: its own stamp check reloads the file,
        # so a concurrent request never touches a half-closed index.
        if getattr(index, "_shared_cache", None) is None:
            try:
                index.close()
                index.initialize(dim=config.embedding_dim)
            except Exception:
                pass
        return VectorRecoveryReport(
            status="recovered",
            dirty=False,
            attempted=True,
            document_count=stage.document_count,
        )
    if stage.status == "fts_only":
        return VectorRecoveryReport(
            status="fts_only",
            dirty=True,
            attempted=True,
            document_count=stage.document_count,
            reason_codes=tuple(stage.reason_codes),
        )
    return VectorRecoveryReport(
        status="failed",
        dirty=True,
        attempted=True,
        document_count=stage.document_count,
        reason_codes=tuple(stage.reason_codes) or ("stage_failed",),
    )


class ContextVectorRecoveryLoop:
    """Low-frequency, bounded recovery for resident Context vector caches.

    Defaults are deliberately conservative: at least one minute between
    attempts, exponential backoff on failure, and a per-round document budget
    so a large library is encoded only when the round can afford it. The loop
    never raises into the caller's polling loop and never blocks it for longer
    than the rebuild itself.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = 60.0,
        failure_backoff_seconds: float = 60.0,
        maximum_backoff_seconds: float = 900.0,
        document_budget: int = 20_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (
            ("interval_seconds", interval_seconds),
            ("failure_backoff_seconds", failure_backoff_seconds),
            ("maximum_backoff_seconds", maximum_backoff_seconds),
        ):
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if type(document_budget) is not int or document_budget <= 0:
            raise ValueError("document_budget must be a positive integer")
        if interval_seconds < 60.0:
            raise ValueError("recovery must not run more often than once a minute")
        self.interval_seconds = float(interval_seconds)
        self.failure_backoff_seconds = float(failure_backoff_seconds)
        self.maximum_backoff_seconds = float(maximum_backoff_seconds)
        self.document_budget = document_budget
        self._clock = clock
        self._next_attempt_at = 0.0
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def next_attempt_at(self) -> float:
        return self._next_attempt_at

    def due(self) -> bool:
        return self._clock() >= self._next_attempt_at

    def tick(self, services: Iterable) -> tuple[VectorRecoveryReport, ...]:
        """Attempt at most one recovery per due interval across all services.

        The next attempt is scheduled from the outcome of this round: the base
        interval after success or no work, an escalated bounded backoff after a
        failure. Repeated polls inside that window are silent.
        """
        if not self.due():
            return ()
        reports: list[VectorRecoveryReport] = []
        failed = False
        for service in services:
            report = self._recover_one(service)
            if report is None:
                continue
            reports.append(report)
            if report.status in ("failed", "unavailable"):
                failed = True
        self._consecutive_failures = self._consecutive_failures + 1 if failed else 0
        self._defer(
            self._backoff() if self._consecutive_failures else self.interval_seconds
        )
        return tuple(reports)

    # -- internals --

    def _defer(self, seconds: float) -> None:
        self._next_attempt_at = self._clock() + max(0.0, float(seconds))

    def _backoff(self) -> float:
        exponent = max(0, self._consecutive_failures - 1)
        return min(
            self.maximum_backoff_seconds,
            self.failure_backoff_seconds * (2 ** min(exponent, 8)),
        )

    def _recover_one(self, service) -> VectorRecoveryReport | None:
        """Return a report when this service needed attention, else None."""
        try:
            config = service.config
            store = service.store
            index = service.vector_index
            engine = service.embedding_engine
        except Exception:
            return VectorRecoveryReport(
                status="failed", dirty=True, attempted=False,
                reason_codes=("service_unavailable",),
            )
        try:
            if not bool(index.is_dirty()):
                return None
        except Exception:
            return VectorRecoveryReport(
                status="failed", dirty=True, attempted=False,
                reason_codes=("index_unavailable",),
            )
        try:
            documents = len(store.list_vector_documents())
        except Exception:
            return VectorRecoveryReport(
                status="failed", dirty=True, attempted=False,
                reason_codes=("source_unreadable",),
            )
        if documents > self.document_budget:
            return VectorRecoveryReport(
                status="deferred",
                dirty=True,
                attempted=False,
                document_count=documents,
                reason_codes=("document_budget_exceeded",),
            )
        try:
            return recover_context_vector(config, store, index, engine)
        except Exception as exc:
            return VectorRecoveryReport(
                status="failed",
                dirty=True,
                attempted=True,
                reason_codes=(exc.__class__.__name__,),
            )
