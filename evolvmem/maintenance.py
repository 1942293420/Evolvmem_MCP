"""One-shot historical backfill: deterministic plan and invariant verification.

``build_plan`` is a pure read-only computation over the current database:
legacy mapping lag, per-row resolver outcomes, the retention-driven archive
preview, and a SHA-256 digest over a canonical JSON payload (semantic-table
fingerprints, the registry/alias snapshot, the resolver version, and the
planned actions). The same store state always yields the same digest, which
is what ``maintenance_cli apply`` gates on under the exclusive cutover lock.

Nothing here carries content: item references are typed ids (``legacy:3`` /
``item:12``), reasons and failure details are stable lower-snake codes, and
fingerprints are counts plus timestamps. Human-reviewed resolution rows
(``accepted``/``rejected``) are final: the plan never counts them among the
pending review buckets and the apply backfill never rewrites them.

Like the other operator CLIs, opening the store idempotently bootstraps the
Context schema (``CREATE TABLE IF NOT EXISTS``); the plan never writes a
semantic row.
"""

from dataclasses import dataclass
import hashlib
import json
import re

from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import ContextContentType, ContextValidationError
from evolvmem.context_service import _KNOWN_GENERIC_WORKSPACE_NAMES
from evolvmem.context_store import ContextStore
from evolvmem.project_models import (
    ProjectResolutionDecision,
    ProjectResolutionRequest,
    ProjectResolutionState,
)
from evolvmem.project_resolver import ProjectResolver
from evolvmem.project_rollup import ProjectRollupGenerator
from evolvmem.project_store import ProjectStore
from evolvmem.vector_index import VectorIndex


