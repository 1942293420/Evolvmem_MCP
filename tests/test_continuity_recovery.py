"""中断恢复回归：continuity_begin / continuity_find 与跨进程恢复。

红→绿顺序（docs/2026-09-09-continuity-recovery.md）：
  1. begin：显式项目声明下幂等登记、绑定并创建/更新 focus 任务；重复调用
     不产生重复项目/别名/任务；无声明或路径形项目名一律拒绝。
  2. find：按项目名/中文别名/任务关键词从通用目录发现未完成任务；唯一
     候选附可回读 checkpoint；多候选给有限列表且绝不切换 focus；失败
     分类区分未登记与无任务。
  3. 跨进程：begin→已验证步骤→进程结束→新进程按别名发现/恢复同一任务。

测试只用 test_config 临时目录与 tmp_path fixture，绝不碰真实库。
"""

import json

import pytest

from evolvmem.mcp_contract import (
    _PRIMARY_INSTRUCTIONS,
    _PRIMARY_INSTRUCTIONS_KIMI,
)
from evolvmem.mcp_server import _CONTEXT_ERROR_MESSAGES

from tests.test_continuity_mcp import (  # noqa: F401  (fixture re-export)
    _bind,
    _bootstrap_key,
    _call,
    _make_server,
    _tools_list,
    git_workspace,
)


@pytest.fixture
def server(test_config):
    return _make_server(test_config, mode="compat", adapter="kimi")


@pytest.fixture
def ready_server(test_config, git_workspace):
    """key 已 bootstrap 但尚未登记任何项目的 compat/kimi server。"""
    server = _make_server(test_config, mode="compat", adapter="kimi")
    _bootstrap_key(test_config)
    return server


def _begin_args(workspace, **overrides):
    args = {
        "workspace_path": str(workspace),
        "project": "权限审批",
        "alias": "审批",
        "objective": "完成审批链重构",
        "next_action": "先写失败回归",
    }
    args.update(overrides)
    return args


def _begin(server, workspace, **overrides):
    result, payload = _call(
        server, "continuity_begin", _begin_args(workspace, **overrides)
    )
    assert "isError" not in result, payload
    return payload


# ---- 工具列出与指令 ----


def test_begin_and_find_listed_for_cutover_adapters(test_config):
    for mode in ("compat", "shadow", "primary"):
        for adapter in ("codex", "kimi", "dsh"):
            server = _make_server(test_config, mode=mode, adapter=adapter)
            names = {tool["name"] for tool in _tools_list(server)}
            assert {"continuity_begin", "continuity_find"} <= names, (
                mode,
                adapter,
            )


def test_instructions_cover_begin_and_find():
    for text in (_PRIMARY_INSTRUCTIONS, _PRIMARY_INSTRUCTIONS_KIMI):
        assert "continuity_begin" in text
        assert "continuity_find" in text
        assert "make_focus=true" in text  # 既有冻结段不变


# ---- begin：登记 + 绑定 + 任务闭环 ----


def test_begin_registers_binds_and_creates_focused_workstream(
    ready_server, git_workspace
):
    created = _begin(ready_server, git_workspace)
    assert created["project"] == "权限审批"
    assert created["created"] is True
    assert created["registered"] is True
    assert created["alias_added"] is True
    assert created["bound"] is True
    assert created["workstream_id"].startswith("ws_")
    assert created["status"] == "open"
    assert created["checkpoint_revision"] == 1
    assert created["focus_revision"] == 1

    _, resumed = _call(
        ready_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "ok"
    assert resumed["workstream_id"] == created["workstream_id"]
    assert "完成审批链重构" in resumed["checkpoint"]["l1"]


def test_begin_is_idempotent_across_repeated_calls(ready_server, git_workspace):
    first = _begin(ready_server, git_workspace)
    second = _begin(ready_server, git_workspace)
    assert second["created"] is False
    assert second["registered"] is False
    assert second["alias_added"] is False
    assert second["workstream_id"] == first["workstream_id"]
    # 无新内容：不产生新 checkpoint 版本
    assert second["checkpoint_revision"] == first["checkpoint_revision"]

    store = ready_server.context_service.store
    conn = store._connection()
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_project_registry"
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_project_aliases"
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_project_workspace_bindings"
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM continuity_workstreams"
    ).fetchone()["c"] == 1


