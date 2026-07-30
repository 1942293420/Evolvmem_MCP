"""hooks module tests."""

from evolvmem.hooks import get_session_start_block, get_stop_prompt
from evolvmem.memory_store import MemoryStore


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


class TestStopHook:
    def test_stop_prompt_includes_conversation(self):
        prompt = get_stop_prompt("user: We decided to use Redis for caching\nassistant: OK, noted")

        assert "Redis" in prompt
        assert "Retention" in prompt or "extract" in prompt
