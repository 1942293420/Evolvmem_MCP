"""Synthetic regressions from the continuity review; no real logs or databases.

Run with the worktree on PYTHONPATH and PYTHONDONTWRITEBYTECODE=1.  All data,
workspace keys and rollout files live under pytest's temporary directory.
Only known review reproductions belong here; this file does not patch code.

主控在 Kimi review3 运行期间加入这份独立审查合成回归，属于本轮已批准验收，
请在最终回归中一并通过。当前 10 passed / 6 failed：begin 重放不能改较新焦点，
追加结果应清除旧 blockers，超长完整追加推进 offset，拒绝文件不能饿死旧日志，
turn_context 改工作区须识别混合来源，state 不保存原始敏感用户文本。
剩余明确要求：固定单文件读取字节/事件预算及有界 state，避免全文件 events/calls；
保存来源 session/行或事件 ID 到断点可追溯位置，不能只有没有 session 的 L 数字。
这些只是原验收的具体复现，不增加服务/依赖或新的业务范围。
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextLayer
from evolvmem.context_store import ContextStore
from evolvmem.continuity_backfill import MAX_LINE_BYTES, run_backfill
from evolvmem.continuity_models import (
    ContinuityBeginRequest,
    ContinuityCheckpointRequest,
    ContinuityError,
    ContinuityFindRequest,
    ContinuityResumeRequest,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_store import ProjectStore, ProjectStoreError
from evolvmem.workspace_identity import WorkspaceIdentityProvider


@dataclass
class ReviewEnv:
    root: Path
    workspace: Path
    sessions: Path
    state_path: Path
    store: ContextStore
    provider: WorkspaceIdentityProvider
    service: ContinuityService
    project: str = "review-project"

    def begin(self, objective, **options):
        values = {
            "workspace_path": str(self.workspace),
            "project": self.project,
            "objective": objective,
        }
        values.update(options)
        return self.service.begin(ContinuityBeginRequest(**values))

    def checkpoint(self, previous=None, *, action="update", **options):
        values = {
            "action": action,
            "workspace_path": str(self.workspace),
            "project_hint": self.project,
        }
        if previous is not None:
            values.update(
                workstream_id=previous.workstream_id,
                expected_checkpoint_revision=previous.checkpoint_revision,
                expected_state_version=previous.state_version,
            )
        values.update(options)
        return self.service.checkpoint(ContinuityCheckpointRequest(**values))

    def resume(self, project=None):
        return self.service.resume(ContinuityResumeRequest(
            workspace_path=str(self.workspace),
            project_hint=project or self.project,
        ))

    def rows(self):
        return self.store._connection().execute(
            "SELECT * FROM continuity_workstreams ORDER BY id"
        ).fetchall()

    def payload(self, workstream_id):
        row = self.store._connection().execute(
            "SELECT current_context_id FROM continuity_workstreams WHERE id=?",
            (workstream_id,),
        ).fetchone()
        assert row is not None, f"Missing workstream {workstream_id}"
        return json.loads(self.store.get_layer(row["current_context_id"], ContextLayer.L2))

    def rollout(self, events, *, mtime=100):
        session_id = str(uuid.uuid4())
        path = self.sessions / f"rollout-2026-09-08T10-00-00-{session_id}.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(self.workspace),
                "source": "cli",
                "timestamp": "2026-09-08T10:00:00Z",
            },
        }
        path.write_text(
            "".join(json.dumps(event) + "\n" for event in [meta, *events]),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return session_id, path

    def backfill(self, **options):
        values = {
            "sessions_root": self.sessions,
            "project": self.project,
            "workspace_path": str(self.workspace),
            "state_path": self.state_path,
            "apply": True,
            "idle_minutes": 0,
            "now": 10000,
        }
        values.update(options)
        return run_backfill(self.store, self.provider, **values)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Config honours this environment override even with an explicit data_dir.
    monkeypatch.delenv("EVOLVMEM_DATA_DIR", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = tmp_path / "synthetic-sessions"
    sessions.mkdir()
    config = Config(data_dir=tmp_path / "synthetic-memory")
    assert config.data_dir.is_relative_to(tmp_path)
    provider = WorkspaceIdentityProvider(config.data_dir / "workspace.key")
    provider.bootstrap_key()
    with ContextStore(config) as store:
        yield ReviewEnv(
            tmp_path, workspace, sessions, tmp_path / "backfill-state.json",
            store, provider, ContinuityService(config, store, provider),
        )


def _project_store(env) -> ProjectStore:
    return ProjectStore(
        env.store._connection(), env.store._require_transaction, generic_names=()
    )


def _merge_to_canonical(env, source, canonical, alias):
    """Replay the coordinator's evidence-backed merge shape on temp data.

    Data setup, not a begin call: the source registry row stays as an archived
    audit record, while the alias, the workstreams, the focus row and the
    workspace binding all point at the canonical project.
    """
    conn = env.store._connection()
    with env.store.transaction():
        projects = _project_store(env)
        projects.register_project(canonical)
        projects.add_alias(alias, canonical)
        for table in (
            "continuity_workstreams",
            "continuity_focus",
            "context_project_workspace_bindings",
        ):
            conn.execute(
                f"UPDATE {table} SET project=? WHERE project=?",
                (canonical, source),
            )
        row = conn.execute(
            "SELECT revision FROM context_project_registry WHERE project=?",
            (source,),
        ).fetchone()
        projects.archive_project(source, expected_revision=int(row["revision"]))


def _registry(env):
    return {
        row["project"]: row["status"]
        for row in env.store._connection().execute(
            "SELECT project, status FROM context_project_registry"
        )
    }


def _bindings(env):
    return [
        (row["project"], row["state"])
        for row in env.store._connection().execute(
            "SELECT project, state FROM context_project_workspace_bindings"
        )
    ]


def user(text):
    return {
        "type": "response_item",
        "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def call(call_id, name="functions.exec_command"):
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call", "call_id": call_id,
            "name": name, "arguments": '{"cmd":"printf synthetic"}',
        },
    }


def output(call_id):
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output", "call_id": call_id,
            "output": (
                "Chunk ID: synthetic\nWall time: 0.1 seconds\n"
                "Process exited with code 0\nFinal output:\nsynthetic check passed\n"
            ),
        },
    }


def append_event(path, event):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    os.utime(path, (100, 100))


def test_begin_replay_preserves_newer_verified_progress(env):
    initial = env.begin(
        "Implement approvals", completed_steps=("Initial inspection",),
        current_step="Implementation", next_action="Write tests",
    )
    latest = env.checkpoint(
        initial, completed_steps=("Initial inspection", "Approval tests passed"),
        current_step="Release preparation", next_action="Deploy verified changes",
    )
    replay = env.begin(
        "Implement approvals", completed_steps=("Initial inspection",),
        current_step="Implementation", next_action="Write tests",
    )
    actual = env.payload(initial.workstream_id)
    assert replay.workstream_id == initial.workstream_id
    assert actual["completed_steps"] == ["Initial inspection", "Approval tests passed"]
    assert actual["current_step"] == "Release preparation"
    assert actual["next_action"] == "Deploy verified changes"
    assert replay.checkpoint_revision == latest.checkpoint_revision


def test_begin_new_objective_does_not_overwrite_focused_task(env):
    first = env.begin("Task A", completed_steps=("A tests passed",))
    second = env.begin("Task B", next_action="Implement B")
    assert second.workstream_id != first.workstream_id
    assert env.payload(first.workstream_id)["objective"] == "Task A"
    assert env.payload(second.workstream_id)["objective"] == "Task B"
    assert "A tests passed" not in env.payload(second.workstream_id)["completed_steps"]


def test_begin_replay_keeps_task_identity_and_newer_focus(env):
    first = env.begin("Task A", next_action="Implement A")
    second = env.checkpoint(
        action="create", objective="Task B", completed_steps=("B tests passed",),
        make_focus=True, expected_focus_revision=first.focus_revision,
    )
    replay = env.begin("Task A", next_action="Implement A")
    assert replay.workstream_id == first.workstream_id
    assert env.payload(second.workstream_id)["objective"] == "Task B"
    assert env.payload(second.workstream_id)["completed_steps"] == ["B tests passed"]
    resumed = env.resume()
    assert resumed.workstream_id == second.workstream_id
    assert resumed.focus_revision == second.focus_revision


def test_alias_cannot_redirect_an_existing_canonical_project(env):
    original = env.begin("Alpha task", project="alpha")
    try:
        env.begin("Beta task", project="beta", alias="alpha")
    except (ContinuityError, ProjectStoreError):
        pass  # Safe rejection is acceptable; no particular error code is required.
    resumed = env.resume("alpha")
    assert resumed.workstream_id == original.workstream_id
    assert resumed.checkpoint["project"] == "alpha"


def test_project_declaration_cannot_duplicate_an_existing_alias(env):
    original = env.begin("Alpha task", project="alpha", alias="approval")
    try:
        env.begin("Alpha task", project="approval")
    except (ContinuityError, ProjectStoreError):
        pass
    projects = [row[0] for row in env.store._connection().execute(
        "SELECT project FROM context_project_registry"
    )]
    assert len(projects) == 1, f"Alias created a second project: {projects}"
    assert env.resume("approval").workstream_id == original.workstream_id


def test_case_variant_registration_preserves_project_identity(env):
    original = env.begin("Alpha task", project="Alpha")
    other = env.root / "second-workspace"
    other.mkdir()
    try:
        env.begin("Second workspace task", project="alpha", workspace_path=str(other))
    except (ContinuityError, ProjectStoreError):
        pass
    projects = [row[0] for row in env.store._connection().execute(
        "SELECT project FROM context_project_registry"
    )]
    assert len(projects) == 1, f"Case variant created conflicting projects: {projects}"
    assert env.resume("Alpha").workstream_id == original.workstream_id


def test_begin_resolves_archived_alias_to_active_canonical(env):
    original = env.begin(
        "实现AI采购审批", project="AI采购",
        completed_steps=("梳理采购审批字段",), next_action="接通审批流",
    )
    _merge_to_canonical(env, source="AI采购", canonical="ai_purchase", alias="AI采购")

    replayed = env.begin("实现AI采购审批", project="AI采购")

    assert replayed.project == "ai_purchase"
    assert replayed.workstream_id == original.workstream_id
    assert replayed.created is False
    assert replayed.registered is False
    assert replayed.alias_added is False
    assert replayed.checkpoint_revision == original.checkpoint_revision
    assert replayed.focus_revision == original.focus_revision
    assert env.payload(original.workstream_id)["objective"] == "实现AI采购审批"
    # 旧登记行只作为 archived 审计记录保留，不得再生出同名 active 项目
    assert _registry(env) == {"AI采购": "archived", "ai_purchase": "active"}
    # 原有绑定迁移后即复用，begin 不新增绑定
    assert _bindings(env) == [("ai_purchase", "active")]
    rows = env.rows()
    assert len(rows) == 1, "begin recreated a workstream instead of reusing it"
    assert rows[0]["id"] == original.workstream_id
    assert rows[0]["project"] == "ai_purchase"
    assert rows[0]["workspace_fingerprint"] == env.provider.resolve(
        str(env.workspace)
    ).fingerprint

    # 同一声明再带上原中文别名：该别名本就属于这个 canonical，不冲突不新增
    replayed_with_alias = env.begin(
        "实现AI采购审批", project="AI采购", alias="AI采购"
    )
    assert replayed_with_alias.project == "ai_purchase"
    assert replayed_with_alias.workstream_id == original.workstream_id
    assert replayed_with_alias.alias_added is False
    assert _registry(env) == {"AI采购": "archived", "ai_purchase": "active"}
    assert _bindings(env) == [("ai_purchase", "active")]


def test_begin_via_resolved_alias_registers_new_alias_on_canonical(env):
    env.begin("实现AI采购审批", project="AI采购")
    _merge_to_canonical(env, source="AI采购", canonical="ai_purchase", alias="AI采购")

    result = env.begin("实现AI采购审批", project="AI采购", alias="采购")

    assert result.project == "ai_purchase"
    assert result.alias_added is True, "新别名必须挂在 canonical 上而非声明名上"
    aliases = {
        row["alias"]: row["project"]
        for row in env.store._connection().execute(
            "SELECT alias, project FROM context_project_aliases"
        )
    }
    assert aliases == {"AI采购": "ai_purchase", "采购": "ai_purchase"}


def test_begin_resolves_registered_english_alias_to_canonical(env):
    original = env.begin("Implement approvals", project="alpha", alias="approval")

    replayed = env.begin("Implement approvals", project="approval")

    assert replayed.project == "alpha"
    assert replayed.workstream_id == original.workstream_id
    assert replayed.created is False
    assert replayed.registered is False
    assert _registry(env) == {"alpha": "active"}
    assert len(env.rows()) == 1


def test_begin_rejects_alias_conflicting_with_same_named_active_canonical(env):
    env.begin("Alpha task", project="AI采购")
    env.begin("Purchase task", project="ai_purchase")
    with env.store.transaction():
        _project_store(env).add_alias("AI采购", "ai_purchase")

    with pytest.raises(ContinuityError) as failure:
        env.begin("Third task", project="AI采购")

    assert failure.value.code == "alias_conflict", (
        "同名 active canonical 与指向他处的 alias 是冲突，必须报错而不是猜"
    )
    assert _registry(env) == {"AI采购": "active", "ai_purchase": "active"}
    assert len(env.rows()) == 2, "被拒绝的 begin 不得留下半登记状态"


def test_begin_rejects_alias_to_archived_target(env):
    env.begin("Purchase task", project="ai_purchase")
    conn = env.store._connection()
    with env.store.transaction():
        projects = _project_store(env)
        projects.add_alias("AI采购", "ai_purchase")
        row = conn.execute(
            "SELECT revision FROM context_project_registry WHERE project='ai_purchase'"
        ).fetchone()
        projects.archive_project("ai_purchase", expected_revision=int(row["revision"]))

    with pytest.raises(ContinuityError) as failure:
        env.begin("New task", project="AI采购")

    assert failure.value.code == "project_archived"
    assert _registry(env) == {"ai_purchase": "archived"}, "别名不得回退注册同名项目"
    assert len(env.rows()) == 1


def test_begin_rejects_alias_with_missing_target(env):
    env.begin("Purchase task", project="ai_purchase")
    with env.store.transaction():
        _project_store(env).add_alias("AI采购", "ai_purchase")
    # 模拟离线维护清掉了目标登记行：别名悬空时绝不能回退注册
    raw = sqlite3.connect(str(env.store.config.db_path))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM context_project_registry WHERE project='ai_purchase'")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(ContinuityError) as failure:
        env.begin("New task", project="AI采购")

    assert failure.value.code == "alias_conflict"
    assert _registry(env) == {}, "悬空别名不得回退注册同名项目"
    assert len(env.rows()) == 1


def test_begin_via_alias_with_new_objective_creates_task_in_canonical(env):
    original = env.begin("实现AI采购审批", project="AI采购")
    _merge_to_canonical(env, source="AI采购", canonical="ai_purchase", alias="AI采购")

    created = env.begin("实现AI采购对账", project="AI采购")

    assert created.project == "ai_purchase"
    assert created.workstream_id != original.workstream_id
    assert created.created is True
    assert _registry(env) == {"AI采购": "archived", "ai_purchase": "active"}
    assert _bindings(env) == [("ai_purchase", "active")]
    assert {row["project"] for row in env.rows()} == {"ai_purchase"}
    assert env.payload(created.workstream_id)["objective"] == "实现AI采购对账"
    assert env.payload(original.workstream_id)["objective"] == "实现AI采购审批"


def test_begin_without_alias_keeps_existing_registration_behavior(env):
    created = env.begin("中文任务", project="中文项目")

    assert created.project == "中文项目"
    assert created.registered is True
    assert _registry(env) == {"中文项目": "active"}

    conn = env.store._connection()
    with env.store.transaction():
        row = conn.execute(
            "SELECT revision FROM context_project_registry WHERE project='中文项目'"
        ).fetchone()
        _project_store(env).archive_project(
            "中文项目", expected_revision=int(row["revision"])
        )

    with pytest.raises(ContinuityError) as failure:
        env.begin("中文任务", project="中文项目")

    assert failure.value.code == "project_archived", "无别名时 archived 同名项目照旧拒绝"


def test_begin_never_fuzzy_matches_a_merged_alias(env):
    env.begin("实现AI采购审批", project="AI采购")
    _merge_to_canonical(env, source="AI采购", canonical="ai_purchase", alias="AI采购")

    unrelated = env.begin("无关任务", project="AI采购部")

    assert unrelated.project == "AI采购部"
    assert unrelated.registered is True
    assert _registry(env) == {
        "AI采购": "archived",
        "ai_purchase": "active",
        "AI采购部": "active",
    }


def test_begin_request_alias_cannot_steal_another_projects_alias(env):
    env.begin("Alpha task", project="alpha", alias="approval")

    with pytest.raises(ContinuityError) as failure:
        env.begin("Beta task", project="beta", alias="approval")

    assert failure.value.code == "alias_conflict"
    assert _registry(env) == {"alpha": "active"}, "被拒绝的 begin 不得半登记 beta"
    assert len(env.rows()) == 1


@pytest.mark.parametrize("action,status", [("complete", "completed"), ("cancel", "cancelled")])
def test_begin_replay_does_not_recreate_a_terminal_task(env, action, status):
    initial = env.begin("Implement approvals", next_action="Implement")
    env.checkpoint(initial, action=action)
    try:
        env.begin("Implement approvals", next_action="Implement")
    except (ContinuityError, ProjectStoreError):
        pass  # Returning the terminal task or refusing a replay are both safe.
    rows = env.rows()
    assert len(rows) == 1, "Same begin request recreated a terminal task"
    assert rows[0]["id"] == initial.workstream_id
    assert rows[0]["status"] == status


def test_find_limit_does_not_turn_multiple_matches_into_unique_match(env):
    env.begin("API work", project="approval-api")
    other = env.root / "ui-workspace"
    other.mkdir()
    env.begin("UI work", project="approval-ui", workspace_path=str(other))
    found = env.service.find(ContinuityFindRequest(query="approval", limit=1))
    assert found.code == "ambiguous"
    assert found.checkpoint is None
    assert len(found.candidates) <= 1


@pytest.mark.parametrize("unrelated_count", [1, 10])
def test_find_keyword_returns_matching_task_despite_unrelated_tasks(env, unrelated_count):
    target = env.begin("Unique needle task", make_focus=False)
    for index in range(unrelated_count):
        env.checkpoint(action="create", objective=f"Unrelated bookkeeping {index}")
    # Simulate normal elapsed time without sleeping or relying on random ID order.
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE continuity_workstreams SET updated_at='2000-01-01 00:00:00' WHERE id=?",
            (target.workstream_id,),
        )
    found = env.service.find(ContinuityFindRequest(query="needle"))
    visible_ids = {
        task.workstream_id for project in found.candidates for task in project.workstreams
    }
    assert target.workstream_id in visible_ids, "The task that matched was omitted"
    assert found.code == "ok"
    assert found.checkpoint["workstream_id"] == target.workstream_id


def test_backfill_output_append_clears_resolved_blocker(env):
    _, path = env.rollout([user("Implement approvals"), call("c1")])
    first = env.backfill()
    assert len(env.rows()) == 1
    task_id = first[0].workstream_id
    assert env.payload(task_id)["blockers"], "Fixture must begin with a pending call"
    append_event(path, output("c1"))
    env.backfill()
    actual = env.payload(task_id)
    assert actual["blockers"] == [], "Resolved call remains blocked after its output arrives"
    assert actual["completed_steps"]
    assert len(env.rows()) == 1


def test_backfill_oversized_complete_append_advances_scan_position(env):
    session_id, path = env.rollout([user("Implement approvals")])
    env.backfill()
    before = json.loads(env.state_path.read_text())[session_id]["offset"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("x" * (MAX_LINE_BYTES + 1) + "\n")
    os.utime(path, (100, 100))
    env.backfill()
    after = json.loads(env.state_path.read_text())[session_id]["offset"]
    assert after > before, "Complete oversized append will be reread on every scan"


def test_rejected_newest_rollout_does_not_starve_an_older_valid_task(env):
    env.rollout([user("Valid older task")], mtime=90)
    env.rollout([user("diff --git a/foo b/foo")], mtime=100)
    for _ in range(3):
        env.backfill(max_per_run=1)
    objectives = [env.payload(row["id"])["objective"] for row in env.rows()]
    assert "Valid older task" in objectives, "Newest rejected source monopolized scan quota"


def test_mixed_turn_context_does_not_attach_foreign_progress_to_initial_project(env):
    other = env.root / "other-project"
    other.mkdir()
    env.rollout([
        user("Implement Project A"),
        {"type": "turn_context", "payload": {"cwd": str(other)}},
        user("Switch to Project B and fix its export"),
        call("b1", name="other_project_export"),
        output("b1"),
    ])
    reports = env.backfill()
    assert reports
    for row in env.rows():
        if row["project"] == env.project:
            payload = env.payload(row["id"])
            assert "other_project_export" not in json.dumps(payload), (
                "Project B progress was imported as Project A work"
            )


def test_backfill_state_does_not_persist_raw_sensitive_user_content(env):
    marker = "sk-review-only-not-real-1234567890"
    env.rollout([user(f"Use api_key = {marker} to complete setup")])
    reports = env.backfill()
    assert reports
    state_text = env.state_path.read_text() if env.state_path.exists() else ""
    assert marker not in state_text, "Raw user credential was copied into scan state"
    for row in env.rows():
        assert marker not in json.dumps(env.payload(row["id"]))
