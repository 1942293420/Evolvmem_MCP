"""Continuity domain service: versioned workstream checkpoints + focus CAS.

One transaction per mutation, borrowing the ContextStore connection exactly
like ProjectStore/ProjectRollupGenerator do. Concurrency is per-row revision
CAS: the workstream row updates conditionally on
``checkpoint_revision``/``state_version`` and the focus pointer on its own
``revision``; any rowcount!=1 rolls the whole mutation back and surfaces a
stable ``ContinuityError`` code, never exception text.

Checkpoints are ordinary ContextItems on the stable identity
``project:{project}:workstream:{workstream_id}:checkpoint`` with content type
WORKSTREAM_CHECKPOINT (excluded from default retrieval); every mutation
supersedes the previous active item. The L2 layer is canonical JSON whose
authoritative fields (workstream id/project/fingerprint/revisions/status/
repo anchor) are written back by the server — client copies that disagree are
rejected wholesale with ``content_rejected``.

Repo anchors are collected server-side from the transient workspace path via
``git`` subprocesses (no shell, timeout=5, stderr discarded); the raw path
never reaches a table, log, or result. Every mutation writes one
``continuity_events`` row in the same transaction; failure events are written
in a fresh transaction after the rollback so the audit survives.
"""

from collections.abc import Mapping
import json
import logging
import re
import secrets
import sqlite3
import subprocess

from evolvmem.config import Config
from evolvmem.context_layers import validate_layers
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.continuity_models import (
    ContinuityAction,
    ContinuityBeginRequest,
    ContinuityBeginResult,
    ContinuityCheckpointRequest,
    ContinuityCheckpointResult,
    ContinuityError,
    ContinuityFindRequest,
    ContinuityFindResult,
    ContinuityImportRequest,
    ContinuityImportResult,
    ContinuityResumeRequest,
    ContinuityResumeResult,
    FindProjectCandidate,
    FindWorkstreamSummary,
    WorkstreamSummary,
    _FOCUS_ACTIONS,
    _STATE_TRANSITIONS,
    _TERMINAL_STATUSES,
    _ALL_ACTIONS,
)
from evolvmem.extraction_policy import contains_sensitive_text
from evolvmem.project_store import ProjectStore, ProjectStoreError
from evolvmem.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityProvider,
)

logger = logging.getLogger(__name__)


# Content hard caps (module constants per design): individual strings and
# arrays are bounded before the layer budgets apply.
MAX_CHECKPOINT_STRING_CHARS = 2000
MAX_CHECKPOINT_ARRAY_ITEMS = 50


def _resolve_bound_project(
    store: ContextStore, fingerprint: str, hint: str
) -> str | None:
    """Resolve one active project using continuity's authoritative rules."""
    conn = store._connection()
    if hint.strip():
        normalized = hint.strip().casefold()
        aliases = conn.execute(
            "SELECT alias, project FROM context_project_aliases"
        ).fetchall()
        alias_map = {
            row["alias"].casefold(): row["project"] for row in aliases
        }
        candidate = alias_map.get(normalized, normalized)
        row = conn.execute(
            "SELECT project FROM context_project_registry "
            "WHERE lower(project)=lower(?) AND status='active'",
            (candidate,),
        ).fetchone()
        if row is None:
            return None
        project = row["project"]
        bound = conn.execute(
            "SELECT 1 FROM context_project_workspace_bindings "
            "WHERE workspace_fingerprint=? AND project=? AND state='active'",
            (fingerprint, project),
        ).fetchone()
        return project if bound is not None else None
    rows = conn.execute(
        "SELECT project, is_default FROM context_project_workspace_bindings "
        "WHERE workspace_fingerprint=? AND state='active'",
        (fingerprint,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        defaults = [row for row in rows if row["is_default"]]
        if len(defaults) != 1:
            return None
        rows = defaults
    project = rows[0]["project"]
    active = conn.execute(
        "SELECT 1 FROM context_project_registry "
        "WHERE project=? AND status='active'",
        (project,),
    ).fetchone()
    return project if active is not None else None


_MAX_CANDIDATES = 50
_FIND_SCAN_LIMIT = 200
_FIND_WORKSTREAMS_PER_PROJECT = 10
_GIT_TIMEOUT_SECONDS = 5

# begin/import 复用判定时只比较这六个内容键；parent/source 由写入路径自管
_CONTENT_COMPARE_KEYS = (
    "objective",
    "accepted_decisions",
    "completed_steps",
    "current_step",
    "next_action",
    "blockers",
)


def _content_unchanged(previous: Mapping, merged: Mapping) -> bool:
    """Content-key equality: a repeated begin/import with no new content
    must not burn a checkpoint revision."""
    for key in _CONTENT_COMPARE_KEYS:
        old = previous.get(key)
        new = merged.get(key)
        if isinstance(new, list):
            old_list = list(old) if isinstance(old, list) else []
            if old_list != new:
                return False
        else:
            old_text = old if isinstance(old, str) else ""
            if old_text != (new or ""):
                return False
    return True


def _normalize_objective(text: object) -> str:
    """任务身份的规范化目标串：折叠空白 + casefold。"""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).casefold()

# Server-authoritative L2 keys: a client-submitted value that disagrees with
# the server's rejects the whole request with ``content_rejected``.
_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "workstream_id",
        "project",
        "workspace_fingerprint",
        "checkpoint_revision",
        "state_version",
        "status",
        "repo",
    }
)
_L2_STRING_FIELDS = ("objective", "current_step", "next_action")
_L2_ARRAY_FIELDS = ("accepted_decisions", "completed_steps", "blockers")

_PATCH_MARKERS_RE = re.compile(
    r"diff --git \S|\n--- (?:a/|/dev)|\n\+\+\+ (?:b/|/dev)|@@ -\d"
)


def _has_forbidden_content(text: str) -> bool:
    """Absolute paths, credential-shaped text, and patch bodies are banned."""
    if any(
        token.startswith("/") or token.startswith("~/")
        for token in text.split()
    ):
        return True
    if contains_sensitive_text(text):
        return True
    return _PATCH_MARKERS_RE.search(text) is not None


