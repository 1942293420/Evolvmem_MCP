"""项目提及召回：检测、预算、只读 MCP 工具的专用回归测试。

覆盖已授权需求：
- 当前任务提到已登记项目 B → 只召回 B 的项目详情（不跨未提及项目）；
- 英文 canonical 名/别名完整词边界，中文别名子串；
- 过短/通用别名过滤，歧义不猜（绝不返回错误项目）；
- 最多两个提及项目、总预算有界；
- 不传 workspace_path（无续接路由、不改工作区绑定与 continuity 焦点）；
- 未提及时空 block；
- primary 健康白名单正常暴露只读工具，legacy/compat/降级不暴露。
"""

import hashlib

import numpy as np
import pytest

from evolvmem.config import Config
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextServiceStatus,
    ContextSessionStartResult,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService
from evolvmem.legacy_models import LegacyAddRequest
from evolvmem.mcp_contract import tool_specs
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.memory_store import MemoryStore
from evolvmem.project_models import ProjectRegistrySnapshot
from evolvmem.project_mention_recall import (
    DEFAULT_MAX_CHARS,
    ProjectRecallResult,
    detect_mentioned_projects,
    recall_mentioned_projects,
)
from evolvmem.project_store import ProjectStore
from evolvmem.retriever import Retriever


# ---------------------------------------------------------------------------
# 检测：本用户已登记 active project 的 canonical 名/别名
# ---------------------------------------------------------------------------


class TestDetectMentionedProjects:
    def test_english_canonical_name_matches_only_as_a_full_word(self):
        matches = detect_mentioned_projects(
            "排查 beta 的部署问题；betamax 不算，beta2 也不算",
            projects=("beta",),
        )
        assert [match.project for match in matches] == ["beta"]
        assert matches[0].surface.casefold() == "beta"

    def test_ascii_mention_is_case_insensitive(self):
        matches = detect_mentioned_projects(
            "Please check EVOLVMEM now", projects=("evolvmem",),
        )
        assert [match.project for match in matches] == ["evolvmem"]

    def test_chinese_alias_matches_as_substring(self):
        matches = detect_mentioned_projects(
            "记忆插件最近的失败记录",
            projects=("evolvmem",),
            aliases=(("记忆插件", "evolvmem"),),
        )
        assert [match.project for match in matches] == ["evolvmem"]

    def test_short_and_generic_surfaces_are_ignored(self):
        matches = detect_mentioned_projects(
            "p 和 workspace 都不应命中；a 也不行",
            projects=("proj", "a"),
            aliases=(("p", "proj"), ("workspace", "proj")),
            generic_names=("workspace",),
        )
        assert matches == ()

    def test_alias_to_unregistered_project_is_ignored(self):
        matches = detect_mentioned_projects(
            "ghost 项目的问题",
            projects=("beta",),
            aliases=(("ghost", "ghostproj"),),
        )
        assert matches == ()

    def test_ambiguous_surface_is_never_guessed(self):
        # canonical 名 "shared" 同时是 other 的别名：两个身份都可能，绝不猜
        matches = detect_mentioned_projects(
            "shared 的问题",
            projects=("shared", "other"),
            aliases=(("shared", "other"),),
        )
        assert matches == ()

    def test_at_most_two_projects_in_first_mention_order(self):
        matches = detect_mentioned_projects(
            "先 gamma，再 alpha，最后 beta",
            projects=("alpha", "beta", "gamma"),
        )
        assert [match.project for match in matches] == ["gamma", "alpha"]

    def test_overlapping_surfaces_prefer_the_longer_match(self):
        matches = detect_mentioned_projects(
            "记忆插件的部署",
            projects=("alpha", "beta"),
            aliases=(("记忆", "alpha"), ("记忆插件", "beta")),
        )
        assert [match.project for match in matches] == ["beta"]

    def test_repeated_mentions_are_deduplicated(self):
        matches = detect_mentioned_projects(
            "beta 又 beta 了", projects=("beta",),
        )
        assert [match.project for match in matches] == ["beta"]


