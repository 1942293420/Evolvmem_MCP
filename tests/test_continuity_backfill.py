"""Codex 中断会话有界增量补录回归（docs/2026-09-09-continuity-recovery.md）。

红→绿顺序：
  1. 无 checkpoint 的中断 fixture：扫描→保守补录（目标、已回结果工具、
     无结果工具记待核实）→新进程可发现/恢复；再次扫描不产生重复任务。
  2. 追加日志/尾部半行/超长行/活跃会话/跨工作区 cwd/敏感与路径内容。
  3. 人工 checkpoint 与终态任务优先：不覆盖、不复活、不动 focus。
  4. dry-run 为默认；apply 需显式 project + workspace 归属。

所有 rollout JSONL 均为虚构 fixture，绝不读真实 Codex 会话。
"""

import json
import os
import time
import uuid as uuidlib

import pytest

from evolvmem.continuity_backfill import run_backfill
from evolvmem.context_models import ContextLayer
from evolvmem.context_store import ContextStore
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_store import ProjectStore

from tests.test_continuity_mcp import (  # noqa: F401  (fixture re-export)
    _bootstrap_key,
    _call,
    _make_server,
    git_workspace,
)


def _meta(session_id, cwd):
    return {
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd),
                    "timestamp": "2026-09-08T10:00:00Z"},
    }


def _user(text):
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }


def _user_mirror(text):
    return {"type": "event_msg",
            "payload": {"type": "user_message", "message": text}}


def _call_event(call_id, name):
    return {
        "type": "response_item",
        "payload": {"type": "function_call", "name": name,
                    "call_id": call_id, "arguments": "{}"},
    }


def _output_event(call_id):
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output",
                    "call_id": call_id, "output": "ok"},
    }


def _age(path, seconds=7200):
    old = time.time() - seconds
    os.utime(path, (old, old))


def _write_rollout(root, session_id, events, *, mtime_age=7200):
    path = root / "2026" / "09" / "08" / (
        f"rollout-2026-09-08T10-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    _age(path, mtime_age)
    return path


def _basic_events(workspace):
    session_id = str(uuidlib.uuid4())
    return session_id, [
        _meta(session_id, workspace),
        _user("继续权限审批：完成审批链重构"),
        _user_mirror("继续权限审批：完成审批链重构"),
        _call_event("c1", "shell"),
        _output_event("c1"),
        _call_event("c2", "apply_patch"),  # 无结果 → 待核实
    ]


@pytest.fixture
def env(test_config, git_workspace, tmp_path):
    """已 bootstrap key、已登记绑定项目的 store + 虚构 codex sessions 根。"""
    provider = _bootstrap_key(test_config)
    store = ContextStore(test_config)
    store.initialize()
    fingerprint = provider.resolve(str(git_workspace)).fingerprint
    with store.transaction():
        ps = ProjectStore(
            store._connection(), store._require_transaction, generic_names=()
        )
        ps.register_project("权限审批")
        ps.bind_workspace(
            fingerprint, "权限审批", method="test", make_default=True
        )
    roots = tmp_path / "codex_sessions"
    roots.mkdir()
    state_path = tmp_path / "backfill_state.json"
    yield store, provider, git_workspace, roots, state_path
    store.close()


def _run(env, *, apply=True, **overrides):
    store, provider, workspace, roots, state_path = env
    kwargs = {
        "sessions_root": roots,
        "project": "权限审批",
        "workspace_path": str(workspace),
        "state_path": state_path,
        "apply": apply,
    }
    kwargs.update(overrides)
    return run_backfill(store, provider, **kwargs)


def _workstreams(store):
    return store._connection().execute(
        "SELECT * FROM continuity_workstreams ORDER BY created_at, id"
    ).fetchall()


def _l2_of(store, row):
    raw = store.get_layer(int(row["current_context_id"]), ContextLayer.L2)
    return json.loads(raw)


# ---- 基本补录闭环 ----


def test_dry_run_reports_without_writing(env):
    workspace, roots = env[2], env[3]
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)
    reports = _run(env, apply=False)
    assert len(reports) == 1
    assert reports[0].action == "create"
    assert reports[0].session_id == session_id
    assert _workstreams(env[0]) == []
    assert not env[4].exists()  # dry-run 不落状态