def _check_client_content(client: Mapping) -> None:
    """Bound and screen the non-authoritative checkpoint content fields."""
    for field in _L2_STRING_FIELDS:
        value = client.get(field, "")
        if not isinstance(value, str) or len(value) > MAX_CHECKPOINT_STRING_CHARS:
            raise ContinuityError("content_rejected")
        if _has_forbidden_content(value):
            raise ContinuityError("content_rejected")
    for field in _L2_ARRAY_FIELDS:
        value = client.get(field, ())
        if isinstance(value, (str, bytes)):
            raise ContinuityError("content_rejected")
        try:
            items = tuple(value)
        except TypeError:
            raise ContinuityError("content_rejected") from None
        if len(items) > MAX_CHECKPOINT_ARRAY_ITEMS:
            raise ContinuityError("content_rejected")
        for item in items:
            if not isinstance(item, str) or len(item) > MAX_CHECKPOINT_STRING_CHARS:
                raise ContinuityError("content_rejected")
            if _has_forbidden_content(item):
                raise ContinuityError("content_rejected")
    parent = client.get("parent_workstream_id")
    if parent is not None and not isinstance(parent, str):
        raise ContinuityError("content_rejected")
    ids = client.get("source_context_ids", ())
    if isinstance(ids, (str, bytes)):
        raise ContinuityError("content_rejected")
    try:
        id_items = tuple(ids)
    except TypeError:
        raise ContinuityError("content_rejected") from None
    if len(id_items) > MAX_CHECKPOINT_ARRAY_ITEMS or any(
        type(item) is not int or item <= 0 for item in id_items
    ):
        raise ContinuityError("content_rejected")