# ---------------------------------------------------------------------------
# 召回：每个提及项目一次只读 session_start，预算共享
# ---------------------------------------------------------------------------


def _snapshot(projects=(), aliases=(), generic_names=()):
    return ProjectRegistrySnapshot(
        projects=tuple(projects),
        aliases=tuple(aliases),
        bindings=(),
        generic_names=tuple(generic_names),
        revision=1,
    )


class _RecallFakeService:
    """只实现共享函数需要的鸭子接口；记录每次 session_start 请求。"""

    def __init__(self, *, snapshot, blocks=None, ids=None, failures=()):
        self._snapshot = snapshot
        self.blocks = dict(blocks or {})
        self.ids = dict(ids or {})
        self.failures = frozenset(failures)
        self.requests = []

    def _project_store(self):
        if self._snapshot is None:
            raise AttributeError("project store unavailable")
        snapshot = self._snapshot

        class _Store:
            def snapshot(self_inner):
                return snapshot

        return _Store()

    def session_start(self, request, *, project_only=False):
        assert project_only is True
        self.requests.append(request)
        if request.project in self.failures:
            raise RuntimeError("session_start failed")
        block = self.blocks.get(request.project, "")
        limit = request.max_chars if request.max_chars is not None else len(block)
        block = block[:limit]
        return ContextSessionStartResult(
            block=block,
            selected_ids=self.ids.get(request.project, ()),
            used_chars=len(block),
            excluded_counts=(),
        )


class TestRecallMentionedProjects:
    def test_mentioned_project_b_returns_only_b_details(self):
        service = _RecallFakeService(
            snapshot=_snapshot(("alpha", "beta")),
            blocks={
                "beta": (
                    "[BEGIN EVOLVMEM CONTEXT HISTORY]\n"
                    "B 的部署细节：先备份再灰度\n"
                    "[END EVOLVMEM CONTEXT HISTORY]"
                )
            },
            ids={"beta": (11,)},
        )

        result = recall_mentioned_projects(
            service, query="看看 beta 的部署细节", max_chars=4000,
        )

        assert isinstance(result, ProjectRecallResult)
        assert result.matched_projects == ("beta",)
        assert "B 的部署细节：先备份再灰度" in result.block
        assert result.block.startswith("[BEGIN EVOLVMEM PROJECT RECALL]")
        assert result.block.rstrip().endswith("[END EVOLVMEM PROJECT RECALL]")
        assert result.selected_ids == (11,)
        assert result.used_chars == len(result.block)
        assert len(service.requests) == 1
        request = service.requests[0]
        assert request.project == "beta"
        assert request.query == "看看 beta 的部署细节"
        # 不传 workspace_path：不触发续接路由，不改绑定/焦点
        assert request.workspace_path == ""

    def test_query_without_mention_returns_empty_and_never_queries(self):
        service = _RecallFakeService(
            snapshot=_snapshot(("alpha", "beta")),
            blocks={"beta": "beta details"},
        )

        result = recall_mentioned_projects(
            service, query="今天天气不错，继续上一件事", max_chars=4000,
        )

        assert result.block == ""
        assert result.selected_ids == ()
        assert result.matched_projects == ()
        assert result.used_chars == 0
        assert service.requests == []

    def test_ambiguous_mention_never_returns_the_wrong_project(self):
        service = _RecallFakeService(
            snapshot=_snapshot(
                ("shared", "other"), aliases=(("shared", "other"),),
            ),
            blocks={"shared": "shared details", "other": "other details"},
        )

        result = recall_mentioned_projects(
            service, query="shared 的问题", max_chars=4000,
        )

        assert result.block == ""
        assert result.matched_projects == ()
        assert service.requests == []

    def test_budget_is_shared_across_two_mentions(self):
        long_block = "项目详情 " * 400
        service = _RecallFakeService(
            snapshot=_snapshot(("alpha", "beta")),
            blocks={"alpha": long_block, "beta": long_block},
            ids={"alpha": (1,), "beta": (2, 3)},
        )

        result = recall_mentioned_projects(
            service, query="对比 alpha 和 beta", max_chars=1200,
        )

        assert result.matched_projects == ("alpha", "beta")
        assert len(result.block) <= 1200
        assert result.used_chars == len(result.block)
        assert result.selected_ids == (1, 2, 3)
        assert len(service.requests) == 2
        assert [request.project for request in service.requests] == [
            "alpha", "beta",
        ]
        for request in service.requests:
            assert request.max_chars is not None and request.max_chars >= 1
        assert sum(
            request.max_chars for request in service.requests
        ) <= 1200

    def test_budget_too_small_for_envelope_returns_empty_block(self):
        service = _RecallFakeService(
            snapshot=_snapshot(("beta",)), blocks={"beta": "beta details"},
        )

        result = recall_mentioned_projects(
            service, query="beta 的问题", max_chars=10,
        )

        assert result.block == ""
        assert result.selected_ids == ()
        assert result.matched_projects == ("beta",)
        assert service.requests == []

    def test_matched_project_without_history_yields_empty_block(self):
        service = _RecallFakeService(snapshot=_snapshot(("beta",)), blocks={})

        result = recall_mentioned_projects(
            service, query="beta 的问题", max_chars=4000,
        )

        assert result.block == ""
        assert result.selected_ids == ()
        assert result.matched_projects == ("beta",)
        assert len(service.requests) == 1

    def test_snapshot_failure_fails_open_to_empty_result(self):
        service = _RecallFakeService(snapshot=None)

        result = recall_mentioned_projects(
            service, query="beta 的问题", max_chars=4000,
        )

        assert result.block == ""
        assert result.matched_projects == ()
        assert service.requests == []

    def test_session_start_failure_propagates_to_caller(self):
        service = _RecallFakeService(
            snapshot=_snapshot(("beta",)), failures=("beta",),
        )

        with pytest.raises(RuntimeError):
            recall_mentioned_projects(
                service, query="beta 的问题", max_chars=4000,
            )

    def test_invalid_max_chars_is_rejected(self):
        service = _RecallFakeService(snapshot=_snapshot(("beta",)))
        for bad in (0, -1, "4000", None, True):
            with pytest.raises(ValueError):
                recall_mentioned_projects(
                    service, query="beta 的问题", max_chars=bad,
                )

    def test_empty_query_returns_empty_result(self):
        service = _RecallFakeService(snapshot=_snapshot(("beta",)))

        result = recall_mentioned_projects(service, query="   ", max_chars=4000)

        assert result == ProjectRecallResult(
            block="", selected_ids=(), matched_projects=(), used_chars=0,
        )
        assert service.requests == []