def test_interrupted_session_backfilled_conservatively(env):
    store, provider, workspace, roots, _ = env
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)

    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    rows = _workstreams(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "open"
    assert row["id"] == reports[0].workstream_id
    payload = _l2_of(store, row)
    assert "审批链重构" in payload["objective"]
    assert any("shell" in step for step in payload["completed_steps"])
    assert any(
        "apply_patch" in item and "待核实" in item
        for item in payload["blockers"]
    )
    # 补录绝不抢占用户正在做的其他任务 focus
    focus = store._connection().execute(
        "SELECT workstream_id FROM continuity_focus"
    ).fetchone()
    assert focus["workstream_id"] is None


def test_rescan_and_append_are_incremental_and_idempotent(env):
    store, provider, workspace, roots, _ = env
    session_id, events = _basic_events(workspace)
    path = _write_rollout(roots, session_id, events)

    first = _run(env)
    second = _run(env)
    assert [r.action for r in second] == ["no_new_events"]
    assert len(_workstreams(store)) == 1
    revision = _workstreams(store)[0]["checkpoint_revision"]

    # 追加：c2 得到结果 + 新的无结果调用
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_output_event("c2")) + "\n")
        handle.write(json.dumps(_call_event("c3", "shell")) + "\n")
    _age(path)
    third = _run(env)
    assert [r.action for r in third] == ["update"]
    rows = _workstreams(store)
    assert len(rows) == 1
    assert rows[0]["checkpoint_revision"] > revision
    assert rows[0]["id"] == first[0].workstream_id
    payload = _l2_of(store, rows[0])
    assert any("apply_patch" in step for step in payload["completed_steps"])
    assert any("shell" in item for item in payload["blockers"])

    # 尾部半行：不消费、不报错，补全后被下一轮读取
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"response_item","payload":{"type":"function_')
    _age(path)
    fourth = _run(env)
    assert [r.action for r in fourth] == ["no_new_events"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write('call","name":"shell","call_id":"c4"}}\n')
    _age(path)
    fifth = _run(env)
    assert [r.action for r in fifth] == ["update"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert sum("shell" in item for item in payload["blockers"]) == 2


def test_oversized_line_is_skipped_but_offset_advances(env):
    store, provider, workspace, roots, _ = env
    session_id, events = _basic_events(workspace)
    path = _write_rollout(roots, session_id, events)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type":"response_item","payload":{
            "type":"message","role":"assistant",
            "content":[{"type":"output_text","text":"x" * (1024 * 1024 + 10)}],
        }}) + "\n")
        handle.write(json.dumps(_call_event("c9", "shell")) + "\n")
    _age(path)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert any("shell" in item and "待核实" in item
               for item in payload["blockers"])
    assert _run(env)[0].action == "no_new_events"


# ---- 归属与安全边界 ----


def test_active_session_is_skipped(env):
    workspace, roots = env[2], env[3]
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events, mtime_age=0)  # 刚被写过
    reports = _run(env, idle_minutes=30)
    assert [r.action for r in reports] == ["active"]
    assert _workstreams(env[0]) == []


def test_foreign_cwd_session_is_candidate_only(env, tmp_path):
    workspace, roots = env[2], env[3]
    elsewhere = tmp_path / "someone-else"
    elsewhere.mkdir()
    session_id, events = _basic_events(elsewhere)
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["candidate"]
    assert reports[0].reason == "cwd_mismatch"
    assert _workstreams(env[0]) == []


def test_paths_and_credentials_never_reach_checkpoint(env):
    store, provider, workspace, roots, _ = env
    session_id = str(uuidlib.uuid4())
    events = [
        _meta(session_id, workspace),
        _user("重构 /home/alice/secret/project 的审批链"),
        _call_event("c1", "shell"),
        _output_event("c1"),
    ]
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert "/home" not in json.dumps(payload, ensure_ascii=False)

    # 凭据形目标：整条按内容边界省略，绝不入库
    session2 = str(uuidlib.uuid4())
    events2 = [
        _meta(session2, workspace),
        _user("设置 api_key = sk-live-9f8e7d6c5b"),
        _call_event("c1", "shell"),
    ]
    _write_rollout(roots, session2, events2)
    reports = _run(env)
    created = [r for r in reports if r.session_id == session2]
    assert [r.action for r in created] == ["create"]
    rows = _workstreams(store)
    assert len(rows) == 2
    payload2 = _l2_of(store, rows[1])
    assert "sk-live" not in json.dumps(payload2, ensure_ascii=False)
    assert payload2["objective"] != "设置 api_key = sk-live-9f8e7d6c5b"


# ---- 人工 checkpoint 与终态优先 ----


def _apply_basic(env):
    workspace, roots = env[2], env[3]
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    return session_id, reports[0].workstream_id


