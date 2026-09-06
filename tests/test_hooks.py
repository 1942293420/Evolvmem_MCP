"""hooks module tests."""

import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from evolvmem.context_models import ContextMode, ContextStatus
from evolvmem.context_service import ContextService
from evolvmem.hooks import get_session_start_block, get_stop_prompt
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


class TestSessionStartHook:
    def test_empty_memories_returns_empty_string(self, test_config):
        result = get_session_start_block(config=test_config)
        assert result == ""

    def test_active_memories_formatted_in_block(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="user:pref:language", value="Chinese", tags=["preference"])
            store.add(
                key="project:arch:db",
                value="PostgreSQL",
                tags=["architecture", "database"],
            )

        result = get_session_start_block(config=test_config)

        assert "Persistent Memory" in result
        assert "EvolvMem" in result
        assert "user:pref:language" in result
        assert "Chinese" in result
        assert "[preference]" in result
        assert "project:arch:db" in result
        assert "PostgreSQL" in result
        assert "[architecture,database]" in result

    def test_inject_max_count_limits_entries(self, test_config):
        test_config.inject_max_count = 3
        with MemoryStore(test_config) as store:
            for i in range(5):
                store.add(key=f"p:t:{i}", value=f"value {i}")

        result = get_session_start_block(config=test_config)

        bullets = [l for l in result.splitlines() if l.startswith("- **")]
        assert len(bullets) == 3
        # 落选者由索引区（或计数行）覆盖
        index_lines = [l for l in result.splitlines()
                       if l.startswith("- p:t:") and not l.startswith("- **")]
        assert len(index_lines) == 2
        assert "memory_search" in result

    def test_inject_max_chars_budget(self, test_config):
        test_config.inject_max_chars = 200
        test_config.inject_index_max_chars = 0  # 关闭索引层，退化为纯截断
        with MemoryStore(test_config) as store:
            for i in range(5):
                store.add(key=f"p:t:{i}", value="x" * 150)

        result = get_session_start_block(config=test_config)

        bullets = [l for l in result.splitlines() if l.startswith("- **")]
        assert len(bullets) == 1
        assert "4 more memories not injected" in result

    def test_no_omission_note_when_all_fit(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="p:t:0", value="small value")

        result = get_session_start_block(config=test_config)

        assert "not injected" not in result

    def test_pinned_always_injected_regardless_of_recency(self, test_config):
        with MemoryStore(test_config) as store:
            # pinned 但很老
            store.add(key="user:constraint:no-prod", value="禁止直接操作生产库",
                      attribute="constraint", importance=8.0, tier="pinned")
            # normal 但更新（updated_at 更晚）
            for i in range(5):
                store.add(key=f"p:t:fact:{i}", value=f"fact {i}",
                          attribute="fact", importance=3.0)

        result = get_session_start_block(config=test_config)

        assert "## 常驻记忆" in result
        assert "禁止直接操作生产库" in result

    def test_normal_memories_ranked_by_importance(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:trivial", value="无关紧要的小事",
                      attribute="fact", importance=2.0)
            store.add(key="p:t:decision:arch", value="核心架构决策",
                      attribute="decision", importance=9.0)

        result = get_session_start_block(config=test_config)

        arch_pos = result.index("核心架构决策")
        trivial_pos = result.index("无关紧要的小事")
        assert arch_pos < trivial_pos

    def test_pinned_budget_separate_from_normal(self, test_config):
        test_config.inject_pinned_max_chars = 100
        test_config.inject_max_chars = 400
        with MemoryStore(test_config) as store:
            for i in range(5):
                store.add(key=f"u:constraint:{i}", value="x" * 90,
                          attribute="constraint", importance=8.0, tier="pinned")
            store.add(key="p:t:fact:0", value="普通事实", importance=5.0)

        result = get_session_start_block(config=test_config)

        # pinned 预算只容纳 1 条，其余 4 条 pinned 落入索引层
        assert "普通事实" in result  # normal 层不被 pinned 挤占
        assert result.count("- **u:constraint:") == 1

    def test_key_prefix_quota_prevents_domination(self, test_config):
        test_config.inject_key_prefix_quota = 2
        with MemoryStore(test_config) as store:
            for i in range(5):
                store.add(key=f"project:purchase:fact:{i}", value=f"采购记忆 {i}",
                          importance=9.0)
            store.add(key="project:other:fact:0", value="其他项目记忆",
                      importance=5.0)

        result = get_session_start_block(config=test_config)

        bullets = [l for l in result.splitlines() if l.startswith("- **project:purchase")]
        assert len(bullets) == 2
        assert "其他项目记忆" in result

    def test_reference_tier_never_injected_in_full(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="project:tech:arch:big-doc",
                      value="很长的参考文档 " + "x" * 300,
                      importance=9.0, tier="reference")
            store.add(key="p:t:fact:small", value="普通事实",
                      importance=5.0)

        result = get_session_start_block(config=test_config)

        # reference 不进全文层（即使 importance 很高），只出现在索引行
        assert "- **project:tech:arch:big-doc**" not in result
        assert "很长的参考文档" not in result
        assert "- project:tech:arch:big-doc" in result
        # normal 记忆照常注入
        assert "普通事实" in result

    def test_omitted_memories_appear_as_index_lines(self, test_config):
        test_config.inject_max_chars = 200
        test_config.inject_index_max_chars = 500
        with MemoryStore(test_config) as store:
            for i in range(4):
                store.add(key=f"p:t:fact:{i}", value="x" * 150,
                          tags=["t"], importance=5.0)

        result = get_session_start_block(config=test_config)

        assert "## 记忆索引" in result
        assert "- p:t:fact:" in result  # 索引行（无 ** 加粗）
        assert "memory_search" in result

    def test_index_budget_overflow_shows_count_only(self, test_config):
        test_config.inject_max_chars = 200
        test_config.inject_index_max_chars = 60
        with MemoryStore(test_config) as store:
            for i in range(6):
                store.add(key=f"p:t:fact:{i}", value="x" * 150, importance=5.0)

        result = get_session_start_block(config=test_config)

        assert "more memories not injected" in result

    def test_index_lines_ordered_by_score(self, test_config):
        test_config.inject_pinned_max_chars = 50  # pinned 层只留 1 条
        test_config.inject_max_chars = 200        # 精选层只留 1 条
        test_config.inject_index_max_chars = 30   # 索引层只留 1 行
        with MemoryStore(test_config) as store:
            # pin 占满 pinned 预算 → low 落选进 pinned_omit；
            # first 占满精选预算 → high 落选进 normal_omit。
            # 旧拼接顺序 pinned_omit 在 normal_omit 前，低分 low 会先入索引；
            # 索引层应按 score 降序，只显示分最高的 high。
            store.add(key="p:t:fact:pin", value="p" * 40,
                      importance=9.0, tier="pinned")
            store.add(key="p:t:fact:low", value="低分",
                      importance=1.0, tier="pinned")
            store.add(key="p:t:fact:first", value="x" * 180, importance=9.0)
            store.add(key="p:t:fact:high", value="高分", importance=8.0)

        result = get_session_start_block(config=test_config)

        assert "- p:t:fact:high" in result
        assert "- p:t:fact:low" not in result

    def test_session_start_creates_forgetting_marker(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="p:t:0", value="some value")

        get_session_start_block(config=test_config)

        assert (test_config.data_dir / ".last_forget").exists()

    def test_session_start_creates_consolidation_marker(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="p:t:0", value="some value")
        get_session_start_block(config=test_config)
        assert (test_config.data_dir / ".last_consolidate").exists()

    def test_consolidation_skipped_when_disabled(self, test_config):
        test_config.consolidate_auto_run_hours = 0
        with MemoryStore(test_config) as store:
            store.add(key="p:t:0", value="some value")
        get_session_start_block(config=test_config)
        assert not (test_config.data_dir / ".last_consolidate").exists()

    def test_cwd_project_boosts_matching_memories(self, test_config, monkeypatch):
        monkeypatch.chdir(test_config.data_dir)  # basename = 临时目录名，不含 'purchase'
        with MemoryStore(test_config) as store:
            # 不匹配的先插入：无 relevance 接线时基线序为「其他记忆」在前，
            # 只有 relevance 加分能把「采购记忆」翻到前面
            store.add(key="project:other:fact:b", value="其他记忆", importance=5.0)
            store.add(key="project:purchase:fact:a", value="采购记忆", importance=5.0)
            # 冻结 recency：两条 updated_at 强制相同，排除跨秒墙钟微差
            # （无公开 API 写时间戳，直接走连接；'now' 为 UTC 当前秒，不触发自动遗忘）
            store._conn.execute(
                "UPDATE memories SET updated_at=strftime('%Y-%m-%d %H:%M:%S','now')")
            store._conn.commit()
        # 别名让任意目录都匹配 purchase
        test_config.inject_project_aliases = {
            test_config.data_dir.name: "purchase"}
        result = get_session_start_block(config=test_config)
        assert result.index("采购记忆") < result.index("其他记忆")