# ---------------------------------------------------------------------------
# MCP 契约：primary 健康白名单
# ---------------------------------------------------------------------------


def _health(**overrides):
    values = dict(
        mode=ContextMode.PRIMARY,
        adapter="codex",
        ready=True,
        status_counts={},
        mapping_count=0,
        projection_lag=0,
        context_vector_ready=True,
        context_vector_dirty=False,
        legacy_vector_ready=False,
        legacy_vector_dirty=False,
        diagnostics=(),
        reason_codes=(),
    )
    values.update(overrides)
    return ContextServiceStatus(**values)


class TestMcpContract:
    @pytest.mark.parametrize("adapter", ["codex", "kimi", "dsh"])
    def test_primary_ready_exposes_read_only_project_recall(self, adapter):
        specs = {
            spec.name: spec
            for spec in tool_specs(
                adapter=adapter, mode=ContextMode.PRIMARY,
                health=_health(adapter=adapter),
            )
        }

        spec = specs["context_project_recall"]
        assert spec.annotations.get("readOnlyHint") is True
        assert spec.input_schema["required"] == ["query"]
        properties = spec.input_schema["properties"]
        assert properties["query"]["type"] == "string"
        assert properties["max_chars"]["type"] == "integer"
        assert properties["max_chars"]["default"] == DEFAULT_MAX_CHARS
        assert properties["max_chars"]["minimum"] == 1
        # 只按查询文本与已登记项目匹配，调用方不能指定 project/workspace
        assert set(properties) == {"query", "max_chars"}

    @pytest.mark.parametrize("mode", [ContextMode.LEGACY, ContextMode.COMPAT])
    def test_legacy_and_compat_never_expose_project_recall(self, mode):
        names = {
            spec.name
            for spec in tool_specs(
                adapter="codex", mode=mode, health=_health(mode=mode),
            )
        }
        assert "context_project_recall" not in names

    @pytest.mark.parametrize("health", [None, _health(ready=False)])
    def test_degraded_primary_never_exposes_project_recall(self, health):
        names = {
            spec.name
            for spec in tool_specs(
                adapter="codex", mode=ContextMode.PRIMARY, health=health,
            )
        }
        assert "context_project_recall" not in names


