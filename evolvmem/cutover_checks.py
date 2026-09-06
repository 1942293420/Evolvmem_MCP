"""Side-effect-free cutover gate evaluation: preflight, projection lag, shadow, primary.

Every function in this module is a pure inspection. ``run_preflight`` opens
the database through a read-only, query-only SQLite URI, reads legacy vector
metadata straight from the file header, and checks space and permissions with
stat calls; it never initializes stores or indexes, never creates tables,
directories, lock files, or vector artifacts, and never writes. Report
payloads stay inside the public summary vocabulary: counts, booleans, reason
codes, sizes, checksum prefixes, and durations — never memory content,
queries, stanza values, secrets, or absolute paths.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
import hashlib
import os
import shutil
import sqlite3
import stat
import time

from usearch.index import Index

from evolvmem.codex_config import (
    REQUIRED_CONTEXT_TOOLS,
    CodexConfigEditor,
    CodexConfigError,
)
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import (
    ContextLayer,
    ContextStatus,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore
from evolvmem.cutover_models import (
    CutoverPreflightReport,
    PrimaryGateEvidence,
    PrimaryGateReport,
    ProjectionLagReport,
    ShadowComparison,
    ShadowGateReport,
)


# The six base tables constituting the Context schema; virtual FTS tables and
# triggers derive from them and are covered by the partial-schema check below.
_CONTEXT_TABLES = (
    "context_items",
    "context_layers",
    "session_archives",
    "context_sources",
    "context_evidence",
    "legacy_memory_migrations",
)

# Free space must fit a full database backup, a second working copy, the old
# vector file, and fixed slack for manifests and staging.
_SPACE_SLACK_BYTES = 64 * 1024 * 1024

_CHECKSUM_PREFIX_CHARS = 16

_CODEX_TIMEOUT_FIELDS = ("startup_timeout_sec", "tool_timeout_sec")


def duplicate_active_legacy_ids(rows: Sequence[Mapping]) -> frozenset[int]:
    """The migrator's duplicate-active policy over minimal legacy row mappings.

    Rows need ``id``, ``key``, ``attribute``, ``status``, and ``updated_at``
    entries; grouping and the deterministic winner choice reuse the public
    LegacyMemoryMigrator conversion policy so preflight, projection-lag, and
    migration can never drift apart.
    """
    active_by_identity: dict[tuple[str, object], list[Mapping]] = {}
    for row in rows:
        if LegacyMemoryMigrator.status_for(row.get("status")) is not ContextStatus.ACTIVE:
            continue
        legacy_id = LegacyMemoryMigrator.legacy_id_for(row["id"])
        content_type = LegacyMemoryMigrator.content_type_for(row)
        identity = (
            LegacyMemoryMigrator.identity_key_for(row.get("key"), legacy_id),
            LegacyMemoryMigrator.scope_for(content_type),
        )
        active_by_identity.setdefault(identity, []).append(row)

    duplicates: set[int] = set()
    for group in active_by_identity.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda row: (
                LegacyMemoryMigrator.source_text(row.get("updated_at")),
                LegacyMemoryMigrator.legacy_id_for(row["id"]),
            ),
            reverse=True,
        )
        duplicates.update(
            LegacyMemoryMigrator.legacy_id_for(row["id"]) for row in ordered[1:]
        )
    return frozenset(duplicates)


def run_preflight(config: Config, *, codex_config_path) -> CutoverPreflightReport:
    """Inspect cutover readiness without any write, creation, or mutation.

    The Codex config path is always explicit; nothing here decides user or
    project config precedence.
    """
    started = time.monotonic()
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if codex_config_path is None:
        raise TypeError("codex_config_path is required")

    reason_codes: list[str] = []

    config_diagnostics = config.validate_runtime()
    config_ok = not config_diagnostics
    if not config_ok:
        reason_codes.append("config_invalid")

    database = _inspect_database(config, reason_codes)
    vector = _inspect_legacy_vector(config, reason_codes)
    codex = _inspect_codex_stanza(Path(codex_config_path), reason_codes)
    space = _inspect_space(
        config,
        db_size_bytes=database["fields"]["db_size_bytes"],
        old_vector_size_bytes=vector["fields"]["old_vector_size_bytes"],
        reason_codes=reason_codes,
    )

    checks = {
        "config_ok": config_ok,
        "database_ok": database["database_ok"],
        "schema_ok": database["schema_ok"],
        "vector_ok": vector["vector_ok"],
        "codex_ok": codex["codex_ok"],
        "space_ok": space["space_ok"],
    }
    duration_ms = (time.monotonic() - started) * 1000.0
    return CutoverPreflightReport(
        ready=all(checks.values()),
        reason_codes=tuple(reason_codes),
        config_diagnostics=config_diagnostics,
        duration_ms=duration_ms,
        **checks,
        **database["fields"],
        **vector["fields"],
        **codex["fields"],
        **space["fields"],
    )


# ---- database inspection (read-only, query-only) ----


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the database pinned to a read-only URI with query_only enforced.

    A checkpointed database (no live -wal file) is opened with immutable=1
    so SQLite never creates -shm/-wal sidecars and the directory tree stays
    byte-identical. A live -wal forces shared read-only access instead, so
    counts still reflect every committed row; SQLite may then maintain its
    own recovery sidecars, but never database bytes.
    """
    wal_path = db_path.with_name(db_path.name + "-wal")
    live_wal = wal_path.is_file() and wal_path.stat().st_size > 0
    uri = db_path.expanduser().resolve().as_uri() + "?mode=ro"
    if not live_wal:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _inspect_database(config: Config, reason_codes: list[str]) -> dict:
    fields = {
        "legacy_schema_present": False,
        "legacy_rows": 0,
        "legacy_status_counts": {},
        "duplicate_active_count": 0,
        "context_schema_present": False,
        "context_items": 0,
        "db_size_bytes": 0,
        "db_sha256_prefix": "",
    }
    db_path = config.db_path
    if not db_path.is_file():
        reason_codes.append("database_missing")
        return {"database_ok": False, "schema_ok": False, "fields": fields}

    fields["db_size_bytes"] = db_path.stat().st_size
    fields["db_sha256_prefix"] = _file_sha256_prefix(db_path)
    try:
        conn = _open_readonly(db_path)
    except (OSError, sqlite3.Error):
        reason_codes.append("database_unreadable")
        return {"database_ok": False, "schema_ok": False, "fields": fields}

    database_ok = True
    schema_ok = True
    try:
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        if not quick_rows or any(str(row[0]).lower() != "ok" for row in quick_rows):
            reason_codes.append("quick_check_failed")
            database_ok = False

        table_names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "memories" in table_names:
            fields["legacy_schema_present"] = True
            fields["legacy_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )
            status_counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM memories GROUP BY status"
                )
            }
            fields["legacy_status_counts"] = status_counts
            fields["duplicate_active_count"] = len(
                duplicate_active_legacy_ids(_legacy_identity_rows(conn))
            )
        else:
            reason_codes.append("legacy_table_missing")
            schema_ok = False

        present_context = sum(1 for name in _CONTEXT_TABLES if name in table_names)
        fields["context_schema_present"] = present_context == len(_CONTEXT_TABLES)
        if "context_items" in table_names:
            fields["context_items"] = int(
                conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0]
            )
        if 0 < present_context < len(_CONTEXT_TABLES):
            reason_codes.append("context_schema_partial")
            schema_ok = False
    except (OSError, sqlite3.Error):
        reason_codes.append("database_unreadable")
        database_ok = False
        schema_ok = False
    finally:
        conn.close()
    return {"database_ok": database_ok, "schema_ok": schema_ok, "fields": fields}