class TestSessionStartCutoverRouting:
    """SessionStart 的维护写入经 ContextService 兼容门面路由。"""

    @staticmethod
    def _compat_seed_service(config):
        """compat 模式的播种服务（legacy 投影 schema 先就位）。"""
        with MemoryStore(config):
            pass
        service = ContextService(config)
        service.initialize(mode=ContextMode.COMPAT, adapter="test-hooks")
        return service

    def test_compat_block_keeps_legacy_format_and_archives_both_sides(
            self, test_config):
        test_config.context_mode = "compat"
        seed = self._compat_seed_service(test_config)
        facade = seed.legacy_facade()
        expired_id = facade.add(key="p:t:fact:expired", value="过期的规则",
                                expires_at="2020-01-01 00:00:00")
        facade.add(key="user:pref:language", value="Chinese",
                   tags=["preference"])
        expired_ctx = seed.store.resolve_legacy_mapping(expired_id)
        assert expired_ctx is not None

        result = get_session_start_block(config=test_config)

        # 旧格式渲染不变
        assert "Persistent Memory" in result
        assert "- **user:pref:language** [preference]: Chinese" in result
        # 过期记忆不注入
        assert "过期的规则" not in result
        # 自动遗忘按原节奏跑（marker 落盘），且经门面归档双侧
        assert (test_config.data_dir / ".last_forget").exists()
        assert facade.get_by_id(expired_id)["status"] == "archived"
        assert seed.store.get_item(expired_ctx).status is ContextStatus.ARCHIVED
        seed.close()

    def test_legacy_mode_session_start_writes_legacy_only(self, test_config):
        """切换前 legacy 模式：渲染与维护保持旧形状，Core 表保持空。"""
        test_config.context_mode = "legacy"
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:expired", value="过期事实",
                      expires_at="2020-01-01 00:00:00")
            store.add(key="p:t:fact:fresh", value="现行事实")

        result = get_session_start_block(config=test_config)

        assert "现行事实" in result
        assert "过期事实" not in result
        with MemoryStore(test_config) as store:
            rows = store.get_by_key("p:t:fact:expired")
            assert rows[0]["status"] == "archived"  # 维护照旧归档 legacy 侧
        conn = sqlite3.connect(test_config.db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM context_items").fetchone()[0] == 0
        finally:
            conn.close()

    def test_invalid_context_mode_falls_back_to_legacy_without_crashing(
            self, test_config):
        """非法 context_mode（如 typo）不得让 SessionStart hook 崩溃：按 legacy
        渲染与维护，Context 功能 fail-closed，Core 表保持空。"""
        test_config.context_mode = "primray"
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:expired", value="过期事实",
                      expires_at="2020-01-01 00:00:00")
            store.add(key="p:t:fact:fresh", value="现行事实")

        result = get_session_start_block(config=test_config)

        assert "现行事实" in result
        assert "过期事实" not in result
        with MemoryStore(test_config) as store:
            rows = store.get_by_key("p:t:fact:expired")
            assert rows[0]["status"] == "archived"  # legacy 维护照旧
        conn = sqlite3.connect(test_config.db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM context_items").fetchone()[0] == 0
        finally:
            conn.close()


class TestSessionStartPrimaryCoreInjection:
    """primary 模式：SessionStart 注入切换到 Context Core（fail-open 回退）。"""

    _CORE_BEGIN = "[BEGIN EVOLVMEM CONTEXT HISTORY]"
    _LEGACY_HEADER = "## Persistent Memory (from EvolvMem plugin)"

    @staticmethod
    def _seed_dual(config, adds):
        """compat 模式双侧写入（索引因无 embedding 引擎保持 dirty）。"""
        with MemoryStore(config):
            pass
        config.context_mode = "compat"
        service = ContextService(config)
        service.initialize(mode=ContextMode.COMPAT, adapter="test-hooks")
        facade = service.legacy_facade()
        context_ids = []
        for kwargs in adds:
            legacy_id = facade.add(**kwargs)
            context_ids.append(service.store.resolve_legacy_mapping(legacy_id))
        service.close()
        config.context_mode = "primary"
        return context_ids

    @classmethod
    def _seed_primary_ready(cls, config, adds):
        """双侧写入 + 占位向量重建，让 primary 启动不变量全部满足。"""
        context_ids = cls._seed_dual(config, adds)
        index = VectorIndex(config, path=config.context_vector_path)
        index.initialize(dim=config.embedding_dim)
        index.rebuild(
            context_ids,
            [np.ones(config.embedding_dim, dtype=np.float32)
             for _ in context_ids],
        )
        index.close()
        return context_ids

    def test_primary_injects_core_rendered_block(self, test_config):
        self._seed_primary_ready(test_config, [
            dict(key="user:constraint:no-prod", value="禁止直接操作生产库",
                 attribute="constraint", importance=8.0, tier="pinned"),
        ])

        result = get_session_start_block(config=test_config)

        # Core 渲染的 bounded L1 块（含「不可信历史」边界声明），而非旧四层格式
        assert self._CORE_BEGIN in result
        assert "untrusted historical data" in result
        assert "禁止直接操作生产库" in result
        assert self._LEGACY_HEADER not in result
        # 维护节奏不变：自动遗忘/合并按原节奏触发
        assert (test_config.data_dir / ".last_forget").exists()
        assert (test_config.data_dir / ".last_consolidate").exists()

    def test_primary_project_identity_uses_basename_with_aliases(
            self, test_config, monkeypatch, tmp_path):
        self._seed_primary_ready(test_config, [
            dict(key="user:constraint:no-prod", value="禁止直接操作生产库",
                 attribute="constraint", importance=8.0, tier="pinned"),
        ])
        workdir = tmp_path / "wd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        # 目录名 → inject_project_aliases → context_project_aliases 两跳归一化
        test_config.inject_project_aliases = {"wd": "mid"}
        test_config.context_project_aliases = {"mid": "final"}
        requests = []
        real_session_start = ContextService.session_start

        def spy(service, request):
            requests.append(request)
            return real_session_start(service, request)

        monkeypatch.setattr(ContextService, "session_start", spy)

        result = get_session_start_block(config=test_config)

        assert self._CORE_BEGIN in result
        (request,) = requests
        # 只传规范化项目名，绝不传绝对路径
        assert request.project == "final"
        assert "/" not in request.project and "\\" not in request.project
        # session start 没有用户 query：项目名充当弱相关性信号（与 legacy
        # 评分的 cwd 项目加分同源），pinned 种子例外照常生效
        assert request.query == "final"
        assert request.workspace_path == str(workdir)

    def test_primary_degraded_falls_back_to_legacy_render(self, test_config):
        # 索引 dirty（写入时无 embedding 引擎）→ primary 健康复查 degraded
        self._seed_dual(test_config, [
            dict(key="p:t:fact:fresh", value="现行事实", importance=5.0),
        ])

        result = get_session_start_block(config=test_config)

        assert self._LEGACY_HEADER in result
        assert "现行事实" in result
        assert self._CORE_BEGIN not in result

    def test_primary_empty_core_block_falls_back_to_legacy_render(
            self, test_config, monkeypatch):
        # pinned 事实不享受无命中豁免（豁免已收窄到三类 policy），Core 渲染空块
        self._seed_primary_ready(test_config, [
            dict(key="user:fact:pinned-note", value="置顶的普通事实",
                 attribute="fact", importance=8.0, tier="pinned"),
        ])
        requests = []
        real_session_start = ContextService.session_start

        def spy(service, request):
            requests.append(request)
            return real_session_start(service, request)

        monkeypatch.setattr(ContextService, "session_start", spy)

        result = get_session_start_block(config=test_config)

        assert len(requests) == 1  # Core 路径确实进入并返回空块
        assert self._LEGACY_HEADER in result
        assert "置顶的普通事实" in result
        assert self._CORE_BEGIN not in result

    def test_primary_core_failure_falls_back_and_closes_service(
            self, test_config, monkeypatch):
        self._seed_primary_ready(test_config, [
            dict(key="p:t:fact:fresh", value="现行事实", importance=5.0),
        ])

        def boom(service, request):
            raise RuntimeError("core boom")

        monkeypatch.setattr(ContextService, "session_start", boom)
        real_close = ContextService.close
        close_calls = []

        def counting_close(service):
            close_calls.append(1)
            real_close(service)

        monkeypatch.setattr(ContextService, "close", counting_close)

        result = get_session_start_block(config=test_config)

        # Core 异常静默回退旧渲染；服务实例照常关闭，不残留跨进程资源
        assert self._LEGACY_HEADER in result
        assert "现行事实" in result
        assert self._CORE_BEGIN not in result
        assert close_calls == [1]

    def test_shadow_mode_still_uses_legacy_render(self, test_config):
        """冻结：shadow 模式的 SessionStart 注入仍走 legacy 渲染，一字不变。"""
        self._seed_dual(test_config, [
            dict(key="user:constraint:no-prod", value="禁止直接操作生产库",
                 attribute="constraint", importance=8.0, tier="pinned"),
        ])
        test_config.context_mode = "shadow"

        result = get_session_start_block(config=test_config)

        assert self._LEGACY_HEADER in result
        assert "禁止直接操作生产库" in result
        assert self._CORE_BEGIN not in result


class TestStopHook:
    def test_stop_prompt_includes_conversation(self):
        prompt = get_stop_prompt("user: We decided to use Redis for caching\nassistant: OK, noted")

        assert "Redis" in prompt
        assert "保留规则" in prompt


class TestProjectDigestLayer:
    @staticmethod
    def _date(days_ago: int) -> str:
        return time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - days_ago * 86400))

    def test_digest_layer_groups_by_project(self, test_config):
        d0, d1 = self._date(0), self._date(1)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:eva:progress:log:{d0}-0900",
                      value="EVA 部署了 TEI")
            store.add(key=f"project:evolvmem:progress:log:{d1}-0800",
                      value="evolvmem 三期完成")
            store.add(key="p:t:fact:0", value="普通事实")

        result = get_session_start_block(config=test_config)

        assert "最近项目动态" in result
        assert f"【eva】{d0[5:]} EVA 部署了 TEI" in result
        assert f"【evolvmem】{d1[5:]} evolvmem 三期完成" in result
        # 新的在前
        assert result.index("EVA 部署了 TEI") < result.index("evolvmem 三期完成")

    def test_legacy_four_segment_log_key(self, test_config):
        d0 = self._date(0)
        with MemoryStore(test_config) as store:
            store.add(key=f"eva:progress:log:{d0}-infra", value="旧格式日志内容")

        result = get_session_start_block(config=test_config)

        assert f"【eva】{d0[5:]} 旧格式日志内容" in result

    def test_digest_per_project_cap(self, test_config):
        d = [self._date(i) for i in range(3)]
        with MemoryStore(test_config) as store:
            for i in range(3):
                store.add(key=f"project:eva:progress:log:{d[i]}-0{i}00",
                          value=f"第{i}天日志")

        result = get_session_start_block(config=test_config)

        assert "第0天日志" in result
        assert "第1天日志" in result
        assert "第2天日志" not in result

    def test_digest_old_logs_filtered(self, test_config):
        old = self._date(40)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:eva:progress:log:{old}-0900",
                      value="很久以前的日志")
            store.add(key="p:t:fact:0", value="普通事实")

        result = get_session_start_block(config=test_config)

        assert "最近项目动态" not in result
        # 老日志不回退到精选/索引层
        assert "progress:log" not in result
        assert "普通事实" in result

    def test_digest_logs_excluded_from_scored_and_index(self, test_config):
        d0 = self._date(0)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:eva:progress:log:{d0}-0900",
                      value="高重要性日志", importance=9.0)

        result = get_session_start_block(config=test_config)

        # 不进精选层（加粗行），也不进索引层（key 行）
        assert "- **project:eva:progress:log" not in result
        assert f"- project:eva:progress:log:{d0}-0900" not in result

    def test_digest_char_budget(self, test_config):
        test_config.digest_max_chars = 100
        d0, d1 = self._date(0), self._date(1)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:aaa:progress:log:{d0}-0900",
                      value="x" * 60)
            store.add(key=f"project:bbb:progress:log:{d1}-0900",
                      value="y" * 60)

        result = get_session_start_block(config=test_config)

        assert "x" * 60 in result  # 第一条永远保留
        assert "y" * 60 not in result

    def test_digest_disabled_when_budget_zero(self, test_config):
        test_config.digest_max_chars = 0
        d0 = self._date(0)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:eva:progress:log:{d0}-0900",
                      value="日志内容")

        result = get_session_start_block(config=test_config)

        assert "最近项目动态" not in result

    def test_only_logs_still_produces_block(self, test_config):
        d0 = self._date(0)
        with MemoryStore(test_config) as store:
            store.add(key=f"project:eva:progress:log:{d0}-0900",
                      value="唯一日志")

        result = get_session_start_block(config=test_config)

        assert "最近项目动态" in result
        assert "唯一日志" in result


