"""MCP 合约测试：continuity_resume / continuity_checkpoint / continuity_list。

冻结行为（Task 8）：codex/kimi 的 compat/shadow/primary 列出三个续接工具
（compat 也放行——续接就绪只依赖 continuity schema 与 workspace key，在
handler 层检查，不复用 Core serving gate，所以降级 primary 也仍列出并可调用）；
legacy 与非切换 adapter 不列出，隐藏工具经 tools/call 也只能得到 Unknown。
schema 与 Task 7 的 Request dataclass 字段严格对齐（additionalProperties:
False）；resume/list 只读，checkpoint 是写工具。所有稳定错误码在
_CONTEXT_ERROR_MESSAGES 有文案，响应不携带绝对路径/key 位置/traceback。

测试只用 test_config 临时目录与 tmp_path 下的 git fixture，绝不碰真实库。
"""

import json
import shutil
import subprocess

import pytest

from evolvmem.context_models import ContextMode, ContextSessionStartRequest
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.continuity_models import (
    ContinuityAction,
    ContinuityCheckpointRequest,
    ContinuityResumeRequest,
    _CONTINUITY_ERROR_CODES,
    _RESUME_CODES,
)
from evolvmem.mcp_contract import (
    _PRIMARY_INSTRUCTIONS,
    _PRIMARY_INSTRUCTIONS_KIMI,
    tool_specs,
)
from evolvmem.mcp_server import MemoryMCPServer, _CONTEXT_ERROR_MESSAGES
from evolvmem.memory_store import MemoryStore
from evolvmem.project_store import ProjectStore
from evolvmem.workspace_identity import WorkspaceIdentityProvider
from evolvmem.vector_index import VectorIndex


_CONTINUITY_TOOLS = {
    "continuity_resume", "continuity_checkpoint", "continuity_list",
}


# ---- fixtures and helpers（仿 test_mcp_protocol.py 的 server 构造） ----