def test_human_checkpoint_is_never_overwritten(env, test_config):
    store, provider, workspace, _, _ = env
    session_id, workstream_id = _apply_basic(env)

    service = ContinuityService(test_config, store, provider)
    from evolvmem.continuity_models import ContinuityCheckpointRequest
    result = service.checkpoint(ContinuityCheckpointRequest(
        action="update",
        workspace_path=str(workspace),
        workstream_id=workstream_id,
        current_step="人工确认的步骤",
        expected_checkpoint_revision=1,
        expected_state_version=1,
    ))
    assert result.checkpoint_revision == 2

    # 追加日志后再扫：人工更新的版本更高 → 保留人工 checkpoint
    path = next(env[3].glob(f"**/*{session_id}*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_call_event("c7", "shell")) + "\n")
    _age(path)
    reports = _run(env)
    assert [r.action for r in reports] == ["human_checkpoint_kept"]
    row = _workstreams(store)[0]
    assert row["checkpoint_revision"] == 2
    payload = _l2_of(store, row)
    assert payload["current_step"] == "人工确认的步骤"


def test_completed_workstream_is_never_resurrected(env, test_config):
    store, provider, workspace, roots, _ = env
    session_id, workstream_id = _apply_basic(env)
    service = ContinuityService(test_config, store, provider)
    from evolvmem.continuity_models import ContinuityCheckpointRequest
    service.checkpoint(ContinuityCheckpointRequest(
        action="complete",
        workspace_path=str(workspace),
        workstream_id=workstream_id,
        expected_checkpoint_revision=1,
        expected_state_version=1,
    ))
    path = next(roots.glob(f"**/*{session_id}*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_call_event("c8", "shell")) + "\n")
    _age(path)
    reports = _run(env)
    assert [r.action for r in reports] == ["terminal_kept"]
    row = _workstreams(store)[0]
    assert row["status"] == "completed"
    assert row["checkpoint_revision"] == 2


# ---- 新进程恢复 ----


def test_fresh_server_objects_recover_backfilled_workstream(
    env, test_config, git_workspace
):
    """对象级新客户端恢复（同进程新 server/service 对象）。

    真实 OS 进程 SIGKILL(-9) 后按别名找回由主控用两个独立 MCP 子进程
    另行验证；本测试只覆盖对象级关闭/重建后的数据可读性。
    """
    _apply_basic(env)
    env[0].close()  # 旧客户端对象结束

    server = _make_server(test_config, mode="compat", adapter="kimi")
    _, found = _call(server, "continuity_find", {"query": "审批链"})
    assert found["code"] == "ok"
    checkpoint = found["checkpoint"]
    assert "审批链重构" in checkpoint["l1"]
    assert "待核实" in checkpoint["l1"]
    _, resumed = _call(
        server,
        "continuity_resume",
        {"workspace_path": str(git_workspace), "project_hint": "权限审批"},
    )
    assert resumed["code"] == "needs_focus_confirmation"
    assert len(resumed["candidates"]) == 1
    server.shutdown()


# ---- 审查回归：全局自动补录与原生 Codex 结构（先红后绿） ----


def _auto_run(env, *, apply=True, **overrides):
    store, provider = env[0], env[1]
    kwargs = {
        "sessions_root": env[3],
        "project": "",
        "workspace_path": "",
        "state_path": env[4],
        "apply": apply,
    }
    kwargs.update(overrides)
    return run_backfill(store, provider, **kwargs)


def test_auto_mode_applies_only_for_uniquely_bound_cwd(env, tmp_path):
    """自动模式：cwd 指纹唯一匹配 active binding 才补录，否则只候选。"""
    store, provider, workspace, roots, _ = env
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    stray_id, stray_events = _basic_events(unbound)
    _write_rollout(roots, stray_id, stray_events)
    gone_id, gone_events = _basic_events(tmp_path / "gone")
    _write_rollout(roots, gone_id, gone_events)

    reports = {r.session_id: r for r in _auto_run(env)}
    assert reports[session_id].action == "create"
    assert reports[stray_id].action == "candidate"
    assert reports[stray_id].reason == "no_unique_binding"
    assert reports[gone_id].action == "candidate"
    assert reports[gone_id].reason == "cwd_unavailable"
    rows = _workstreams(store)
    assert len(rows) == 1
    assert rows[0]["project"] == "权限审批"


def test_auto_mode_ambiguous_binding_is_candidate_only(env):
    """同一指纹多个 active 项目且无唯一默认 → 候选，不猜项目。"""
    store, provider, workspace, roots, _ = env
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with store.transaction():
        ps = ProjectStore(
            store._connection(), store._require_transaction, generic_names=()
        )
        ps.register_project("另一个项目")
        ps.bind_workspace(
            fingerprint, "另一个项目", method="test", make_default=False
        )
        store._connection().execute(
            "UPDATE context_project_workspace_bindings SET is_default=0"
        )
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)
    reports = _auto_run(env)
    assert [r.action for r in reports] == ["candidate"]
    assert reports[0].reason == "no_unique_binding"
    assert _workstreams(store) == []


def test_subagent_sessions_are_excluded(env):
    """session_meta 带 subagent source/parent_thread_id 的不是用户任务。"""
    workspace = env[2]
    session_id = str(uuidlib.uuid4())
    meta = _meta(session_id, workspace)
    meta["payload"]["source"] = {"subagent": {"other": "guardian"}}
    meta["payload"]["parent_thread_id"] = str(uuidlib.uuid4())
    events = [meta, _user("审查器内部会话"), _call_event("c1", "shell")]
    _write_rollout(env[3], session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["skipped"]
    assert reports[0].reason == "subagent_session"
    assert _workstreams(env[0]) == []


def test_injected_context_is_not_user_objective(env):
    """environment_context / AGENTS.md 注入不当用户目标。"""
    store, workspace, roots = env[0], env[2], env[3]
    session_id = str(uuidlib.uuid4())
    events = [
        _meta(session_id, workspace),
        _user("<environment_context>cwd=... </environment_context>"),
        _user("# AGENTS.md instructions\n- do things"),
        _user("真正的目标：修复审批超时"),
        _call_event("c1", "shell"),
        _output_event("c1"),
    ]
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert payload["objective"] == "真正的目标：修复审批超时"


def test_failed_tool_result_is_pending_verification(env):
    """exit_code 非零/失败结果不算已完成：只标返回失败、待核实。"""
    store, workspace, roots = env[0], env[2], env[3]
    session_id = str(uuidlib.uuid4())
    failed_output = {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1",
                    "output": json.dumps({"output": "boom",
                                          "metadata": {"exit_code": 1}})},
    }
    events = [
        _meta(session_id, workspace),
        _user("修复审批超时"),
        _call_event("c1", "shell"),
        failed_output,
        _call_event("c2", "shell"),
        _output_event("c2"),
    ]
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert len(payload["completed_steps"]) == 1
    assert any(
        "返回失败" in item and "待核实" in item for item in payload["blockers"]
    )


def test_custom_tool_calls_and_arg_hints(env):
    """原生 custom_tool_call/output 结构入 completed，步骤带可理解提示。"""
    store, workspace, roots = env[0], env[2], env[3]
    session_id = str(uuidlib.uuid4())
    custom_call = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "name": "shell",
                    "call_id": "k1",
                    "arguments": json.dumps({"command": ["pytest", "tests"]})},
    }
    custom_output = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "k1",
                    "output": "1 passed"},
    }
    events = [
        _meta(session_id, workspace),
        _user("跑通回归"),
        custom_call,
        custom_output,
    ]
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["create"]
    payload = _l2_of(store, _workstreams(store)[0])
    assert any(
        "shell" in step and "pytest tests" in step
        for step in payload["completed_steps"]
    )