def _build_l2_payload(*, client: Mapping, authority: Mapping) -> str:
    """Merge client content with server authority into canonical L2 JSON.

    Authoritative keys repeated by the client must match the server value;
    any disagreement rejects the whole request. The merged document is
    canonical (sorted keys, tight separators) so identical states serialize
    byte-identically.
    """
    for key in _AUTHORITY_KEYS:
        if key in client and client[key] != authority.get(key):
            raise ContinuityError("content_rejected")
    _check_client_content(client)
    merged = {**client, **authority}
    return json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _truncate(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    if maximum == 1:
        return "…"
    return value[: maximum - 1].rstrip() + "…"


class ContinuityService:
    """Workstream checkpoint / focus domain operations on a borrowed store."""

    VERSION = "continuity.v1"

    def __init__(self, config: Config, store: ContextStore, workspace_identity) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        if not isinstance(workspace_identity, WorkspaceIdentityProvider):
            raise ContextValidationError(
                "workspace_identity must be a WorkspaceIdentityProvider instance"
            )
        self._config = config
        self._store = store
        self._workspace_identity = workspace_identity

    # ---- public API ----

    def checkpoint(
        self, request: ContinuityCheckpointRequest
    ) -> ContinuityCheckpointResult:
        """Apply one action from the closed whitelist under revision CAS."""
        if not isinstance(request, ContinuityCheckpointRequest):
            raise TypeError("request must be a ContinuityCheckpointRequest")
        if request.action not in _ALL_ACTIONS:
            raise ContinuityError("invalid_action")
        identity = self._resolve_workspace(request.workspace_path)
        project = self._resolve_project(identity.fingerprint, request.project_hint)
        if project is None:
            raise ContinuityError("project_unresolved")
        try:
            if request.action == ContinuityAction.CREATE.value:
                anchor = _collect_repo_anchor(request.workspace_path)
                return self._create(request, project, identity.fingerprint, anchor)
            if request.action in _FOCUS_ACTIONS:
                return self._mutate_focus(request, project, identity.fingerprint)
            anchor = _collect_repo_anchor(request.workspace_path)
            return self._mutate_content(request, project, identity.fingerprint, anchor)
        except ContinuityError as exc:
            self._record_failure_event(request, exc.code)
            raise

    def resume(self, request: ContinuityResumeRequest) -> ContinuityResumeResult:
        """Exact-pointer resume; never touches FTS/HNSW, never raises on data."""
        if not isinstance(request, ContinuityResumeRequest):
            raise TypeError("request must be a ContinuityResumeRequest")
        if not self._schema_ready():
            return ContinuityResumeResult(code="continuity_not_ready")
        try:
            identity = self._workspace_identity.resolve(request.workspace_path)
        except WorkspaceIdentityError as exc:
            logger.debug("continuity resume without workspace identity: %s", exc.code)
            return ContinuityResumeResult(code="continuity_not_ready")
        fingerprint = identity.fingerprint
        project = self._resolve_project(fingerprint, request.project_hint)
        if project is None:
            return ContinuityResumeResult(code="no_continuation")
        focus = self._focus_row(project, fingerprint)
        focus_revision = int(focus["revision"]) if focus is not None else 0
        focus_target = focus["workstream_id"] if focus is not None else None
        if not focus_target:
            candidates = self._unfinished_summaries(project, fingerprint)
            if not candidates:
                return ContinuityResumeResult(
                    code="no_continuation", focus_revision=focus_revision
                )
            code = (
                "needs_focus_confirmation" if len(candidates) == 1 else "ambiguous"
            )
            return ContinuityResumeResult(
                code=code,
                focus_revision=focus_revision,
                candidates=candidates[:_MAX_CANDIDATES],
            )
        row = self._workstream_row(focus_target)
        if (
            row is None
            or row["project"] != project
            or row["status"] in _TERMINAL_STATUSES
        ):
            return ContinuityResumeResult(
                code="dangling_focus",
                workstream_id=str(focus_target),
                focus_revision=focus_revision,
            )
        staleness = self._staleness(row, request.workspace_path, fingerprint)
        checkpoint = None
        if staleness != "wrong_workspace":
            checkpoint = self._checkpoint_payload(row)
        return ContinuityResumeResult(
            code="ok",
            workstream_id=row["id"],
            context_id=int(row["current_context_id"]),
            checkpoint_revision=int(row["checkpoint_revision"]),
            state_version=int(row["state_version"]),
            focus_revision=focus_revision,
            status=row["status"],
            staleness=staleness,
            checkpoint=checkpoint,
        )

    def list_open(
        self, request: ContinuityResumeRequest
    ) -> tuple[WorkstreamSummary, ...]:
        """Unfinished workstream summaries for this project/workspace (L0 only)."""
        if not isinstance(request, ContinuityResumeRequest):
            raise TypeError("request must be a ContinuityResumeRequest")
        if not self._schema_ready():
            return ()
        try:
            identity = self._workspace_identity.resolve(request.workspace_path)
        except WorkspaceIdentityError:
            return ()
        project = self._resolve_project(identity.fingerprint, request.project_hint)
        if project is None:
            return ()
        return self._unfinished_summaries(project, identity.fingerprint)

    # ---- begin: explicit-declaration register + bind + workstream ----

    def begin(self, request: ContinuityBeginRequest) -> ContinuityBeginResult:
        """Idempotently register/bind the declared project and create its
        focused workstream, or read back the existing one.

        The explicit caller declaration is the only project signal: nothing
        is inferred from a generic home/cwd. Content policy is validated
        before any write, and registration, alias, binding and workstream
        creation share one atomic transaction — a rejected begin leaves no
        half-registered state. Task identity is the (normalized) objective,
        never the focus pointer: a replayed begin reads back the matching
        workstream without overwriting newer checkpoints, different
        objectives create distinct workstreams, and a replay over a terminal
        workstream neither resurrects nor duplicates it. Progress updates
        after begin go through the explicit checkpoint/CAS protocol.
        """
        if not isinstance(request, ContinuityBeginRequest):
            raise TypeError("request must be a ContinuityBeginRequest")
        # 内容策略先行：任何登记/绑定之前完整校验
        _check_client_content(
            {
                "objective": request.objective,
                "accepted_decisions": list(request.accepted_decisions),
                "completed_steps": list(request.completed_steps),
                "current_step": request.current_step,
                "next_action": request.next_action,
                "blockers": list(request.blockers),
            }
        )
        identity = self._resolve_workspace(request.workspace_path)
        fingerprint = identity.fingerprint
        # 登记/别名/绑定/建任务同一原子边界（内层 checkpoint 事务并入外层）
        with self._store.transaction():
            project_store = ProjectStore(
                self._store._connection(),
                self._store._require_transaction,
                generic_names=(),
            )
            registered = self._ensure_project(project_store, request.project)
            alias_added = self._ensure_alias(
                project_store, request.project, request.alias
            )
            self._ensure_binding(project_store, fingerprint, request.project)

            focus_row = self._focus_row(request.project, fingerprint)
            focus_revision = (
                int(focus_row["revision"]) if focus_row is not None else 0
            )
            focused_id = (
                focus_row["workstream_id"] if focus_row is not None else None
            )
            target = terminal = None
            if request.objective:
                wanted = _normalize_objective(request.objective)
                for row in self._unfinished_rows(request.project, fingerprint):
                    previous = self._load_previous_content(
                        int(row["current_context_id"])
                    )
                    if _normalize_objective(previous.get("objective", "")) == wanted:
                        target = row
                        break
                if target is None:
                    for row in self._terminal_rows(request.project, fingerprint):
                        previous = self._load_previous_content(
                            int(row["current_context_id"])
                        )
                        if (
                            _normalize_objective(previous.get("objective", ""))
                            == wanted
                        ):
                            terminal = row
                            break
            elif focused_id:
                row = self._workstream_row(focused_id)
                if (
                    row is not None
                    and row["project"] == request.project
                    and row["status"] not in _TERMINAL_STATUSES
                ):
                    target = row

            created = False
            if terminal is not None:
                # 相同目标的终态任务：读回不复活、不新增、不动 focus
                result = ContinuityCheckpointResult(
                    workstream_id=terminal["id"],
                    checkpoint_revision=int(terminal["checkpoint_revision"]),
                    state_version=int(terminal["state_version"]),
                    focus_revision=focus_revision,
                    status=terminal["status"],
                    context_id=int(terminal["current_context_id"]),
                )
            elif target is None:
                result = self.checkpoint(
                    ContinuityCheckpointRequest(
                        action=ContinuityAction.CREATE.value,
                        workspace_path=request.workspace_path,
                        project_hint=request.project,
                        objective=request.objective,
                        accepted_decisions=request.accepted_decisions,
                        completed_steps=request.completed_steps,
                        current_step=request.current_step,
                        next_action=request.next_action,
                        blockers=request.blockers,
                        make_focus=request.make_focus,
                        expected_focus_revision=(
                            focus_revision if request.make_focus else None
                        ),
                    )
                )
                created = True
            elif request.make_focus and focused_id != target["id"]:
                # 只挂焦点（focus-only CAS），绝不改写已有任务内容；
                # 焦点已在其他未完成任务上时绝不抢焦点，只读回
                focused_row = (
                    self._workstream_row(focused_id) if focused_id else None
                )
                focus_available = (
                    focused_row is None
                    or focused_row["status"] in _TERMINAL_STATUSES
                )
                if focus_available:
                    result = self.checkpoint(
                        ContinuityCheckpointRequest(
                            action=ContinuityAction.SWITCH_FOCUS.value,
                            workspace_path=request.workspace_path,
                            project_hint=request.project,
                            workstream_id=target["id"],
                            expected_focus_revision=focus_revision,
                        )
                    )
                else:
                    result = ContinuityCheckpointResult(
                        workstream_id=target["id"],
                        checkpoint_revision=int(target["checkpoint_revision"]),
                        state_version=int(target["state_version"]),
                        focus_revision=focus_revision,
                        status=target["status"],
                        context_id=int(target["current_context_id"]),
                    )
            else:
                # 幂等命中：原样读回，不消耗任何 revision
                result = ContinuityCheckpointResult(
                    workstream_id=target["id"],
                    checkpoint_revision=int(target["checkpoint_revision"]),
                    state_version=int(target["state_version"]),
                    focus_revision=focus_revision,
                    status=target["status"],
                    context_id=int(target["current_context_id"]),
                )
        return ContinuityBeginResult(
            project=request.project,
            workstream_id=result.workstream_id,
            checkpoint_revision=result.checkpoint_revision,
            state_version=result.state_version,
            focus_revision=result.focus_revision,
            status=result.status,
            context_id=result.context_id,
            created=created,
            updated=False,
            registered=registered,
            alias_added=alias_added,
            bound=True,
        )

    def _ensure_project(self, project_store: ProjectStore, project: str) -> bool:
        """Register the declared project if missing.

        canonical 与 alias 共享一个 casefold 规范化命名空间（与 resolver
        一致）：大小写变体项目名、撞别人别名的项目名都是硬冲突；
        archived 项目保持人工处理。"""
        row = self._store._connection().execute(
            "SELECT project, status FROM context_project_registry "
            "WHERE lower(project)=lower(?)",
            (project,),
        ).fetchone()
        if row is not None:
            if row["project"] != project:
                raise ContinuityError("alias_conflict")
            if row["status"] != "active":
                raise ContinuityError("project_archived")
            return False
        conflict = self._store._connection().execute(
            "SELECT 1 FROM context_project_aliases WHERE lower(alias)=lower(?)",
            (project,),
        ).fetchone()
        if conflict is not None:
            raise ContinuityError("alias_conflict")
        project_store.register_project(project)
        return True

    def _ensure_alias(
        self, project_store: ProjectStore, project: str, alias: str
    ) -> bool:
        """Insert the optional alias; any namespace collision is a hard
        conflict, never a silent re-point."""
        if not alias or alias.casefold() == project.casefold():
            return False
        rows = self._store._connection().execute(
            "SELECT alias, project FROM context_project_aliases"
        ).fetchall()
        existing = {
            row["alias"].casefold(): row["project"] for row in rows
        }.get(alias.casefold())
        if existing is not None:
            if existing != project:
                raise ContinuityError("alias_conflict")
            return False
        canonical = self._store._connection().execute(
            "SELECT 1 FROM context_project_registry WHERE lower(project)=lower(?)",
            (alias,),
        ).fetchone()
        if canonical is not None:
            raise ContinuityError("alias_conflict")
        project_store.add_alias(alias, project)
        return True

    def _ensure_binding(
        self, project_store: ProjectStore, fingerprint: str, project: str
    ) -> None:
        """Bind (idempotently re-activate) the workspace as this project's
        default; a foreign default keeps its slot instead of erroring."""
        try:
            project_store.bind_workspace(
                fingerprint, project, method="begin", make_default=True
            )
        except ProjectStoreError as exc:
            if exc.code != "default_binding_conflict":
                raise
            project_store.bind_workspace(
                fingerprint, project, method="begin", make_default=False
            )

    # ---- find: layered cross-project discovery (read-only) ----

    def find(self, request: ContinuityFindRequest) -> ContinuityFindResult:
        """Discover unfinished workstreams by project name, alias, or task
        keyword. Never switches any focus; workspace verification is reported
        honestly when the caller's path can be fingerprinted."""
        if not isinstance(request, ContinuityFindRequest):
            raise TypeError("request must be a ContinuityFindRequest")
        if not self._schema_ready():
            return ContinuityFindResult(code="project_not_registered")
        query = request.query.strip().casefold()
        if not query:
            return ContinuityFindResult(code="project_not_registered")
        fingerprint = None
        if request.workspace_path.strip():
            try:
                fingerprint = self._workspace_identity.resolve(
                    request.workspace_path
                ).fingerprint
            except WorkspaceIdentityError:
                fingerprint = None  # 发现可用，但如实标注未核验工作区
        conn = self._store._connection()
        projects = tuple(
            row["project"]
            for row in conn.execute(
                "SELECT project FROM context_project_registry "
                "WHERE status='active' ORDER BY project"
            )
        )
        active = set(projects)
        matched: dict[str, set[str]] = {}
        exact: set[str] = set()
        task_rows: dict[str, list] = {}
        for project in projects:
            if project.casefold() == query:
                matched.setdefault(project, set()).add("name")
                exact.add(project)
        for row in conn.execute(
            "SELECT alias, project FROM context_project_aliases"
        ):
            if row["alias"].casefold() == query and row["project"] in active:
                matched.setdefault(row["project"], set()).add("alias")
                exact.add(row["project"])
        for project in projects:
            if project not in matched and query in project.casefold():
                matched.setdefault(project, set()).add("name")
        for row in self._unfinished_rows_any_workspace():
            if row["project"] in active and query in (row["l0"] or "").casefold():
                matched.setdefault(row["project"], set()).add("task")
                task_rows.setdefault(row["project"], []).append(row)
        if not matched:
            return ContinuityFindResult(code="project_not_registered")

        def via_priority(project: str) -> tuple[int, str]:
            if project in exact:
                return (0, project)
            if "name" in matched[project]:
                return (1, project)
            return (2, project)

        # 先在全部匹配项目上判定真实歧义，再按 limit 截断输出；
        # 任务关键词命中只列真正匹配的任务，项目/别名命中列该项目全部任务
        candidates: list[FindProjectCandidate] = []
        unique_row = None
        total = 0
        for project in sorted(matched, key=via_priority):
            if matched[project] & {"name", "alias"}:
                rows = list(self._unfinished_rows_any_workspace(project=project))
            else:
                rows = task_rows.get(project, [])
            summaries = []
            for row in rows[:_FIND_WORKSTREAMS_PER_PROJECT]:
                total += 1
                unique_row = row
                workspace_match = (
                    fingerprint is not None
                    and row["workspace_fingerprint"] == fingerprint
                )
                staleness = ""
                if workspace_match and request.workspace_path.strip():
                    staleness = self._staleness(
                        row, request.workspace_path, fingerprint
                    )
                summaries.append(
                    FindWorkstreamSummary(
                        workstream_id=row["id"],
                        project=row["project"],
                        status=row["status"],
                        checkpoint_revision=int(row["checkpoint_revision"]),
                        state_version=int(row["state_version"]),
                        l0=row["l0"] or "",
                        updated_at=row["updated_at"],
                        workspace_match=workspace_match,
                        staleness=staleness,
                    )
                )
            candidates.append(
                FindProjectCandidate(
                    project=project,
                    matched_via=tuple(sorted(matched[project])),
                    focus_state=self._focus_state(project, fingerprint),
                    workstreams=tuple(summaries),
                )
            )
        if total == 0:
            code = "no_open_workstream"
        elif total == 1:
            code = "ok"
        else:
            code = "ambiguous"
        candidates = candidates[: request.limit]
        if code == "no_open_workstream":
            return ContinuityFindResult(code=code, candidates=tuple(candidates))
        if code == "ok":
            return ContinuityFindResult(
                code=code,
                candidates=tuple(candidates),
                checkpoint=self._checkpoint_payload(unique_row),
            )
        return ContinuityFindResult(code=code, candidates=tuple(candidates))

    def _focus_state(self, project: str, fingerprint: str | None) -> str:
        if fingerprint is None:
            return ""
        row = self._focus_row(project, fingerprint)
        if row is None or not row["workstream_id"]:
            return "none"
        target = self._workstream_row(row["workstream_id"])
        if (
            target is None
            or target["project"] != project
            or target["status"] in _TERMINAL_STATUSES
        ):
            return "dangling"
        return "ok"

    def _unfinished_rows_any_workspace(self, *, project: str | None = None):
        """Unfinished workstreams joined to their L0, across every bound
        workspace (discovery must work from a generic directory)."""
        if project is not None:
            return self._store._connection().execute(
                "SELECT w.*, l.content AS l0 FROM continuity_workstreams w "
                "JOIN context_layers l ON l.item_id=w.current_context_id "
                "AND l.layer='l0' "
                "WHERE w.project=? AND w.status NOT IN ('completed','cancelled') "
                "ORDER BY w.updated_at DESC, w.id LIMIT ?",
                (project, _FIND_SCAN_LIMIT),
            ).fetchall()
        return self._store._connection().execute(
            "SELECT w.*, l.content AS l0 FROM continuity_workstreams w "
            "JOIN context_layers l ON l.item_id=w.current_context_id "
            "AND l.layer='l0' "
            "WHERE w.status NOT IN ('completed','cancelled') "
            "ORDER BY w.updated_at DESC, w.id LIMIT ?",
            (_FIND_SCAN_LIMIT,),
        ).fetchall()

    # ---- import: conservative backfill of interrupted external sessions ----

    def import_interrupted(
        self, request: ContinuityImportRequest
    ) -> ContinuityImportResult:
        """Create or advance the deterministic workstream of one interrupted
        external session. Terminal workstreams are never resurrected, a
        checkpoint revision beyond ``applied_revision`` proves a human edit
        that is never overwritten, and the focus pointer is never touched."""
        if not isinstance(request, ContinuityImportRequest):
            raise TypeError("request must be a ContinuityImportRequest")
        identity = self._resolve_workspace(request.workspace_path)
        project = self._resolve_project(identity.fingerprint, request.project_hint)
        if project is None:
            raise ContinuityError("project_unresolved")
        with self._store.transaction():
            row = self._workstream_row(request.workstream_id)
            if row is not None and (
                row["project"] != project
                or row["workspace_fingerprint"] != identity.fingerprint
            ):
                raise ContinuityError("workstream_not_found")
            if row is None:
                anchor = _collect_repo_anchor(request.workspace_path)
                created = self._create(
                    ContinuityCheckpointRequest(
                        action=ContinuityAction.CREATE.value,
                        workspace_path=request.workspace_path,
                        project_hint=request.project_hint,
                        objective=request.objective,
                        completed_steps=request.completed_steps,
                        current_step=request.current_step,
                        next_action=request.next_action,
                        blockers=request.blockers,
                    ),
                    project,
                    identity.fingerprint,
                    anchor,
                    workstream_id=request.workstream_id,
                )
                return ContinuityImportResult(
                    code="created",
                    workstream_id=created.workstream_id,
                    checkpoint_revision=created.checkpoint_revision,
                    state_version=created.state_version,
                    status=created.status,
                    context_id=created.context_id,
                )
            base = {
                "workstream_id": row["id"],
                "checkpoint_revision": int(row["checkpoint_revision"]),
                "state_version": int(row["state_version"]),
                "status": row["status"],
                "context_id": int(row["current_context_id"]),
            }
            if row["status"] in _TERMINAL_STATUSES:
                return ContinuityImportResult(code="terminal_kept", **base)
            if int(row["checkpoint_revision"]) > request.applied_revision:
                return ContinuityImportResult(
                    code="human_checkpoint_kept", **base
                )
            previous = self._load_previous_content(int(row["current_context_id"]))
            # 替换语义的有效负载：列表字段是全量重算（空即清空），标量字段
            # 为空时沿用上版，避免wiping 人工可读目标
            def pick_text(new: str, old: object) -> str:
                return new if new else (old if isinstance(old, str) else "")

            effective = ContinuityCheckpointRequest(
                action=ContinuityAction.UPDATE.value,
                workspace_path=request.workspace_path,
                project_hint=request.project_hint,
                workstream_id=request.workstream_id,
                objective=pick_text(request.objective, previous.get("objective")),
                completed_steps=request.completed_steps,
                current_step=pick_text(
                    request.current_step, previous.get("current_step")
                ),
                next_action=pick_text(
                    request.next_action, previous.get("next_action")
                ),
                blockers=request.blockers,
                expected_checkpoint_revision=int(row["checkpoint_revision"]),
                expected_state_version=int(row["state_version"]),
            )
            merged = self._request_content(effective)
            if _content_unchanged(previous, merged):
                return ContinuityImportResult(code="unchanged", **base)
            anchor = _collect_repo_anchor(request.workspace_path)
            result = self._mutate_content(
                effective, project, identity.fingerprint, anchor, merge=False
            )
            return ContinuityImportResult(
                code="updated",
                workstream_id=result.workstream_id,
                checkpoint_revision=result.checkpoint_revision,
                state_version=result.state_version,
                status=result.status,
                context_id=result.context_id,
            )

    # ---- workspace / project resolution ----

    def _resolve_workspace(self, workspace_path: str):
        try:
            return self._workspace_identity.resolve(workspace_path)
        except WorkspaceIdentityError as exc:
            # fail-closed: 身份不可用只暴露稳定码，不区分缺失/权限/变更
            raise ContinuityError("workspace_key_missing") from None

    def _resolve_project(self, fingerprint: str, hint: str) -> str | None:
        """Deterministic (binding-gated) project for one workspace fingerprint.

        A hint is alias-normalized and must name a registered active project
        with an active binding on this fingerprint. Without a hint, exactly
        one active binding (or a single active default among several) decides;
        anything else is unresolved — never a guess.
        """
        return _resolve_bound_project(self._store, fingerprint, hint)

    def _schema_ready(self) -> bool:
        row = self._store._connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='continuity_workstreams'"
        ).fetchone()
        return row is not None

    # ---- create ----

    def _create(
        self,
        request: ContinuityCheckpointRequest,
        project: str,
        fingerprint: str,
        anchor: dict,
        *,
        workstream_id: str | None = None,
    ) -> ContinuityCheckpointResult:
        if request.expected_checkpoint_revision != 0 or request.expected_state_version != 0:
            raise ContinuityError("revision_conflict")
        if request.make_focus and request.expected_focus_revision is None:
            raise ContinuityError("invalid_action")
        with self._store.transaction():
            if workstream_id is None:
                workstream_id = "ws_" + secrets.token_hex(8)
            parent_id = self._validate_parent(
                request.parent_workstream_id,
                workstream_id=workstream_id,
                project=project,
                fingerprint=fingerprint,
            )
            self._validate_sources(request.source_context_ids, project)
            item = self._write_checkpoint_item(
                workstream_id=workstream_id,
                project=project,
                fingerprint=fingerprint,
                parent_id=parent_id,
                status="open",
                checkpoint_revision=1,
                state_version=1,
                content=self._request_content(request),
                anchor=anchor,
                source_context_ids=request.source_context_ids,
            )
            now = _now_iso()
            try:
                self._store._connection().execute(
                    "INSERT INTO continuity_workstreams("
                    "id, project, workspace_fingerprint, parent_id,"
                    " current_context_id, checkpoint_revision, state_version,"
                    " status, repo_kind, repo_branch, repo_root_commit,"
                    " repo_head_commit, created_at, updated_at, completed_at"
                    ") VALUES (?, ?, ?, ?, ?, 1, 1, 'open', ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        workstream_id,
                        project,
                        fingerprint,
                        parent_id,
                        item.id,
                        anchor["kind"],
                        anchor["branch"],
                        anchor["root_commit"],
                        anchor["head_commit"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # 唯一约束/外键冲突：并发 create 只有一个胜者
                raise ContinuityError("revision_conflict") from None
            focus_revision = self._current_focus_revision(project, fingerprint)
            if request.make_focus:
                focus_revision = self._set_focus(
                    project,
                    fingerprint,
                    workstream_id,
                    expected_revision=request.expected_focus_revision,
                    conflict_code="revision_conflict",
                )
            self._record_event(
                workstream_id,
                "create",
                before_revision=None,
                after_revision=1,
                before_state_version=None,
                after_state_version=1,
            )
        return ContinuityCheckpointResult(
            workstream_id=workstream_id,
            checkpoint_revision=1,
            state_version=1,
            focus_revision=focus_revision,
            status="open",
            context_id=item.id,
        )

    # ---- content mutations ----

    def _mutate_content(
        self,
        request: ContinuityCheckpointRequest,
        project: str,
        fingerprint: str,
        anchor: dict,
        *,
        merge: bool = True,
    ) -> ContinuityCheckpointResult:
        if request.make_focus and request.expected_focus_revision is None:
            raise ContinuityError("invalid_action")
        with self._store.transaction():
            row = self._workstream_row(request.workstream_id)
            if (
                row is None
                or row["project"] != project
                or row["workspace_fingerprint"] != fingerprint
            ):
                raise ContinuityError("workstream_not_found")
            transitions = _STATE_TRANSITIONS.get(row["status"], {})
            next_status = transitions.get(request.action)
            if next_status is None:
                raise ContinuityError("invalid_transition")
            if (
                int(row["checkpoint_revision"]) != request.expected_checkpoint_revision
                or int(row["state_version"]) != request.expected_state_version
            ):
                raise ContinuityError("revision_conflict")

            previous = self._load_previous_content(int(row["current_context_id"]))
            if merge:
                content = self._merged_content(request, previous)
            else:
                # 补录的替换语义：请求字段是全量重算状态，空列表即已清空
                content = self._request_content(request)
            source_ids = content["source_context_ids"]
            parent_id = row["parent_id"]
            if request.parent_workstream_id:
                parent_id = self._validate_parent(
                    request.parent_workstream_id,
                    workstream_id=request.workstream_id,
                    project=project,
                    fingerprint=fingerprint,
                )
            self._validate_sources(source_ids, project)
            new_revision = int(row["checkpoint_revision"]) + 1
            new_state_version = int(row["state_version"]) + 1
            item = self._write_checkpoint_item(
                workstream_id=request.workstream_id,
                project=project,
                fingerprint=fingerprint,
                parent_id=parent_id,
                status=next_status,
                checkpoint_revision=new_revision,
                state_version=new_state_version,
                content=content,
                anchor=anchor,
                source_context_ids=source_ids,
            )
            now = _now_iso()
            completed_at = (
                now if next_status in _TERMINAL_STATUSES else row["completed_at"]
            )
            cursor = self._store._connection().execute(
                "UPDATE continuity_workstreams SET current_context_id=?,"
                " checkpoint_revision=?, state_version=?, status=?, parent_id=?,"
                " repo_kind=?, repo_branch=?, repo_root_commit=?,"
                " repo_head_commit=?, updated_at=?, completed_at=? "
                "WHERE id=? AND checkpoint_revision=? AND state_version=?",
                (
                    item.id,
                    new_revision,
                    new_state_version,
                    next_status,
                    parent_id,
                    anchor["kind"],
                    anchor["branch"],
                    anchor["root_commit"],
                    anchor["head_commit"],
                    now,
                    completed_at,
                    request.workstream_id,
                    request.expected_checkpoint_revision,
                    request.expected_state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ContinuityError("revision_conflict")
            focus_revision = self._current_focus_revision(project, fingerprint)
            if request.make_focus:
                focus_revision = self._set_focus(
                    project,
                    fingerprint,
                    request.workstream_id,
                    expected_revision=request.expected_focus_revision,
                    conflict_code="focus_conflict",
                )
            self._record_event(
                request.workstream_id,
                request.action,
                before_revision=int(row["checkpoint_revision"]),
                after_revision=new_revision,
                before_state_version=int(row["state_version"]),
                after_state_version=new_state_version,
            )
        return ContinuityCheckpointResult(
            workstream_id=request.workstream_id,
            checkpoint_revision=new_revision,
            state_version=new_state_version,
            focus_revision=focus_revision,
            status=next_status,
            context_id=item.id,
        )

    # ---- focus-only mutations ----

    def _mutate_focus(
        self,
        request: ContinuityCheckpointRequest,
        project: str,
        fingerprint: str,
    ) -> ContinuityCheckpointResult:
        if request.expected_focus_revision is None:
            raise ContinuityError("invalid_action")
        with self._store.transaction():
            if request.action == ContinuityAction.SWITCH_FOCUS.value:
                return self._switch_focus(request, project, fingerprint)
            return self._clear_focus(request, project, fingerprint)

    def _switch_focus(
        self,
        request: ContinuityCheckpointRequest,
        project: str,
        fingerprint: str,
    ) -> ContinuityCheckpointResult:
        row = self._workstream_row(request.workstream_id)
        if (
            row is None
            or row["project"] != project
            or row["workspace_fingerprint"] != fingerprint
        ):
            raise ContinuityError("workstream_not_found")
        if row["status"] in _TERMINAL_STATUSES:
            raise ContinuityError("focus_conflict")
        focus_revision = self._set_focus(
            project,
            fingerprint,
            request.workstream_id,
            expected_revision=request.expected_focus_revision,
            conflict_code="focus_conflict",
        )
        self._record_event(
            request.workstream_id,
            "switch_focus",
            before_revision=int(row["checkpoint_revision"]),
            after_revision=int(row["checkpoint_revision"]),
            before_state_version=int(row["state_version"]),
            after_state_version=int(row["state_version"]),
        )
        return ContinuityCheckpointResult(
            workstream_id=request.workstream_id,
            checkpoint_revision=int(row["checkpoint_revision"]),
            state_version=int(row["state_version"]),
            focus_revision=focus_revision,
            status=row["status"],
            context_id=int(row["current_context_id"]),
        )

    def _clear_focus(
        self,
        request: ContinuityCheckpointRequest,
        project: str,
        fingerprint: str,
    ) -> ContinuityCheckpointResult:
        previous = self._focus_row(project, fingerprint)
        conn = self._store._connection()
        cursor = conn.execute(
            "UPDATE continuity_focus SET workstream_id=NULL,"
            " revision=revision+1, updated_at=? "
            "WHERE project=? AND workspace_fingerprint=? AND revision=?",
            (
                _now_iso(),
                project,
                fingerprint,
                request.expected_focus_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ContinuityError("focus_conflict")
        new_revision = int(previous["revision"]) + 1 if previous is not None else 0
        self._record_event(
            previous["workstream_id"] if previous is not None else None,
            "clear_focus",
            before_revision=None,
            after_revision=None,
            before_state_version=None,
            after_state_version=None,
        )
        return ContinuityCheckpointResult(focus_revision=new_revision)

    def _set_focus(
        self,
        project: str,
        fingerprint: str,
        workstream_id: str,
        *,
        expected_revision: int | None,
        conflict_code: str,
    ) -> int:
        """CAS the pre-built focus row onto a workstream; the row is never
        inserted here nor deleted anywhere — bindings pre-build it."""
        cursor = self._store._connection().execute(
            "UPDATE continuity_focus SET workstream_id=?, revision=revision+1,"
            " updated_at=? "
            "WHERE project=? AND workspace_fingerprint=? AND revision=?",
            (workstream_id, _now_iso(), project, fingerprint, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ContinuityError(conflict_code)
        row = self._focus_row(project, fingerprint)
        return int(row["revision"])

    # ---- validation helpers (all inside the caller's transaction) ----

    def _validate_parent(
        self,
        parent_id: str,
        *,
        workstream_id: str,
        project: str,
        fingerprint: str,
    ) -> str | None:
        if not parent_id:
            return None
        if parent_id == workstream_id:
            raise ContinuityError("invalid_parent")
        row = self._workstream_row(parent_id)
        if (
            row is None
            or row["project"] != project
            or row["workspace_fingerprint"] != fingerprint
            or row["status"] in _TERMINAL_STATUSES
        ):
            raise ContinuityError("invalid_parent")
        # 沿父链上溯：回到自身或重复访问即环
        seen = {workstream_id, parent_id}
        current = row["parent_id"]
        while current:
            if current in seen:
                raise ContinuityError("invalid_parent")
            seen.add(current)
            ancestor = self._workstream_row(current)
            if ancestor is None:
                break
            current = ancestor["parent_id"]
        return parent_id

    def _validate_sources(self, source_ids: tuple[int, ...], project: str) -> None:
        for source_id in source_ids:
            item = self._store.get_item(source_id, include_layers=False)
            if (
                item is None
                or item.status is ContextStatus.DELETED
                or (item.project != project and item.scope is not ContextScope.GLOBAL)
            ):
                raise ContinuityError("invalid_source")

    # ---- checkpoint item construction ----

    def _request_content(self, request: ContinuityCheckpointRequest) -> dict:
        return {
            "objective": request.objective,
            "accepted_decisions": list(request.accepted_decisions),
            "completed_steps": list(request.completed_steps),
            "current_step": request.current_step,
            "next_action": request.next_action,
            "blockers": list(request.blockers),
            "parent_workstream_id": request.parent_workstream_id or None,
            "source_context_ids": list(request.source_context_ids),
        }

    def _load_previous_content(self, context_id: int) -> dict:
        raw = self._store.get_layer(context_id, ContextLayer.L2)
        if raw is None:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.debug("continuity checkpoint L2 unparsable; starting fresh")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _merged_content(
        self, request: ContinuityCheckpointRequest, previous: dict
    ) -> dict:
        """Request fields win when provided; empty fields carry forward."""

        def pick_text(new: str, old: object) -> str:
            return new if new else (old if isinstance(old, str) else "")

        def pick_list(new: tuple, old: object) -> list:
            if new:
                return list(new)
            return list(old) if isinstance(old, list) else []

        def pick_ids(new: tuple, old: object) -> list:
            if new:
                return [int(item) for item in new]
            if isinstance(old, list):
                return [int(item) for item in old if type(item) is int and item > 0]
            return []

        return {
            "objective": pick_text(request.objective, previous.get("objective")),
            "accepted_decisions": pick_list(
                request.accepted_decisions, previous.get("accepted_decisions")
            ),
            "completed_steps": pick_list(
                request.completed_steps, previous.get("completed_steps")
            ),
            "current_step": pick_text(
                request.current_step, previous.get("current_step")
            ),
            "next_action": pick_text(
                request.next_action, previous.get("next_action")
            ),
            "blockers": pick_list(request.blockers, previous.get("blockers")),
            "parent_workstream_id": request.parent_workstream_id
            or previous.get("parent_workstream_id"),
            "source_context_ids": pick_ids(
                request.source_context_ids, previous.get("source_context_ids")
            ),
        }

    def _write_checkpoint_item(
        self,
        *,
        workstream_id: str,
        project: str,
        fingerprint: str,
        parent_id: str | None,
        status: str,
        checkpoint_revision: int,
        state_version: int,
        content: dict,
        anchor: dict,
        source_context_ids,
    ):
        authority = {
            "schema_version": 1,
            "workstream_id": workstream_id,
            "project": project,
            "workspace_fingerprint": fingerprint,
            "checkpoint_revision": checkpoint_revision,
            "state_version": state_version,
            "status": status,
            "repo": {
                "kind": anchor["kind"],
                "branch": anchor["branch"],
                "root_commit": anchor["root_commit"],
                "head_commit": anchor["head_commit"],
            },
        }
        client = dict(content)
        client["parent_workstream_id"] = parent_id
        client["source_context_ids"] = [int(item) for item in source_context_ids]
        l2 = _build_l2_payload(client=client, authority=authority)
        l0 = _truncate(
            f"[{status}] {content['objective'] or '（无目标）'}"
            f" — 下一步: {content['next_action'] or '（未设定）'}",
            self._config.context_l0_max_chars,
        )
        l1 = self._render_l1(content, status)
        layers = ContextLayers(l0=l0, l1=l1, l2=l2, generator=self.VERSION)
        try:
            validate_layers(layers, self._config)
        except ContextValidationError:
            raise ContinuityError("content_rejected") from None
        draft = ContextItemDraft(
            identity_key=f"project:{project}:workstream:{workstream_id}:checkpoint",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
            layers=layers,
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            tier=ContextTier.NORMAL,
        )
        item = self._store.supersede_active(draft)
        conn = self._store._connection()
        for source_id in source_context_ids:
            conn.execute(
                "INSERT INTO context_sources ("
                "item_id, archive_id, source_kind, source_ref,"
                " extraction_version, created_at"
                ") VALUES (?, NULL, 'context_reference', ?, ?, ?)",
                (item.id, str(int(source_id)), self.VERSION, _now_iso()),
            )
        if tuple(source_context_ids):
            conn.execute(
                "UPDATE context_items SET source_count=("
                "SELECT COUNT(*) FROM context_sources WHERE item_id=?"
                ") WHERE id=?",
                (item.id, item.id),
            )
        return item

    def _render_l1(self, content: dict, status: str) -> str:
        lines = [f"目标: {content['objective'] or '（无）'}"]
        if content["accepted_decisions"]:
            lines.append("已确认方案: " + "；".join(content["accepted_decisions"]))
        if content["completed_steps"]:
            lines.append("已完成: " + "；".join(content["completed_steps"]))
        lines.append(f"当前步骤: {content['current_step'] or '（无）'}")
        lines.append(f"下一步: {content['next_action'] or '（未设定）'}")
        if content["blockers"]:
            lines.append("阻塞: " + "；".join(content["blockers"]))
        lines.append(f"状态: {status}")
        return "\n".join(lines)

    # ---- reads ----

    def _workstream_row(self, workstream_id: str):
        if not workstream_id:
            return None
        return self._store._connection().execute(
            "SELECT * FROM continuity_workstreams WHERE id=?", (workstream_id,)
        ).fetchone()

    def _focus_row(self, project: str, fingerprint: str):
        return self._store._connection().execute(
            "SELECT * FROM continuity_focus "
            "WHERE project=? AND workspace_fingerprint=?",
            (project, fingerprint),
        ).fetchone()

    def _current_focus_revision(self, project: str, fingerprint: str) -> int:
        row = self._focus_row(project, fingerprint)
        return int(row["revision"]) if row is not None else 0

    def _unfinished_rows(self, project: str, fingerprint: str):
        return self._store._connection().execute(
            "SELECT * FROM continuity_workstreams "
            "WHERE project=? AND workspace_fingerprint=? "
            "AND status NOT IN ('completed', 'cancelled') "
            "ORDER BY updated_at DESC, id LIMIT ?",
            (project, fingerprint, _MAX_CANDIDATES),
        ).fetchall()

    def _terminal_rows(self, project: str, fingerprint: str):
        return self._store._connection().execute(
            "SELECT * FROM continuity_workstreams "
            "WHERE project=? AND workspace_fingerprint=? "
            "AND status IN ('completed', 'cancelled') "
            "ORDER BY updated_at DESC, id LIMIT ?",
            (project, fingerprint, _MAX_CANDIDATES),
        ).fetchall()

    def _unfinished_summaries(
        self, project: str, fingerprint: str
    ) -> tuple[WorkstreamSummary, ...]:
        rows = self._unfinished_rows(project, fingerprint)
        return tuple(self._summary_of(row) for row in rows)

    def _summary_of(self, row) -> WorkstreamSummary:
        l0 = self._store.get_layer(int(row["current_context_id"]), ContextLayer.L0)
        return WorkstreamSummary(
            workstream_id=row["id"],
            project=row["project"],
            status=row["status"],
            checkpoint_revision=int(row["checkpoint_revision"]),
            state_version=int(row["state_version"]),
            l0=l0 or "",
            updated_at=row["updated_at"],
        )

    def _checkpoint_payload(self, row) -> dict:
        """L0/L1 plus the L2 authoritative fields; never the raw L2 text."""
        context_id = int(row["current_context_id"])
        return {
            "l0": self._store.get_layer(context_id, ContextLayer.L0) or "",
            "l1": self._store.get_layer(context_id, ContextLayer.L1) or "",
            "workstream_id": row["id"],
            "project": row["project"],
            "workspace_fingerprint": row["workspace_fingerprint"],
            "checkpoint_revision": int(row["checkpoint_revision"]),
            "state_version": int(row["state_version"]),
            "status": row["status"],
            "repo": {
                "kind": row["repo_kind"],
                "branch": row["repo_branch"],
                "root_commit": row["repo_root_commit"],
                "head_commit": row["repo_head_commit"],
            },
        }

    # ---- staleness ----

    def _staleness(self, row, workspace_path: str, fingerprint: str) -> str:
        if row["workspace_fingerprint"] != fingerprint:
            return "wrong_workspace"
        if row["repo_kind"] != "git" or not row["repo_head_commit"]:
            return "unknown"
        anchor = _collect_repo_anchor(workspace_path)
        if anchor["kind"] != "git" or not anchor["head_commit"]:
            return "unknown"
        checkpoint_head = row["repo_head_commit"]
        current_head = anchor["head_commit"]
        if current_head == checkpoint_head and anchor["branch"] == row["repo_branch"]:
            return "fresh"
        checkpoint_is_ancestor = _is_ancestor(
            workspace_path, checkpoint_head, current_head
        )
        current_is_ancestor = _is_ancestor(
            workspace_path, current_head, checkpoint_head
        )
        if not checkpoint_is_ancestor and not current_is_ancestor:
            return "head_diverged"
        if anchor["branch"] != row["repo_branch"]:
            return "branch_changed"
        if checkpoint_is_ancestor:
            return "head_advanced"
        return "fresh"

    # ---- events ----

    def _record_event(
        self,
        workstream_id: str | None,
        event_type: str,
        *,
        before_revision: int | None,
        after_revision: int | None,
        before_state_version: int | None,
        after_state_version: int | None,
        error_code: str = "",
    ) -> None:
        self._store._connection().execute(
            "INSERT INTO continuity_events("
            "workstream_id, event_type, before_revision, after_revision,"
            " before_state_version, after_state_version, error_code, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workstream_id,
                event_type,
                before_revision,
                after_revision,
                before_state_version,
                after_state_version,
                error_code,
                _now_iso(),
            ),
        )

    def _record_failure_event(
        self, request: ContinuityCheckpointRequest, code: str
    ) -> None:
        """Persist the failed mutation in a fresh transaction so the audit
        survives the rollback; best-effort and content-free."""
        try:
            row = self._workstream_row(request.workstream_id)
            with self._store.transaction():
                self._record_event(
                    request.workstream_id or None,
                    code,
                    before_revision=(
                        int(row["checkpoint_revision"]) if row is not None else None
                    ),
                    after_revision=None,
                    before_state_version=(
                        int(row["state_version"]) if row is not None else None
                    ),
                    after_state_version=None,
                    error_code=code,
                )
        except Exception:
            logger.debug("continuity failure event could not be recorded: %s", code)


# ---- repo anchors (server-side, shell-free) ----


def _git(workspace_path: str, *args: str) -> str | None:
    """One read-only git call; None on any failure, stderr discarded."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace_path, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _collect_repo_anchor(workspace_path: str) -> dict:
    """Server-collected repo anchor; non-git workspaces record only the kind."""
    anchor = {"kind": "non_git", "branch": "", "root_commit": "", "head_commit": ""}
    if _git(workspace_path, "rev-parse", "--show-toplevel") is None:
        return anchor
    branch = _git(workspace_path, "branch", "--show-current")
    if not branch:
        branch = _git(workspace_path, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(workspace_path, "rev-parse", "HEAD")
    roots = _git(workspace_path, "rev-list", "--max-parents=0", "HEAD")
    return {
        "kind": "git",
        "branch": branch or "",
        "root_commit": roots.splitlines()[0].strip() if roots else "",
        "head_commit": head or "",
    }


def _is_ancestor(workspace_path: str, older: str, newer: str) -> bool:
    """True only when git confirms ``older`` is an ancestor of ``newer``."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace_path, "merge-base", "--is-ancestor", older, newer],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0