def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_workspace(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git binary not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _git(repo, "config", "user.email", "continuity@example.invalid")
    _git(repo, "config", "user.name", "continuity-test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _make_server(test_config, *, mode, adapter):
    """MemoryMCPServer + 注入的 ContextService（模式/适配器一致）。

    注入服务的服务器不经过初始化门闩；legacy 投影 schema 由一次性
    MemoryStore 引导。primary 不打开 context 向量索引 → 故意处于降级态，
    用于证明续接工具不依赖 Core serving gate。
    """
    test_config.context_mode = mode
    test_config.adapter = adapter
    with MemoryStore(test_config):
        pass
    server = MemoryMCPServer(config=test_config)
    server._init_done.set()  # 注入服务的服务器不经过初始化门闩
    server.vidx.initialize(dim=test_config.embedding_dim)
    try:
        parsed = ContextMode(mode)
    except ValueError:
        return server  # 非法配置：fail-closed，不创建服务
    service = ContextService(test_config)
    service._legacy_vector = server.vidx
    service.initialize(mode=parsed, adapter=adapter)
    server.context_service = service
    return server


def _bootstrap_key(test_config) -> WorkspaceIdentityProvider:
    provider = WorkspaceIdentityProvider(
        key_path=test_config.data_dir / "workspace.key"
    )
    provider.bootstrap_key()
    return provider


def _bind(server, provider, workspace, project="proj") -> None:
    """Register + bind in one transaction; pre-builds the empty focus row."""
    store = server.context_service.store
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with store.transaction():
        ps = ProjectStore(
            store._connection(), store._require_transaction, generic_names=()
        )
        ps.register_project(project)
        ps.bind_workspace(fingerprint, project, method="test", make_default=True)


@pytest.fixture
def server_kimi_compat(test_config):
    return _make_server(test_config, mode="compat", adapter="kimi")


@pytest.fixture
def server_kimi_primary(test_config):
    return _make_server(test_config, mode="primary", adapter="kimi")


@pytest.fixture
def bound_compat_server(test_config, git_workspace):
    """compat/kimi server with a bootstrapped key and a bound workspace."""
    server = _make_server(test_config, mode="compat", adapter="kimi")
    provider = _bootstrap_key(test_config)
    _bind(server, provider, git_workspace)
    return server


def _tools_list(server, req_id=3):
    resp = server._handle_request({
        "method": "tools/list", "id": req_id, "jsonrpc": "2.0",
    })
    return resp["result"]["tools"]


def _tool_names(server):
    return {tool["name"] for tool in _tools_list(server)}


def _call(server, name, arguments, req_id=7):
    resp = server._handle_request({
        "method": "tools/call", "id": req_id, "jsonrpc": "2.0",
        "params": {"name": name, "arguments": arguments},
    })
    result = resp["result"]
    payload = json.loads(result["content"][0]["text"])
    return result, payload


def _create_args(workspace, **overrides):
    args = {
        "action": "create",
        "workspace_path": str(workspace),
        "objective": "交付续接域层",
        "next_action": "写测试",
    }
    args.update(overrides)
    return args


# ---- 工具列出矩阵（brief Step 1 用例 1） ----


def test_continuity_tools_listed_for_kimi_compat(server_kimi_compat):
    names = _tool_names(server_kimi_compat)
    assert {"continuity_resume", "continuity_checkpoint", "continuity_list"} <= names


@pytest.mark.parametrize("mode", ["compat", "shadow", "primary"])
@pytest.mark.parametrize("adapter", ["codex", "kimi", "dsh"])
def test_cutover_adapters_list_continuity_tools(test_config, mode, adapter):
    server = _make_server(test_config, mode=mode, adapter=adapter)
    assert _CONTINUITY_TOOLS <= _tool_names(server)


def test_degraded_primary_still_lists_and_serves_continuity(
    test_config, git_workspace
):
    """降级 primary 不过 Core serving gate：续接工具仍列出且可调用。"""
    server = _make_server(test_config, mode="primary", adapter="kimi")
    assert server.context_service.status().ready is False  # 降级前提

    assert _CONTINUITY_TOOLS <= _tool_names(server)
    provider = _bootstrap_key(test_config)
    _bind(server, provider, git_workspace)
    result, payload = _call(
        server, "continuity_resume", {"workspace_path": str(git_workspace)}
    )
    assert "isError" not in result
    assert payload["code"] == "no_continuation"


@pytest.mark.parametrize("mode", ["legacy"])
@pytest.mark.parametrize("adapter", ["codex", "kimi", "dsh"])
def test_legacy_mode_does_not_list_continuity_tools(test_config, mode, adapter):
    server = _make_server(test_config, mode=mode, adapter=adapter)
    names = _tool_names(server)
    assert names.isdisjoint(_CONTINUITY_TOOLS)
    result, payload = _call(
        server, "continuity_resume", {"workspace_path": "/tmp"}
    )
    assert result["isError"] is True
    assert "Unknown tool" in payload["error"]


@pytest.mark.parametrize("adapter", ["claude", "web", ""])
@pytest.mark.parametrize("mode", ["compat", "shadow", "primary"])
def test_non_cutover_adapters_never_list_continuity_tools(
    test_config, mode, adapter
):
    server = _make_server(test_config, mode=mode, adapter=adapter)
    assert _tool_names(server).isdisjoint(_CONTINUITY_TOOLS)
    result, payload = _call(
        server, "continuity_checkpoint", _create_args("/tmp")
    )
    assert result["isError"] is True
    assert "Unknown tool" in payload["error"]


@pytest.mark.parametrize("adapter", ["codex", "kimi", "dsh"])
def test_invalid_mode_does_not_list_continuity_tools(test_config, adapter):
    server = _make_server(test_config, mode="turbo", adapter=adapter)
    assert _tool_names(server).isdisjoint(_CONTINUITY_TOOLS)


# ---- schema 冻结（registry 单元级） ----


class TestContinuityToolSchemas:
    def _specs(self):
        return {
            spec.name: spec
            for spec in tool_specs(
                adapter="kimi", mode=ContextMode.COMPAT, health=None
            )
        }

    def test_additional_properties_closed(self):
        for name in _CONTINUITY_TOOLS:
            schema = self._specs()[name].input_schema
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False, name

    def test_primary_instructions_create_a_focused_workstream(self):
        for text in (_PRIMARY_INSTRUCTIONS, _PRIMARY_INSTRUCTIONS_KIMI):
            assert "make_focus=true" in text
            assert "expected_focus_revision" in text

    def test_readonly_annotations(self):
        specs = self._specs()
        assert specs["continuity_resume"].annotations["readOnlyHint"] is True
        assert specs["continuity_list"].annotations["readOnlyHint"] is True
        # checkpoint 是写工具：绝不能标只读
        assert specs["continuity_checkpoint"].annotations.get(
            "readOnlyHint"
        ) is not True

    def test_resume_and_list_schema_align_with_request(self):
        import dataclasses

        request_fields = {
            field.name
            for field in dataclasses.fields(ContinuityResumeRequest)
        }
        for name in ("continuity_resume", "continuity_list"):
            schema = self._specs()[name].input_schema
            assert schema["required"] == ["workspace_path"]
            assert set(schema["properties"]) == request_fields == {
                "workspace_path", "project_hint",
            }
            assert schema["properties"]["workspace_path"]["type"] == "string"
            assert schema["properties"]["project_hint"]["type"] == "string"

    def test_checkpoint_schema_aligns_with_request(self):
        import dataclasses

        from evolvmem.mcp_server import MemoryMCPServer

        schema = self._specs()["continuity_checkpoint"].input_schema
        assert sorted(schema["required"]) == ["action", "workspace_path"]
        # schema 属性、handler 白名单与 Request dataclass 字段三方严格对齐
        request_fields = {
            field.name
            for field in dataclasses.fields(ContinuityCheckpointRequest)
        }
        assert set(schema["properties"]) == request_fields
        assert set(MemoryMCPServer._CHECKPOINT_FIELDS) == request_fields
        assert schema["properties"]["action"]["enum"] == [
            member.value for member in ContinuityAction
        ]
        assert schema["properties"]["make_focus"]["type"] == "boolean"
        for field in (
            "expected_checkpoint_revision", "expected_state_version",
            "expected_focus_revision",
        ):
            assert schema["properties"][field]["minimum"] == 0
        for field in ("accepted_decisions", "completed_steps", "blockers"):
            assert schema["properties"][field]["type"] == "array"
            assert schema["properties"][field]["items"] == {"type": "string"}
        assert schema["properties"]["source_context_ids"]["items"] == {
            "type": "integer", "minimum": 1,
        }


# ---- 严格参数校验（brief Step 1 用例 2） ----


def test_checkpoint_rejects_unknown_args(server_kimi_primary):
    result, payload = _call(
        server_kimi_primary,
        "continuity_checkpoint",
        {"action": "create", "bogus": 1},
    )
    assert result["isError"] is True
    assert payload["error"] == "invalid_arguments"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"action": "create"},  # 缺 workspace_path
        {"workspace_path": "/tmp"},  # 缺 action
        {"action": "create", "workspace_path": 7},
        {"action": "create", "workspace_path": "/tmp", "make_focus": 1},
        {"action": "create", "workspace_path": "/tmp",
         "expected_checkpoint_revision": True},
        {"action": "create", "workspace_path": "/tmp",
         "expected_state_version": "1"},
        {"action": "create", "workspace_path": "/tmp",
         "expected_focus_revision": -1},
        {"action": "create", "workspace_path": "/tmp",
         "accepted_decisions": "不是数组"},
        {"action": "create", "workspace_path": "/tmp",
         "source_context_ids": [0]},
    ],
)
def test_checkpoint_argument_validation_is_stable(server_kimi_compat, args):
    result, payload = _call(server_kimi_compat, "continuity_checkpoint", args)
    assert result["isError"] is True
    assert payload["error"] == "invalid_arguments"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "Traceback" not in rendered


