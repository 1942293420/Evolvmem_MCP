"""Repair-round regression tests for the coordinator review notes.

Confirmed defects reproduced against the previous revision:
1. ``as_of`` after a successor became effective returned BOTH the old
   superseded decision and the successor.
2. Reviewing a workstream-bound checkpoint into another project returned
   ``ok=true`` while ``continuity_workstreams.project`` stayed behind, leaving
   the workstream inconsistent/stranded.

Everything runs on synthetic temp stores through the real
service/store/continuity/web boundaries.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextItemDraft,
    ContextLayers,
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
from evolvmem.project_store import ProjectStore, ProjectStoreError
from evolvmem.workspace_identity import WorkspaceIdentityProvider
from tests.test_memory_trust import Env, FP, item, register, resolution


@pytest.fixture
def env(temp_dir):
    instance = Env(temp_dir)
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# helpers: real continuity workstream + trusted typed writes
# ---------------------------------------------------------------------------


def _make_item(
    env, identity_key, *, project, l0, content_type=ContextContentType.FACT,
    scope=ContextScope.PROJECT, tier=ContextTier.NORMAL,
    status=ContextStatus.ACTIVE, importance=8.0, confidence=0.9, **temporal,
):
    return env.store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=content_type,
            layers=ContextLayers(
                l0=l0, l1=f"detail for {l0}", l2="synthetic",
                generator="trust-repair",
            ),
            project=project,
            scope=scope,
            tier=tier,
            status=status,
            importance=importance,
            confidence=confidence,
            **temporal,
        )
    )


def _bind_workspace(env, workspace, project):
    provider = WorkspaceIdentityProvider(
        key_path=env.config.data_dir / "workspace.key"
    )
    provider.bootstrap_key()
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with env.store.transaction():
        env.projects.register_project(project)
        env.projects.bind_workspace(
            fingerprint, project, method="test", make_default=True
        )
    return provider, fingerprint


def _continuity(env, workspace, project="evolvmem"):
    provider, _ = _bind_workspace(env, workspace, project)
    return ContinuityService(env.config, env.store, provider), provider


def _create_workstream(
    env,
    workspace,
    *,
    project="evolvmem",
    objective="完成部署脚本",
    completed_steps=(),
    current_step="",
    next_action="",
    status=None,
):
    continuity, _ = _continuity(env, workspace, project)
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
    return continuity, created


def _review_row(env, item_id):
    return env.store._connection().execute(
        "SELECT * FROM context_project_resolutions WHERE item_id=?", (item_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# 1. unknown ownership: hold, trusted provenance, resume independence
# ---------------------------------------------------------------------------


def test_no_resolution_project_record_is_held_from_default_injection(env):
    register(env, "evolvmem", "EvolvMem")
    unreviewed = item(
        env, "project:evolvmem:fact:unreviewed", project="evolvmem",
        l0="zebra 无归属记录的结论", importance=8.0,
    )
    trusted = item(
        env, "project:evolvmem:fact:trusted", project="evolvmem",
        l0="zebra 有归属记录的结论", importance=8.0,
    )
    resolution(env, trusted.id, state="resolved", review="accepted")

    session = env.service.session_start(
        ContextSessionStartRequest(project="evolvmem", query="zebra"),
        project_only=True,
    )
    assert trusted.id in session.selected_ids
    assert unreviewed.id not in session.selected_ids
    reasons = {entry.item_id: entry.reason for entry in session.ownership_exclusions}
    assert reasons[unreviewed.id] == "project_ownership_unverified"


def test_typed_continuity_checkpoint_gets_trusted_provenance(tmp_path, env):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, created = _create_workstream(
        env, workspace, objective="完成部署脚本",
        completed_steps=("打包",), current_step="联调", next_action="写好文档",
    )
    row = _review_row(env, created.context_id)
    assert row is not None, "typed continuity must record explicit provenance"
    assert row["resolution_state"] == "resolved"
    assert row["review_state"] == "not_required"
    assert row["method"] == "explicit_project"

    read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
    assert created.context_id in read.selected_ids
    assert read.diagnostics["ownership"][str(created.context_id)] == "confirmed"


def test_exact_continuity_resume_survives_a_recall_hold(tmp_path, env):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    continuity, created = _create_workstream(
        env, workspace, objective="完成部署脚本",
        completed_steps=("打包",), current_step="等待业务验收", next_action="补验收证据",
    )
    # Simulate an unreviewed/held checkpoint: drop its trusted provenance row.
    with env.store.transaction():
        env.store._connection().execute(
            "DELETE FROM context_project_resolutions WHERE item_id=?",
            (created.context_id,),
        )
    read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
    assert created.context_id not in read.selected_ids
    held = {row["id"]: row["reason"] for row in read.diagnostics["excluded"]}
    assert held[created.context_id] == "project_ownership_unverified"

    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(workspace))
    )
    assert resumed.code == "ok"
    assert resumed.workstream_id == created.workstream_id
    assert resumed.context_id == created.context_id
    assert resumed.status == "open"
    assert "当前步骤: 等待业务验收" in resumed.checkpoint["l1"]
    assert "下一步: 补验收证据" in resumed.checkpoint["l1"]


def test_global_decision_row_keeps_the_unverified_label(env):
    register(env, "evolvmem")
    global_row = item(
        env, "project:evolvmem:checkpoint:global", project="evolvmem",
        l0="evolvmem 全局决议断点", l1="目标: 全局决议\n已完成: 无",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    with env.store.transaction():
        env.projects.record_resolution(
            global_row.id,
            ProjectResolutionDecision.global_decision("v1"),
        )
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO continuity_workstreams (id, project,"
            " workspace_fingerprint, current_context_id, checkpoint_revision,"
            " state_version, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ws-global", "evolvmem", FP, global_row.id, 1, 1, "open",
             "2026-10-01 00:00:00", "2026-10-01 00:00:00"),
        )
    read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
    assert global_row.id in read.selected_ids
    assert "ownership=unverified" in read.text
    assert read.diagnostics["ownership"][str(global_row.id)] == "unverified"


def test_global_applicable_policy_is_unaffected_by_the_hold(env):
    register(env, "evolvmem")
    policy = _make_item(
        env, "global:policy:pinned", project="",
        l0="zebra 全局约束", content_type=ContextContentType.CONSTRAINT,
        scope=ContextScope.GLOBAL, confidence=0.9,
        tier=ContextTier.PINNED, importance=9.0,
    )
    session = env.service.session_start(
        ContextSessionStartRequest(project="evolvmem", query="zebra"),
    )
    assert policy.id in session.selected_ids


# ---------------------------------------------------------------------------
# 2. as_of successor boundary
# ---------------------------------------------------------------------------


def _decision_pair(env, identity="project:evolvmem:decision:supplier"):
    old = item(
        env, identity, project="evolvmem", l0="zebra 供应商 旧决定",
        content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", importance=8.0,
    )
    new = env.store.supersede_active(
        ContextItemDraft(
            identity_key=identity,
            content_type=ContextContentType.DECISION,
            layers=ContextLayers(
                l0="zebra 供应商 新决定", l1="detail", l2="source",
                generator="trust-repair",
            ),
            project="evolvmem",
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            importance=8.0,
            confidence=0.9,
            effective_from="2026-06-01 00:00:00",
        )
    )
    return old, new


def _as_of_ids(env, query, as_of):
    return [
        result.id
        for result in env.service.search(
            ContextSearchRequest(
                query=query, project="evolvmem", top_k=10, as_of=as_of
            )
        )
    ]


def test_as_of_after_successor_effective_returns_only_the_successor(env):
    register(env, "evolvmem")
    old, new = _decision_pair(env)
    assert _as_of_ids(env, "zebra 供应商", "2026-07-01 00:00:00") == [new.id]
    assert old.id not in _as_of_ids(env, "zebra 供应商", "2026-07-01 00:00:00")


def test_as_of_before_successor_effective_returns_the_old_decision(env):
    register(env, "evolvmem")
    old, new = _decision_pair(env)
    assert _as_of_ids(env, "zebra 供应商", "2026-03-01 00:00:00") == [old.id]


def test_explicit_until_preserved_but_successor_takes_precedence(env):
    """Same-identity supersession beats an overlapping explicit end for reads.

    The explicit ``effective_until`` stays stored on the superseded row (history
    is preserved), but once the successor is effective the older record is no
    longer served as a second simultaneously-current decision. Contract update
    2026-10-02 (Views review): the coordinator's
    ``tests/test_memory_trust_coordinator.py`` probe is the authority here.
    """
    register(env, "evolvmem")
    old = item(
        env, "project:evolvmem:decision:contract", project="evolvmem",
        l0="zebra 供应商 合同窗口", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
        effective_until="2026-12-31 00:00:00", importance=8.0,
    )
    new = env.store.supersede_active(
        ContextItemDraft(
            identity_key="project:evolvmem:decision:contract",
            content_type=ContextContentType.DECISION,
            layers=ContextLayers(
                l0="zebra 供应商 合同新窗口", l1="detail", l2="source",
                generator="trust-repair",
            ),
            project="evolvmem",
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            importance=8.0,
            confidence=0.9,
            effective_from="2026-06-01 00:00:00",
        )
    )
    ids = _as_of_ids(env, "zebra 供应商", "2026-08-01 00:00:00")
    assert ids == [new.id]
    stored = env.store.get_item(old.id, include_layers=False)
    assert stored.effective_until == "2026-12-31 00:00:00"
    assert stored.status is ContextStatus.SUPERSEDED


def test_as_of_after_delayed_backfill_successor_returns_only_successor(env):
    register(env, "evolvmem")
    later = item(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="zebra 供应商 六月生效决定", content_type=ContextContentType.DECISION,
        effective_from="2026-06-01 00:00:00", importance=8.0,
    )
    backfilled = env.store.supersede_active(
        ContextItemDraft(
            identity_key="project:evolvmem:decision:supplier",
            content_type=ContextContentType.DECISION,
            layers=ContextLayers(
                l0="zebra 供应商 三月补录事件", l1="detail", l2="source",
                generator="trust-repair",
            ),
            project="evolvmem",
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            importance=8.0,
            confidence=0.9,
            effective_from="2026-03-01 00:00:00",
            occurred_at="2026-03-01 00:00:00",
            mentioned_at="2026-10-01 00:00:00",
        )
    )
    assert _as_of_ids(env, "zebra 供应商", "2026-04-01 00:00:00") == [backfilled.id]
    assert _as_of_ids(env, "zebra 供应商", "2026-07-01 00:00:00") == [later.id]


def test_superseded_non_decision_stays_excluded_at_as_of(env):
    register(env, "evolvmem")
    fact = item(
        env, "project:evolvmem:fact:retired", project="evolvmem",
        l0="zebra 退役事实", importance=8.0,
        effective_from="2026-01-01 00:00:00",
    )
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET status='superseded' WHERE id=?", (fact.id,)
        )
    assert fact.id not in _as_of_ids(env, "zebra", "2026-03-01 00:00:00")


def test_superseded_unknown_dates_are_honestly_excluded_at_as_of(env):
    register(env, "evolvmem")
    undated = item(
        env, "project:evolvmem:decision:undated", project="evolvmem",
        l0="zebra 无日期旧决定", content_type=ContextContentType.DECISION,
    )
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET status='superseded' WHERE id=?",
            (undated.id,),
        )
    assert undated.id not in _as_of_ids(env, "zebra", "2026-03-01 00:00:00")


# ---------------------------------------------------------------------------
# 3. seed window gate + gating before truncation
# ---------------------------------------------------------------------------


def test_seed_paths_respect_the_window_gate(env):
    register(env, "evolvmem")
    future_policy = _make_item(
        env, "project:evolvmem:constraint:future", project="evolvmem",
        l0="zebra 未来生效约束", content_type=ContextContentType.CONSTRAINT,
        tier=ContextTier.PINNED, importance=9.0, confidence=0.9,
        effective_from="2027-01-01 00:00:00",
    )
    expired_summary = _make_item(
        env, "project:evolvmem:session_summary:expired", project="evolvmem",
        l0="zebra 过期会话摘要",
        content_type=ContextContentType.SESSION_SUMMARY,
        importance=8.0,
        effective_from="2026-01-01 00:00:00",
        effective_until="2026-03-01 00:00:00",
    )
    live_policy = _make_item(
        env, "project:evolvmem:constraint:live", project="evolvmem",
        l0="zebra 当前约束", content_type=ContextContentType.CONSTRAINT,
        tier=ContextTier.PINNED, importance=9.0, confidence=0.9,
    )
    for entry in (future_policy, expired_summary, live_policy):
        resolution(env, entry.id, state="resolved", review="accepted")
    session = env.service.session_start(
        ContextSessionStartRequest(project="evolvmem", query="zebra"),
        project_only=True,
    )
    assert live_policy.id in session.selected_ids
    assert future_policy.id not in session.selected_ids
    assert expired_summary.id not in session.selected_ids


def test_ownership_gate_is_applied_before_final_truncation(env):
    register(env, "evolvmem")
    held = [
        item(
            env, f"project:evolvmem:fact:held-{index}", project="evolvmem",
            l0="zebra 无归属高分条目", importance=10.0,
        )
        for index in range(12)
    ]
    trusted = item(
        env, "project:evolvmem:fact:trusted", project="evolvmem",
        l0="zebra 有归属条目", importance=1.0,
    )
    resolution(env, trusted.id, state="resolved", review="accepted")
    session = env.service.session_start(
        ContextSessionStartRequest(project="evolvmem", query="zebra"),
        project_only=True,
    )
    assert trusted.id in session.selected_ids
    assert not {entry.id for entry in held} & set(session.selected_ids)


# ---------------------------------------------------------------------------
# 4. workstream-bound review must not strand the workstream
# ---------------------------------------------------------------------------


def test_cross_project_workstream_reassignment_is_refused(tmp_path, env):
    register(env, "inventory")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, created = _create_workstream(
        env, workspace, project="evolvmem", objective="库存盘点",
        current_step="第一期", next_action="继续盘点",
    )
    before_item = env.store.get_item(created.context_id, include_layers=False)
    before_ws = env.store._connection().execute(
        "SELECT * FROM continuity_workstreams WHERE id=?",
        (created.workstream_id,),
    ).fetchone()
    before_l1 = env.store.get_layer(created.context_id, ContextLayer.L1)

    with pytest.raises(ProjectStoreError) as excinfo:
        with env.store.transaction():
            env.projects.review_item_project(
                created.context_id, "inventory", expected_revision=0
            )
    assert excinfo.value.code == "workstream_project_mismatch"

    after_item = env.store.get_item(created.context_id, include_layers=False)
    after_ws = env.store._connection().execute(
        "SELECT * FROM continuity_workstreams WHERE id=?",
        (created.workstream_id,),
    ).fetchone()
    assert after_item.project == before_item.project == "evolvmem"
    assert after_item.identity_key == before_item.identity_key
    assert after_ws["project"] == before_ws["project"] == "evolvmem"
    assert env.store.get_layer(created.context_id, ContextLayer.L1) == before_l1
    # The exact resume still finds the untouched workstream.
    continuity, _ = _continuity(env, workspace)
    resumed = continuity.resume(ContinuityResumeRequest(workspace_path=str(workspace)))
    assert resumed.code == "ok" and resumed.workstream_id == created.workstream_id


def test_same_project_confirm_and_reject_hold_a_workstream(tmp_path, env):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, created = _create_workstream(env, workspace, objective="部署脚本")
    with env.store.transaction():
        env.store._connection().execute(
            "DELETE FROM context_project_resolutions WHERE item_id=?",
            (created.context_id,),
        )
    with env.store.transaction():
        env.projects.review_item_project(
            created.context_id, "evolvmem", expected_revision=0
        )
    row = _review_row(env, created.context_id)
    assert row["review_state"] == "accepted"
    assert row["resolved_project"] == "evolvmem"
    assert env.store.get_item(created.context_id, include_layers=False).project == (
        "evolvmem"
    )
    # A reject keeps the record but holds it out of default recall.
    with env.store.transaction():
        env.projects.reject_resolution(created.context_id, expected_revision=int(row["revision"]))
    read = read_recent_project_progress(env.service, "evolvmem", max_chars=4000)
    assert created.context_id not in read.selected_ids
    held = {entry["id"]: entry["reason"] for entry in read.diagnostics["excluded"]}
    assert held[created.context_id] == "project_ownership_rejected"
    assert env.store.get_item(created.context_id, include_layers=False).project == (
        "evolvmem"
    )


def test_reviewer_surfaces_identity_conflict_instead_of_raising(tmp_path, env):
    register(env, "evolvmem")
    register(env, "inventory")
    shared_key = "project:shared:fact:same-identity"
    first = item(
        env, shared_key, project="evolvmem", l0="zebra 第一份",
    )
    item(env, shared_key, project="inventory", l0="zebra 第二份")
    with pytest.raises(ProjectStoreError) as excinfo:
        with env.store.transaction():
            env.projects.review_item_project(
                first.id, "inventory", expected_revision=0
            )
    assert excinfo.value.code == "identity_conflict"
    assert env.store.get_item(first.id, include_layers=False).project == "evolvmem"
    # The web review entry surfaces the same stable code and rolls back whole.
    from evolvmem.web_server import api_resolution_accept

    result = api_resolution_accept(
        env.service, first.id, {"project": "inventory", "expected_revision": 0}
    )
    assert result == {"ok": False, "error": "identity_conflict"}
    assert env.store.get_item(first.id, include_layers=False).project == "evolvmem"
    assert _review_row(env, first.id) is None  # the failed review left no row


# ---------------------------------------------------------------------------
# 5. absent-row CAS for the unreviewed review queue
# ---------------------------------------------------------------------------


def test_absent_row_review_uses_exact_cas(tmp_path, env):
    from evolvmem.web_server import api_resolution_accept, api_resolutions

    class _NoLegacy:
        def get_by_ids(self, ids):
            return []

    register(env, "evolvmem")
    register(env, "inventory")
    row = item(
        env, "project:inventory:checkpoint:misassigned", project="inventory",
        l0="inventory 断点",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    unreviewed = api_resolutions(_NoLegacy(), env.store, {"state": "unreviewed"})
    assert [entry["item_id"] for entry in unreviewed] == [row.id]
    assert unreviewed[0]["revision"] == 0

    stale = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 1}
    )
    assert stale == {"ok": False, "error": "resolution_not_found"}
    assert env.store.get_item(row.id, include_layers=False).project == "inventory"

    accepted = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 0}
    )
    assert accepted["ok"] is True
    assert env.store.get_item(row.id, include_layers=False).project == "evolvmem"

    duplicate = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 0}
    )
    assert duplicate == {"ok": False, "error": "revision_conflict"}
    again = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 2}
    )
    assert again == {"ok": False, "error": "revision_conflict"}


# ---------------------------------------------------------------------------
# 6. web decision-window routes
# ---------------------------------------------------------------------------


def test_web_get_decision_window_is_read_only(env):
    from evolvmem.web_server import api_decision_window_get

    register(env, "evolvmem")
    entry = item(
        env, "project:evolvmem:decision:webget", project="evolvmem",
        l0="zebra web GET 决策", content_type=ContextContentType.DECISION,
    )
    payload = api_decision_window_get(env.service, entry.id)
    assert payload["ok"] is True
    assert payload["effective_from"] is None
    item_after = env.store.get_item(entry.id, include_layers=False)
    assert item_after.effective_from is None
    assert item_after.updated_at == entry.updated_at


def test_web_post_decision_window_preserves_unspecified_fields(env):
    from evolvmem.web_server import api_decision_window

    register(env, "evolvmem")
    entry = item(
        env, "project:evolvmem:decision:webpost", project="evolvmem",
        l0="zebra web POST 决策", content_type=ContextContentType.DECISION,
        effective_until="2026-12-31 00:00:00",
        occurred_at="2026-02-01 00:00:00",
    )
    written = api_decision_window(
        env.service, {"id": entry.id, "effective_from": "2026-03-01T08:00:00+08:00"}
    )
    assert written["ok"] is True
    assert written["effective_from"] == "2026-03-01 00:00:00"
    assert written["effective_until"] == "2026-12-31 00:00:00"
    assert written["occurred_at"] == "2026-02-01 00:00:00"
    cleared = api_decision_window(
        env.service, {"id": entry.id, "occurred_at": None}
    )
    assert cleared["occurred_at"] is None
    assert cleared["effective_until"] == "2026-12-31 00:00:00"


# ---------------------------------------------------------------------------
# 7. LAN memory revision observes temporal writes
# ---------------------------------------------------------------------------


def test_temporal_write_bumps_lan_revision_and_refreshes_triggers(tmp_path):
    from tests.test_lan_sharing import settings_for
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_context import prepare_memory_revision
    from evolvmem.lan_tools import LanTools

    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    try:
        server = runtime.server_for("jiangli")
        store = server.context_service.store
        # Simulate a pre-2026-10-02 trigger definition that ignores temporal columns.
        with store.transaction():
            store._connection().execute(
                "DROP TRIGGER IF EXISTS lan_revision_context_items_update"
            )
            store._connection().execute(
                "CREATE TRIGGER lan_revision_context_items_update "
                "AFTER UPDATE OF status ON context_items "
                "WHEN NEW.status IS NOT OLD.status "
                "BEGIN UPDATE lan_memory_revision SET revision=revision+1 WHERE id=1; END"
            )
        prepare_memory_revision(server)
        sql = store._connection().execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='lan_revision_context_items_update'"
        ).fetchone()[0]
        assert "effective_from" in sql

        adapter = LanTools(runtime)
        added = adapter.call_tool(
            "jiangli", "memory_add",
            dict(key="project:lan:fact:dated", value="known date holder value",
                 request_id="add-dated"),
        )
        assert "error" not in added, added
        item_id = added["context_id"]
        before = adapter._memory_revision("jiangli")
        before_hash = store._connection().execute(
            "SELECT content_hash FROM context_layers "
            "WHERE item_id=? AND layer='l0'", (item_id,)
        ).fetchone()[0]
        written = adapter.call_tool(
            "jiangli", "context_decision_window",
            dict(id=item_id, effective_from="2026-03-01T00:00:00Z",
                 request_id="date-write"),
        )
        assert "error" not in written, written
        after = adapter._memory_revision("jiangli")
        after_hash = store._connection().execute(
            "SELECT content_hash FROM context_layers "
            "WHERE item_id=? AND layer='l0'", (item_id,)
        ).fetchone()[0]
        assert after != before, "temporal write must move the LAN memory revision"
        assert after_hash == before_hash, "content text must not change"
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 8. MCP contract exposure
# ---------------------------------------------------------------------------


def test_mcp_context_search_contract_exposes_as_of_and_temporal(env):
    from evolvmem.context_models import ContextMode
    from evolvmem.mcp_server import MemoryMCPServer
    from evolvmem.memory_store import MemoryStore

    register(env, "evolvmem", "EvolvMem")
    entry = item(
        env, "project:evolvmem:decision:mcp", project="evolvmem",
        l0="zebra MCP 决策", content_type=ContextContentType.DECISION,
        effective_from="2026-03-01 00:00:00", importance=8.0,
    )
    resolution(env, entry.id, state="resolved", review="accepted")

    env.config.context_mode = ContextMode.SHADOW.value
    env.config.adapter = "codex"
    with MemoryStore(env.config):
        pass
    server = MemoryMCPServer(config=env.config)
    server._init_done.set()
    server.context_service = env.service

    tools = {
        tool["name"]: tool
        for tool in server._handle_request(
            {"method": "tools/list", "id": 1, "jsonrpc": "2.0"}
        )["result"]["tools"]
    }
    assert "as_of" in tools["context_search"]["inputSchema"]["properties"]
    recall_schema = tools["context_project_recall"]["inputSchema"]
    assert "diagnostics" in tools["context_project_recall"]["description"]
    assert recall_schema["required"] == ["query"]

    def call(name, arguments):
        response = server._handle_request({
            "method": "tools/call", "id": 9, "jsonrpc": "2.0",
            "params": {"name": name, "arguments": arguments},
        })
        assert "error" not in response, response
        return json.loads(response["result"]["content"][0]["text"])

    recall = call("context_project_recall", {"query": "evolvmem 进展"})
    assert recall["matched_projects"] == ["evolvmem"]
    assert isinstance(recall["diagnostics"], dict)
    assert isinstance(recall["excluded"], list)
    json.dumps(recall)  # must survive MCP serialization intact
    search = call(
        "context_search",
        {"query": "zebra MCP", "project": "evolvmem", "as_of": "2026-04-01"},
    )
    assert search["as_of"] == "2026-04-01 00:00:00"
    assert [row["id"] for row in search["results"]] == [entry.id]
    assert search["results"][0]["temporal_state"] == "known"
