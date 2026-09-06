"""Rolling per-project knowledge summaries (B2).

Frozen deterministic rules (pinned by tests, mirroring the playbook
generator's injected-LLM convention):

- Degradation: a missing LLM yields ``skipped``/``llm_unavailable`` and
  writes nothing; a project without sources yields ``skipped``/
  ``no_sources``. Neither is an error.
- Dedup: the source set is every active SESSION_SUMMARY of the project
  (created_at ascending) plus the active atomic items
  (DECISION/FACT/EXPERIENCE) created strictly after the rollup row's
  ``covered_through`` watermark. When the set's sha256 — with the generator
  VERSION mixed in — matches a ``ready`` rollup row, the project is
  ``skipped``/``unchanged`` and the LLM is never called.
- Prompt assembly: redacted copies of the sources' L1 texts plus the
  previous summary's L1 are sent. L2 and raw session material never leave
  the database, and redaction never mutates the stored layers.
- Output gates (the playbook generator's set, unchanged): JSON parse
  failure, a missing/non-string/empty layer, sensitive content,
  low-information content, or a layer over its configured character budget
  fails the attempt with a stable reason; the old active summary stays
  untouched and the rollup row records ``failed``, preserving the previous
  ``current_context_id``/``covered_through`` so a later run retries.
- Success: one transaction supersedes the active
  ``project:{project}:knowledge:current`` identity with the new
  PROJECT_SUMMARY, links every source through a ``context_reference``
  context_sources row, and upserts the ``context_project_rollups`` row
  (``ready``, newest source's created_at as ``covered_through``).
- Vector handoff: strictly after the commit, the injected ``vector_sync``
  callable (the ContextService post-commit convention) retires the
  superseded summary's L0 vector and upserts the new one. A failure flips
  the rollup row to ``vector_dirty`` — the summary stays authoritative.

Logs and reports carry ids and reason codes only — never prompt text, layer
content, absolute paths, or backend exception messages.
"""

from dataclasses import dataclass
import hashlib
import json
import logging
import re

from evolvmem.config import Config
from evolvmem.context_layers import normalize_content
from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.extraction_policy import (
    contains_cjk,
    contains_sensitive_text,
    redact_messages,
)

logger = logging.getLogger(__name__)

_STATUS_READY = "ready"
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"
_STATUS_VECTOR_DIRTY = "vector_dirty"
_STATUSES = frozenset(
    {_STATUS_READY, _STATUS_SKIPPED, _STATUS_FAILED, _STATUS_VECTOR_DIRTY}
)

_REASON_NONE = ""
_REASON_LLM_UNAVAILABLE = "llm_unavailable"
_REASON_NO_SOURCES = "no_sources"
_REASON_UNCHANGED = "unchanged"
_SKIP_REASONS = frozenset(
    {_REASON_LLM_UNAVAILABLE, _REASON_NO_SOURCES, _REASON_UNCHANGED}
)
_REASON_LLM_NO_RESPONSE = "llm_no_response"
_REASON_INVALID_JSON = "invalid_json"
_REASON_SENSITIVE_CONTENT = "sensitive_content"
_REASON_LOW_INFORMATION = "low_information"
_REASON_LAYER_TOO_LONG = "layer_too_long"
_FAILED_REASONS = frozenset(
    {
        _REASON_LLM_NO_RESPONSE,
        _REASON_INVALID_JSON,
        _REASON_SENSITIVE_CONTENT,
        _REASON_LOW_INFORMATION,
        _REASON_LAYER_TOO_LONG,
    }
)
_REASONS = frozenset({_REASON_NONE} | _SKIP_REASONS | _FAILED_REASONS)

_LAYER_NAMES = ("l0", "l1", "l2")
_GENERATION_TARGETS = {"l0": 160, "l1": 800, "l2": 3000}
_ATOMIC_SOURCE_TYPES = (
    ContextContentType.DECISION.value,
    ContextContentType.FACT.value,
    ContextContentType.EXPERIENCE.value,
)
_ROLLUP_ROW_COLUMNS = (
    "project, current_context_id, source_set_hash, covered_through,"
    " generator_version, status, revision"
)