@pytest.mark.parametrize(
    "tool",
    ["continuity_resume", "continuity_list"],
)
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"workspace_path": 7},
        {"workspace_path": "/tmp", "bogus": 1},
        {"workspace_path": "/tmp", "project_hint": 3},
    ],
)
def test_read_tool_argument_validation_is_stable(server_kimi_compat, tool, args):
    result, payload = _call(server_kimi_compat, tool, args)
    assert result["isError"] is True
    assert payload["error"] == "invalid_arguments"


# ---- readiness 门禁：schema 未建 / key 缺失 ----


@pytest.mark.parametrize(
    "tool,args",
    [
        ("continuity_resume", {"workspace_path": "/tmp"}),
        ("continuity_list", {"workspace_path": "/tmp"}),
        ("continuity_checkpoint",
         {"action": "create", "workspace_path": "/tmp"}),
    ],
)
def test_missing_workspace_key_returns_continuity_not_ready(
    server_kimi_compat, tool, args
):
    # 未 bootstrap workspace.key：三个工具都 fail-closed 到稳定码
    result, payload = _call(server_kimi_compat, tool, args)
    assert result["isError"] is True
    assert payload["error"] == "continuity_not_ready"


def test_missing_schema_returns_continuity_not_ready(test_config, tmp_path):
    test_config.context_mode = "compat"
    test_config.adapter = "kimi"
    test_config.db_path.touch()
    store = ContextStore(test_config)
    store.initialize(create_schema=False)  # 旧库：无 continuity 表
    server = MemoryMCPServer(config=test_config)
    server._init_done.set()
    server.context_service = ContextService(test_config, store=store)
    _bootstrap_key(test_config)  # key 就绪，只缺 schema

    result, payload = _call(
        server, "continuity_resume", {"workspace_path": str(tmp_path)}
    )
    assert result["isError"] is True
    assert payload["error"] == "continuity_not_ready"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert str(test_config.data_dir) not in rendered
    assert "Traceback" not in rendered


