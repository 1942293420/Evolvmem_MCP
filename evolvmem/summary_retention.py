"""Coverage-gated retention for session summaries and their raw archives.

Frozen rules (pinned by tests):

- Per project, active SESSION_SUMMARY items are ordered newest first
  (``created_at DESC, id DESC``). Items beyond ``context_session_summary_keep``
  whose id belongs to the project's rollup source closure (Task 5's
  ``covered_source_ids``) are archived and their ``session_archive_holds``
  released — both writes share one transaction per project.
- An expired summary (``expires_at <= now``) without rollup coverage is
  never archived: the project is reported in ``pending_projects`` and a
  ``context_project_rollups`` row is created with ``status='pending'`` when
  none exists; an existing row is never downgraded.
- Every freshly written summary whose extraction batch carries a
  ``source_archive_id`` records a ``rollup_pending`` hold in the same write
  transaction, so the raw archive outlives its summary until the rollup
  covers it; ``SessionArchiver._purge_rows`` skips held archives.
- Project-free summaries (``project=''``) are out of scope: the coverage
  closure rejects an empty project, so they are never archived, held, or
  reported pending — the sweep skips them entirely.

Logs and reports carry ids, projects, and reason codes only — never item
content, payload paths, or backend exception messages.
"""

from dataclasses import dataclass
import logging

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextStatus,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.project_rollup import ProjectRollupGenerator

logger = logging.getLogger(__name__)

HOLD_REASON_ROLLUP_PENDING = "rollup_pending"


def is_session_summary_projection(record: dict) -> bool:
    """Return whether a legacy projection row is owned by summary retention."""
    key = record.get("key", "")
    attribute = record.get("attribute", "")
    return (
        isinstance(key, str)
        and isinstance(attribute, str)
        and attribute.strip().casefold() == "fact"
        and ":progress:log:" in key.casefold()
    )


@dataclass(frozen=True, slots=True)
class SummaryRetentionReport:
    """One retention sweep's outcome: ids and project names, never content.

    ``archived_ids`` are the covered summaries archived for exceeding the
    keep count; ``held_ids`` are the expired-but-uncovered summaries that
    stay active; ``pending_projects`` names the projects whose rollup must
    run before their expired summaries can retire.
    """

    archived_ids: tuple[int, ...] = ()
    held_ids: tuple[int, ...] = ()
    pending_projects: tuple[str, ...] = ()


def insert_rollup_pending_hold(
    store: ContextStore, archive_id: int, source_context_id: int
) -> None:
    """Link one archive to the summary it must outlive; idempotent.

    Joins the caller's transaction so the hold commits or rolls back with
    the summary write that justifies it.
    """
    store._require_transaction("insert_rollup_pending_hold")
    store._connection().execute(
        "INSERT OR IGNORE INTO session_archive_holds("
        "archive_id, source_context_id, reason, created_at"
        ") VALUES (?, ?, ?, ?)",
        (archive_id, source_context_id, HOLD_REASON_ROLLUP_PENDING, _now_iso()),
    )


class SummaryRetention:
    """Sweeps session summaries against the rollup coverage gate."""

    def __init__(self, config: Config, store: ContextStore) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        self._config = config
        self._store = store

    def sweep(self, now: str) -> SummaryRetentionReport:
        """Archive covered summaries beyond keep; hold expired uncovered ones."""
        if not isinstance(now, str) or not now.strip():
            raise ContextValidationError("now must be a non-empty string")
        store = self._store
        conn = store._connection()
        projects = [
            str(row["project"])
            for row in conn.execute(
                "SELECT DISTINCT project FROM context_items "
                "WHERE status='active' AND content_type=? AND project != '' "
                "ORDER BY project",
                (ContextContentType.SESSION_SUMMARY.value,),
            ).fetchall()
        ]
        rollup = ProjectRollupGenerator(self._config, store)
        archived: list[int] = []
        held: list[int] = []
        pending: list[str] = []
        for project in projects:
            rows = conn.execute(
                "SELECT id, expires_at FROM context_items "
                "WHERE project=? AND status='active' AND content_type=? "
                "ORDER BY created_at DESC, id DESC",
                (project, ContextContentType.SESSION_SUMMARY.value),
            ).fetchall()
            covered = rollup.covered_source_ids(project)
            overflow = rows[self._config.context_session_summary_keep:]
            archive_ids = [
                int(row["id"]) for row in overflow if int(row["id"]) in covered
            ]
            held_ids = [
                int(row["id"])
                for row in rows
                if row["expires_at"] is not None
                and str(row["expires_at"]) <= now
                and int(row["id"]) not in covered
            ]
            if not archive_ids and not held_ids:
                continue
            with store.transaction():
                for item_id in archive_ids:
                    store.set_item_status(item_id, ContextStatus.ARCHIVED)
                    conn.execute(
                        "DELETE FROM session_archive_holds"
                        " WHERE source_context_id=?",
                        (item_id,),
                    )
                if held_ids:
                    self._ensure_pending_rollup_row(conn, project)
            archived.extend(archive_ids)
            held.extend(held_ids)
            if held_ids:
                pending.append(project)
        return SummaryRetentionReport(
            archived_ids=tuple(archived),
            held_ids=tuple(held),
            pending_projects=tuple(pending),
        )

    def _ensure_pending_rollup_row(self, conn, project: str) -> None:
        """Insert a 'pending' rollup marker; an existing row is never downgraded."""
        existing = conn.execute(
            "SELECT 1 FROM context_project_rollups WHERE project=?",
            (project,),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO context_project_rollups ("
            "project, current_context_id, source_set_hash, covered_through,"
            " generator_version, status, revision, updated_at"
            ") VALUES (?, NULL, '', NULL, ?, 'pending', 1, ?)",
            (project, ProjectRollupGenerator.VERSION, _now_iso()),
        )