# Mirror the playbook gate's informativeness notion: after removing
# redaction markers and punctuation/whitespace, substantive Chinese text
# must remain.
_REDACTION_MARKER_RE = re.compile(
    r"\[已脱敏:(?:private_key|url凭据|token|api_key|password|凭据位置)\]"
)
_LOW_INFO_STRIP_RE = re.compile(r"[\s，。；：、.!?！？]+")


@dataclass(frozen=True, slots=True)
class ProjectRollupReport:
    """One project's rollup outcome: ids and reason codes, never content."""

    project: str
    status: str
    reason: str = ""
    context_id: int | None = None
    covered_through: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project, str) or not self.project.strip():
            raise ContextValidationError("project must be a non-empty string")
        object.__setattr__(self, "project", self.project.strip())
        if self.status not in _STATUSES:
            raise ContextValidationError(
                "status must be one of " + ", ".join(sorted(_STATUSES))
            )
        if self.reason not in _REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_REASONS))
            )
        if self.context_id is not None and (
            type(self.context_id) is not int or self.context_id <= 0
        ):
            raise ContextValidationError(
                "context_id must be a positive integer or None"
            )
        if self.covered_through is not None and not isinstance(
            self.covered_through, str
        ):
            raise ContextValidationError("covered_through must be a string or None")
        if self.status in (_STATUS_READY, _STATUS_VECTOR_DIRTY):
            if self.reason != _REASON_NONE or self.context_id is None:
                raise ContextValidationError(
                    "ready/vector_dirty reports carry no reason and a context_id"
                )
        if self.status == _STATUS_FAILED and self.reason not in _FAILED_REASONS:
            raise ContextValidationError("failed reports must carry a failure reason")
        if self.status == _STATUS_SKIPPED and self.reason not in _SKIP_REASONS:
            raise ContextValidationError("skipped reports must carry a skip reason")