# ---- create → resume 往返与 CAS ----


def test_create_then_resume_roundtrip_returns_same_workstream(
    bound_compat_server, git_workspace
):
    _, created = _call(
        bound_compat_server,
        "continuity_checkpoint",
        _create_args(
            git_workspace, make_focus=True, expected_focus_revision=0
        ),
    )
    assert created["workstream_id"].startswith("ws_")
    assert created["checkpoint_revision"] == 1
    assert created["state_version"] == 1
    assert created["focus_revision"] == 1
    assert created["status"] == "open"
    assert created["context_id"] > 0

    result, resumed = _call(
        bound_compat_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert "isError" not in result
    assert resumed["code"] == "ok"
    assert resumed["workstream_id"] == created["workstream_id"]
    assert resumed["context_id"] == created["context_id"]
    assert resumed["checkpoint_revision"] == 1
    assert resumed["state_version"] == 1
    assert resumed["status"] == "open"
    assert resumed["staleness"] == "fresh"
    assert resumed["candidates"] == []
    checkpoint = resumed["checkpoint"]
    assert checkpoint["workstream_id"] == created["workstream_id"]
    assert checkpoint["project"] == "proj"
    assert "交付续接域层" in checkpoint["l1"]
    assert "l2" not in checkpoint  # L2 原文不外泄

    # 响应不携带绝对路径、key 位置或 traceback
    rendered = json.dumps(resumed, ensure_ascii=False)
    assert str(git_workspace) not in rendered
    assert "workspace.key" not in rendered
    assert "Traceback" not in rendered


def test_checkpoint_keeps_fresh_primary_process_ready(
    test_config, git_workspace
):
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    index.initialize(dim=test_config.embedding_dim)
    index.rebuild([], [])
    index.close()

    server = _make_server(test_config, mode="compat", adapter="codex")
    provider = _bootstrap_key(test_config)
    _bind(server, provider, git_workspace)
    result, created = _call(
        server,
        "continuity_checkpoint",
        _create_args(
            git_workspace, make_focus=True, expected_focus_revision=0,
        ),
    )
    assert "isError" not in result
    assert created["context_id"] > 0
    server.shutdown()

    fresh = ContextService(test_config)
    fresh.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    fresh.vector_index.initialize(dim=test_config.embedding_dim)
    fresh._refresh_health()
    try:
        assert fresh.status().ready is True
        names = {spec.name for spec in tool_specs(
            adapter="codex", mode=ContextMode.PRIMARY,
            health=fresh.status(),
        )}
        assert "experience_recall" in names
    finally:
        fresh.close()


def test_continuity_list_returns_l0_summaries_only(
    bound_compat_server, git_workspace
):
    _, created = _call(
        bound_compat_server, "continuity_checkpoint", _create_args(git_workspace)
    )
    result, listed = _call(
        bound_compat_server,
        "continuity_list",
        {"workspace_path": str(git_workspace)},
    )
    assert "isError" not in result
    assert listed["count"] == 1
    row = listed["workstreams"][0]
    assert row["workstream_id"] == created["workstream_id"]
    assert row["project"] == "proj"
    assert row["status"] == "open"
    assert row["checkpoint_revision"] == 1
    assert row["state_version"] == 1
    assert row["l0"]
    assert "交付续接域层" in row["l0"]
    for forbidden in ("l1", "l2"):
        assert forbidden not in row


def test_cas_conflict_returns_revision_conflict(
    bound_compat_server, git_workspace
):
    _, created = _call(
        bound_compat_server, "continuity_checkpoint", _create_args(git_workspace)
    )
    result, payload = _call(
        bound_compat_server,
        "continuity_checkpoint",
        {
            "action": "update",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "current_step": "实现域层",
            "expected_checkpoint_revision": 0,  # 过期 CAS token
            "expected_state_version": 0,
        },
    )
    assert result["isError"] is True
    assert payload["error"] == "revision_conflict"
    assert payload["message"]  # 稳定文案随码返回

    # 用最新 revision 重写：成功推进到 revision 2
    _, updated = _call(
        bound_compat_server,
        "continuity_checkpoint",
        {
            "action": "update",
            "workspace_path": str(git_workspace),
            "workstream_id": created["workstream_id"],
            "current_step": "实现域层",
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    assert updated["checkpoint_revision"] == 2
    assert updated["state_version"] == 2
    assert updated["status"] == "open"


def test_unknown_action_maps_to_invalid_action(
    bound_compat_server, git_workspace
):
    result, payload = _call(
        bound_compat_server,
        "continuity_checkpoint",
        _create_args(git_workspace, action="explode"),
    )
    assert result["isError"] is True
    assert payload["error"] == "invalid_action"


# ---- resume 短回路码 ----


def test_resume_shortcircuit_codes(bound_compat_server, git_workspace):
    # 无 focus + 单一未完成 → needs_focus_confirmation
    _, first = _call(
        bound_compat_server, "continuity_checkpoint", _create_args(git_workspace)
    )
    _, resumed = _call(
        bound_compat_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "needs_focus_confirmation"
    assert resumed["checkpoint"] is None
    assert resumed["message"] == _CONTEXT_ERROR_MESSAGES[
        "needs_focus_confirmation"
    ]
    assert len(resumed["candidates"]) == 1
    candidate = resumed["candidates"][0]
    assert candidate["workstream_id"] == first["workstream_id"]
    assert candidate["l0"]
    assert "l1" not in candidate

    # 无 focus + 多个未完成 → ambiguous
    _call(
        bound_compat_server,
        "continuity_checkpoint",
        _create_args(git_workspace, objective="第二条工作流"),
    )
    _, resumed = _call(
        bound_compat_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "ambiguous"
    assert len(resumed["candidates"]) == 2

    # focus 指向终态 → dangling_focus
    _, focused = _call(
        bound_compat_server,
        "continuity_checkpoint",
        _create_args(
            git_workspace, objective="会被完成", make_focus=True,
            expected_focus_revision=0,
        ),
    )
    _call(
        bound_compat_server,
        "continuity_checkpoint",
        {
            "action": "complete",
            "workspace_path": str(git_workspace),
            "workstream_id": focused["workstream_id"],
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        },
    )
    _, resumed = _call(
        bound_compat_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "dangling_focus"
    assert resumed["checkpoint"] is None


def test_resume_no_continuation_for_bound_empty_project(
    bound_compat_server, git_workspace
):
    _, resumed = _call(
        bound_compat_server,
        "continuity_resume",
        {"workspace_path": str(git_workspace)},
    )
    assert resumed["code"] == "no_continuation"
    assert resumed["candidates"] == []
    assert resumed["checkpoint"] is None


def test_unbound_workspace_is_not_an_error(bound_compat_server, tmp_path):
    # 未绑定 workspace：resume → no_continuation，list → 空，都不是协议错误
    other = tmp_path / "other"
    other.mkdir()
    result, resumed = _call(
        bound_compat_server, "continuity_resume", {"workspace_path": str(other)}
    )
    assert "isError" not in result
    assert resumed["code"] == "no_continuation"
    result, listed = _call(
        bound_compat_server, "continuity_list", {"workspace_path": str(other)}
    )
    assert "isError" not in result
    assert listed["count"] == 0


# ---- 错误码文案覆盖 ----


def test_every_continuity_code_has_a_stable_message():
    codes = set(_CONTINUITY_ERROR_CODES) | (set(_RESUME_CODES) - {"ok"})
    assert codes  # 防呆：集合非空
    for code in sorted(codes):
        message = _CONTEXT_ERROR_MESSAGES.get(code)
        assert isinstance(message, str) and message.strip(), code
        # 文案本身也是稳定码的镜像：不得携带路径/traceback 形状
        assert "Traceback" not in message


# ---- session_start 透传 workspace_path ----


def test_session_start_request_carries_workspace_path():
    request = ContextSessionStartRequest(
        project="p", query="q", workspace_path=" /worktrees/repo "
    )
    assert request.workspace_path == "/worktrees/repo"
    # 可选：缺省为空串
    default = ContextSessionStartRequest(project="p", query="q")
    assert default.workspace_path == ""


def test_session_start_accepts_workspace_path_over_mcp(test_config):
    server = _make_server(test_config, mode="shadow", adapter="codex")
    result, started = _call(
        server,
        "context_session_start",
        {"project": "evolvmem", "query": "复核",
         "workspace_path": "/worktrees/repo"},
    )
    assert "isError" not in result
    assert "block" in started

    result, payload = _call(
        server,
        "context_session_start",
        {"project": "evolvmem", "query": "复核", "workspace_path": 7},
    )
    assert result["isError"] is True
    assert payload["error"] == "invalid_arguments"


# ---- session_start 续接信号透传 ----


_CANDIDATE_KEYS = {
    "workstream_id", "project", "status", "checkpoint_revision",
    "state_version", "l0", "updated_at",
}


def _bound_shadow_server(test_config, git_workspace):
    """shadow/kimi server：过 context 读门禁且续接就绪（key + 绑定）。"""
    server = _make_server(test_config, mode="shadow", adapter="kimi")
    provider = _bootstrap_key(test_config)
    _bind(server, provider, git_workspace)
    return server


def test_session_start_surfaces_continuation_over_mcp(test_config, git_workspace):
    server = _bound_shadow_server(test_config, git_workspace)
    _call(server, "continuity_checkpoint", _create_args(git_workspace))

    # 续接意图 + workspace_path：无 focus 单一未完成 → needs_focus_confirmation
    result, started = _call(
        server,
        "context_session_start",
        {"project": "proj", "query": "继续原任务",
         "workspace_path": str(git_workspace)},
    )
    assert "isError" not in result
    assert "block" in started
    assert started["continuation_code"] == "needs_focus_confirmation"
    continuation = started["continuation"]
    # 有界键集：候选元数据 + L0，绝无 L2 原文或绝对路径
    assert set(continuation) == {"candidates"}
    assert len(continuation["candidates"]) == 1
    assert set(continuation["candidates"][0]) <= _CANDIDATE_KEYS
    rendered = json.dumps(started, ensure_ascii=False)
    assert str(git_workspace) not in rendered
    assert "Traceback" not in rendered


def test_session_start_without_intent_surfaces_empty_continuation_code(
    test_config, git_workspace
):
    server = _bound_shadow_server(test_config, git_workspace)
    _call(server, "continuity_checkpoint", _create_args(git_workspace))

    result, started = _call(
        server,
        "context_session_start",
        {"project": "proj", "query": "项目进展如何",
         "workspace_path": str(git_workspace)},
    )
    assert "isError" not in result
    assert started["continuation_code"] == ""
    assert "continuation" not in started


# ---- instructions 增补 ----


class TestContinuityInstructions:
    @pytest.mark.parametrize(
        "text", [_PRIMARY_INSTRUCTIONS, _PRIMARY_INSTRUCTIONS_KIMI]
    )
    def test_continuity_paragraph_appended(self, text):
        for required in (
            "continuity_checkpoint",
            "continuity_resume",
            "revision_conflict",
            "staleness=fresh",
            "untrusted history",
        ):
            assert required in text, required

    def test_variants_still_differ_only_in_the_session_phrase(self):
        assert _PRIMARY_INSTRUCTIONS_KIMI == _PRIMARY_INSTRUCTIONS.replace(
            "every new Codex session", "every new session"
        )

    def test_original_directive_stays_self_contained_within_512_chars(self):
        # 追加段不得破坏既有首段的前 512 字符自包含冻结
        head = _PRIMARY_INSTRUCTIONS[:512]
        assert "call context_session_start exactly once" in head
        assert "continue without memory" in head