class MaintenanceError(Exception):
    """Stable machine-readable failure code; never carries detail text."""

    def __init__(self, code: str, *, backup_directory: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.backup_directory = backup_directory


# ---- stable vocabularies ----

_ACTION_MIGRATE = "migrate"
_ACTION_BACKFILL = "backfill_project"
_ACTION_QUEUE_REVIEW = "queue_review"
_ACTION_ARCHIVE_LOG = "archive_log"
_ACTIONS = frozenset(
    {_ACTION_MIGRATE, _ACTION_BACKFILL, _ACTION_QUEUE_REVIEW, _ACTION_ARCHIVE_LOG}
)

_REASON_UNMAPPED = "unmapped_legacy_row"
_REASON_RESOLVED = "resolver_resolved"
_REASON_RESOLUTION_MISSING = "resolution_row_missing"
_REASON_GLOBAL = "global_scope"
_REASON_CONFLICT = "resolver_conflict"
_REASON_UNRESOLVED = "resolver_unresolved"
_REASON_COVERED = "covered_beyond_keep"
_REASONS = frozenset(
    {
        _REASON_UNMAPPED,
        _REASON_RESOLVED,
        _REASON_RESOLUTION_MISSING,
        _REASON_GLOBAL,
        _REASON_CONFLICT,
        _REASON_UNRESOLVED,
        _REASON_COVERED,
    }
)

_HUMAN_FINAL_REVIEW_STATES = frozenset({"accepted", "rejected"})
_LEGACY_SOURCE_VERSION = "legacy-v1"
_PLAN_SCHEMA = "evolvmem.maintenance_plan"
_PLAN_VERSION = 1
_SAFE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{0,64}$")
_ITEM_REF_PATTERN = re.compile(r"^(legacy|item):[0-9]+$")

# Read-only fingerprint per semantic table: COUNT(*) plus the maximum of its
# own deterministic timestamp column (several tables carry no updated_at).
_FINGERPRINT_TABLES: tuple[tuple[str, str], ...] = (
    ("memories", "updated_at"),
    ("context_items", "updated_at"),
    ("context_layers", "updated_at"),
    ("session_archives", "created_at"),
    ("context_sources", "created_at"),
    ("context_evidence", "created_at"),
    ("legacy_memory_migrations", "migrated_at"),
    ("context_project_registry", "updated_at"),
    ("context_project_aliases", "updated_at"),
    ("context_project_workspace_bindings", "updated_at"),
    ("context_project_resolutions", "updated_at"),
    ("context_project_rollups", "updated_at"),
    ("session_archive_holds", "created_at"),
)


def _require_config(config: Config) -> None:
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")


def _require_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ContextValidationError(f"{field_name} must be a non-negative integer")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


# ---- plan models ----


@dataclass(frozen=True, slots=True)
class MaintenanceAction:
    """One planned write step: typed id reference and stable codes only."""

    item_ref: str
    action: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_ref, str) or not _ITEM_REF_PATTERN.fullmatch(
            self.item_ref
        ):
            raise ContextValidationError("item_ref must be a typed id reference")
        if self.action not in _ACTIONS:
            raise ContextValidationError(
                "action must be one of " + ", ".join(sorted(_ACTIONS))
            )
        if self.reason not in _REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_REASONS))
            )

    def public_dict(self) -> dict:
        return {
            "item_ref": self.item_ref,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MaintenanceProjectSummary:
    """Per-project counts plus the retention-driven archive preview."""

    project: str
    active_items: int
    session_summaries: int
    expected_archives: int

    def __post_init__(self) -> None:
        if not isinstance(self.project, str):
            raise ContextValidationError("project must be a string")
        for name in ("active_items", "session_summaries", "expected_archives"):
            _require_non_negative_int(getattr(self, name), name)

    def public_dict(self) -> dict:
        return {
            "project": self.project,
            "active_items": self.active_items,
            "session_summaries": self.session_summaries,
            "expected_archives": self.expected_archives,
        }


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    """The read-only backfill preview plus its deterministic digest.

    ``resolved``/``conflict``/``unresolved``/``global_`` count resolver
    outcomes over all legacy rows, excluding rows whose resolution a human
    already finalized (those are settled and never re-planned).
    """

    legacy_total: int
    mapped: int
    unmapped: int
    resolved: int
    conflict: int
    unresolved: int
    global_: int
    projects: tuple[MaintenanceProjectSummary, ...]
    planned_actions: tuple[MaintenanceAction, ...]
    digest: str

    def __post_init__(self) -> None:
        for name in (
            "legacy_total",
            "mapped",
            "unmapped",
            "resolved",
            "conflict",
            "unresolved",
            "global_",
        ):
            _require_non_negative_int(getattr(self, name), name)
        if self.mapped + self.unmapped != self.legacy_total:
            raise ContextValidationError("mapped + unmapped must equal legacy_total")
        if not isinstance(self.projects, tuple) or not all(
            isinstance(entry, MaintenanceProjectSummary) for entry in self.projects
        ):
            raise ContextValidationError(
                "projects must be a tuple of MaintenanceProjectSummary"
            )
        if not isinstance(self.planned_actions, tuple) or not all(
            isinstance(action, MaintenanceAction) for action in self.planned_actions
        ):
            raise ContextValidationError(
                "planned_actions must be a tuple of MaintenanceAction"
            )
        if not _is_sha256(self.digest):
            raise ContextValidationError("digest must be a lowercase SHA-256 hex")

    def public_dict(self) -> dict:
        return {
            "schema": _PLAN_SCHEMA,
            "version": _PLAN_VERSION,
            "legacy_total": self.legacy_total,
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "resolved": self.resolved,
            "conflict": self.conflict,
            "unresolved": self.unresolved,
            "global_": self.global_,
            "projects": [entry.public_dict() for entry in self.projects],
            "planned_actions": [action.public_dict() for action in self.planned_actions],
            "digest": self.digest,
        }


# ---- verify models ----


@dataclass(frozen=True, slots=True)
class MaintenanceInvariant:
    """One invariant outcome; ``detail`` is a stable code on failure only."""

    name: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ContextValidationError("name must be a non-empty string")
        if type(self.passed) is not bool:
            raise ContextValidationError("passed must be a boolean")
        if not isinstance(self.detail, str) or not _SAFE_CODE_PATTERN.fullmatch(
            self.detail
        ):
            raise ContextValidationError("detail must be a lower-snake code")
        if self.passed and self.detail:
            raise ContextValidationError("a passed invariant carries no detail")

    def public_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class MaintenanceVerifyReport:
    """All invariant outcomes plus the plan digest they were measured against."""

    ok: bool
    invariants: tuple[MaintenanceInvariant, ...]
    plan_digest: str

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise ContextValidationError("ok must be a boolean")
        if not isinstance(self.invariants, tuple) or not all(
            isinstance(entry, MaintenanceInvariant) for entry in self.invariants
        ):
            raise ContextValidationError(
                "invariants must be a tuple of MaintenanceInvariant"
            )
        if not _is_sha256(self.plan_digest):
            raise ContextValidationError("plan_digest must be a lowercase SHA-256 hex")
        if self.ok and not all(entry.passed for entry in self.invariants):
            raise ContextValidationError("ok requires every invariant to pass")

    def public_dict(self) -> dict:
        return {
            "schema": "evolvmem.maintenance_verify",
            "version": _PLAN_VERSION,
            "ok": self.ok,
            "plan_digest": self.plan_digest,
            "invariants": [entry.public_dict() for entry in self.invariants],
        }


# ---- shared read helpers (also used by maintenance_cli's apply) ----


@dataclass(frozen=True, slots=True)
class _RowAssessment:
    """One legacy row's mapping state and resolver decision."""

    legacy_id: int
    item_id: int | None
    decision: ProjectResolutionDecision


def _generic_workspace_names(config: Config) -> tuple[str, ...]:
    """The same generic-name policy the production write path resolves with."""
    names: list[str] = []
    for aliases in (config.context_project_aliases, config.inject_project_aliases):
        if isinstance(aliases, dict):
            names.extend(str(name) for name in aliases)
    names.extend(_KNOWN_GENERIC_WORKSPACE_NAMES)
    return tuple(dict.fromkeys(names))


def _project_store(config: Config, store: ContextStore) -> ProjectStore:
    return ProjectStore(
        store._connection(),
        store._require_transaction,
        generic_names=_generic_workspace_names(config),
    )


def _request_for_row(row: dict) -> ProjectResolutionRequest:
    """Resolver request from one legacy row's stored signals only."""
    content_type = LegacyMemoryMigrator.content_type_for(row)
    return ProjectResolutionRequest(
        content_type=content_type.value,
        scope=LegacyMemoryMigrator.scope_for(content_type).value,
        key=LegacyMemoryMigrator.source_text(row.get("key")),
        tags=LegacyMemoryMigrator.tags_for(row.get("tags")),
        source_session=LegacyMemoryMigrator.source_text(row.get("source_session")),
        source_version=_LEGACY_SOURCE_VERSION,
    )


def _assess_rows(
    store: ContextStore, resolver: ProjectResolver, snapshot
) -> tuple[_RowAssessment, ...]:
    assessments = []
    for row in store.iter_legacy_rows():
        legacy_id = LegacyMemoryMigrator.legacy_id_for(row["id"])
        item_id = row.get("context_item_id")
        assessments.append(
            _RowAssessment(
                legacy_id=legacy_id,
                item_id=None if item_id is None else int(item_id),
                decision=resolver.resolve(_request_for_row(row), snapshot),
            )
        )
    return tuple(assessments)


def _canonical_digest(payload: dict) -> str:
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fingerprint(conn) -> dict:
    """Per-table COUNT(*)+MAX(timestamp); absent tables are explicit."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    fingerprint: dict[str, dict] = {}
    for table, timestamp_column in _FINGERPRINT_TABLES:
        if table not in tables:
            fingerprint[table] = {"present": False, "count": 0, "max_updated": ""}
            continue
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        max_updated = conn.execute(
            f"SELECT COALESCE(MAX({timestamp_column}), '') FROM {table}"
        ).fetchone()[0]
        fingerprint[table] = {
            "present": True,
            "count": int(count),
            "max_updated": str(max_updated),
        }
    return fingerprint


def _registry_payload(snapshot) -> dict:
    return {
        "projects": list(snapshot.projects),
        "aliases": [[alias, project] for alias, project in snapshot.aliases],
        "bindings": [
            {
                "workspace_fingerprint": binding.workspace_fingerprint,
                "project": binding.project,
                "state": binding.state,
                "is_default": binding.is_default,
            }
            for binding in snapshot.bindings
        ],
        "generic_names": list(snapshot.generic_names),
        "revision": snapshot.revision,
    }


# ---- plan ----


def _build_plan_with_store(config: Config, store: ContextStore) -> MaintenancePlan:
    conn = store._connection()
    snapshot = _project_store(config, store).snapshot()
    resolver = ProjectResolver()
    assessments = _assess_rows(store, resolver, snapshot)

    resolved = conflict = unresolved = global_ = mapped = 0
    actions: list[MaintenanceAction] = []
    for assessment in assessments:
        state = assessment.decision.state
        if assessment.item_id is None:
            actions.append(
                MaintenanceAction(
                    f"legacy:{assessment.legacy_id}", _ACTION_MIGRATE, _REASON_UNMAPPED
                )
            )
        else:
            mapped += 1
        resolution = None
        project = ""
        if assessment.item_id is not None:
            resolution = conn.execute(
                "SELECT resolution_state, resolved_project, review_state "
                "FROM context_project_resolutions WHERE item_id=?",
                (assessment.item_id,),
            ).fetchone()
            item = conn.execute(
                "SELECT project FROM context_items WHERE id=?",
                (assessment.item_id,),
            ).fetchone()
            project = "" if item is None else str(item["project"])
        human_final = resolution is not None and str(
            resolution["review_state"]
        ) in _HUMAN_FINAL_REVIEW_STATES
        if human_final:
            continue  # a human decision is final: never counted, never re-planned
        if state is ProjectResolutionState.RESOLVED:
            resolved += 1
        elif state is ProjectResolutionState.CONFLICT:
            conflict += 1
        elif state is ProjectResolutionState.UNRESOLVED:
            unresolved += 1
        else:
            global_ += 1  # global and ignored both stay project-free
        if assessment.item_id is None:
            continue
        target = assessment.decision.resolved_project
        resolution_matches = (
            resolution is not None
            and str(resolution["resolution_state"]) == state.value
            and str(resolution["resolved_project"]) == target
        )
        if state in (
            ProjectResolutionState.RESOLVED,
            ProjectResolutionState.GLOBAL,
            ProjectResolutionState.IGNORED,
        ):
            if project != target or not resolution_matches:
                if state is not ProjectResolutionState.RESOLVED:
                    reason = _REASON_GLOBAL
                elif resolution is None:
                    reason = _REASON_RESOLUTION_MISSING
                else:
                    reason = _REASON_RESOLVED
                actions.append(
                    MaintenanceAction(
                        f"item:{assessment.item_id}", _ACTION_BACKFILL, reason
                    )
                )
        elif not resolution_matches:
            actions.append(
                MaintenanceAction(
                    f"item:{assessment.item_id}",
                    _ACTION_QUEUE_REVIEW,
                    _REASON_CONFLICT
                    if state is ProjectResolutionState.CONFLICT
                    else _REASON_UNRESOLVED,
                )
            )

    projects, archive_actions = _project_summaries(config, store)
    actions.extend(archive_actions)

    payload = {
        "schema": _PLAN_SCHEMA,
        "version": _PLAN_VERSION,
        "resolver_version": ProjectResolver.VERSION,
        "fingerprint": _fingerprint(conn),
        "registry": _registry_payload(snapshot),
        "planned_actions": [action.public_dict() for action in actions],
    }
    return MaintenancePlan(
        legacy_total=len(assessments),
        mapped=mapped,
        unmapped=len(assessments) - mapped,
        resolved=resolved,
        conflict=conflict,
        unresolved=unresolved,
        global_=global_,
        projects=projects,
        planned_actions=tuple(actions),
        digest=_canonical_digest(payload),
    )


def _project_summaries(
    config: Config, store: ContextStore
) -> tuple[tuple[MaintenanceProjectSummary, ...], list[MaintenanceAction]]:
    """Active-item counts and the retention archive preview per project.

    The preview mirrors SummaryRetention's archive rule (overflow beyond the
    keep count that the rollup already covers). The project-free bucket is
    counted but never previewed for archives: the coverage closure rejects an
    empty project, so nothing is predicted for it.
    """
    conn = store._connection()
    rollup = ProjectRollupGenerator(config, store)
    keep = config.context_session_summary_keep
    summaries: list[MaintenanceProjectSummary] = []
    actions: list[MaintenanceAction] = []
    names = [
        str(row["project"])
        for row in conn.execute(
            "SELECT DISTINCT project FROM context_items "
            "WHERE status='active' ORDER BY project"
        )
    ]
    for name in names:
        active_items = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM context_items "
                "WHERE project=? AND status='active'",
                (name,),
            ).fetchone()["c"]
        )
        summary_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM context_items WHERE project=? AND status='active' "
                "AND content_type=? ORDER BY created_at DESC, id DESC",
                (name, ContextContentType.SESSION_SUMMARY.value),
            )
        ]
        archive_ids: list[int] = []
        if name:
            covered = rollup.covered_source_ids(name)
            archive_ids = [item_id for item_id in summary_ids[keep:] if item_id in covered]
        summaries.append(
            MaintenanceProjectSummary(
                project=name,
                active_items=active_items,
                session_summaries=len(summary_ids),
                expected_archives=len(archive_ids),
            )
        )
        actions.extend(
            MaintenanceAction(f"item:{item_id}", _ACTION_ARCHIVE_LOG, _REASON_COVERED)
            for item_id in archive_ids
        )
    return tuple(summaries), actions


def build_plan(config: Config) -> MaintenancePlan:
    """Compute the read-only, deterministic maintenance plan."""
    _require_config(config)
    with ContextStore(config) as store:
        return _build_plan_with_store(config, store)


# ---- verify ----


def _vector_cache_state(config: Config) -> tuple[int, bool]:
    """(document count, dirty) of the on-disk context vector cache.

    A missing cache file is a clean empty cache unless a dirty marker says a
    rebuild never completed; an unreadable cache is reported as (-1, True).
    """
    index = VectorIndex(config, path=config.context_vector_path)
    try:
        if not config.context_vector_path.exists():
            return 0, index.is_dirty()
        index.initialize(dim=config.embedding_dim)
        return index.count(), index.is_dirty()
    except Exception:
        return -1, True
    finally:
        index.close()


def _verify_with_store(config: Config, store: ContextStore) -> MaintenanceVerifyReport:
    conn = store._connection()
    plan_before = _build_plan_with_store(config, store)
    invariants: list[MaintenanceInvariant] = []

    def record(name: str, passed: bool, detail: str) -> None:
        invariants.append(
            MaintenanceInvariant(name=name, passed=passed, detail="" if passed else detail)
        )

    record("mapping_lag_zero", plan_before.unmapped == 0, "mapping_lag")

    layer_counts: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT item_id, layer, COUNT(*) AS c FROM context_layers "
        "WHERE item_id IN (SELECT context_item_id FROM legacy_memory_migrations) "
        "GROUP BY item_id, layer"
    ):
        layer_counts.setdefault(int(row["item_id"]), {})[str(row["layer"])] = int(
            row["c"]
        )
    mapped_ids = [
        int(row["context_item_id"])
        for row in conn.execute(
            "SELECT context_item_id FROM legacy_memory_migrations"
        )
    ]
    layers_ok = all(
        layer_counts.get(item_id) == {"l0": 1, "l1": 1, "l2": 1}
        for item_id in mapped_ids
    )
    record("mapped_item_layers", layers_ok, "layer_set_mismatch")

    resolved_empty = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM context_items i "
            "JOIN context_project_resolutions r ON r.item_id=i.id "
            "WHERE i.status='active' AND r.resolution_state='resolved' "
            "AND i.project=''"
        ).fetchone()["c"]
    )
    record(
        "resolved_project_nonempty", resolved_empty == 0, "resolved_project_empty"
    )

    # Same scope as the plan side: only legacy-migrated rows count; pending
    # resolutions written by the live write path are not plan drift.
    pending = {
        str(row["resolution_state"]): int(row["c"])
        for row in (conn.execute(
            "SELECT resolution_state, COUNT(*) AS c FROM context_project_resolutions "
            "WHERE review_state='pending' "
            "AND item_id IN (SELECT m.context_item_id FROM legacy_memory_migrations m "
            "JOIN memories old ON old.id=m.legacy_memory_id) "
            "GROUP BY resolution_state"
        ) if store.legacy_memory_table_exists() else ())
    }
    counts_match = (
        pending.get("conflict", 0) == plan_before.conflict
        and pending.get("unresolved", 0) == plan_before.unresolved
    )
    record("review_counts_match_plan", counts_match, "review_count_drift")

    singleton_bad = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "SELECT project FROM context_items "
            "WHERE status='active' AND content_type=? "
            "GROUP BY project HAVING COUNT(*) != 1)",
            (ContextContentType.PROJECT_SUMMARY.value,),
        ).fetchone()["c"]
    )
    record("project_summary_singleton", singleton_bad == 0, "summary_not_singleton")

    documents = len(store.list_vector_documents())
    vector_count, vector_dirty = _vector_cache_state(config)
    vector_ok = not vector_dirty and vector_count == documents
    record(
        "vector_documents_match",
        vector_ok,
        "vector_dirty" if vector_dirty else "vector_count_mismatch",
    )

    plan_after = _build_plan_with_store(config, store)
    record(
        "plan_digest_stable",
        plan_after.digest == plan_before.digest,
        "plan_digest_drift",
    )

    return MaintenanceVerifyReport(
        ok=all(entry.passed for entry in invariants),
        invariants=tuple(invariants),
        plan_digest=plan_before.digest,
    )


def verify_invariants(config: Config) -> MaintenanceVerifyReport:
    """Check every post-apply invariant against the current database."""
    _require_config(config)
    with ContextStore(config) as store:
        return _verify_with_store(config, store)