class ProjectRollupGenerator:
    """Generates or refreshes one project's rolling knowledge summary.

    ``llm`` is the narrow ``callable(prompt) -> str | None`` the playbook
    generator popularized; ``vector_sync`` is the optional post-commit
    handoff ``callable(new_context_id, superseded_id, l0) -> bool`` returning
    True only when the derived vector cache reflects the new active summary.
    Both default to their explicit degradations.
    """

    VERSION = "project-rollup.v1"

    def __init__(self, config: Config, store: ContextStore, *, llm=None,
                 vector_sync=None) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        if llm is not None and not callable(llm):
            raise ContextValidationError("llm must be a callable or None")
        if vector_sync is not None and not callable(vector_sync):
            raise ContextValidationError("vector_sync must be a callable or None")
        self._config = config
        self._store = store
        self._llm = llm
        self._vector_sync = vector_sync

    # ---- public API ----

    def rollup_project(self, project: str) -> ProjectRollupReport:
        """Roll one project's current sources into its knowledge summary."""
        project = _normalize_project(project)
        if self._llm is None:
            logger.warning("project rollup skipped: %s", _REASON_LLM_UNAVAILABLE)
            return ProjectRollupReport(
                project=project,
                status=_STATUS_SKIPPED,
                reason=_REASON_LLM_UNAVAILABLE,
            )
        row = self._rollup_row(project)
        previous_covered = row["covered_through"] if row is not None else None
        sources = self._collect_sources(project, previous_covered)
        if not sources:
            logger.warning("project rollup skipped: %s", _REASON_NO_SOURCES)
            return ProjectRollupReport(
                project=project, status=_STATUS_SKIPPED, reason=_REASON_NO_SOURCES
            )
        source_ids = tuple(source.id for source in sources)
        source_hash = _source_set_hash(source_ids, self.VERSION)
        if (
            row is not None
            and row["status"] == _STATUS_READY
            and row["source_set_hash"] == source_hash
        ):
            return ProjectRollupReport(
                project=project,
                status=_STATUS_SKIPPED,
                reason=_REASON_UNCHANGED,
                context_id=_row_context_id(row),
                covered_through=row["covered_through"],
            )

        current = self._current_summary(project)
        prompt = self._build_prompt(project, sources, current)
        response = self._call_llm(prompt)
        if response is None:
            logger.warning("project rollup failed: %s", _REASON_LLM_NO_RESPONSE)
            return self._fail(project, source_hash, row, _REASON_LLM_NO_RESPONSE)
        gate_reason, layers = self._gate_output(response)
        if gate_reason is not None:
            logger.warning("project rollup failed: %s", gate_reason)
            return self._fail(project, source_hash, row, gate_reason)

        covered_through = max(source.created_at for source in sources)
        superseded_id = current.id if current is not None else None
        draft = ContextItemDraft(
            identity_key=_rollup_identity_key(project),
            content_type=ContextContentType.PROJECT_SUMMARY,
            layers=ContextLayers(
                l0=layers["l0"],
                l1=layers["l1"],
                l2=layers["l2"],
                generator=self.VERSION,
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            tier=ContextTier.NORMAL,
            tags=(),
            importance=max(source.importance for source in sources),
            confidence=min(source.confidence for source in sources),
        )
        store = self._store
        with store.transaction():
            item = store.supersede_active(draft)
            conn = store._connection()
            for source_id in source_ids:
                conn.execute(
                    "INSERT INTO context_sources ("
                    "item_id, archive_id, source_kind, source_ref,"
                    " extraction_version, created_at"
                    ") VALUES (?, NULL, 'context_reference', ?, ?, ?)",
                    (item.id, str(source_id), self.VERSION, _now_iso()),
                )
            conn.execute(
                "UPDATE context_items SET source_count=("
                "SELECT COUNT(*) FROM context_sources WHERE item_id=?"
                ") WHERE id=?",
                (item.id, item.id),
            )
            self._upsert_ready_row(conn, project, item.id, source_hash, covered_through)
        return self._after_commit(project, item, superseded_id, covered_through)

    def rollup_all(self) -> tuple[ProjectRollupReport, ...]:
        """Roll every project that has sources or an existing rollup row."""
        rows = self._store._connection().execute(
            "SELECT project FROM context_project_rollups "
            "UNION "
            "SELECT project FROM context_items "
            "WHERE status='active' AND project != '' "
            "AND content_type IN ('session_summary', 'decision', 'fact',"
            " 'experience') "
            "ORDER BY project"
        ).fetchall()
        return tuple(self.rollup_project(row["project"]) for row in rows)

    def covered_source_ids(self, project: str) -> frozenset[int]:
        """Relational source closure of the project's current ready summary.

        Task 6's retention gate depends on this set: only items whose ids
        appear as ``context_reference`` sources of the ready PROJECT_SUMMARY
        count as covered.
        """
        project = _normalize_project(project)
        row = self._rollup_row(project)
        if (
            row is None
            or row["status"] != _STATUS_READY
            or row["current_context_id"] is None
        ):
            return frozenset()
        ids: set[int] = set()
        for source in self._store.list_item_sources(int(row["current_context_id"])):
            if source["source_kind"] != "context_reference":
                continue
            try:
                ids.add(int(source["source_ref"]))
            except (TypeError, ValueError):
                continue
        return frozenset(ids)

    # ---- source set and current summary ----

    def _collect_sources(
        self, project: str, covered_through: str | None
    ) -> tuple[ContextItem, ...]:
        conn = self._store._connection()
        rows = conn.execute(
            "SELECT id FROM context_items WHERE project=? AND status='active' "
            "AND content_type=? ORDER BY created_at, id",
            (project, ContextContentType.SESSION_SUMMARY.value),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        atomic_sql = (
            "SELECT id FROM context_items WHERE project=? AND status='active' "
            "AND content_type IN (?, ?, ?)"
        )
        params: tuple = (project, *_ATOMIC_SOURCE_TYPES)
        if covered_through is not None:
            atomic_sql += " AND created_at > ?"
            params = (*params, covered_through)
        atomic_sql += " ORDER BY created_at, id"
        ids.extend(int(row["id"]) for row in conn.execute(atomic_sql, params))
        return tuple(self._load_source(item_id) for item_id in ids)

    def _load_source(self, item_id: int) -> ContextItem:
        item = self._store.get_item(item_id)
        if item is None or item.layers is None:  # pragma: no cover
            raise RuntimeError("rollup source could not be reloaded")
        return item

    def _current_summary(self, project: str) -> ContextItem | None:
        """The active PROJECT_SUMMARY holding the rollup identity, if any."""
        return next(
            (
                item
                for item in self._store.get_by_identity(
                    _rollup_identity_key(project),
                    project=project,
                    scope=ContextScope.PROJECT,
                )
                if item.status is ContextStatus.ACTIVE
            ),
            None,
        )

    def _rollup_row(self, project: str):
        return self._store._connection().execute(
            f"SELECT {_ROLLUP_ROW_COLUMNS} FROM context_project_rollups"
            " WHERE project=?",
            (project,),
        ).fetchone()

    # ---- prompt, LLM call, and output gates ----

    def _build_prompt(
        self,
        project: str,
        sources: tuple[ContextItem, ...],
        current: ContextItem | None,
    ) -> str:
        """Assemble the prompt from redacted copies of L1 content only."""
        config = self._config
        limits = {
            "l0": min(config.context_l0_max_chars, _GENERATION_TARGETS["l0"]),
            "l1": min(config.context_l1_max_chars, _GENERATION_TARGETS["l1"]),
            "l2": min(config.context_l2_max_chars, _GENERATION_TARGETS["l2"]),
        }
        lines = [
            "你是项目知识整理助手。以下是同一项目的会话摘要与最新知识条目"
            "（只含细节层文本，不含原始会话）。",
            "请将它们滚动整理成该项目的当前知识摘要。",
            "",
            f"项目: {project}",
            "",
        ]
        if current is not None and current.layers is not None:
            lines.append(f"旧摘要: {_redacted_prompt_copy(current.layers.l1)}")
            lines.append("")
        for index, source in enumerate(sources, start=1):
            lines.append(
                f"条目 {index} 细节: {_redacted_prompt_copy(source.layers.l1)}"
            )
            lines.append("")
        lines.extend(
            [
                '只返回一个 JSON 对象：{"l0": "...", "l1": "...", "l2": "..."}，'
                "不要输出任何其他文本。",
                f"l0 为一句话项目状态要点，不超过 {limits['l0']} 字；",
                f"l1 为当前进展、关键决定与待办，不超过 {limits['l1']} 字；",
                f"l2 为完整细节与来源脉络，不超过 {limits['l2']} 字；",
                "全部使用中文；不得包含密钥、token、密码等任何敏感信息。",
            ]
        )
        return "\n".join(lines)

    def _call_llm(self, prompt: str) -> str | None:
        """Invoke the narrow LLM callable; any failure degrades to None."""
        try:
            response = self._llm(prompt)
        except Exception:
            logger.warning("project rollup llm call failed")
            return None
        return response if isinstance(response, str) else None

    def _gate_output(self, response: str) -> tuple[str | None, dict | None]:
        """Apply the playbook generator's frozen output gates unchanged."""
        try:
            payload = json.loads(response.strip())
        except ValueError:
            return _REASON_INVALID_JSON, None
        if not isinstance(payload, dict):
            return _REASON_INVALID_JSON, None
        layers: dict[str, str] = {}
        for name in _LAYER_NAMES:
            raw = payload.get(name)
            if not isinstance(raw, str):
                return _REASON_INVALID_JSON, None
            content = normalize_content(raw)
            if not content:
                return _REASON_INVALID_JSON, None
            layers[name] = content
        for name in _LAYER_NAMES:
            if contains_sensitive_text(layers[name]):
                return _REASON_SENSITIVE_CONTENT, None
        for name in _LAYER_NAMES:
            if _is_low_information(layers[name]):
                return _REASON_LOW_INFORMATION, None
        limits = {
            "l0": self._config.context_l0_max_chars,
            "l1": self._config.context_l1_max_chars,
            "l2": self._config.context_l2_max_chars,
        }
        for name in _LAYER_NAMES:
            if len(layers[name]) > limits[name]:
                return _REASON_LAYER_TOO_LONG, None
        return None, layers

    # ---- write paths ----

    def _fail(
        self, project: str, source_hash: str, row, reason: str
    ) -> ProjectRollupReport:
        """Record the failed attempt; the old summary and watermark stay."""
        with self._store.transaction():
            self._upsert_failed_row(self._store._connection(), project, source_hash)
        return ProjectRollupReport(
            project=project,
            status=_STATUS_FAILED,
            reason=reason,
            context_id=_row_context_id(row) if row is not None else None,
            covered_through=row["covered_through"] if row is not None else None,
        )

    def _upsert_ready_row(
        self,
        conn,
        project: str,
        context_id: int,
        source_hash: str,
        covered_through: str,
    ) -> None:
        now = _now_iso()
        existing = conn.execute(
            "SELECT revision FROM context_project_rollups WHERE project=?",
            (project,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO context_project_rollups ("
                "project, current_context_id, source_set_hash, covered_through,"
                " generator_version, status, revision, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'ready', 1, ?)",
                (project, context_id, source_hash, covered_through, self.VERSION, now),
            )
            return
        conn.execute(
            "UPDATE context_project_rollups SET current_context_id=?,"
            " source_set_hash=?, covered_through=?, generator_version=?,"
            " status='ready', revision=?, updated_at=? WHERE project=?",
            (
                context_id,
                source_hash,
                covered_through,
                self.VERSION,
                int(existing["revision"]) + 1,
                now,
                project,
            ),
        )

    def _upsert_failed_row(self, conn, project: str, source_hash: str) -> None:
        now = _now_iso()
        existing = conn.execute(
            "SELECT revision FROM context_project_rollups WHERE project=?",
            (project,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO context_project_rollups ("
                "project, current_context_id, source_set_hash, covered_through,"
                " generator_version, status, revision, updated_at"
                ") VALUES (?, NULL, ?, NULL, ?, 'failed', 1, ?)",
                (project, source_hash, self.VERSION, now),
            )
            return
        # the last good pointer and watermark survive; only the attempt moves
        conn.execute(
            "UPDATE context_project_rollups SET source_set_hash=?,"
            " generator_version=?, status='failed', revision=?, updated_at=?"
            " WHERE project=?",
            (source_hash, self.VERSION, int(existing["revision"]) + 1, now, project),
        )

    def _after_commit(
        self,
        project: str,
        item: ContextItem,
        superseded_id: int | None,
        covered_through: str,
    ) -> ProjectRollupReport:
        """Post-commit vector handoff; the committed summary stays authoritative."""
        layers = item.layers
        if layers is None:  # pragma: no cover - the store always reloads layers
            raise RuntimeError("freshly written rollup summary lacks layers")
        if self._vector_sync is None:
            return ProjectRollupReport(
                project=project,
                status=_STATUS_READY,
                context_id=item.id,
                covered_through=covered_through,
            )
        try:
            synced = bool(self._vector_sync(item.id, superseded_id, layers.l0))
        except Exception:
            logger.warning("project rollup vector sync failed: sync raised")
            synced = False
        if synced:
            return ProjectRollupReport(
                project=project,
                status=_STATUS_READY,
                context_id=item.id,
                covered_through=covered_through,
            )
        logger.warning("project rollup vector sync failed: %s", _STATUS_VECTOR_DIRTY)
        with self._store.transaction():
            self._store._connection().execute(
                "UPDATE context_project_rollups SET status='vector_dirty',"
                " revision=revision+1, updated_at=? WHERE project=?",
                (_now_iso(), project),
            )
        return ProjectRollupReport(
            project=project,
            status=_STATUS_VECTOR_DIRTY,
            context_id=item.id,
            covered_through=covered_through,
        )


def _redacted_prompt_copy(text: str) -> str:
    messages, _ = redact_messages([{"role": "context", "content": text}])
    return messages[0]["content"]


def _rollup_identity_key(project: str) -> str:
    return f"project:{project}:knowledge:current"


def _normalize_project(project: str) -> str:
    if not isinstance(project, str):
        raise ContextValidationError("project must be a non-empty string")
    normalized = project.strip()
    if not normalized:
        raise ContextValidationError("project must be a non-empty string")
    return normalized


def _row_context_id(row) -> int | None:
    if row["current_context_id"] is None:
        return None
    return int(row["current_context_id"])


def _source_set_hash(source_ids: tuple[int, ...], version: str) -> str:
    payload = ",".join(sorted(map(str, source_ids))) + version
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_low_information(text: str) -> bool:
    informative = _REDACTION_MARKER_RE.sub("", text)
    informative = _LOW_INFO_STRIP_RE.sub("", informative)
    return not informative or not contains_cjk(informative)