# ---------------------------------------------------------------------------
# MCP 工具端到端：真实 ContextService + sqlite 临时库
# ---------------------------------------------------------------------------


class _FakeVectorIndex:
    def __init__(self, path, *, initialized=True):
        self.path = path.resolve()
        self.initialized = initialized
        self.dirty = False
        self.ids = set()

    def is_dirty(self):
        return self.dirty

    def count(self):
        if not self.initialized:
            raise RuntimeError("index is not initialized")
        return len(self.ids)

    def mark_dirty(self):
        self.dirty = True

    def preserve_dirty(self):
        pass

    def clear_dirty(self):
        self.dirty = False

    def initialize(self, dim=768):
        self.initialized = True

    def add(self, mem_id, embedding):
        self.ids.add(mem_id)

    def remove(self, mem_id):
        self.ids.discard(mem_id)
        return False

    def save(self):
        pass

    def search(self, embedding, k):
        return []

    def close(self):
        pass


class _FakeEngine:
    is_loaded = True

    def __init__(self, dim):
        self._dim = dim

    def encode_document(self, text):
        return self._encode(text)

    def encode_query(self, text):
        return self._encode(text)

    def _encode(self, text):
        digest = hashlib.md5(text.encode()).digest()
        rng = np.random.RandomState(int.from_bytes(digest[:4], "big"))
        vector = rng.randn(self._dim).astype(np.float32)
        return (vector / np.linalg.norm(vector)).tolist()

    def close(self):
        pass


def _make_server(test_config, *, mode="primary", adapter="codex",
                 degraded=False):
    test_config.context_mode = mode
    test_config.adapter = adapter
    with MemoryStore(test_config):
        pass
    server = MemoryMCPServer(config=test_config)
    server._init_done.set()
    server.vidx.initialize(dim=test_config.embedding_dim)
    parsed = ContextMode(mode)
    engine = _FakeEngine(test_config.embedding_dim)
    context_index = _FakeVectorIndex(
        test_config.context_vector_path, initialized=not degraded,
    )
    service = ContextService(
        test_config, vector_index=context_index, embedding_engine=engine,
    )
    service._legacy_vector = _FakeVectorIndex(test_config.vector_path)
    service.initialize(mode=parsed, adapter=adapter)
    server.context_service = service
    facade = service.legacy_facade()
    server.retriever = Retriever(test_config, facade, server.vidx, server.engine)
    server.conflict_detector = ConflictDetector(facade)
    return server


def _register(service, *projects, aliases=()):
    store = service.store
    with store.transaction():
        project_store = ProjectStore(
            store._connection(), store._require_transaction, generic_names=(),
        )
        for project in projects:
            project_store.register_project(project)
        for alias, project in aliases:
            project_store.add_alias(alias, project)


def _seed(service, key, value):
    return service.legacy_add(
        LegacyAddRequest(
            key=key, value=value, attribute="fact", confidence=0.9,
            tier="normal",
        )
    )