def _legacy_identity_rows(conn: sqlite3.Connection) -> list[dict]:
    """Minimal legacy rows for identity/status policy; tolerant of old schemas."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")}

    def column(name: str, default_sql: str) -> str:
        return f'"{name}"' if name in columns else default_sql

    attribute = (
        column("attribute", "''")
        if "attribute" in columns
        else column("category", "''")
    )
    empty_text = "''"
    archived_status = "'archived'"
    selections = (
        f"{column('id', 'rowid')} AS id",
        f"{column('key', empty_text)} AS key",
        f"{attribute} AS attribute",
        f"{column('status', archived_status)} AS status",
        f"{column('updated_at', empty_text)} AS updated_at",
    )
    rows = conn.execute(f"SELECT {', '.join(selections)} FROM memories").fetchall()
    return [dict(row) for row in rows]


# ---- legacy vector inspection (file metadata only, never initialized) ----


def _inspect_legacy_vector(config: Config, reason_codes: list[str]) -> dict:
    fields = {
        "old_vector_present": False,
        "old_vector_size_bytes": 0,
        "old_vector_sha256_prefix": "",
        "old_vector_count": None,
        "old_vector_dimension": None,
        "old_vector_dirty": False,
    }
    path = config.vector_path
    fields["old_vector_dirty"] = path.with_suffix(f"{path.suffix}.dirty").exists()
    if not path.is_file():
        # No legacy vector file is a clean FTS-only starting point.
        return {"vector_ok": True, "fields": fields}

    fields["old_vector_present"] = True
    fields["old_vector_size_bytes"] = path.stat().st_size
    fields["old_vector_sha256_prefix"] = _file_sha256_prefix(path)
    vector_ok = True
    try:
        metadata = Index.metadata(str(path))
    except Exception:
        metadata = None
    if not isinstance(metadata, dict):
        reason_codes.append("old_vector_unreadable")
        vector_ok = False
    else:
        count = metadata.get("count_present")
        dimension = metadata.get("dimensions")
        if isinstance(count, int) and count >= 0:
            fields["old_vector_count"] = count
        if isinstance(dimension, int) and dimension > 0:
            fields["old_vector_dimension"] = dimension
        if dimension != config.embedding_dim:
            reason_codes.append("old_vector_dimension_mismatch")
            vector_ok = False
    return {"vector_ok": vector_ok, "fields": fields}


# ---- Codex stanza inspection (values stay private; only booleans leave) ----


def _inspect_codex_stanza(config_path: Path, reason_codes: list[str]) -> dict:
    fields = {
        "codex_stanza_present": False,
        "codex_context_tools_ok": False,
        "codex_config_sha256_prefix": "",
    }
    try:
        snapshot = CodexConfigEditor(config_path).snapshot()
    except (CodexConfigError, OSError):
        reason_codes.append("codex_stanza_missing")
        return {"codex_ok": False, "fields": fields}

    fields["codex_stanza_present"] = True
    fields["codex_config_sha256_prefix"] = snapshot.source_file_sha256[
        :_CHECKSUM_PREFIX_CHARS
    ]
    stanza = snapshot.stanza
    codex_ok = True

    command = stanza.get("command")
    if not isinstance(command, str) or not command.strip():
        reason_codes.append("codex_command_missing")
        codex_ok = False
    args = stanza.get("args")
    if args is not None and (
        not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)
    ):
        reason_codes.append("codex_args_invalid")
        codex_ok = False
    env = stanza.get("env")
    if env is not None and not isinstance(env, Mapping):
        reason_codes.append("codex_env_invalid")
        codex_ok = False
    for name in _CODEX_TIMEOUT_FIELDS:
        timeout = stanza.get(name)
        if timeout is not None and (
            type(timeout) not in (int, float) or timeout <= 0
        ):
            reason_codes.append("codex_timeout_invalid")
            codex_ok = False

    tools_ok = _codex_tools_enableable(stanza)
    fields["codex_context_tools_ok"] = tools_ok
    if not tools_ok:
        reason_codes.append("codex_tools_blocked")
        codex_ok = False
    return {"codex_ok": codex_ok, "fields": fields}


def _codex_tools_enableable(stanza: Mapping) -> bool:
    """All four context tools must be invocable without policy edits."""
    enabled = stanza.get("enabled_tools")
    if enabled is not None:
        if not isinstance(enabled, list) or any(
            not isinstance(tool, str) for tool in enabled
        ):
            return False
        if any(tool not in enabled for tool in REQUIRED_CONTEXT_TOOLS):
            return False
    disabled = stanza.get("disabled_tools")
    if disabled is not None:
        if not isinstance(disabled, list) or any(
            not isinstance(tool, str) for tool in disabled
        ):
            return False
        if any(tool in disabled for tool in REQUIRED_CONTEXT_TOOLS):
            return False
    return True


# ---- space and backup-parent inspection (stat only, nothing created) ----


def _inspect_space(
    config: Config,
    *,
    db_size_bytes: int,
    old_vector_size_bytes: int,
    reason_codes: list[str],
) -> dict:
    required = 2 * db_size_bytes + old_vector_size_bytes + _SPACE_SLACK_BYTES
    backup_parent = config.data_dir / "backups"
    anchor = _nearest_existing_ancestor(backup_parent)
    space_ok = True
    writable = False
    free_space = 0
    if anchor is None:
        reason_codes.append("backup_parent_not_writable")
        space_ok = False
    else:
        free_space = shutil.disk_usage(anchor).free
        mode = anchor.stat().st_mode
        writable = bool(mode & stat.S_IWUSR) and os.access(anchor, os.W_OK)
        if not writable:
            reason_codes.append("backup_parent_not_writable")
            space_ok = False
        if free_space < required:
            reason_codes.append("insufficient_free_space")
            space_ok = False
    return {
        "space_ok": space_ok,
        "fields": {
            "free_space_bytes": free_space,
            "required_space_bytes": required,
            "backup_parent_writable": writable,
        },
    }


def _nearest_existing_ancestor(path: Path) -> Path | None:
    """Closest existing ancestor of a path that must not be created here."""
    candidate = path
    while True:
        if candidate.exists():
            return candidate if candidate.is_dir() else candidate.parent
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _file_sha256_prefix(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:_CHECKSUM_PREFIX_CHARS]


# ---- projection lag: read-only per-class divergence counting ----

_ALL_LAYER_NAMES = frozenset({ContextLayer.L0.value, ContextLayer.L1.value, ContextLayer.L2.value})


def check_projection_lag(
    config: Config,
    store: ContextStore,
    *,
    legacy_vector_index=None,
    context_vector_index=None,
) -> ProjectionLagReport:
    """Count every projection/Core divergence class on an initialized store.

    Each remaining legacy row must map to exactly one ContextItem with
    exactly L0/L1/L2, the migrated status (including the duplicate-active
    candidate policy), the L1 deterministically derived from the legacy
    value, and supersession links mirrored through the mapping. Legacy rows
    physically removed by an explicit dual hard delete no longer exist, so
    they legitimately appear in neither scan. Vector caches are derived
    indexes, never projection truth: their health is reported separately
    and never contributes to the lag total.
    """
    started = time.monotonic()
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if not isinstance(store, ContextStore):
        raise ContextValidationError("store must be a ContextStore instance")

    migrator = LegacyMemoryMigrator(store, config)
    rows = store.iter_legacy_rows()
    mapping_rows = [
        (int(row["legacy_memory_id"]), int(row["context_item_id"]))
        for row in store._connection().execute(
            "SELECT legacy_memory_id, context_item_id FROM legacy_memory_migrations"
        )
    ]

    mapping_by_legacy = dict(mapping_rows)
    target_counts: dict[int, int] = {}
    for _legacy_id, item_id in mapping_rows:
        target_counts[item_id] = target_counts.get(item_id, 0) + 1
    duplicate_mapping_target = sum(
        count - 1 for count in target_counts.values() if count > 1
    )
    legacy_ids = {LegacyMemoryMigrator.legacy_id_for(row["id"]) for row in rows}
    orphan_mapping = sum(
        1 for legacy_id, _item_id in mapping_rows if legacy_id not in legacy_ids
    )

    mapped_item_ids = sorted(
        {int(row["context_item_id"]) for row in rows if row["context_item_id"] is not None}
    )
    items_by_id, layers_by_item = _load_mapped_items(store, mapped_item_ids)

    duplicate_active = duplicate_active_legacy_ids(rows)
    missing_mapping = 0
    layer_mismatch = 0
    status_mismatch = 0
    l1_mismatch = 0
    supersession_mismatch = 0
    dangling_item_mapping = 0

    for row in rows:
        legacy_id = LegacyMemoryMigrator.legacy_id_for(row["id"])
        if row["context_item_id"] is None:
            missing_mapping += 1
            continue
        item_id = int(row["context_item_id"])
        item = items_by_id.get(item_id)
        if item is None:
            dangling_item_mapping += 1
            continue
        if layers_by_item.get(item_id, frozenset()) != _ALL_LAYER_NAMES:
            layer_mismatch += 1
        expected_status = (
            ContextStatus.CANDIDATE
            if legacy_id in duplicate_active
            else LegacyMemoryMigrator.status_for(row.get("status"))
        )
        if item["status"] != expected_status.value:
            status_mismatch += 1
        content_type = LegacyMemoryMigrator.content_type_for(row)
        original_l2 = LegacyMemoryMigrator.source_text(row.get("value")).replace(
            "\r\n", "\n"
        )
        expected_l1 = migrator.layers_for(original_l2, content_type).l1
        if store.get_layer(item_id, ContextLayer.L1) != expected_l1:
            l1_mismatch += 1
        if (
            item["supersedes"] != _expected_link(row.get("supersedes"), mapping_by_legacy)
            or item["superseded_by"]
            != _expected_link(row.get("superseded_by"), mapping_by_legacy)
        ):
            supersession_mismatch += 1

    lag = (
        missing_mapping
        + duplicate_mapping_target
        + layer_mismatch
        + status_mismatch
        + l1_mismatch
        + supersession_mismatch
        + orphan_mapping
        + dangling_item_mapping
    )
    legacy_vector_count, legacy_vector_dirty = _vector_health(legacy_vector_index)
    context_vector_count, context_vector_dirty = _vector_health(context_vector_index)
    return ProjectionLagReport(
        projection_lag=lag,
        missing_mapping=missing_mapping,
        duplicate_mapping_target=duplicate_mapping_target,
        layer_mismatch=layer_mismatch,
        status_mismatch=status_mismatch,
        l1_mismatch=l1_mismatch,
        supersession_mismatch=supersession_mismatch,
        orphan_mapping=orphan_mapping,
        dangling_item_mapping=dangling_item_mapping,
        legacy_rows=len(rows),
        mapping_rows=len(mapping_rows),
        legacy_vector_count=legacy_vector_count,
        legacy_vector_dirty=legacy_vector_dirty,
        context_vector_count=context_vector_count,
        context_vector_dirty=context_vector_dirty,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


def _load_mapped_items(
    store: ContextStore, item_ids: list[int]
) -> tuple[dict[int, dict], dict[int, frozenset]]:
    """Status/links and layer-name sets for mapped items, in two queries.

    Raw SQL because retrieval records inner-join L0, which would misclassify
    an item missing only its L0 row as dangling instead of layer-incomplete.
    """
    if not item_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in item_ids)
    conn = store._connection()
    items = {
        int(row["id"]): {
            "status": str(row["status"]),
            "supersedes": row["supersedes"],
            "superseded_by": row["superseded_by"],
        }
        for row in conn.execute(
            "SELECT id, status, supersedes, superseded_by FROM context_items "
            f"WHERE id IN ({placeholders})",
            tuple(item_ids),
        )
    }
    layers: dict[int, set] = {}
    for row in conn.execute(
        f"SELECT item_id, layer FROM context_layers WHERE item_id IN ({placeholders})",
        tuple(item_ids),
    ):
        layers.setdefault(int(row["item_id"]), set()).add(str(row["layer"]))
    return items, {item_id: frozenset(names) for item_id, names in layers.items()}


def _expected_link(value: object, mapping_by_legacy: dict[int, int]) -> int | None:
    """Legacy supersession link mapped into Context ID space, or None."""
    if value is None or value == "":
        return None
    try:
        legacy_id = LegacyMemoryMigrator.legacy_id_for(value)
    except ValueError:
        return None
    return mapping_by_legacy.get(legacy_id)


def _vector_health(index) -> tuple[int | None, bool | None]:
    """Best-effort (count, dirty) metadata; None when not inspectable."""
    if index is None:
        return None, None
    try:
        count: int | None = int(index.count())
    except Exception:
        count = None
    try:
        dirty: bool | None = bool(index.is_dirty())
    except Exception:
        dirty = None
    return count, dirty


# ---- shadow comparison: pure, content-free ID-overlap evaluation ----

# The overlap window and the acceptance thresholds come from the design's
# shadow acceptance section; they are gate constants, not retrieval settings.
_SHADOW_TOP_K = 5
_SHADOW_MIN_OVERLAP = 0.80
_SHADOW_MIN_SEMANTIC_RELEVANT = 5


def compare_shadow(
    legacy_ids: Sequence[int],
    core_ids: Sequence[int],
    mapping: Mapping[int, int],
    *,
    expected_relevant: int,
    below_threshold_core_ids: frozenset[int] = frozenset(),
) -> ShadowComparison:
    """Compare one legacy/Core ranked pair through the ID mapping, content-free.

    The mapped legacy top-1 must stay the Core top-1 (the exact/CJK rule).
    overlap@5 divides the shared mapped IDs in the top-5 window by the
    accountable mapped legacy IDs; pure-vector IDs the Core dropped below
    ``context_vector_min_similarity`` are excluded from that accounting, so
    legitimate threshold drops never count as overlap failures. When every
    mapped legacy hit was excluded the overlap is vacuously 1.0; when no
    legacy ID maps at all there is no evidence and the overlap is 0.0.
    """
    legacy = _id_tuple(legacy_ids, "legacy_ids")
    core = _id_tuple(core_ids, "core_ids")
    id_mapping = _id_mapping(mapping)
    if type(expected_relevant) is not int or expected_relevant < 0:
        raise ContextValidationError("expected_relevant must be a non-negative integer")
    excluded_ids = _id_tuple(
        tuple(below_threshold_core_ids), "below_threshold_core_ids"
    )
    excluded_set = frozenset(excluded_ids)

    mapped_legacy = [id_mapping[legacy_id] for legacy_id in legacy if legacy_id in id_mapping]
    unmapped_legacy_count = len(legacy) - len(mapped_legacy)

    if not legacy and not core:
        top1_match = True
    elif not legacy or not core:
        top1_match = False
    else:
        legacy_top = id_mapping.get(legacy[0])
        top1_match = legacy_top is not None and legacy_top == core[0]

    mapped_top = [
        id_mapping[legacy_id]
        for legacy_id in legacy[:_SHADOW_TOP_K]
        if legacy_id in id_mapping
    ]
    excluded = [core_id for core_id in mapped_top if core_id in excluded_set]
    accounted = [core_id for core_id in mapped_top if core_id not in excluded_set]
    if not accounted:
        overlap = 1.0 if mapped_top else 0.0
    else:
        hits = len(set(accounted) & set(core[:_SHADOW_TOP_K]))
        overlap = hits / len(accounted)

    return ShadowComparison(
        expected_relevant=expected_relevant,
        legacy_count=len(legacy),
        core_count=len(core),
        mapped_legacy_count=len(mapped_legacy),
        unmapped_legacy_count=unmapped_legacy_count,
        below_threshold_excluded=len(excluded),
        top1_match=top1_match,
        overlap_at_5=overlap,
    )


def evaluate_shadow_gate(
    comparisons: Sequence[ShadowComparison],
    *,
    min_overlap: float = _SHADOW_MIN_OVERLAP,
    min_semantic_relevant: int = _SHADOW_MIN_SEMANTIC_RELEVANT,
) -> ShadowGateReport:
    """Aggregate shadow comparisons against the design acceptance thresholds.

    Comparisons expecting at least ``min_semantic_relevant`` relevant items
    are semantic queries judged on overlap@5; the rest are exact/CJK queries
    judged on the mapped top-1. With no comparisons the thresholds were never
    demonstrated, so the gate does not pass.
    """
    try:
        items = tuple(comparisons)
    except TypeError as exc:
        raise ContextValidationError(
            "comparisons must be an iterable of ShadowComparison"
        ) from exc
    if any(not isinstance(item, ShadowComparison) for item in items):
        raise ContextValidationError("comparisons must be an iterable of ShadowComparison")
    if type(min_overlap) not in (int, float) or not 0.0 <= min_overlap <= 1.0:
        raise ContextValidationError("min_overlap must be between 0 and 1")
    if type(min_semantic_relevant) is not int or min_semantic_relevant <= 0:
        raise ContextValidationError("min_semantic_relevant must be a positive integer")

    exact_total = exact_top1_matches = semantic_total = semantic_overlap_passes = 0
    for comparison in items:
        if comparison.expected_relevant >= min_semantic_relevant:
            semantic_total += 1
            if comparison.overlap_at_5 >= min_overlap:
                semantic_overlap_passes += 1
        else:
            exact_total += 1
            if comparison.top1_match:
                exact_top1_matches += 1

    reason_codes: list[str] = []
    if not items:
        reason_codes.append("shadow_evidence_missing")
    if exact_top1_matches < exact_total:
        reason_codes.append("shadow_exact_top1_failed")
    if semantic_overlap_passes < semantic_total:
        reason_codes.append("shadow_overlap_below_threshold")

    return ShadowGateReport(
        comparisons=len(items),
        exact_total=exact_total,
        exact_top1_matches=exact_top1_matches,
        semantic_total=semantic_total,
        semantic_overlap_passes=semantic_overlap_passes,
        thresholds_met=not reason_codes,
        reason_codes=tuple(reason_codes),
    )


def _id_tuple(values: Sequence[int], field_name: str) -> tuple[int, ...]:
    try:
        ids = tuple(values)
    except TypeError as exc:
        raise ContextValidationError(
            f"{field_name} must be a sequence of integer IDs"
        ) from exc
    if any(type(item_id) is not int or item_id <= 0 for item_id in ids):
        raise ContextValidationError(f"{field_name} must contain only positive integer IDs")
    return ids


def _id_mapping(mapping: Mapping[int, int]) -> dict[int, int]:
    if not isinstance(mapping, Mapping):
        raise ContextValidationError("mapping must map legacy IDs to context IDs")
    converted = dict(mapping)
    for legacy_id, context_id in converted.items():
        if type(legacy_id) is not int or legacy_id <= 0:
            raise ContextValidationError("mapping keys must be positive integer legacy IDs")
        if type(context_id) is not int or context_id <= 0:
            raise ContextValidationError("mapping values must be positive integer context IDs")
    return converted


# ---- primary gate: one reusable invariant evaluator, computed verdicts ----


def verify_primary_gate(evidence: PrimaryGateEvidence) -> PrimaryGateReport:
    """Evaluate the formal primary invariants over measured evidence.

    Pure: every input is a typed evidence field and ``ready_primary`` is the
    conjunction of all required checks — a caller cannot supply readiness,
    only evidence.
    """
    if not isinstance(evidence, PrimaryGateEvidence):
        raise ContextValidationError("evidence must be a PrimaryGateEvidence instance")

    checks = {
        "quick_check_ok": evidence.quick_check_ok,
        "mapping_complete": evidence.legacy_rows_unmapped == 0
        and evidence.duplicate_mapping_targets == 0,
        "layers_complete": evidence.mapped_items_with_wrong_layers == 0,
        "migration_idempotent": evidence.second_migration_created == 0,
        "projection_lag_zero": evidence.projection_lag == 0,
        "vector_ready": evidence.vector_healthy or evidence.fts_only_approved,
        "shadow_thresholds_met": evidence.shadow_thresholds_met,
        "config_clean": not evidence.config_diagnostics,
    }
    reason_codes: list[str] = []
    if not checks["quick_check_ok"]:
        reason_codes.append("quick_check_failed")
    if evidence.legacy_rows_unmapped:
        reason_codes.append("legacy_mapping_incomplete")
    if evidence.duplicate_mapping_targets:
        reason_codes.append("duplicate_mapping_target")
    if not checks["layers_complete"]:
        reason_codes.append("layer_invariant_failed")
    if not checks["migration_idempotent"]:
        reason_codes.append("migration_not_idempotent")
    if not checks["projection_lag_zero"]:
        reason_codes.append("projection_lag_nonzero")
    if not checks["vector_ready"]:
        reason_codes.append("context_vector_unhealthy")
    if not checks["shadow_thresholds_met"]:
        reason_codes.append("shadow_thresholds_unmet")
    if not checks["config_clean"]:
        reason_codes.append("config_diagnostics_present")

    return PrimaryGateReport(
        ready_primary=all(checks.values()),
        reason_codes=tuple(reason_codes),
        config_diagnostics=evidence.config_diagnostics,
        legacy_rows_unmapped=evidence.legacy_rows_unmapped,
        duplicate_mapping_targets=evidence.duplicate_mapping_targets,
        mapped_items_with_wrong_layers=evidence.mapped_items_with_wrong_layers,
        second_migration_created=evidence.second_migration_created,
        projection_lag=evidence.projection_lag,
        **checks,
    )


def collect_primary_gate_evidence(
    config: Config,
    store: ContextStore,
    *,
    context_vector_index=None,
    second_migration_created: int | None = None,
    fts_only_approved: bool = False,
    shadow_thresholds_met: bool = False,
) -> PrimaryGateEvidence:
    """Read-only DB-side gate evidence; ceremony proofs stay explicit inputs.

    Everything collectible without writing is derived from live state. When
    no measured second-migration count is supplied, idempotence is derived
    read-only: the migrator only creates items for currently unmapped rows,
    so the unmapped count is exactly what a second run would create.
    ``fts_only_approved`` and ``shadow_thresholds_met`` are ceremony proofs
    that default to unproven.
    """
    if not isinstance(config, Config):
        raise ContextValidationError("config must be a Config instance")
    if not isinstance(store, ContextStore):
        raise ContextValidationError("store must be a ContextStore instance")

    quick_check_ok = _quick_check_ok(store)
    lag = check_projection_lag(config, store)
    if second_migration_created is None:
        second_migration_created = lag.missing_mapping
    return PrimaryGateEvidence(
        quick_check_ok=quick_check_ok,
        legacy_rows_unmapped=lag.missing_mapping,
        duplicate_mapping_targets=lag.duplicate_mapping_target,
        mapped_items_with_wrong_layers=lag.layer_mismatch,
        second_migration_created=second_migration_created,
        projection_lag=lag.projection_lag,
        vector_healthy=_context_vector_healthy(config, store, context_vector_index),
        fts_only_approved=fts_only_approved,
        shadow_thresholds_met=shadow_thresholds_met,
        config_diagnostics=config.validate_runtime(),
    )


def _quick_check_ok(store: ContextStore) -> bool:
    try:
        rows = store._connection().execute("PRAGMA quick_check").fetchall()
    except Exception:
        return False
    return bool(rows) and all(str(row[0]).lower() == "ok" for row in rows)


def _context_vector_healthy(config: Config, store: ContextStore, index) -> bool:
    """Path match, no dirty marker, and count parity with active L0 documents."""
    if index is None:
        return False
    try:
        if index.path != config.context_vector_path.resolve():
            return False
        if bool(index.is_dirty()):
            return False
        return int(index.count()) == len(store.list_vector_documents())
    except Exception:
        return False
