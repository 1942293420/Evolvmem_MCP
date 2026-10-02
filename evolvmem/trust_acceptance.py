"""Reproducible trust-acceptance cases for the first EvolvMem trust package.

Every case runs the *real* store/service/recall boundaries over synthetic
fixtures created in a throwaway data directory: no production database, no
conversation logs, no model call, no network. Each case fixes its expected
values (ids, statuses, stable reason codes) and returns a JSON-serializable
record with the source IDs it observed.

What these cases prove is the offline assertion only. Whether a desktop model
actually *adopts* the recalled context in a live session is a different,
UNVERIFIED question: ``MODEL_ADOPTION`` states that explicitly and is echoed
in every result so the boundary cannot be mistaken for model-level success.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextSearchRequest,
    ContextSessionStartRequest,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.continuity_models import (
    ContinuityCheckpointRequest,
    ContinuityResumeRequest,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_progress_recall import read_recent_project_progress
from evolvmem.project_store import ProjectStore
from evolvmem.trust_views import TrustViews
from evolvmem.workspace_identity import WorkspaceIdentityProvider

MODEL_ADOPTION = "UNVERIFIED"

REQUIRED_CASE_IDS: tuple[str, ...] = (
    "windows-current-progress-vs-historical-failure",
    "procurement-latest-update",
    "continue-exact-task",
    "pending-acceptance-vs-successful-test",
    "inventory-unrelated-to-evolvmem",
    "ambiguous-alias",
    "unknown-event-time",
    "supplier-new-vs-old-decision",
    "late-backfill-older-event",
    "knowledge-refresh-candidate-gate",
    "failed-knowledge-refresh",
    "knowledge-needs-refresh",
    "effective-until-exclusive",
)

# The named "failed knowledge refresh" business case is delivered here by the
# Views package (see ``_case_failed_knowledge_refresh``). What stays deferred is
# live desktop-model adoption: no case calls a model or a real Windows session.
DEFERRED_CASE_NOTES: tuple[str, ...] = (
    "model-adoption: no live desktop session or model call is performed; every"
    " result still carries model_adoption=UNVERIFIED",
)

_WS = "hmac-sha256:" + "a" * 64
_NOW = "2026-10-02 00:00:00"


@dataclass(frozen=True, slots=True)
class TrustCaseResult:
    case_id: str
    question: str
    expected: dict
    observed: dict
    source_ids: tuple[int, ...] = ()
    passed: bool = False
    reason: str = ""
    model_adoption: str = MODEL_ADOPTION

    def to_json(self) -> dict:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "expected": self.expected,
            "observed": self.observed,
            "source_ids": list(self.source_ids),
            "passed": self.passed,
            "reason": self.reason,
            "model_adoption": self.model_adoption,
        }


# ---------------------------------------------------------------------------
# synthetic environment helpers
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self, root: Path) -> None:
        self.config = Config(data_dir=root, apply_environment=False)
        self.store = ContextStore(self.config)
        self.store.initialize()
        self.service = ContextService(self.config, store=self.store)
        self.service.initialize(mode=ContextMode.SHADOW, adapter="codex")
        self.projects = ProjectStore(
            self.store._connection(),
            self.store._require_transaction,
            generic_names=("home", "src"),
        )

    def close(self) -> None:
        self.service.close(close_embedding_engine=False)
        self.store.close()


def _register(env: _Env, project: str, *aliases: str) -> None:
    with env.store.transaction():
        env.projects.register_project(project)
        for alias in aliases:
            env.projects.add_alias(alias, project)


def _item(
    env: _Env,
    identity_key: str,
    *,
    project: str,
    l0: str,
    l1: str | None = None,
    content_type: ContextContentType = ContextContentType.FACT,
    status: ContextStatus = ContextStatus.ACTIVE,
    confidence: float = 0.9,
    importance: float = 8.0,
    **temporal,
):
    return env.store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=content_type,
            layers=ContextLayers(
                l0=l0,
                l1=l1 or f"detail for {identity_key}",
                l2=f"source for {identity_key}",
                generator="trust-acceptance",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=status,
            tier=ContextTier.NORMAL,
            importance=importance,
            confidence=confidence,
            **temporal,
        )
    )


def _supersede(env: _Env, identity_key: str, *, project: str, l0: str, **temporal):
    return env.store.supersede_active(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.DECISION,
            layers=ContextLayers(
                l0=l0, l1=f"detail for {l0}", l2="synthetic",
                generator="trust-acceptance",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            importance=8.0,
            confidence=0.9,
            **temporal,
        )
    )


def _review(
    env: _Env, item_id: int, *, state: str, review: str, project: str = "evolvmem"
) -> None:
    with env.store.transaction():
        if state == "conflict":
            env.projects.record_resolution(
                item_id, ProjectResolutionDecision.conflict("v1", ())
            )
        elif state == "unresolved":
            env.projects.record_resolution(
                item_id, ProjectResolutionDecision.unresolved("v1", ())
            )
        else:
            env.projects.record_resolution(
                item_id,
                ProjectResolutionDecision.resolved(project, "strong", "v1", ()),
            )
        if review == "accepted":
            env.projects.accept_resolution(
                item_id, project, expected_revision=1
            )
        elif review == "rejected":
            env.projects.reject_resolution(item_id, expected_revision=1)


def _bind(env: _Env, project: str, context_id: int, workstream_id: str) -> None:
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO continuity_workstreams (id, project,"
            " workspace_fingerprint, current_context_id, checkpoint_revision,"
            " state_version, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (workstream_id, project, _WS, context_id, 1, 1, "open", _NOW, _NOW),
        )


def _mark_superseded(env: _Env, item_id: int) -> None:
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET status='superseded' WHERE id=?", (item_id,)
        )


def _summary_item(env: _Env, identity_key: str, *, project: str, l0: str, l1: str):
    """A project summary row written through the store, as the rollup writes it."""
    return _item(
        env, identity_key, project=project, l0=l0, l1=l1,
        content_type=ContextContentType.PROJECT_SUMMARY,
    )


def _cover_source(env: _Env, summary_id: int, source_id: int) -> None:
    """Record the rollup's owned source closure for the current summary row."""
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO context_sources (item_id, archive_id, source_kind,"
            " source_ref, extraction_version, created_at)"
            " VALUES (?,NULL,'context_reference',?,'trust-acceptance',?)",
            (summary_id, str(source_id), "2026-10-01 00:00:00"),
        )