class TestSessionStartArchiveSweep:
    """P4b：shadow/primary 的 SessionStart 先静默跑 archive purge（fail-open）。"""

    def test_shadow_sweeps_before_maintenance_and_render(
            self, test_config, monkeypatch):
        test_config.context_mode = "shadow"
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:0", value="现行事实")
        calls = []
        monkeypatch.setattr(
            ContextService,
            "sweep_archives",
            lambda service: calls.append("sweep"),
        )
        monkeypatch.setattr(
            "evolvmem.hooks._maybe_run_forgetting",
            lambda config, facade: calls.append("forget"),
        )

        result = get_session_start_block(config=test_config)

        assert calls == ["sweep", "forget"]  # purge 先于维护与渲染
        assert "现行事实" in result

    def test_shadow_sweep_failure_is_swallowed_to_stderr(
            self, test_config, monkeypatch, capsys):
        test_config.context_mode = "shadow"
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:0", value="现行事实")

        def boom(service):
            raise RuntimeError("synthetic sweep outage at /secret/path")

        monkeypatch.setattr(ContextService, "sweep_archives", boom)

        result = get_session_start_block(config=test_config)

        assert "现行事实" in result  # fail-open：渲染照常
        err = capsys.readouterr().err
        assert "archive sweep" in err
        assert "/secret/path" not in err

    @pytest.mark.parametrize("mode", ["legacy", "compat"])
    def test_legacy_and_compat_skip_sweep(self, test_config, monkeypatch, mode):
        test_config.context_mode = mode
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:0", value="现行事实")
        calls = []
        monkeypatch.setattr(
            ContextService,
            "sweep_archives",
            lambda service: calls.append(1),
        )

        result = get_session_start_block(config=test_config)

        assert "现行事实" in result
        assert calls == []

    def test_shadow_session_start_purges_expired_archive(self, test_config):
        """真服务真 sweep：SessionStart 真实删除到期 payload 并置 purged。"""
        from evolvmem.context_store import ContextStore
        from evolvmem.session_archive import SessionArchiver

        test_config.context_mode = "shadow"
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:0", value="现行事实")
        with ContextStore(test_config) as cstore:
            cstore.initialize()
            record = SessionArchiver(test_config, cstore).archive_session(
                "proj",
                "kimi",
                "session_old",
                "过期的原始会话正文",
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        payload_file = test_config.data_dir / record.payload_path
        assert payload_file.exists()

        result = get_session_start_block(config=test_config)

        assert "现行事实" in result
        assert not payload_file.exists()
        conn = sqlite3.connect(test_config.db_path)
        try:
            row = conn.execute(
                "SELECT state, purged_at FROM session_archives WHERE id=?",
                (record.id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "purged"
        assert row[1] is not None
