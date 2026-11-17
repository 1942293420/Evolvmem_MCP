"""项目进展召回：当前 workstream 断点（L1）的只读辅助回归测试。

覆盖本轮最小修复的必要单元/集成用例：

- 集成复现：普通 ``session_start`` 知识池（``context_project_recall`` 当前
  唯一来源）不包含 workstream 的当前断点，新辅助函数补上它；
- 只从被注入的 ``service.store``（本用户自己的库）读取，绝不跨库；
- 项目在 ``continuity_workstreams.project`` 与 ``context_items.project``
  两侧严格一致，且 item scope 必须是 project；
- item.status=active、content_type=workstream_checkpoint、layer=l1；
  不读 superseded 旧断点；候选/归档 item 不冒充当前进展；取消/暂停等
  workstream 状态按数据库原值诚实显示；
- 至多最近 3 条，按当前断点 created_at DESC, id DESC；
- ``max_chars`` 是硬总预算：单条可有界截断但保留完整日期/id/状态头部与
  明确截断标识，预算不足返回空，``selected_ids`` 只含被完整包含的 id；
- 只读：不改焦点、绑定、访问计数或任何一行数据。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextMode,
    ContextSessionStartRequest,
    ContextStatus,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.continuity_models import (
    ContinuityBeginRequest,
    ContinuityCheckpointRequest,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_progress_recall import (
    DEFAULT_MAX_CHARS,
    ProjectProgressResult,
    read_recent_project_progress,
)
from evolvmem.workspace_identity import WorkspaceIdentityProvider


# ---------------------------------------------------------------------------
# 环境：真实 store + 真实 ContinuityService 写入断点，辅助函数只读
# ---------------------------------------------------------------------------


@dataclass
class _Env:
    root: Path
    config: Config
    store: ContextStore
    continuity: ContinuityService
    service: ContextService

    def workspace(self, name: str = "workspace") -> str:
        path = self.root / name
        path.mkdir(exist_ok=True)
        return str(path)

    def begin(self, project, objective, *, workspace="workspace", **options):
        return self.continuity.begin(ContinuityBeginRequest(
            workspace_path=self.workspace(workspace),
            project=project,
            objective=objective,
            **options,
        ))

    def checkpoint(self, previous, *, project, workspace="workspace",
                   action="update", **options):
        values = {
            "action": action,
            "workspace_path": self.workspace(workspace),
            "project_hint": project,
            "workstream_id": previous.workstream_id,
            "expected_checkpoint_revision": previous.checkpoint_revision,
            "expected_state_version": previous.state_version,
        }
        values.update(options)
        return self.continuity.checkpoint(ContinuityCheckpointRequest(**values))

    def sql(self, statement, params=()):
        with self.store.transaction():
            self.store._connection().execute(statement, params)


def _new_env(root: Path, *, name: str = "memory") -> _Env:
    config = Config(data_dir=root / name)
    provider = WorkspaceIdentityProvider(config.data_dir / f"{name}.key")
    provider.bootstrap_key()
    store = ContextStore(config)
    store.initialize()
    continuity = ContinuityService(config, store, provider)
    service = ContextService(config, store=store)
    service.initialize(mode=ContextMode.SHADOW, adapter="test-suite")
    return _Env(root, config, store, continuity, service)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Config 显式 data_dir 也认这个环境覆盖，清掉保证数据只落在 tmp_path
    monkeypatch.delenv("EVOLVMEM_DATA_DIR", raising=False)
    created = _new_env(tmp_path)
    try:
        yield created
    finally:
        created.store.close()


_READ_ONLY_TABLES = (
    "context_items",
    "context_layers",
    "context_sources",
    "context_project_registry",
    "context_project_aliases",
    "context_project_workspace_bindings",
    "context_project_resolutions",
    "context_project_rollups",
    "continuity_workstreams",
    "continuity_focus",
    "continuity_events",
)


def _state(env: _Env) -> dict:
    """相关表的完整行 + 连接累计变更数，用于只读断言。

    覆盖记忆、项目注册/绑定、摘要来源与 continuity 焦点：辅助读改任何一样
    都会在这里现形。
    """
    connection = env.store._connection()
    state = {
        table: [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
        for table in _READ_ONLY_TABLES
    }
    state["changes"] = connection.total_changes
    return state


# ---------------------------------------------------------------------------
# 当前断点：内容、selected_ids、新断点优先、排序与上限
# ---------------------------------------------------------------------------


class TestCurrentCheckpointRecall:
    def test_current_checkpoint_is_returned_with_its_context_id(self, env):
        begun = env.begin(
            "alpha", "移植记忆服务",
            completed_steps=("读取现有 schema",),
            current_step="实现召回",
            next_action="跑回归",
        )

        result = read_recent_project_progress(env.service, "alpha", 2000)

        assert isinstance(result, ProjectProgressResult)
        assert "移植记忆服务" in result.text
        assert "读取现有 schema" in result.text
        # 头部完整：日期 / id / 状态
        assert f"id={begun.context_id}" in result.text
        assert "created_at(UTC)=" in result.text
        assert "status=open" in result.text
        # 历史任务记录与 UTC 标注，明确不代表 Git/线上
        assert "历史任务记录" in result.text
        assert "UTC" in result.text
        assert "Git" in result.text
        assert result.selected_ids == (begun.context_id,)
        assert result.used_chars == len(result.text) <= 2000

    def test_default_budget_is_bounded(self, env):
        env.begin("alpha", "默认预算任务")

        result = read_recent_project_progress(env.service, "alpha")

        assert result.text
        assert len(result.text) <= DEFAULT_MAX_CHARS

    def test_only_the_current_checkpoint_of_a_workstream_is_read(self, env):
        begun = env.begin("alpha", "旧目标：先做 A", completed_steps=("旧断点步骤",))
        latest = env.checkpoint(
            begun, project="alpha",
            objective="新目标：先做 B",
            completed_steps=("新断点步骤",),
        )

        result = read_recent_project_progress(env.service, "alpha", 3000)

        assert "新目标：先做 B" in result.text
        assert "新断点步骤" in result.text
        # superseded 旧断点绝不混入
        assert "旧目标：先做 A" not in result.text
        assert "旧断点步骤" not in result.text
        assert result.selected_ids == (latest.context_id,)
        old_item = env.store.get_item(begun.context_id)
        assert old_item.status is ContextStatus.SUPERSEDED

    def test_newest_created_at_wins_over_higher_id(self, env):
        older_date = env.begin("alpha", "九月一日断点")
        newer_date = env.begin("alpha", "九月二日断点")
        # id 大的一条反而日期更旧：必须按 created_at 而不是 id 排序
        env.sql(
            "UPDATE context_items SET created_at='2026-09-02 00:00:00' WHERE id=?",
            (older_date.context_id,),
        )
        env.sql(
            "UPDATE context_items SET created_at='2026-09-01 00:00:00' WHERE id=?",
            (newer_date.context_id,),
        )

        result = read_recent_project_progress(env.service, "alpha", 4000)

        assert result.selected_ids == (older_date.context_id, newer_date.context_id)
        assert result.text.index("九月一日断点") < result.text.index("九月二日断点")

    def test_same_timestamp_orders_by_id_desc_and_limits_to_three(self, env):
        context_ids = [
            env.begin("alpha", f"同日任务 {index}").context_id for index in range(4)
        ]
        env.sql("UPDATE context_items SET created_at='2026-09-10 00:00:00'")

        result = read_recent_project_progress(env.service, "alpha", 4000)

        # 至多最近 3 条；同秒时 id DESC 决定先后
        assert result.selected_ids == (
            context_ids[3], context_ids[2], context_ids[1],
        )
        assert f"id={context_ids[0]}" not in result.text


# ---------------------------------------------------------------------------
# 严格隔离与状态诚信
# ---------------------------------------------------------------------------


class TestStrictIsolationAndStatus:
    def test_other_project_checkpoints_are_never_returned(self, env):
        alpha = env.begin("alpha", "alpha 的进展", workspace="alpha-ws")
        env.begin("beta", "beta 的进展", workspace="beta-ws")

        result = read_recent_project_progress(env.service, "alpha", 3000)

        assert "alpha 的进展" in result.text
        assert "beta 的进展" not in result.text
        assert result.selected_ids == (alpha.context_id,)

    def test_only_the_injected_service_store_is_read(self, env, tmp_path):
        env.begin("alpha", "本用户库里的进展")
        other = _new_env(tmp_path, name="other-memory")
        try:
            other.begin("alpha", "别的用户库里的进展")
            mine = read_recent_project_progress(env.service, "alpha", 2000)
            theirs = read_recent_project_progress(other.service, "alpha", 2000)
        finally:
            other.store.close()

        assert "本用户库里的进展" in mine.text
        assert "别的用户库里的进展" not in mine.text
        assert "别的用户库里的进展" in theirs.text
        assert "本用户库里的进展" not in theirs.text

    def test_mismatched_item_project_is_never_returned(self, env):
        begun = env.begin("alpha", "错配项目断点")
        env.sql(
            "UPDATE context_items SET project='beta' WHERE id=?",
            (begun.context_id,),
        )

        assert read_recent_project_progress(env.service, "alpha", 2000).text == ""
        assert read_recent_project_progress(env.service, "beta", 2000).text == ""

    def test_global_scope_item_is_not_returned(self, env):
        begun = env.begin("alpha", "全局化断点")
        env.sql(
            "UPDATE context_items SET scope='global' WHERE id=?",
            (begun.context_id,),
        )

        result = read_recent_project_progress(env.service, "alpha", 2000)

        assert result.text == ""
        assert result.selected_ids == ()

    def test_non_checkpoint_content_type_is_not_returned(self, env):
        begun = env.begin("alpha", "改成摘要的断点")
        env.sql(
            "UPDATE context_items SET content_type='project_summary' WHERE id=?",
            (begun.context_id,),
        )

        assert read_recent_project_progress(env.service, "alpha", 2000).text == ""

    def test_missing_l1_layer_is_not_returned(self, env):
        begun = env.begin("alpha", "只有 L0 的断点")
        env.sql(
            "DELETE FROM context_layers WHERE item_id=? AND layer='l1'",
            (begun.context_id,),
        )

        assert read_recent_project_progress(env.service, "alpha", 2000).text == ""

    @pytest.mark.parametrize(
        "item_status", ["candidate", "archived", "superseded"],
    )
    def test_non_active_current_item_is_not_claimed_as_progress(
        self, env, item_status,
    ):
        # continuity_workstreams 仍指向该 item，但它不是 active：不得冒充当前进展
        begun = env.begin("alpha", f"{item_status} 断点")
        env.sql(
            "UPDATE context_items SET status=? WHERE id=?",
            (item_status, begun.context_id),
        )

        result = read_recent_project_progress(env.service, "alpha", 2000)

        assert result.text == ""
        assert result.selected_ids == ()

    @pytest.mark.parametrize(
        "action,status", [("cancel", "cancelled"), ("pause", "paused")],
    )
    def test_workstream_status_is_shown_honestly(self, env, action, status):
        begun = env.begin("alpha", "状态诚信断点")
        latest = env.checkpoint(begun, project="alpha", action=action)

        result = read_recent_project_progress(env.service, "alpha", 2000)

        assert f"status={status}" in result.text
        assert f"id={latest.context_id}" in result.text
        assert "状态诚信断点" in result.text
        assert result.selected_ids == (latest.context_id,)

    def test_unknown_or_empty_project_returns_empty(self, env):
        env.begin("alpha", "存在但不该被别的名字读到")

        for project in ("ghost", "  ", ""):
            result = read_recent_project_progress(env.service, project, 2000)
            assert result.text == ""
            assert result.selected_ids == ()


# ---------------------------------------------------------------------------
# 字符预算：硬上限、预算不足为空、有界截断与 selected_ids 诚实
# ---------------------------------------------------------------------------


class TestCharacterBudget:
    @pytest.mark.parametrize(
        "budget", [1, 10, 40, 100, 160, 200, 260, 400, 800, 2000],
    )
    def test_budget_is_a_hard_total_limit(self, env, budget):
        env.begin(
            "alpha", "预算测试任务",
            completed_steps=("阶段一" * 30, "阶段二" * 30),
            current_step="当前步骤" * 20,
            next_action="下一步" * 20,
        )

        result = read_recent_project_progress(env.service, "alpha", budget)

        assert len(result.text) <= budget
        assert result.used_chars == len(result.text)

    def test_budget_too_small_for_header_and_content_returns_empty(self, env):
        env.begin("alpha", "放不下的断点", completed_steps=("阶段一",))

        for budget in (1, 40, 100):
            result = read_recent_project_progress(env.service, "alpha", budget)
            assert result.text == ""
            assert result.selected_ids == ()

    def test_truncated_entry_keeps_complete_header_and_explicit_marker(self, env):
        begun = env.begin(
            "alpha",
            "把 Windows 可用性修复做成可复现验收" * 6,
            accepted_decisions=("先只读",) * 20,
            completed_steps=("阶段一完成" * 30,),
            current_step="实现辅助函数" * 10,
            next_action="跑回归" * 10,
        )

        result = read_recent_project_progress(env.service, "alpha", 240)

        assert len(result.text) <= 240
        # 头部完整保留
        assert f"id={begun.context_id}" in result.text
        assert "status=open" in result.text
        assert "created_at(UTC)=" in result.text
        # 目标/已完成仍各有一段，长行与省略都有明确标识
        assert "目标" in result.text
        assert "已完成" in result.text
        assert "〔截断〕" in result.text
        assert "〔其余内容因字符预算省略〕" in result.text
        # 被截断 ⇒ 不是被完整包含
        assert result.selected_ids == ()

    def test_selected_ids_only_cover_fully_included_entries(self, env):
        long_entry = env.begin("alpha", "旧任务" * 40)
        short_entry = env.begin("alpha", "新任务", completed_steps=("完成",))

        result = read_recent_project_progress(env.service, "alpha", 340)

        assert len(result.text) <= 340
        # 新条完整 → 计入 selected_ids；旧条只保留头部与截断片段 → 不计入
        assert f"id={short_entry.context_id}" in result.text
        assert f"id={long_entry.context_id}" in result.text
        assert result.selected_ids == (short_entry.context_id,)
        assert "〔截断〕" in result.text

    @pytest.mark.parametrize("budget", [0, -1, True, "100", None, 1.5])
    def test_invalid_budget_is_rejected(self, env, budget):
        with pytest.raises(ValueError):
            read_recent_project_progress(env.service, "alpha", budget)


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_reading_changes_no_row_focus_or_access_count(self, env):
        env.begin(
            "alpha", "只读断点",
            completed_steps=("不要被计数",), current_step="读一下",
        )
        before = _state(env)

        result = read_recent_project_progress(env.service, "alpha", 2000)

        assert result.text
        assert _state(env) == before

    def test_service_without_a_store_fails_open_to_empty(self, env):
        env.begin("alpha", "无 store 时的边界")
        before = _state(env)

        class _NoStore:
            pass

        result = read_recent_project_progress(_NoStore(), "alpha", 2000)

        assert result.text == ""
        assert result.selected_ids == ()
        assert _state(env) == before


# ---------------------------------------------------------------------------
# 集成复现：普通知识池遗漏当前断点，新辅助函数补上
# ---------------------------------------------------------------------------


class TestNormalKnowledgePoolGap:
    def test_session_start_pool_omits_the_current_checkpoint(self, env):
        """普通 session_start 知识池（context_project_recall 当前唯一来源）
        不带出 continuity_workstreams 当前指向的断点；这正是本修复的缺口。"""
        begun = env.begin(
            "alpha", "Windows 可用性修复",
            completed_steps=("阶段一已完成-windows-mark",),
        )

        pool = env.service.session_start(
            ContextSessionStartRequest(
                project="alpha", query="alpha 最近更新", max_chars=4000,
            ),
            project_only=True,
        )

        assert "阶段一已完成-windows-mark" not in pool.block

        progress = read_recent_project_progress(env.service, "alpha", 2000)

        assert "阶段一已完成-windows-mark" in progress.text
        assert progress.selected_ids == (begun.context_id,)
        # 断点维持默认 0.5 置信度：本修复不靠提分让它进普通池
        assert env.store.get_item(begun.context_id).confidence == 0.5

    def test_reading_progress_does_not_disturb_session_start_pool(self, env):
        begun = env.begin(
            "alpha", "互不干扰",
            completed_steps=("池外进度-mark",),
        )
        before = _state(env)

        read_recent_project_progress(env.service, "alpha", 2000)
        pool = env.service.session_start(
            ContextSessionStartRequest(
                project="alpha", query="alpha 最近更新", max_chars=4000,
            ),
            project_only=True,
        )

        # 辅助读没有改写知识池内容，也没有把断点塞进旧池
        assert "池外进度-mark" not in pool.block
        assert _state(env)["continuity_workstreams"] == before["continuity_workstreams"]
        assert _state(env)["continuity_focus"] == before["continuity_focus"]
        assert begun.context_id > 0