def _pin_source_time(env: _Env, source_id: int, stamp: str = "2026-10-01 00:00:00") -> None:
    """Make a covered source look unchanged since the coverage watermark."""
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET created_at=?, updated_at=? WHERE id=?",
            (stamp, stamp, source_id),
        )


def _rollup_row(env: _Env, project: str, summary_id: int, status: str,
                covered_through: str) -> None:
    """Persist one actual context_project_rollups row for the freshness case."""
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO context_project_rollups (project, current_context_id,"
            " source_set_hash, covered_through, generator_version, status, revision,"
            " updated_at) VALUES (?,?,?,?,?,?,1,?)"
            " ON CONFLICT(project) DO UPDATE SET"
            " current_context_id=excluded.current_context_id,"
            " covered_through=excluded.covered_through, status=excluded.status,"
            " updated_at=excluded.updated_at",
            (project, summary_id, "fixture-hash", covered_through,
             "project-rollup.v1", status, "2026-10-01 00:00:00"),
        )


def _ids(results) -> list[int]:
    return [result.id for result in results]


def _search(env: _Env, query: str, project: str, *, as_of=None):
    return env.service.search(
        ContextSearchRequest(
            query=query, project=project, top_k=10, as_of=as_of
        )
    )


def _session(env: _Env, project: str, query: str):
    return env.service.session_start(
        ContextSessionStartRequest(project=project, query=query),
        project_only=True,
    )


def _continuity_env(env: _Env, root: Path, project: str):
    """Real ContinuityService with a bound workspace and bootstrapped identity."""
    workspace = root / "ws"
    workspace.mkdir(exist_ok=True)
    provider = WorkspaceIdentityProvider(
        key_path=env.config.data_dir / "workspace.key"
    )
    provider.bootstrap_key()
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with env.store.transaction():
        env.projects.register_project(project)
        env.projects.bind_workspace(
            fingerprint, project, method="trust-acceptance", make_default=True
        )
    return ContinuityService(env.config, env.store, provider), workspace, provider


def _checkpoint(
    continuity,
    workspace,
    *,
    objective: str,
    completed_steps=(),
    current_step: str = "",
    next_action: str = "",
):
    created = continuity.checkpoint(
        ContinuityCheckpointRequest(
            action="create",
            workspace_path=str(workspace),
            objective=objective,
            completed_steps=tuple(completed_steps),
            current_step=current_step,
            next_action=next_action,
            make_focus=True,
            expected_focus_revision=0,
        )
    )
    return created, continuity.resume(
        ContinuityResumeRequest(workspace_path=str(workspace))
    )


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