def test_begin_reuses_unfocused_workstream_with_same_objective(
    ready_server, git_workspace
):
    first = _begin(ready_server, git_workspace, make_focus=False)
    assert first["created"] is True
    second = _begin(ready_server, git_workspace, make_focus=True)
    assert second["created"] is False
    assert second["workstream_id"] == first["workstream_id"]
    _, listed = _call(
        ready_server,
        "continuity_list",
        {"workspace_path": str(git_workspace)},
    )
    assert listed["count"] == 1
    _, resumed = _call(
        ready_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "ok"
    assert resumed["workstream_id"] == first["workstream_id"]


def test_begin_rejects_alias_conflict(ready_server, git_workspace, tmp_path):
    _begin(ready_server, git_workspace)
    other = tmp_path / "other"
    other.mkdir()
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(other, project="另一个项目"),  # alias 仍用「审批」
    )
    assert result["isError"] is True
    assert payload["error"] == "alias_conflict"


@pytest.mark.parametrize(
    "project",
    ["", "  ", "/abs/path", "~/home", "a/b", "a\\b"],
)
def test_begin_rejects_missing_or_path_shaped_project(
    ready_server, git_workspace, project
):
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(git_workspace, project=project, alias=""),
    )
    assert result["isError"] is True
    assert payload["error"] in ("invalid_arguments", "invalid_project_name")


def test_begin_error_codes_have_stable_messages():
    for code in (
        "invalid_project_name",
        "alias_conflict",
        "project_archived",
        "project_not_registered",
        "no_open_workstream",
    ):
        message = _CONTEXT_ERROR_MESSAGES.get(code)
        assert isinstance(message, str) and message.strip(), code
        assert "Traceback" not in message


# ---- find：分层发现 ----