def test_backfill_prefers_existing_human_workstream(env, test_config):
    """已有同目标人工任务：复用读回，不另造每会话平行任务。"""
    store, provider, workspace, roots, _ = env
    service = ContinuityService(test_config, store, provider)
    from evolvmem.continuity_models import ContinuityCheckpointRequest

    human = service.checkpoint(ContinuityCheckpointRequest(
        action="create",
        workspace_path=str(workspace),
        objective="继续权限审批：完成审批链重构",
        next_action="人工下一步",
        make_focus=True,
        expected_focus_revision=0,
    ))
    session_id, events = _basic_events(workspace)
    _write_rollout(roots, session_id, events)
    reports = _run(env)
    assert [r.action for r in reports] == ["existing"]
    assert reports[0].workstream_id == human.workstream_id
    rows = _workstreams(store)
    assert len(rows) == 1
    assert rows[0]["checkpoint_revision"] == 1  # 人工 checkpoint 原样


def test_lost_state_never_overwrites_backfilled_or_human(env):
    """状态文件丢失后重扫：revision 高于已记录版本 → 保留，不覆盖。"""
    store, _, _, roots, state_path = env
    session_id, workstream_id = _apply_basic(env)
    state_path.unlink()  # 状态损坏/丢失
    reports = _run(env)
    assert [r.action for r in reports] == ["human_checkpoint_kept"]
    row = _workstreams(store)[0]
    assert row["checkpoint_revision"] == 1  # 未重复生成、未覆盖
    assert len(_workstreams(store)) == 1