def _add_project_pinned(service, project: str, index: int) -> int:
    """One project-scoped pinned constraint; fills the recall item cap.

    Direct store writes bypass the service write path, so the fake context
    index is updated explicitly to keep primary health ready.
    """
    item = service.store.create_item(ContextItemDraft(
        identity_key=f"project:{project}:constraint:pinned-{index}",
        content_type=ContextContentType.CONSTRAINT,
        layers=ContextLayers(
            l0=f"{project} 约束 {index}。",
            l1=f"{project} 约束 {index}：必须保留。",
            l2=f"{project} 约束 {index} 的来源。",
            generator="test-suite",
        ),
        project=project,
        scope=ContextScope.PROJECT,
        status=ContextStatus.ACTIVE,
        tier=ContextTier.PINNED,
        importance=9.0,
        confidence=1.0,
    ))
    service.vector_index.add(item.id, [0.0] * service.config.embedding_dim)
    return item.id


def _add_ready_rollup_summary(service, project: str, *, l1: str) -> int:
    """Create the project's current summary and point a ready rollup at it."""
    item = service.store.create_item(ContextItemDraft(
        identity_key=f"project:{project}:knowledge:current",
        content_type=ContextContentType.PROJECT_SUMMARY,
        layers=ContextLayers(
            l0=f"{project} 当前状态。", l1=l1, l2=f"{project} 完整细节。",
            generator="test-suite",
        ),
        project=project,
        scope=ContextScope.PROJECT,
        status=ContextStatus.ACTIVE,
        importance=9.0,
        confidence=0.9,
    ))
    # Direct store writes bypass the service write path, so primary health
    # would otherwise see the item missing from the context vector index.
    service.vector_index.add(item.id, [0.0] * service.config.embedding_dim)
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_project_rollups (project, current_context_id,"
            " source_set_hash, covered_through, generator_version, status,"
            " revision, updated_at) VALUES (?, ?, '', '', 'test-suite', 'ready',"
            " 1, ?)",
            (project, item.id, "2026-09-21 00:00:00"),
        )
    return item.id


