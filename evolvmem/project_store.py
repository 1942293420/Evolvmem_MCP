"""Project registry, workspace binding, and resolution persistence.

The store borrows the ContextStore connection and transaction boundary,
mirroring LegacyProjectionRepository: it has no lifecycle or commit powers of
its own, so every write requires the owner's active transaction and commits or
rolls back with the owner's outer boundary.

Concurrency is per-row revision CAS: conditional updates check rowcount==1 and
fail with a stable ``ProjectStoreError`` code, never with exception text.
The registry snapshot revision is the sum of each table's maximum revision, so
any registered change moves it; archived projects stay in the table but leave
the active snapshot.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3

from evolvmem.project_models import (
    ProjectRegistrySnapshot,
    ProjectResolutionDecision,
    ProjectResolutionState,
    WorkspaceBindingSnapshot,
)


def _now_iso() -> str:
    """Return a UTC timestamp whose lexical order matches chronological order."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ProjectStoreError(Exception):
    """Stable machine-readable failure code; never carries detail text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProjectResolutionRow:
    """One context_project_resolutions row, column names preserved."""

    item_id: int
    resolution_state: str
    decision_source: str
    review_state: str
    proposed_project: str
    resolved_project: str
    confidence: str
    method: str
    evidence_json: str
    resolver_version: str
    revision: int
    reviewed_at: str | None
    created_at: str
    updated_at: str


_PENDING_STATES = frozenset(
    {ProjectResolutionState.CONFLICT, ProjectResolutionState.UNRESOLVED}
)

_SNAPSHOT_TABLES = (
    "context_project_registry",
    "context_project_aliases",
    "context_project_workspace_bindings",
)


class ProjectStore:
    """Registry/binding/resolution persistence on a borrowed connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        require_transaction: Callable[[str], None],
        *,
        generic_names: tuple[str, ...],
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if not callable(require_transaction):
            raise TypeError("require_transaction must be callable")
        connection.row_factory = sqlite3.Row
        self._conn = connection
        self._require_owner_transaction = require_transaction
        self._generic_names = tuple(generic_names)

    # ---- reads ----

    def snapshot(self) -> ProjectRegistrySnapshot:
        """Assemble the resolver's immutable registry view.

        Only active projects are exposed; every binding state is included so
        the resolver can apply its own candidate/active/revoked rules. The
        revision is the sum of the three tables' maximum revisions.
        """
        projects = self._conn.execute(
            "SELECT project FROM context_project_registry "
            "WHERE status='active' ORDER BY project"
        ).fetchall()
        aliases = self._conn.execute(
            "SELECT alias, project FROM context_project_aliases ORDER BY alias"
        ).fetchall()
        bindings = self._conn.execute(
            "SELECT workspace_fingerprint, project, state, is_default "
            "FROM context_project_workspace_bindings "
            "ORDER BY workspace_fingerprint, project"
        ).fetchall()
        return ProjectRegistrySnapshot(
            projects=tuple(row["project"] for row in projects),
            aliases=tuple((row["alias"], row["project"]) for row in aliases),
            bindings=tuple(
                WorkspaceBindingSnapshot(
                    workspace_fingerprint=row["workspace_fingerprint"],
                    project=row["project"],
                    state=row["state"],
                    is_default=bool(row["is_default"]),
                )
                for row in bindings
            ),
            generic_names=self._generic_names,
            revision=sum(self._max_revision(table) for table in _SNAPSHOT_TABLES),
        )

    def list_pending_resolutions(
        self, *, limit: int = 100
    ) -> tuple[ProjectResolutionRow, ...]:
        rows = self._conn.execute(
            "SELECT * FROM context_project_resolutions "
            "WHERE review_state='pending' ORDER BY item_id LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(ProjectResolutionRow(**dict(row)) for row in rows)

    # ---- registry writes ----

    def register_project(self, project: str, display_name: str = "") -> None:
        """Insert a project as active; an existing row is left untouched."""
        self._require_owner_transaction("project_store.register_project")
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO context_project_registry"
            "(project, status, display_name, revision, created_at, updated_at) "
            "VALUES (?, 'active', ?, 1, ?, ?) ON CONFLICT(project) DO NOTHING",
            (project, display_name, now, now),
        )

    def set_display_name(
        self, project: str, display_name: str, *, expected_revision: int
    ) -> None:
        """Set (or clear) a project's human-facing display name (revision CAS)."""
        self._require_owner_transaction("project_store.set_display_name")
        cursor = self._conn.execute(
            "UPDATE context_project_registry "
            "SET display_name=?, revision=revision+1, updated_at=? "
            "WHERE project=? AND revision=?",
            (display_name, _now_iso(), project, expected_revision),
        )
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_registry WHERE project=?",
                (project,),
                missing="project_not_found",
            )

    def archive_project(self, project: str, *, expected_revision: int) -> None:
        self._require_owner_transaction("project_store.archive_project")
        cursor = self._conn.execute(
            "UPDATE context_project_registry "
            "SET status='archived', revision=revision+1, updated_at=? "
            "WHERE project=? AND revision=?",
            (_now_iso(), project, expected_revision),
        )
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_registry WHERE project=?",
                (project,),
                missing="project_not_found",
            )

    # ---- alias writes ----

    def add_alias(self, alias: str, project: str) -> None:
        """Insert a globally unique alias for a registered project."""
        self._require_owner_transaction("project_store.add_alias")
        self._require_project(project)
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO context_project_aliases"
                "(alias, project, revision, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (alias, project, now, now),
            )
        except sqlite3.IntegrityError:
            raise ProjectStoreError("alias_conflict") from None

    def remove_alias(self, alias: str, *, expected_revision: int) -> None:
        self._require_owner_transaction("project_store.remove_alias")
        cursor = self._conn.execute(
            "DELETE FROM context_project_aliases WHERE alias=? AND revision=?",
            (alias, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ProjectStoreError("revision_conflict")

    # ---- binding writes ----

    def bind_workspace(
        self,
        workspace_fingerprint: str,
        project: str,
        *,
        method: str,
        make_default: bool,
    ) -> None:
        """Create (or re-activate) an active binding and pre-build its focus row.

        An existing candidate/revoked row is promoted to active with
        revision+1. A second active default for the same fingerprint trips the
        partial unique index and surfaces as ``default_binding_conflict``; the
        owner transaction is left intact for the caller to continue or roll
        back.
        """
        self._require_owner_transaction("project_store.bind_workspace")
        self._require_project(project)
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO context_project_workspace_bindings"
                "(workspace_fingerprint, project, state, is_default, method,"
                " revision, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?, 1, ?, ?) "
                "ON CONFLICT(workspace_fingerprint, project) DO UPDATE SET "
                "state='active', is_default=excluded.is_default, "
                "method=excluded.method, "
                "revision=revision+1, updated_at=excluded.updated_at",
                (
                    workspace_fingerprint,
                    project,
                    1 if make_default else 0,
                    method,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ProjectStoreError("default_binding_conflict") from None
        self._conn.execute(
            "INSERT INTO continuity_focus"
            "(project, workspace_fingerprint, workstream_id, revision, updated_at) "
            "VALUES (?, ?, NULL, 0, ?) "
            "ON CONFLICT(project, workspace_fingerprint) DO NOTHING",
            (project, workspace_fingerprint, now),
        )

    def revoke_binding(
        self,
        workspace_fingerprint: str,
        project: str,
        *,
        expected_revision: int,
    ) -> None:
        """Revoke a binding and clear its focus workstream in one transaction."""
        self._require_owner_transaction("project_store.revoke_binding")
        now = _now_iso()
        cursor = self._conn.execute(
            "UPDATE context_project_workspace_bindings "
            "SET state='revoked', is_default=0, revision=revision+1, updated_at=? "
            "WHERE workspace_fingerprint=? AND project=? AND revision=?",
            (now, workspace_fingerprint, project, expected_revision),
        )
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_workspace_bindings "
                "WHERE workspace_fingerprint=? AND project=?",
                (workspace_fingerprint, project),
                missing="binding_not_found",
            )
        self._conn.execute(
            "UPDATE continuity_focus "
            "SET workstream_id=NULL, revision=revision+1, updated_at=? "
            "WHERE project=? AND workspace_fingerprint=?",
            (now, project, workspace_fingerprint),
        )

    def set_default_binding(
        self,
        workspace_fingerprint: str,
        project: str,
        *,
        expected_revision: int,
    ) -> None:
        self._require_owner_transaction("project_store.set_default_binding")
        try:
            cursor = self._conn.execute(
                "UPDATE context_project_workspace_bindings "
                "SET is_default=1, revision=revision+1, updated_at=? "
                "WHERE workspace_fingerprint=? AND project=? "
                "AND state='active' AND revision=?",
                (_now_iso(), workspace_fingerprint, project, expected_revision),
            )
        except sqlite3.IntegrityError:
            raise ProjectStoreError("default_binding_conflict") from None
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_workspace_bindings "
                "WHERE workspace_fingerprint=? AND project=?",
                (workspace_fingerprint, project),
                missing="binding_not_found",
            )

    # ---- resolution writes ----

    def record_resolution(
        self, item_id: int, decision: ProjectResolutionDecision
    ) -> None:
        """Upsert the automatic decision for an item.

        Resolved/global/ignored decisions need no review; conflict and
        unresolved stay pending for a human. Re-recording replaces the whole
        decision (reviewed_at resets to NULL) and bumps the revision.
        """
        self._require_owner_transaction("project_store.record_resolution")
        now = _now_iso()
        review_state = (
            "pending" if decision.state in _PENDING_STATES else "not_required"
        )
        evidence_json = json.dumps(
            list(decision.evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._conn.execute(
            "INSERT INTO context_project_resolutions"
            "(item_id, resolution_state, decision_source, review_state,"
            " proposed_project, resolved_project, confidence, method,"
            " evidence_json, resolver_version, revision, reviewed_at,"
            " created_at, updated_at) "
            "VALUES (?, ?, 'automatic', ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET "
            "resolution_state=excluded.resolution_state, "
            "decision_source=excluded.decision_source, "
            "review_state=excluded.review_state, "
            "proposed_project=excluded.proposed_project, "
            "resolved_project=excluded.resolved_project, "
            "confidence=excluded.confidence, "
            "method=excluded.method, "
            "evidence_json=excluded.evidence_json, "
            "resolver_version=excluded.resolver_version, "
            "reviewed_at=NULL, "
            "revision=revision+1, updated_at=excluded.updated_at",
            (
                item_id,
                decision.state.value,
                review_state,
                decision.proposed_project,
                decision.resolved_project,
                decision.confidence,
                decision.method,
                evidence_json,
                decision.resolver_version,
                now,
                now,
            ),
        )

    def accept_resolution(
        self, item_id: int, project: str, *, expected_revision: int
    ) -> None:
        """Human-accept a resolution and set the item's project atomically."""
        self._require_owner_transaction("project_store.accept_resolution")
        self._require_project(project)
        now = _now_iso()
        cursor = self._conn.execute(
            "UPDATE context_project_resolutions "
            "SET review_state='accepted', decision_source='human', "
            "resolved_project=?, reviewed_at=?, "
            "revision=revision+1, updated_at=? "
            "WHERE item_id=? AND revision=?",
            (project, now, now, item_id, expected_revision),
        )
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_resolutions WHERE item_id=?",
                (item_id,),
                missing="resolution_not_found",
            )
        self._conn.execute(
            "UPDATE context_items SET project=?, updated_at=? WHERE id=?",
            (project, now, item_id),
        )

    def reject_resolution(self, item_id: int, *, expected_revision: int) -> None:
        """Human-reject a resolution; the item's project stays untouched."""
        self._require_owner_transaction("project_store.reject_resolution")
        now = _now_iso()
        cursor = self._conn.execute(
            "UPDATE context_project_resolutions "
            "SET review_state='rejected', decision_source='human', reviewed_at=?, "
            "revision=revision+1, updated_at=? "
            "WHERE item_id=? AND revision=?",
            (now, now, item_id, expected_revision),
        )
        if cursor.rowcount != 1:
            self._raise_cas_failure(
                "SELECT 1 FROM context_project_resolutions WHERE item_id=?",
                (item_id,),
                missing="resolution_not_found",
            )

    # ---- seeding ----

    def seed_from_config(self, aliases: dict[str, str]) -> None:
        """Insert missing projects/aliases from config; existing rows win."""
        self._require_owner_transaction("project_store.seed_from_config")
        now = _now_iso()
        for alias, project in aliases.items():
            self._conn.execute(
                "INSERT INTO context_project_registry"
                "(project, status, revision, created_at, updated_at) "
                "VALUES (?, 'active', 1, ?, ?) ON CONFLICT(project) DO NOTHING",
                (project, now, now),
            )
            self._conn.execute(
                "INSERT INTO context_project_aliases"
                "(alias, project, revision, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?) ON CONFLICT(alias) DO NOTHING",
                (alias, project, now, now),
            )

    # ---- helpers ----

    def _max_revision(self, table: str) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(revision), 0) AS max_revision FROM {table}"
        ).fetchone()
        return int(row["max_revision"])

    def _require_project(self, project: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM context_project_registry WHERE project=?", (project,)
        ).fetchone()
        if row is None:
            raise ProjectStoreError("project_not_found")

    def _raise_cas_failure(
        self, exists_sql: str, params: tuple, *, missing: str
    ) -> None:
        """Distinguish a stale revision from a missing row after rowcount==0."""
        exists = self._conn.execute(exists_sql, params).fetchone() is not None
        raise ProjectStoreError("revision_conflict" if exists else missing)