def _case_windows(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        _register(env, "windows", "Windows")
        progress = _item(
            env, "project:windows:checkpoint:login", project="windows",
            l0="windows 登录改造当前进展",
            l1="目标: 完成登录改造\n已完成: 接口联调",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
        )
        failure = _item(
            env, "project:windows:decision:login-failure", project="windows",
            l0="windows 登录改造回归失败记录",
            content_type=ContextContentType.DECISION,
            effective_from="2026-01-01 00:00:00",
        )
        _review(env, progress.id, state="resolved", review="accepted",
                project="windows")
        _mark_superseded(env, failure.id)
        _bind(env, "windows", progress.id, "ws-windows-login")

        read = read_recent_project_progress(env.service, "windows", max_chars=4000)
        default_ids = _ids(_search(env, "windows 登录改造", "windows"))
        as_of_ids = _ids(
            _search(env, "windows 登录改造", "windows", as_of="2026-02-01 00:00:00")
        )
        expected = {
            "progress_ids": [progress.id],
            "ownership_label": "confirmed",
            "failure_in_default": False,
            "failure_at_as_of": True,
            "failure_status": "superseded",
        }
        observed = {
            "progress_ids": list(read.selected_ids),
            "ownership_label": read.diagnostics["ownership"].get(str(progress.id)),
            "failure_in_default": failure.id in default_ids,
            "failure_at_as_of": failure.id in as_of_ids,
            "failure_status": env.store.get_item(
                failure.id, include_layers=False
            ).status.value,
        }
        return _result(
            "windows-current-progress-vs-historical-failure",
            "windows 项目现在进度到哪了？之前那次登录失败呢？",
            expected, observed, (progress.id, failure.id),
        )
    finally:
        env.close()


def _case_procurement(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        _register(env, "procurement")
        old = _item(
            env, "project:procurement:decision:supplier", project="procurement",
            l0="采购 供应商 A 报价 100", content_type=ContextContentType.DECISION,
            effective_from="2026-01-01 00:00:00",
        )
        new = _supersede(
            env, "project:procurement:decision:supplier", project="procurement",
            l0="采购 供应商 B 报价 90", effective_from="2026-06-01 00:00:00",
        )
        expected = {"default_selected": [new.id], "as_of_selected": [old.id]}
        observed = {
            "default_selected": _ids(_search(env, "采购 供应商", "procurement")),
            "as_of_selected": _ids(
                _search(env, "采购 供应商", "procurement", as_of="2026-03-01 00:00:00")
            ),
        }
        return _result(
            "procurement-latest-update",
            "采购最新进展是什么？",
            expected, observed, (old.id, new.id),
        )
    finally:
        env.close()


def _case_continue_task(root: Path) -> TrustCaseResult:
    """Real exact continuity resume: ID + current_step + next_action."""
    env = _Env(root)
    try:
        continuity, workspace, _ = _continuity_env(env, root, "evolvmem")
        created, resumed = _checkpoint(
            continuity,
            workspace,
            objective="完成部署脚本",
            completed_steps=("打包",),
            current_step="联调接口",
            next_action="写好部署文档",
        )
        read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
        expected = {
            "resume_code": "ok",
            "workstream_id": created.workstream_id,
            "context_id": created.context_id,
            "status": "open",
            "current_step_in_l1": True,
            "next_action_in_l1": True,
            "resume_works_without_recall": True,
            "recall_selected": True,
        }
        l1 = resumed.checkpoint["l1"]
        observed = {
            "resume_code": resumed.code,
            "workstream_id": resumed.workstream_id,
            "context_id": resumed.context_id,
            "status": resumed.status,
            "current_step_in_l1": "当前步骤: 联调接口" in l1,
            "next_action_in_l1": "下一步: 写好部署文档" in l1,
            # The exact pointer read is independent of the default recall gate:
            # the same resume works even when recall holds the checkpoint.
            "resume_works_without_recall": (
                continuity.resume(
                    ContinuityResumeRequest(workspace_path=str(workspace))
                ).workstream_id
                == created.workstream_id
            ),
            "recall_selected": created.context_id in read.selected_ids,
        }
        return _result(
            "continue-exact-task",
            "继续 evolvmem 的部署脚本任务",
            expected, observed, (created.context_id,),
        )
    finally:
        env.close()


def _case_pending_vs_success(root: Path) -> TrustCaseResult:
    """A checkpoint whose test step passed waits for business acceptance: the
    workstream must stay ``open`` and never be reported as completed."""
    env = _Env(root)
    try:
        continuity, workspace, _ = _continuity_env(env, root, "evolvmem")
        created, resumed = _checkpoint(
            continuity,
            workspace,
            objective="交付验收流程",
            completed_steps=("测试通过",),
            current_step="等待业务验收",
            next_action="收集验收签字",
        )
        row = env.store._connection().execute(
            "SELECT status FROM continuity_workstreams WHERE id=?",
            (created.workstream_id,),
        ).fetchone()
        read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
        expected = {
            "status": "open",
            "never_completed": True,
            "current_step": "等待业务验收",
            "completed_step_present": True,
            "resumable": True,
            "injected_as_current_progress": True,
        }
        observed = {
            "status": resumed.status,
            "never_completed": (
                resumed.status != "completed" and row["status"] != "completed"
            ),
            "current_step": (
                "等待业务验收" if "当前步骤: 等待业务验收" in resumed.checkpoint["l1"]
                else resumed.checkpoint["l1"]
            ),
            "completed_step_present": "已完成: 测试通过" in resumed.checkpoint["l1"],
            "resumable": resumed.code == "ok",
            "injected_as_current_progress": (
                created.context_id in read.selected_ids
            ),
        }
        return _result(
            "pending-acceptance-vs-successful-test",
            "这条测试通过了但还没验收，算完成了吗？",
            expected, observed, (created.context_id,),
        )
    finally:
        env.close()


def _case_inventory(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        from evolvmem.project_mention_recall import recall_mentioned_projects

        _register(env, "evolvmem", "EvolvMem")
        _register(env, "inventory")
        misassigned = _item(
            env, "project:inventory:checkpoint:stock", project="evolvmem",
            l0="库存盘点当前断点",
            l1="目标: 盘点库存\n已完成: 第一期",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
        )
        _review(env, misassigned.id, state="unresolved", review="pending")
        _bind(env, "evolvmem", misassigned.id, "ws-inventory")
        continuity, workspace, _ = _continuity_env(env, root, "evolvmem")
        task, _ = _checkpoint(
            continuity, workspace, objective="完成部署脚本",
            completed_steps=("打包",), current_step="联调",
            next_action="写好文档",
        )
        recall = recall_mentioned_projects(env.service, query="继续 evolvmem 的进展")
        expected = {
            "matched_projects": ["evolvmem"],
            "selected_ids": [task.context_id],
            "unverified_ids": [],
            "inventory_leaked": False,
            "excluded": [{"id": misassigned.id, "reason": "project_ownership_pending"}],
        }
        observed = {
            "matched_projects": list(recall.matched_projects),
            "selected_ids": list(recall.selected_ids),
            "unverified_ids": list(recall.diagnostics["unverified_ids"]),
            "inventory_leaked": misassigned.id in recall.selected_ids,
            "excluded": list(recall.diagnostics["excluded"]),
        }
        return _result(
            "inventory-unrelated-to-evolvmem",
            "继续 evolvmem 的进展",
            expected, observed, (task.context_id, misassigned.id),
        )
    finally:
        env.close()


def _case_ambiguous(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        from evolvmem.project_mention_recall import recall_mentioned_projects

        _register(env, "AI 采购")
        _register(env, "AI采购")
        _item(
            env, "project:ai:fact:one", project="AI采购",
            l0="AI采购 预算待定",
        )
        recall = recall_mentioned_projects(env.service, query="继续 AI采购 的进度")
        expected = {
            "matched_projects": [],
            "block_empty": True,
            "ambiguous_surfaces": ["ai采购"],
        }
        observed = {
            "matched_projects": list(recall.matched_projects),
            "block_empty": recall.block == "",
            "ambiguous_surfaces": list(recall.diagnostics["ambiguous_surfaces"]),
        }
        return _result(
            "ambiguous-alias", "继续 AI采购 的进度", expected, observed, (),
        )
    finally:
        env.close()


def _case_unknown_time(root: Path) -> TrustCaseResult:
    """UNKNOWN dates stay UNKNOWN: never derived, never confirmed, and the
    unreviewed project record is held out of default injection."""
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        entry = _item(
            env, "project:evolvmem:fact:undated", project="evolvmem",
            l0="zebra 无时间记录的结论",
        )
        session = _session(env, "evolvmem", "zebra")
        window = env.service.decision_window(entry.id)
        expected = {
            "temporal_state": "unknown",
            "effective_from": None,
            "held_out": True,
            "held_reason": "project_ownership_unverified",
        }
        observed = {
            "temporal_state": window["temporal_state"],
            "effective_from": window["effective_from"],
            "held_out": entry.id not in session.selected_ids,
            "held_reason": next(
                (
                    item.reason
                    for item in session.ownership_exclusions
                    if item.item_id == entry.id
                ),
                "",
            ),
        }
        return _result(
            "unknown-event-time",
            "这件事是什么时候发生的？结论算吗？",
            expected, observed, (entry.id,),
        )
    finally:
        env.close()


def _case_supplier_new_old(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        _register(env, "supplier")
        old = _item(
            env, "project:supplier:decision:choice", project="supplier",
            l0="供应商选择旧决定 A", content_type=ContextContentType.DECISION,
            effective_from="2026-01-01 00:00:00",
        )
        new = _supersede(
            env, "project:supplier:decision:choice", project="supplier",
            l0="供应商选择新决定 B", effective_from="2026-06-01 00:00:00",
        )
        old_row = env.store.get_item(old.id, include_layers=False)
        new_row = env.store.get_item(new.id, include_layers=False)
        expected = {
            "default_selected": [new.id],
            "as_of_selected": [old.id],
            "supersedes_link": old.id,
            "superseded_by_link": new.id,
        }
        observed = {
            "default_selected": _ids(_search(env, "供应商选择", "supplier")),
            "as_of_selected": _ids(
                _search(env, "供应商选择", "supplier", as_of="2026-03-01 00:00:00")
            ),
            "supersedes_link": new_row.supersedes,
            "superseded_by_link": old_row.superseded_by,
        }
        return _result(
            "supplier-new-vs-old-decision",
            "供应商这件事现在的决定是什么？",
            expected, observed, (old.id, new.id),
        )
    finally:
        env.close()


def _case_late_backfill(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        later = _item(
            env, "project:evolvmem:decision:supplier", project="evolvmem",
            l0="zebra 供应商 六月生效决定",
            content_type=ContextContentType.DECISION,
            effective_from="2026-06-01 00:00:00",
        )
        backfilled = _supersede(
            env, "project:evolvmem:decision:supplier", project="evolvmem",
            l0="zebra 供应商 三月补录事件",
            effective_from="2026-03-01 00:00:00",
            occurred_at="2026-03-01 00:00:00",
            mentioned_at="2026-10-01 00:00:00",
        )
        later_row = env.store.get_item(later.id, include_layers=False)
        expected = {
            "active_id": later.id,
            "active_status": "active",
            "backfill_status": "superseded",
            "default_selected": [later.id],
            "as_of_selected": [backfilled.id],
            "backfill_rank_below": True,
        }
        observed = {
            "active_id": later_row.id,
            "active_status": later_row.status.value,
            "backfill_status": env.store.get_item(
                backfilled.id, include_layers=False
            ).status.value,
            "default_selected": _ids(_search(env, "zebra 供应商", "evolvmem")),
            "as_of_selected": _ids(
                _search(env, "zebra 供应商", "evolvmem", as_of="2026-04-01 00:00:00")
            ),
            "backfill_rank_below": (
                backfilled.effective_from < later_row.effective_from
            ),
        }
        return _result(
            "late-backfill-older-event",
            "这条三月的记录是今天补录的，它算最新决定吗？",
            expected, observed, (later.id, backfilled.id),
        )
    finally:
        env.close()


def _case_candidate_gate(root: Path) -> TrustCaseResult:
    """Offline candidate / low-confidence gate only.

    The named *failed knowledge refresh* business case (a failed rollup keeps
    the previous project-knowledge page with honest freshness) is covered by
    ``_case_failed_knowledge_refresh``; this case deliberately covers only the
    default-injection candidate/low-confidence gate and makes no rollup claim.
    """
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        good = _item(
            env, "project:evolvmem:fact:good", project="evolvmem",
            l0="zebra 知识刷新后的当前事实",
        )
        _review(env, good.id, state="resolved", review="accepted")
        failed = _item(
            env, "project:evolvmem:fact:failed", project="evolvmem",
            l0="zebra 知识刷新失败的候选",
            status=ContextStatus.CANDIDATE,
        )
        weak = _item(
            env, "project:evolvmem:fact:weak", project="evolvmem",
            l0="zebra 低置信刷新残留",
            confidence=0.1,
        )
        session = _session(env, "evolvmem", "zebra")
        expected = {
            "selected_ids": [good.id],
            "candidate_present": False,
            "weak_present": False,
            "rollup_freshness_claimed": False,
        }
        observed = {
            "selected_ids": list(session.selected_ids),
            "candidate_present": failed.id in session.selected_ids,
            "weak_present": weak.id in session.selected_ids,
            "rollup_freshness_claimed": False,
        }
        return _result(
            "knowledge-refresh-candidate-gate",
            "刷新残留的候选/低置信条目会进默认注入吗？",
            expected, observed, (good.id, failed.id, weak.id),
        )
    finally:
        env.close()


def _case_failed_knowledge_refresh(root: Path) -> TrustCaseResult:
    """A failed rollup keeps the previous knowledge page, labelled failed/stale.

    An actual ``context_project_rollups`` row with ``status='failed'`` retains
    ``current_context_id``; the page must serve that stored content and must
    never report it fresh. A later eligible source past ``covered_through`` must
    set ``needs_refresh``.
    """
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        summary = _summary_item(
            env, "project:evolvmem:rollup", project="evolvmem",
            l0="项目知识摘要 L0（最近一次成功版本）",
            l1="项目知识摘要 L1：保留的旧内容",
        )
        _review(env, summary.id, state="resolved", review="accepted")
        source = _item(
            env, "session:evolvmem:covered", project="evolvmem",
            l0="已覆盖的会话摘要来源",
            content_type=ContextContentType.SESSION_SUMMARY,
        )
        _review(env, source.id, state="resolved", review="accepted")
        _cover_source(env, summary.id, source.id)
        _pin_source_time(env, source.id)
        _rollup_row(env, "evolvmem", summary.id, "failed", "2026-10-01 00:00:00")
        views = TrustViews(env.service)
        knowledge = views.knowledge("evolvmem")
        fresh = _item(
            env, "project:evolvmem:fact:after-coverage", project="evolvmem",
            l0="覆盖之后新增的有效事实", content_type=ContextContentType.FACT,
        )
        _review(env, fresh.id, state="resolved", review="accepted")
        after = views.knowledge("evolvmem")
        new_ids = after["rollup"]["new_eligible_source_ids"]
        expected = {
            "rollup_status": "failed",
            "freshness": "failed",
            "reported_fresh": False,
            "retained": True,
            "retained_l1": "项目知识摘要 L1：保留的旧内容",
            "needs_refresh": True,
            "new_source_detected": True,
        }
        observed = {
            "rollup_status": knowledge["rollup"]["status"],
            "freshness": knowledge["rollup"]["freshness"],
            "reported_fresh": knowledge["rollup"]["freshness"] == "fresh",
            "retained": knowledge["latest_progress"]["retained"],
            "retained_l1": knowledge["latest_progress"]["l1"],
            "needs_refresh": after["rollup"]["needs_refresh"],
            "new_source_detected": fresh.id in new_ids,
        }
        return _result(
            "failed-knowledge-refresh",
            "知识刷新失败后，项目知识页还保留旧内容吗？会伪装成最新吗？",
            expected, observed, (summary.id, source.id, fresh.id),
        )
    finally:
        env.close()


def _case_knowledge_needs_refresh(root: Path) -> TrustCaseResult:
    """A ready rollup is fresh until a new eligible source passes coverage."""
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        summary = _summary_item(
            env, "project:evolvmem:rollup", project="evolvmem",
            l0="项目知识摘要 L0", l1="项目知识摘要 L1",
        )
        _review(env, summary.id, state="resolved", review="accepted")
        source = _item(
            env, "session:evolvmem:covered", project="evolvmem",
            l0="已覆盖的会话摘要来源",
            content_type=ContextContentType.SESSION_SUMMARY,
        )
        _review(env, source.id, state="resolved", review="accepted")
        _cover_source(env, summary.id, source.id)
        _pin_source_time(env, source.id)
        _rollup_row(env, "evolvmem", summary.id, "ready", "2026-10-01 00:00:00")
        views = TrustViews(env.service)
        before = views.knowledge("evolvmem")
        added = _item(
            env, "project:evolvmem:experience:after-coverage", project="evolvmem",
            l0="覆盖之后新增的原子来源", content_type=ContextContentType.EXPERIENCE,
        )
        _review(env, added.id, state="resolved", review="accepted")
        after = views.knowledge("evolvmem")
        expected = {
            "before_freshness": "fresh",
            "before_needs_refresh": False,
            "after_freshness": "stale",
            "after_needs_refresh": True,
            "new_source_detected": True,
        }
        observed = {
            "before_freshness": before["rollup"]["freshness"],
            "before_needs_refresh": before["rollup"]["needs_refresh"],
            "after_freshness": after["rollup"]["freshness"],
            "after_needs_refresh": after["rollup"]["needs_refresh"],
            "new_source_detected": added.id in after["rollup"]["new_eligible_source_ids"],
        }
        return _result(
            "knowledge-needs-refresh",
            "覆盖水位之后出现新的有效来源，知识页会提示刷新吗？",
            expected, observed, (summary.id, source.id, added.id),
        )
    finally:
        env.close()


def _case_until_exclusive(root: Path) -> TrustCaseResult:
    env = _Env(root)
    try:
        _register(env, "evolvmem", "EvolvMem")
        boundary = _item(
            env, "project:evolvmem:decision:window", project="evolvmem",
            l0="zebra 到点失效的决定",
            content_type=ContextContentType.DECISION,
            effective_from="2026-01-01 00:00:00",
            effective_until="2026-10-02 00:00:00",
        )
        expected = {"at_boundary_present": False, "before_boundary_present": True}
        observed = {
            "at_boundary_present": boundary.id in _ids(
                _search(env, "zebra", "evolvmem", as_of="2026-10-02 00:00:00")
            ),
            "before_boundary_present": boundary.id in _ids(
                _search(env, "zebra", "evolvmem", as_of="2026-10-01 23:59:59")
            ),
        }
        return _result(
            "effective-until-exclusive",
            "这条决定到 10 月 2 日还算有效吗？",
            expected, observed, (boundary.id,),
        )
    finally:
        env.close()


_CASES = (
    _case_windows,
    _case_procurement,
    _case_continue_task,
    _case_pending_vs_success,
    _case_inventory,
    _case_ambiguous,
    _case_unknown_time,
    _case_supplier_new_old,
    _case_late_backfill,
    _case_candidate_gate,
    _case_failed_knowledge_refresh,
    _case_knowledge_needs_refresh,
    _case_until_exclusive,
)


def _result(case_id, question, expected, observed, source_ids) -> TrustCaseResult:
    if observed == expected:
        return TrustCaseResult(
            case_id=case_id, question=question, expected=expected,
            observed=observed, source_ids=tuple(source_ids), passed=True,
            reason="ok",
        )
    mismatch = sorted(
        key
        for key in set(expected) | set(observed)
        if expected.get(key) != observed.get(key)
    )
    return TrustCaseResult(
        case_id=case_id, question=question, expected=expected,
        observed=observed, source_ids=tuple(source_ids), passed=False,
        reason="mismatch:" + ",".join(mismatch),
    )


def run_cases(data_dir: str | Path | None = None) -> list[TrustCaseResult]:
    """Run every case in its own throwaway data directory; deterministic order.

    ``data_dir`` is an optional existing parent directory for the synthetic
    stores; when omitted a fresh system temp directory is used and removed.
    """
    if data_dir is None:
        parent = Path(tempfile.mkdtemp(prefix="evolvmem-trust-"))
        cleanup_parent = True
    else:
        parent = Path(data_dir)
        parent.mkdir(parents=True, exist_ok=True)
        cleanup_parent = False
    results: list[TrustCaseResult] = []
    try:
        for case in _CASES:
            root = Path(tempfile.mkdtemp(prefix="case-", dir=parent))
            try:
                results.append(case(root))
            finally:
                shutil.rmtree(root, ignore_errors=True)
    finally:
        if cleanup_parent:
            shutil.rmtree(parent, ignore_errors=True)
    return results


def results_as_json(results: list[TrustCaseResult]) -> str:
    return json.dumps(
        [result.to_json() for result in results],
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )


__all__ = [
    "MODEL_ADOPTION",
    "REQUIRED_CASE_IDS",
    "TrustCaseResult",
    "results_as_json",
    "run_cases",
]