def _tool_call(server, name, arguments, req_id=7):
    response = server._handle_request({
        "method": "tools/call", "id": req_id, "jsonrpc": "2.0",
        "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    import json

    return result, json.loads(result["content"][0]["text"])


def _tool_names(server):
    response = server._handle_request({
        "method": "tools/list", "id": 3, "jsonrpc": "2.0",
    })
    return {tool["name"] for tool in response["result"]["tools"]}


class TestMcpProjectRecallTool:
    def test_mentions_another_project_and_returns_its_details(
            self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "alpha", "beta")
        _seed(service, "project:beta:fact:deploy", "beta 部署前必须先做备份演练")
        _seed(service, "project:alpha:fact:review", "alpha 的评审流程仅限内部")

        assert "context_project_recall" in _tool_names(server)
        result, payload = _tool_call(
            server, "context_project_recall",
            {"query": "beta 部署前必须先做备份演练"},
        )

        assert result.get("isError") is not True, payload
        assert "error" not in payload, payload
        assert payload["matched_projects"] == ["beta"]
        assert "beta 部署前必须先做备份演练" in payload["block"]
        assert "alpha 的评审流程仅限内部" not in payload["block"]
        assert payload["selected_ids"]
        assert payload["used_chars"] == len(payload["block"])
        assert payload["block"].startswith("[BEGIN EVOLVMEM PROJECT RECALL]")
        # 只读不切换绑定/焦点：注册表快照完全不变
        snapshot = service._project_store().snapshot()
        assert snapshot.bindings == ()
        assert snapshot.projects == ("alpha", "beta")

    def test_project_recall_injects_the_current_ready_rollup_summary(
            self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "alpha", "beta")
        # Twelve project-scoped pinned constraints fill the 12-item cap.
        for index in range(12):
            _add_project_pinned(service, "alpha", index)
        summary_id = _add_ready_rollup_summary(
            service, "alpha", l1="alpha 当前进展：每日摘要注入待验收。")
        other_id = _add_ready_rollup_summary(
            service, "beta", l1="beta 当前进展：部署流程待复核。")

        result, payload = _tool_call(
            server, "context_project_recall", {"query": "alpha 的问题"},
        )

        assert result.get("isError") is not True, payload
        assert payload["matched_projects"] == ["alpha"]
        assert summary_id in payload["selected_ids"]
        assert other_id not in payload["selected_ids"]
        assert len(payload["selected_ids"]) <= test_config.context_inject_max_items
        assert payload["used_chars"] == len(payload["block"]) <= DEFAULT_MAX_CHARS
        assert "alpha 当前进展：每日摘要注入待验收。" in payload["block"]
        assert "beta 当前进展：部署流程待复核。" not in payload["block"]

    def test_project_recall_excludes_global_pinned_policy(self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "beta")
        own = _seed(service, "project:beta:fact:deploy", "beta 部署前必须先做备份演练")
        global_item = service.legacy_add(LegacyAddRequest(
            key="user:general:constraint:review", value="所有任务执行前必须确认真实工作区身份。",
            attribute="constraint", confidence=1.0, importance=10, tier="pinned"))
        _, payload = _tool_call(server, "context_project_recall", {
            "query": "beta 部署前必须先做备份演练", "max_chars": 1200})
        assert own.context_id in payload["selected_ids"]
        assert global_item.context_id not in payload["selected_ids"]

    def test_mention_of_two_projects_stays_within_budget(self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "alpha", "beta")
        _seed(service, "project:beta:fact:deploy",
              "alpha 和 beta 的流程：beta 部署前必须先做备份演练")
        _seed(service, "project:alpha:fact:review",
              "alpha 和 beta 的流程：alpha 的评审流程仅限内部")

        _, payload = _tool_call(
            server, "context_project_recall",
            {"query": "alpha 和 beta 的流程", "max_chars": 1200},
        )

        assert payload["matched_projects"] == ["alpha", "beta"]
        assert len(payload["block"]) <= 1200
        assert payload["used_chars"] == len(payload["block"])
        # 两个提及项目都真实渲染，且共享同一预算（Windows 侧 1200 上限可容纳）
        assert "beta 部署前必须先做备份演练" in payload["block"]
        assert "alpha 的评审流程仅限内部" in payload["block"]

    def test_query_without_mention_returns_empty_block(self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "alpha", "beta")
        _seed(service, "project:beta:fact:deploy", "beta 部署前必须先做备份演练")

        _, payload = _tool_call(
            server, "context_project_recall", {"query": "继续之前的排查"},
        )

        assert payload == {
            "block": "",
            "selected_ids": [],
            "matched_projects": [],
            "used_chars": 0,
        }

    def test_alias_mention_is_resolved_to_the_canonical_project(
            self, test_config):
        server = _make_server(test_config)
        service = server.context_service
        _register(service, "evolvmem", aliases=(("记忆插件", "evolvmem"),))
        _seed(service, "project:evolvmem:fact:index", "记忆插件索引重建要先停写入")

        _, payload = _tool_call(
            server, "context_project_recall",
            {"query": "记忆插件索引重建"},
        )

        assert payload["matched_projects"] == ["evolvmem"]
        assert "记忆插件索引重建要先停写入" in payload["block"]

    def test_invalid_arguments_return_stable_error(self, test_config):
        server = _make_server(test_config)
        for arguments in (
            {},
            {"query": ""},
            {"query": "  "},
            {"query": 7},
            {"query": "beta", "max_chars": 0},
            {"query": "beta", "max_chars": "4000"},
            {"query": "beta", "project": "beta"},
        ):
            result, payload = _tool_call(
                server, "context_project_recall", arguments,
            )
            assert result["isError"] is True, arguments
            assert payload["error"] == "invalid_arguments", arguments

    def test_legacy_mode_keeps_the_tool_hidden(self, test_config):
        server = _make_server(test_config, mode="legacy")
        result, payload = _tool_call(
            server, "context_project_recall", {"query": "beta 的问题"},
        )
        assert result["isError"] is True
        assert "Unknown tool" in payload["error"]

    def test_degraded_primary_keeps_the_tool_hidden(self, test_config):
        server = _make_server(test_config, degraded=True)
        result, payload = _tool_call(
            server, "context_project_recall", {"query": "beta 的问题"},
        )
        assert result["isError"] is True
        assert "Unknown tool" in payload["error"]