def test_find_by_alias_from_generic_directory(ready_server, git_workspace, tmp_path):
    created = _begin(ready_server, git_workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result, found = _call(
        ready_server,
        "continuity_find",
        {"query": "审批", "workspace_path": str(elsewhere)},
    )
    assert "isError" not in result
    assert found["code"] == "ok"
    assert len(found["candidates"]) == 1
    candidate = found["candidates"][0]
    assert candidate["project"] == "权限审批"
    assert "alias" in candidate["matched_via"]
    assert candidate["workstreams"][0]["workstream_id"] == created["workstream_id"]
    # 通用目录 ≠ 任务工作区：如实标注，不假装已核验
    assert candidate["workstreams"][0]["workspace_match"] is False
    checkpoint = found["checkpoint"]
    assert checkpoint is not None
    assert checkpoint["workstream_id"] == created["workstream_id"]
    assert "完成审批链重构" in checkpoint["l1"]
    assert "l2" not in checkpoint
    rendered = json.dumps(found, ensure_ascii=False)
    assert str(git_workspace) not in rendered


def test_find_by_task_keyword(ready_server, git_workspace):
    created = _begin(ready_server, git_workspace)
    _, found = _call(ready_server, "continuity_find", {"query": "审批链"})
    assert found["code"] == "ok"
    candidate = found["candidates"][0]
    assert "task" in candidate["matched_via"]
    assert candidate["workstreams"][0]["workstream_id"] == created["workstream_id"]


def test_find_ambiguous_lists_candidates_without_touching_focus(
    ready_server, git_workspace, tmp_path
):
    focused = _begin(
        ready_server, git_workspace, project="审批-后端", alias="后端审批"
    )
    other = tmp_path / "frontend"
    other.mkdir()
    _begin(
        ready_server,
        other,
        project="审批-前端",
        alias="前端审批",
        objective="完成审批前端页面",
    )
    _, found = _call(ready_server, "continuity_find", {"query": "审批"})
    assert found["code"] == "ambiguous"
    assert found["checkpoint"] is None
    projects = {c["project"] for c in found["candidates"]}
    assert projects == {"审批-后端", "审批-前端"}
    # 发现绝不切换其他项目 focus
    _, resumed = _call(
        ready_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "ok"
    assert resumed["workstream_id"] == focused["workstream_id"]


def test_find_distinguishes_unregistered_and_taskless(
    ready_server, git_workspace
):
    _, found = _call(ready_server, "continuity_find", {"query": "从未登记"})
    assert found["code"] == "project_not_registered"
    assert found["candidates"] == []

    # 已登记但任务进入终态 → no_open_workstream，且终态任务不出现在候选里
    created = _begin(ready_server, git_workspace)
    _call(
        ready_server,
        "continuity_checkpoint",
        {
            "action": "complete",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    _, found = _call(ready_server, "continuity_find", {"query": "审批"})
    assert found["code"] == "no_open_workstream"
    assert found["checkpoint"] is None
    for candidate in found["candidates"]:
        assert candidate["workstreams"] == []


def test_find_argument_validation_is_stable(server):
    for args in (
        {},
        {"query": 7},
        {"query": "x", "bogus": 1},
        {"query": "x", "limit": 0},
    ):
        result, payload = _call(server, "continuity_find", args)
        assert result["isError"] is True
        assert payload["error"] == "invalid_arguments", args


def test_begin_argument_validation_is_stable(server):
    for args in (
        {},
        {"workspace_path": "/tmp"},  # 缺 project：不允许凭 cwd 猜项目
        {"project": "p"},  # 缺 workspace_path
        {"workspace_path": "/tmp", "project": "p", "bogus": 1},
        {"workspace_path": "/tmp", "project": "p", "make_focus": 1},
    ):
        result, payload = _call(server, "continuity_begin", args)
        assert result["isError"] is True
        assert payload["error"] == "invalid_arguments", args


# ---- 跨进程恢复 ----


def test_fresh_server_objects_recover_via_alias(test_config, git_workspace):
    """对象级新客户端恢复（同进程新 server/service 对象，非强退进程）。

    真实 OS 进程 SIGKILL(-9) 再按别名找回由主控用两个独立 MCP 子进程
    另行验证；本测试只覆盖对象级 shutdown/重建后的数据可读性与 CAS 续写。
    """
    server1 = _make_server(test_config, mode="compat", adapter="kimi")
    _bootstrap_key(test_config)
    created = _begin(server1, git_workspace)
    _, updated = _call(
        server1,
        "continuity_checkpoint",
        {
            "action": "update",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "completed_steps": ["回归测试已写"],
            "current_step": "实现域层",
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    assert updated["checkpoint_revision"] == 2
    server1.shutdown()  # 强制结束工作进程

    # 新 MCP 客户端（全新进程对象）仅凭别名发现
    server2 = _make_server(test_config, mode="compat", adapter="kimi")
    _, found = _call(server2, "continuity_find", {"query": "审批"})
    assert found["code"] == "ok"
    assert found["checkpoint"]["workstream_id"] == created["workstream_id"]
    assert "回归测试已写" in found["checkpoint"]["l1"]

    _, resumed = _call(
        server2,
        "continuity_resume",
        {"workspace_path": str(git_workspace), "project_hint": "审批"},
    )
    assert resumed["code"] == "ok"
    assert resumed["workstream_id"] == created["workstream_id"]
    assert resumed["checkpoint_revision"] == 2
    assert "实现域层" in resumed["checkpoint"]["l1"]
    assert "回归测试已写" in resumed["checkpoint"]["l1"]

    # 恢复后续写：沿用同一 CAS 协议推进 revision
    _, continued = _call(
        server2,
        "continuity_checkpoint",
        {
            "action": "update",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "current_step": "续写实现",
            "expected_checkpoint_revision": 2,
            "expected_state_version": 2,
        },
    )
    assert continued["checkpoint_revision"] == 3
    server2.shutdown()


# ---- 审查回归（2026-09-09 只读审查复现，先红后绿） ----


def test_begin_replay_does_not_overwrite_newer_progress(
    ready_server, git_workspace
):
    """重放相同 begin 命中已有任务：只读回，绝不拿旧参数覆盖新 checkpoint。"""
    created = _begin(
        ready_server,
        git_workspace,
        current_step="start",
        next_action="implement",
        completed_steps=["initial"],
    )
    _call(
        ready_server,
        "continuity_checkpoint",
        {
            "action": "update",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "completed_steps": ["initial", "tests passed"],
            "current_step": "release",
            "next_action": "deploy",
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    replayed = _begin(
        ready_server,
        git_workspace,
        current_step="start",
        next_action="implement",
        completed_steps=["initial"],
    )
    assert replayed["created"] is False
    assert replayed["workstream_id"] == created["workstream_id"]
    assert replayed["checkpoint_revision"] == 2  # 不回退到 revision 3 覆盖
    _, resumed = _call(
        ready_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    l1 = resumed["checkpoint"]["l1"]
    assert "release" in l1
    assert "tests passed" in l1
    assert "start" not in l1


def test_begin_matches_objective_not_focus(ready_server, git_workspace):
    """焦点不是任务身份：不同目标各自建任务，重放旧目标不覆盖新目标。"""
    first = _begin(
        ready_server, git_workspace,
        objective="目标甲", completed_steps=["甲已完成"], make_focus=True,
    )
    second = _begin(
        ready_server, git_workspace,
        objective="目标乙", completed_steps=["乙已完成"], make_focus=True,
    )
    assert second["created"] is True
    assert second["workstream_id"] != first["workstream_id"]
    # 重放甲：命中甲的原任务读回，不得把乙的内容改写成甲
    replayed = _begin(
        ready_server, git_workspace,
        objective="目标甲", completed_steps=["甲已完成"], make_focus=False,
    )
    assert replayed["created"] is False
    assert replayed["workstream_id"] == first["workstream_id"]
    store = ready_server.context_service.store
    row_b = store._connection().execute(
        "SELECT current_context_id FROM continuity_workstreams WHERE id=?",
        (second["workstream_id"],),
    ).fetchone()
    from evolvmem.context_models import ContextLayer

    payload_b = json.loads(
        store.get_layer(int(row_b["current_context_id"]), ContextLayer.L2)
    )
    assert payload_b["objective"] == "目标乙"
    assert payload_b["completed_steps"] == ["乙已完成"]
    _, listed = _call(
        ready_server,
        "continuity_list",
        {"workspace_path": str(git_workspace)},
    )
    assert listed["count"] == 2


def test_begin_content_rejection_leaves_no_registration(
    ready_server, git_workspace
):
    """内容策略拒绝必须原子：不留项目/别名/绑定/空焦点的半完成登记。"""
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(git_workspace, objective="a" * 2001),
    )
    assert result["isError"] is True
    assert payload["error"] == "content_rejected"
    conn = ready_server.context_service.store._connection()
    for table in (
        "context_project_registry",
        "context_project_aliases",
        "context_project_workspace_bindings",
        "continuity_focus",
        "continuity_workstreams",
    ):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()[
            "c"
        ] == 0, table


def test_begin_rejects_shared_namespace_conflicts(
    ready_server, git_workspace, tmp_path
):
    """canonical 与 alias 共享规范化命名空间（大小写与 resolver 一致）。"""
    _begin(ready_server, git_workspace, project="alpha", alias="")
    other = tmp_path / "other"
    other.mkdir()
    # 别名撞别人的 canonical 名
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(other, project="beta", alias="alpha"),
    )
    assert result["isError"] is True
    assert payload["error"] == "alias_conflict"
    # 冲突失败无副作用：beta 未登记
    conn = ready_server.context_service.store._connection()
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_project_registry"
    ).fetchone()["c"] == 1
    # 项目名撞已有别名
    _begin(ready_server, other, project="gamma", alias="审批别名")
    third = tmp_path / "third"
    third.mkdir()
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(third, project="审批别名", alias=""),
    )
    assert result["isError"] is True
    assert payload["error"] == "alias_conflict"
    # 大小写变体项目名：resolver 只认一个，注册即冲突
    result, payload = _call(
        ready_server,
        "continuity_begin",
        _begin_args(third, project="Alpha", alias=""),
    )
    assert result["isError"] is True
    assert payload["error"] == "alias_conflict"
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_project_registry"
    ).fetchone()["c"] == 2  # 只有 alpha/gamma


def test_find_limit_never_turns_ambiguity_into_unique(
    ready_server, git_workspace, tmp_path
):
    """先判定真实歧义再截断输出：limit 不能把多候选变成唯一。"""
    _begin(ready_server, git_workspace, project="审批-后端", alias="")
    other = tmp_path / "frontend"
    other.mkdir()
    _begin(ready_server, other, project="审批-前端", alias="",
           objective="完成审批前端页面")
    _, found = _call(
        ready_server, "continuity_find", {"query": "审批", "limit": 1}
    )
    assert found["code"] == "ambiguous"
    assert found["checkpoint"] is None
    assert len(found["candidates"]) == 1  # 输出截断，判定不截断
    _, found = _call(
        ready_server, "continuity_find", {"query": "审批", "limit": 10}
    )
    assert found["code"] == "ambiguous"
    assert len(found["candidates"]) == 2


def test_find_task_keyword_returns_only_matching_workstreams(
    ready_server, git_workspace
):
    """任务关键词命中：只返回真正匹配的任务，不被无关任务稀释。"""
    needle = _begin(
        ready_server, git_workspace, objective="修复审批链超时回归",
        alias="", make_focus=False,
    )
    _begin(ready_server, git_workspace, objective="无关任务一", alias="",
           make_focus=False)
    _begin(ready_server, git_workspace, objective="无关任务二", alias="",
           make_focus=False)
    _, found = _call(ready_server, "continuity_find", {"query": "超时回归"})
    assert found["code"] == "ok"
    candidate = found["candidates"][0]
    assert "task" in candidate["matched_via"]
    assert [w["workstream_id"] for w in candidate["workstreams"]] == [
        needle["workstream_id"]
    ]
    assert found["checkpoint"]["workstream_id"] == needle["workstream_id"]


def test_begin_replay_after_completion_does_not_reopen(
    ready_server, git_workspace
):
    """相同目标的重放不复活/不新增终态任务；显式新任务语义走 checkpoint。"""
    created = _begin(ready_server, git_workspace)
    _call(
        ready_server,
        "continuity_checkpoint",
        {
            "action": "complete",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    replayed = _begin(ready_server, git_workspace)
    assert replayed["created"] is False
    assert replayed["workstream_id"] == created["workstream_id"]
    assert replayed["status"] == "completed"
    conn = ready_server.context_service.store._connection()
    assert conn.execute(
        "SELECT COUNT(*) c FROM continuity_workstreams"
    ).fetchone()["c"] == 1
    # 重放不再 CAS focus（既有 complete 不清焦点的悬空语义保持不变）
    focus = conn.execute(
        "SELECT workstream_id, revision FROM continuity_focus"
    ).fetchone()
    assert focus["workstream_id"] == created["workstream_id"]
    assert focus["revision"] == 1  # 与重放前一致，未被重新指配


def test_diagnostic_instructions_keep_continuity_available():
    """降级/非法态的全局说明：经验检索降级，但续接独立可用。"""
    from evolvmem.mcp_contract import _DIAGNOSTIC_INSTRUCTIONS

    assert "unavailable" in _DIAGNOSTIC_INSTRUCTIONS
    assert "continuity" in _DIAGNOSTIC_INSTRUCTIONS
    assert "continuity_resume" in _DIAGNOSTIC_INSTRUCTIONS
