# Tiered Memory Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 SessionStart 注入从「updated_at 倒序 + 字符截断」升级为「pinned 必注入 + 三因子评分竞争 + 索引层渐进披露」，保证注入的一定是重点。

**Architecture:** schema 增加 `importance`/`tier` 两列（幂等迁移 + 按 category 规则回填）；新增 `evolvmem/scoring.py` 计算三因子评分（importance × 0.5 + recency 衰减 × 0.3 + 访问频率 × 0.2）；`hooks.get_session_start_block` 重写为三层注入；extractor prompt 增加 importance/tier/value 长度输出（方案 A：LLM 评分）。

**Tech Stack:** Python 3.10+, SQLite (stdlib), pytest。无新增依赖。

## Global Constraints

- 所有 schema 变更必须幂等：老库（`~/.claude/evolvmem/memory.db`，181 条 active 记忆）重复 initialize 不得报错或重复回填。
- 不新增第三方依赖；只用标准库 + 已有的 usearch/llama-cpp-python。
- 测试模式遵循现有惯例：使用 `test_config` fixture（tests/conftest.py，指向临时目录）。
- 运行测试统一用：`.venv/bin/python -m pytest tests/ -q`（项目根 `/home/jiangli/hermes-memory-plugin`）。
- 现有测试必须全部保持绿色（`update_access` 行为变更涉及 1 个既有断言的修正，见 Task 2）。
- 提交信息格式遵循 git log 现有风格：`feat:` / `fix:` / `refactor:` 前缀。

---

### Task 1: Schema 迁移 —— importance/tier 两列 + 回填

**Files:**
- Modify: `evolvmem/memory_store.py`（`_create_tables`）
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Produces: `memories` 表新增两列：`importance REAL NOT NULL DEFAULT 5.0`、`tier TEXT NOT NULL DEFAULT 'normal'`。后续所有任务依赖这两列存在于 record dict 中（`m["importance"]` / `m["tier"]`）。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_memory_store.py` 的 `TestMemoryStore` 类中：

```python
    def test_schema_has_importance_and_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        cols = {r[1] for r in store._execute("PRAGMA table_info(memories)")}
        assert "importance" in cols
        assert "tier" in cols
        store.close()

    def test_backfill_importance_by_category(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        cid = store.add(key="p:t:constraint:x", value="硬约束", category="constraint")
        did = store.add(key="p:t:decision:x", value="架构决策", category="decision")
        pid = store.add(key="p:t:preference:x", value="用户偏好", category="preference")
        fid = store.add(key="p:t:fact:x", value="普通事实", category="fact")

        # 触发一次迁移回填（模拟老库升级后再次 initialize）
        store.close()
        store2 = MemoryStore(test_config)
        store2.initialize()

        assert store2.get_by_id(cid)["importance"] == 8.0
        assert store2.get_by_id(did)["importance"] == 7.0
        assert store2.get_by_id(pid)["importance"] == 6.0
        assert store2.get_by_id(fid)["importance"] == 5.0
        assert store2.get_by_id(cid)["tier"] == "pinned"
        assert store2.get_by_id(pid)["tier"] == "pinned"
        assert store2.get_by_id(did)["tier"] == "normal"
        store2.close()

    def test_migration_idempotent_on_reinitialize(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.close()
        # 二次、三次 initialize 不报错
        store2 = MemoryStore(test_config)
        store2.initialize()
        store2.close()
        store3 = MemoryStore(test_config)
        store3.initialize()
        store3.close()
```

注意：回填只在「列刚被添加」的那次 initialize 执行，因此上述测试中新库的 add 走默认值（constraint 在 add 时不会自动 pinned）。第二个测试验证的是「老库升级」路径——add 发生在列已存在之后，importance 维持 add 时的值。**修正：本测试应直接验证迁移分支**。改用下面写法——先建库，再手动删列模拟老库过于复杂；简化为：直接在 initialize 后执行一次显式回填函数：

```python
    def test_backfill_importance_by_category(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        cid = store.add(key="p:t:constraint:x", value="硬约束", category="constraint")
        did = store.add(key="p:t:decision:x", value="架构决策", category="decision")
        pid = store.add(key="p:t:preference:x", value="用户偏好", category="preference")
        fid = store.add(key="p:t:fact:x", value="普通事实", category="fact")

        store._backfill_importance_tier()

        assert store.get_by_id(cid)["importance"] == 8.0
        assert store.get_by_id(did)["importance"] == 7.0
        assert store.get_by_id(pid)["importance"] == 6.0
        assert store.get_by_id(fid)["importance"] == 5.0
        assert store.get_by_id(cid)["tier"] == "pinned"
        assert store.get_by_id(pid)["tier"] == "pinned"
        assert store.get_by_id(did)["tier"] == "normal"
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: FAIL — `importance` 列不存在 / `_backfill_importance_tier` 未定义。

- [ ] **Step 3: Write minimal implementation**

在 `evolvmem/memory_store.py` 的 `_create_tables` 方法末尾（现有 `executescript` 之后）追加：

```python
        # --- 幂等迁移：importance / tier 两列（2026-07-28 tiered injection）---
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(memories)")}
        migrated = False
        if "importance" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN importance REAL NOT NULL DEFAULT 5.0"
            )
            migrated = True
        if "tier" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'normal'"
            )
            migrated = True
        if migrated:
            self._backfill_importance_tier()
```

并在 `MemoryStore` 中新增方法：

```python
    # category → 默认 importance（1-10）；pinned 类别集合
    _IMPORTANCE_BY_CATEGORY = {
        "constraint": 8.0,
        "decision": 7.0,
        "preference": 6.0,
        "user_profile": 6.0,
    }
    _PINNED_CATEGORIES = ("constraint", "preference", "user_profile")

    def _backfill_importance_tier(self) -> None:
        """按 category 规则回填 importance/tier。仅在迁移（新增列）时调用一次。"""
        self._conn.execute(
            "UPDATE memories SET importance = CASE category "
            "WHEN 'constraint' THEN 8.0 "
            "WHEN 'decision' THEN 7.0 "
            "WHEN 'preference' THEN 6.0 "
            "WHEN 'user_profile' THEN 6.0 "
            "ELSE 5.0 END"
        )
        self._conn.execute(
            "UPDATE memories SET tier = 'pinned' "
            "WHERE category IN ('constraint', 'preference', 'user_profile')"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: PASS（含全部既有用例）

- [ ] **Step 5: Commit**

```bash
git add evolvmem/memory_store.py tests/test_memory_store.py
git commit -m "feat: add importance/tier columns with idempotent migration and category backfill"
```

---

### Task 2: `add`/`replace` 支持 importance/tier + 修复 update_access 污染 updated_at

**Files:**
- Modify: `evolvmem/memory_store.py`（`_insert_row`、`add`、`replace`、`update_access`）
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Consumes: Task 1 的两列。
- Produces:
  - `MemoryStore.add(key, value, category="", tags=None, source_session="", supersedes=None, importance=5.0, tier="normal") -> int`
  - `MemoryStore.replace(key, new_value, importance=None, tier=None, **kwargs) -> int` — importance/tier 为 None 时继承旧记录的值。
  - `update_access(mem_id)` 不再更新 `updated_at`。

- [ ] **Step 1: Write the failing test**

```python
    def test_add_with_importance_and_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:t:decision:db", value="用 PostgreSQL",
                        category="decision", importance=9.0, tier="pinned")
        rec = store.get_by_id(mid)
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
        store.close()

    def test_replace_inherits_importance_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:decision:db", value="用 MySQL",
                  category="decision", importance=9.0, tier="pinned")
        new_id = store.replace(key="p:t:decision:db", new_value="改用 PostgreSQL")
        rec = store.get_by_id(new_id)
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
        store.close()

    def test_update_access_does_not_touch_updated_at(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:acc:test2", value="access test")
        before = store.get_by_id(mid)["updated_at"]
        import time
        time.sleep(1.1)  # updated_at 精度为秒
        store.update_access(mid)
        after = store.get_by_id(mid)
        assert after["access_count"] == 1
        assert after["updated_at"] == before
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: FAIL — `add() got an unexpected keyword argument 'importance'`；`update_access` 仍会更新 `updated_at`。

- [ ] **Step 3: Write minimal implementation**

`_insert_row` 改为：

```python
    def _insert_row(self, key: str, value: str, category: str,
                    tag_str: str, source_session: str,
                    supersedes: int | None,
                    importance: float = 5.0, tier: str = "normal") -> int:
        """Insert a row into memories and return its id. Does NOT commit."""
        now = _now_iso()
        cur = self._conn.execute(
            "INSERT INTO memories (key, value, status, category, tags, "
            "source_session, supersedes, importance, tier, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, value, category, tag_str, source_session, supersedes,
             importance, tier, now, now),
        )
        return cur.lastrowid
```

`add` 增加参数并透传：

```python
    def add(self, key: str, value: str, category: str = "",
            tags: list[str] | None = None,
            source_session: str = "",
            supersedes: int | None = None,
            importance: float = 5.0, tier: str = "normal") -> int:
        """Insert a new active memory. Returns the new record id."""
        tag_str = ",".join(tags) if tags else ""
        new_id = self._insert_row(key, value, category, tag_str,
                                  source_session, supersedes,
                                  importance=importance, tier=tier)
        self._conn.commit()
        return new_id
```

`replace` 中 importance/tier 缺省继承旧值（在 `old_id = old["id"]` 之后、`_insert_row` 调用处）：

```python
        importance = kwargs.pop("importance", None)
        if importance is None:
            importance = old["importance"]
        tier = kwargs.pop("tier", None)
        if tier is None:
            tier = old["tier"]
```

并将 `_insert_row(...)` 调用改为传入 `importance=importance, tier=tier`。

`update_access` 去掉对 `updated_at` 的更新：

```python
    def update_access(self, mem_id: int) -> None:
        """Increment access_count and update last_accessed (called on retrieval hit).

        Does NOT touch updated_at — recency ordering must reflect writes, not reads.
        """
        self._conn.execute(
            "UPDATE memories SET access_count = access_count + 1, "
            "last_accessed = ? WHERE id = ?",
            (_now_iso(), mem_id),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py tests/test_retriever.py tests/test_forgetting.py -q`
Expected: PASS。注意既有 `test_update_access_count` 只断言 access_count/last_accessed，不受影响。

- [ ] **Step 5: Commit**

```bash
git add evolvmem/memory_store.py tests/test_memory_store.py
git commit -m "feat: add/replace accept importance/tier; update_access no longer touches updated_at"
```

---

### Task 3: 三因子评分模块 `evolvmem/scoring.py`

**Files:**
- Create: `evolvmem/scoring.py`
- Modify: `evolvmem/config.py`
- Test: `tests/test_scoring.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `importance`/`tier` 列、Task 2 修正后的 `last_accessed` 语义。
- Produces:
  - `compute_score(memory: dict, config: Config, now: datetime | None = None) -> float`
  - Config 新字段（含 `save()` 持久化）：
    - `inject_w_importance: float = 0.5`
    - `inject_w_recency: float = 0.3`
    - `inject_w_frequency: float = 0.2`
    - `inject_recency_tau_days: float = 14.0`
    - `inject_freq_norm_cap: int = 20`

- [ ] **Step 1: Write the failing test**

新建 `tests/test_scoring.py`：

```python
"""scoring module tests."""

from datetime import datetime, timezone, timedelta
from evolvmem.scoring import compute_score


def _mem(importance=5.0, access_count=0, last_accessed=None, updated_at=None):
    return {
        "importance": importance,
        "access_count": access_count,
        "last_accessed": last_accessed,
        "updated_at": updated_at or "2026-07-28 00:00:00",
    }


NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


class TestComputeScore:
    def test_higher_importance_scores_higher(self, test_config):
        low = compute_score(_mem(importance=3.0), test_config, now=NOW)
        high = compute_score(_mem(importance=9.0), test_config, now=NOW)
        assert high > low

    def test_recency_decays_with_age(self, test_config):
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        old = _mem(last_accessed="2026-06-28 00:00:00")
        assert compute_score(recent, test_config, now=NOW) > \
               compute_score(old, test_config, now=NOW)

    def test_recency_uses_last_accessed_over_updated_at(self, test_config):
        # updated_at 很老但昨天被访问过 → 仍按新鲜算
        m = _mem(last_accessed="2026-07-27 00:00:00",
                 updated_at="2026-01-01 00:00:00")
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        assert compute_score(m, test_config, now=NOW) == \
               compute_score(recent, test_config, now=NOW)

    def test_frequency_rewards_access_count(self, test_config):
        cold = compute_score(_mem(access_count=0), test_config, now=NOW)
        hot = compute_score(_mem(access_count=10), test_config, now=NOW)
        assert hot > cold

    def test_frequency_capped(self, test_config):
        # 超过 norm cap 后不再增长
        a = compute_score(_mem(access_count=1000000), test_config, now=NOW)
        b = compute_score(_mem(access_count=test_config.inject_freq_norm_cap),
                          test_config, now=NOW)
        assert abs(a - b) < 1e-9

    def test_weights_respected(self, test_config):
        test_config.inject_w_importance = 1.0
        test_config.inject_w_recency = 0.0
        test_config.inject_w_frequency = 0.0
        score = compute_score(_mem(importance=8.0), test_config, now=NOW)
        assert abs(score - 0.8) < 1e-9

    def test_null_last_accessed_falls_back_to_updated_at(self, test_config):
        m = _mem(last_accessed=None, updated_at="2026-07-27 00:00:00")
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        assert compute_score(m, test_config, now=NOW) == \
               compute_score(recent, test_config, now=NOW)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: FAIL — `ModuleNotFoundError: evolvmem.scoring`

- [ ] **Step 3: Write minimal implementation**

新建 `evolvmem/scoring.py`：

```python
"""Three-factor memory scoring: importance + recency decay + access frequency.

Modelled on the Generative Agents retrieval score (recency/importance/relevance).
SessionStart injection has no query, so relevance is replaced by usage frequency.
"""

import math
from datetime import datetime, timezone

from evolvmem.config import Config

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_score(memory: dict, config: Config,
                  now: datetime | None = None) -> float:
    """Compute injection priority score in [0, w_i + w_r + w_f].

    Factors (each normalized to [0, 1] before weighting):
    - importance: memory["importance"] / 10
    - recency: exp(-age_days / inject_recency_tau_days), age from
      last_accessed, falling back to updated_at
    - frequency: log1p(access_count) / log1p(inject_freq_norm_cap), capped at 1
    """
    if now is None:
        now = datetime.now(timezone.utc)

    importance_norm = max(0.0, min(1.0, float(memory.get("importance") or 5.0) / 10.0))

    ts = _parse_ts(memory.get("last_accessed")) or _parse_ts(memory.get("updated_at"))
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0) if ts else 365.0
    recency_norm = math.exp(-age_days / config.inject_recency_tau_days)

    access_count = max(0, int(memory.get("access_count") or 0))
    cap = max(1, int(config.inject_freq_norm_cap))
    frequency_norm = min(1.0, math.log1p(access_count) / math.log1p(cap))

    return (config.inject_w_importance * importance_norm
            + config.inject_w_recency * recency_norm
            + config.inject_w_frequency * frequency_norm)
```

`evolvmem/config.py` 新增字段（放在「SessionStart 注入限额」一节）：

```python
    # --- 注入评分权重（三因子） ---
    inject_w_importance: float = 0.5   # importance/10 的权重
    inject_w_recency: float = 0.3      # exp(-age/tau) 的权重
    inject_w_frequency: float = 0.2    # log1p(access_count) 的权重
    inject_recency_tau_days: float = 14.0  # recency 衰减时间常数（天）
    inject_freq_norm_cap: int = 20     # 访问次数归一化上限
```

并在 `save()` 的 data dict 中追加同名 5 个键。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: PASS（7 个用例）

- [ ] **Step 5: Commit**

```bash
git add evolvmem/scoring.py evolvmem/config.py tests/test_scoring.py
git commit -m "feat: three-factor injection scoring (importance + recency + frequency)"
```

---

### Task 4: hooks 三层注入重写

**Files:**
- Modify: `evolvmem/hooks.py`（`get_session_start_block` 重写）
- Modify: `evolvmem/config.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 3 的 `compute_score` 与 Config 权重字段；Task 1/2 的 `importance`/`tier`/`last_accessed`。
- Produces:
  - Config 新字段（含 `save()`）：
    - `inject_pinned_max_count: int = 10`
    - `inject_pinned_max_chars: int = 2000`
    - `inject_index_max_chars: int = 1000`
    - `inject_key_prefix_quota: int = 3`
  - `get_session_start_block(config) -> str` 输出三段结构：
    1. `## 常驻记忆`（pinned，按 importance 降序，独立预算）
    2. `## 记忆精选`（normal，按 compute_score 降序 + key 前缀配额，占剩余预算）
    3. `## 记忆索引`（落选者单行索引 `- key [tags] (N字)`，索引预算内；超出者只计数）

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_hooks.py` 的 `TestSessionStartHook` 类：

```python
    def test_pinned_always_injected_regardless_of_recency(self, test_config):
        with MemoryStore(test_config) as store:
            # pinned 但很老
            store.add(key="user:constraint:no-prod", value="禁止直接操作生产库",
                      category="constraint", importance=8.0, tier="pinned")
            # normal 但更新（updated_at 更晚）
            for i in range(5):
                store.add(key=f"p:t:fact:{i}", value=f"fact {i}",
                          category="fact", importance=3.0)

        result = get_session_start_block(config=test_config)

        assert "## 常驻记忆" in result
        assert "禁止直接操作生产库" in result

    def test_normal_memories_ranked_by_importance(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="p:t:fact:trivial", value="无关紧要的小事",
                      category="fact", importance=2.0)
            store.add(key="p:t:decision:arch", value="核心架构决策",
                      category="decision", importance=9.0)

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
                          category="constraint", importance=8.0, tier="pinned")
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
```

注意：既有用例 `test_inject_max_count_limits_entries` / `test_inject_max_chars_budget` / `test_no_omission_note_when_all_fit` 断言的提示语 `"N more memories not injected"` 与 `"memory_search"` 在索引层保留该文案，需保证兼容——索引层末尾的计数行文案保持为：
`(N more memories not injected; use the memory_search tool to retrieve them when needed.)`
但索引层本身会展示部分落选 key，因此 `test_inject_max_chars_budget` 中 `"4 more memories not injected"` 的断言可能变为更小数字（部分进入索引行）。**允许修改这两个既有用例的断言**为检查「索引区或计数行覆盖全部落选记忆」。具体改为：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q`
Expected: FAIL — 无 `## 常驻记忆` 段落等。

- [ ] **Step 3: Write minimal implementation**

`evolvmem/config.py` 追加字段（并加入 `save()`）：

```python
    # --- 分层注入预算 ---
    inject_pinned_max_count: int = 10    # pinned 层最多条数
    inject_pinned_max_chars: int = 2000  # pinned 层字符预算
    inject_index_max_chars: int = 1000   # 索引层字符预算（0 = 关闭索引层）
    inject_key_prefix_quota: int = 3     # 同一 key 前缀（前两段）最多注入条数
```

`evolvmem/hooks.py` 重写 `get_session_start_block` 及辅助函数：

```python
"""SessionStart and Stop Hook integration — memory formatting and extraction triggering."""

import sys
import time
from pathlib import Path

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.auto_extractor import AutoExtractor
from evolvmem.forgetting import ForgettingEngine
from evolvmem.scoring import compute_score


def _last_forget_path(config: Config) -> Path:
    return config.data_dir / ".last_forget"


def _maybe_run_forgetting(config: Config, store: MemoryStore) -> None:
    """Run the forgetting engine at most once per forget_auto_run_hours.

    Failures are swallowed — memory maintenance must never block session start.
    """
    try:
        marker = _last_forget_path(config)
        interval_s = config.forget_auto_run_hours * 3600
        if marker.exists():
            last_run = marker.stat().st_mtime
            if time.time() - last_run < interval_s:
                return
        archived = ForgettingEngine(config, store).run()
        marker.touch()
        if archived:
            print(f"[evolvmem] auto-forgetting archived {archived} memories",
                  file=sys.stderr, flush=True)
    except Exception:
        pass


def _format_full_line(m: dict) -> str:
    tags = m.get("tags", "")
    if tags:
        return f"- **{m['key']}** [{tags}]: {m['value']}"
    return f"- **{m['key']}**: {m['value']}"


def _format_index_line(m: dict) -> str:
    tags = m.get("tags", "")
    suffix = f" [{tags}]" if tags else ""
    return f"- {m['key']}{suffix} ({len(m['value'])}字)"


def _take_budget(items: list[dict], max_count: int, max_chars: int,
                 formatter=_format_full_line) -> tuple[list[dict], list[dict]]:
    """Take items within count/char budget. First item is always kept.

    Returns (selected, omitted).
    """
    selected: list[dict] = []
    used = 0
    for i, m in enumerate(items):
        if len(selected) >= max_count:
            return selected, items[i:]
        line_len = len(formatter(m))
        if selected and used + line_len > max_chars:
            return selected, items[i:]
        selected.append(m)
        used += line_len
    return selected, []


def _key_prefix(key: str) -> str:
    """First two segments of a dotted key, e.g. 'project:purchase:fact:x' → 'project:purchase'."""
    parts = key.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else key


def _apply_prefix_quota(items: list[dict], quota: int) -> tuple[list[dict], list[dict]]:
    """Cap items per key prefix. Returns (kept, overflow), both order-preserving."""
    counts: dict[str, int] = {}
    kept: list[dict] = []
    overflow: list[dict] = []
    for m in items:
        prefix = _key_prefix(m["key"])
        if counts.get(prefix, 0) < quota:
            kept.append(m)
            counts[prefix] = counts.get(prefix, 0) + 1
        else:
            overflow.append(m)
    return kept, overflow


def get_session_start_block(config: Config | None = None) -> str:
    """Build the SessionStart injection block with three layers:

    1. Pinned layer — tier='pinned' memories always injected (own budget),
       sorted by importance desc.
    2. Scored layer — normal memories ranked by compute_score()
       (importance + recency + frequency), competing for the remaining
       inject_max_chars budget, with a per-key-prefix quota.
    3. Index layer — omitted memories listed as one-line indexes so the
       agent knows they exist and can fetch them via memory_search.

    Args:
        config: Configuration object, uses defaults when None.

    Returns:
        Formatted system prompt string; empty string if no active memories.
    """
    if config is None:
        config = Config.from_file()

    with MemoryStore(config) as store:
        _maybe_run_forgetting(config, store)
        memories = store.get_active()

    if not memories:
        return ""

    pinned = [m for m in memories if m.get("tier") == "pinned"]
    normal = [m for m in memories if m.get("tier") != "pinned"]

    # Layer 1: pinned, own budget, importance desc
    pinned.sort(key=lambda m: m.get("importance") or 5.0, reverse=True)
    pinned_sel, pinned_omit = _take_budget(
        pinned, config.inject_pinned_max_count, config.inject_pinned_max_chars)

    # Layer 2: normal, scored, prefix quota, remaining budget
    normal.sort(key=lambda m: compute_score(m, config), reverse=True)
    normal_quota, quota_overflow = _apply_prefix_quota(
        normal, config.inject_key_prefix_quota)
    pinned_chars = sum(len(_format_full_line(m)) for m in pinned_sel)
    normal_sel, normal_omit = _take_budget(
        normal_quota,
        max(0, config.inject_max_count - len(pinned_sel)),
        max(0, config.inject_max_chars - pinned_chars))

    # Layer 3: index lines for everything omitted
    omitted = pinned_omit + normal_omit + quota_overflow
    index_sel, index_omit = ([], omitted)
    if config.inject_index_max_chars > 0 and omitted:
        index_sel, index_omit = _take_budget(
            omitted, len(omitted), config.inject_index_max_chars,
            formatter=_format_index_line)

    # Compose
    lines = [
        "## Persistent Memory (from EvolvMem plugin)",
        "",
        "The following are preferences, decisions, and constraints extracted from previous conversations "
        "that are still valid.",
        "These are persisted facts, not context from the current conversation.",
        "Trust priority: user's current instructions > current code and tests > the memories below > history.",
        "If any memory contradicts what the user is currently saying, follow the user's current statement.",
        "Only the most relevant memories are injected here; use the memory_search tool to recall the rest on demand.",
        "",
    ]

    if pinned_sel:
        lines.append("### 常驻记忆 (pinned)")
        lines.extend(_format_full_line(m) for m in pinned_sel)
        lines.append("")

    if normal_sel:
        lines.append("### 记忆精选 (by importance)")
        lines.extend(_format_full_line(m) for m in normal_sel)
        lines.append("")

    if index_sel:
        lines.append("### 记忆索引 (use memory_search to fetch full content)")
        lines.extend(_format_index_line(m) for m in index_sel)

    if index_omit:
        lines.append("")
        lines.append(
            f"({len(index_omit)} more memories not injected; "
            f"use the memory_search tool to retrieve them when needed.)"
        )

    return "\n".join(lines)


def get_stop_prompt(messages_summary: str) -> str:
    """Build the extraction prompt for the Stop Hook.

    Args:
        messages_summary: Conversation summary string (from the hook system).

    Returns:
        Extraction prompt string, to be passed to Claude for analysis.
    """
    extractor = AutoExtractor()
    return extractor.build_extraction_prompt(
        messages=[{"role": "user", "content": messages_summary}],
    )
```

边界情况说明：
- `inject_index_max_chars = 0` 时退化为旧行为（纯截断 + 计数行），保持向后兼容。
- 索引层行首是 `- key`（无 `**`），全文层行首是 `- **key**`，测试中可据此区分。
- `_take_budget` 保留「第一条必保留」语义，与旧版一致。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q`
Expected: PASS（含调整后的既有用例）。再跑全量：`.venv/bin/python -m pytest tests/ -q`

- [ ] **Step 5: Commit**

```bash
git add evolvmem/hooks.py evolvmem/config.py tests/test_hooks.py
git commit -m "feat: three-layer session injection — pinned + scored + index (progressive disclosure)"
```

---

### Task 5: extractor 输出 importance/tier + value 长度约束

**Files:**
- Modify: `evolvmem/auto_extractor.py`
- Test: `tests/test_auto_extractor.py`

**Interfaces:**
- Consumes: Task 2 的 `add(..., importance, tier)`。
- Produces:
  - `CandidateMemory` 新字段：`importance: float = 5.0`、`tier: str = "normal"`
  - `parse_response` 解析 importance（clamp 到 [1, 10]）与 tier（非法值回退 "normal"）
  - `should_persist` 新增：`len(value) > 500` 时拒绝
  - EXTRACTION_PROMPT 增加 importance 评分指引、tier 建议规则、value ≤200 字符硬要求

- [ ] **Step 1: Write the failing test**

先读现有 `tests/test_auto_extractor.py` 确认类名/模式，再追加：

```python
    def test_parse_importance_and_tier(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:decision:x", "value": "用 PostgreSQL", "category": "decision", "importance": 9, "tier": "pinned"}]'
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].importance == 9.0
        assert candidates[0].tier == "pinned"

    def test_parse_importance_clamped(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实", "importance": 42}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].importance == 10.0

    def test_parse_invalid_tier_falls_back_to_normal(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实", "tier": "super"}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].tier == "normal"

    def test_parse_defaults_when_fields_missing(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实"}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].importance == 5.0
        assert candidates[0].tier == "normal"

    def test_should_persist_rejects_overlong_value(self):
        extractor = AutoExtractor()
        c = CandidateMemory(key="p:t:fact:x", value="x" * 501, importance=8.0)
        assert extractor.should_persist(c) is False
        c2 = CandidateMemory(key="p:t:fact:x", value="x" * 200, importance=8.0)
        assert extractor.should_persist(c2) is True

    def test_extraction_prompt_mentions_importance_and_length(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt([{"role": "user", "content": "hi"}])
        assert "importance" in prompt
        assert "200" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_auto_extractor.py -q`
Expected: FAIL — `CandidateMemory` 无 importance 字段等。

- [ ] **Step 3: Write minimal implementation**

`CandidateMemory` 改为：

```python
@dataclass
class CandidateMemory:
    """Candidate memory — information extracted from conversation that may be persisted."""
    key: str
    value: str
    category: str = "fact"
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 5.0
    tier: str = "normal"
```

EXTRACTION_PROMPT 的 Output Format 一节替换为：

```
## Output Format
Return a JSON array, each entry containing:
- key: stable identifier
- value: memory content — MUST be a single sentence, at most 200 characters. Longer content must be split or condensed.
- category: decision | preference | fact | constraint | user_profile
- tags: list of relevant tags
- confidence: 0.0-1.0 confidence score
- importance: integer 1-10. Guide: 9-10 = hard constraints / make-or-break decisions; 7-8 = important architecture or business decisions; 5-6 = ordinary preferences and facts; 3-4 = marginal reference material
- tier: "pinned" if this memory must be visible in EVERY session (constraints, durable user preferences, user profile); otherwise "normal"

If nothing is worth persisting, return an empty array `[]`.
```

`parse_response` 中构造 CandidateMemory 处改为：

```python
            try:
                importance = float(item.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            importance = max(1.0, min(10.0, importance))
            tier = item.get("tier", "normal")
            if tier not in ("pinned", "normal"):
                tier = "normal"
            candidates.append(CandidateMemory(
                key=key,
                value=value,
                category=item.get("category", "fact"),
                tags=item.get("tags", []),
                confidence=float(item.get("confidence", 0.5)),
                importance=importance,
                tier=tier,
            ))
```

`should_persist` 追加长度过滤（放在 confidence 检查之后）：

```python
        # Value too long → skip (extraction prompt requires <= 200 chars; hard cap 500)
        if len(candidate.value) > 500:
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_auto_extractor.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/auto_extractor.py tests/test_auto_extractor.py
git commit -m "feat: extractor outputs importance/tier, enforces value length cap"
```

---

### Task 6: MCP memory_add 透传 importance/tier + config.save 补全

**Files:**
- Modify: `evolvmem/mcp_server.py`（`_memory_add`、tools/list schema）
- Modify: `evolvmem/config.py`（确认 Task 3/4 新增字段都在 `save()` 中）
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: Task 2 的 `add(...)`、Task 5 的字段语义。
- Produces: `memory_add` 接受可选 `importance`（number）与 `tier`（"pinned"|"normal"）参数。

- [ ] **Step 1: Write the failing test**

读现有 `tests/test_integration.py` 确认模式后追加：

```python
    def test_memory_add_with_importance_tier(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:constraint:db",
            "value": "禁止直接操作生产库",
            "category": "constraint",
            "importance": 9.0,
            "tier": "pinned",
        })
        assert result["status"] == "added"
        rec = server.store.get_by_id(result["id"])
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
```

（fixture 名以 test_integration.py 中现有 server fixture 为准。）

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_integration.py -q`
Expected: FAIL — importance/tier 未透传。

- [ ] **Step 3: Write minimal implementation**

`_memory_add` 中读取并透传（add 与 replace 两条路径都传）：

```python
        importance = args.get("importance")
        if importance is not None:
            importance = max(1.0, min(10.0, float(importance)))
        tier = args.get("tier")
        if tier not in ("pinned", "normal"):
            tier = None
```

- add 路径：`self.store.add(..., **({} if importance is None else {"importance": importance}), **({} if tier is None else {"tier": tier}))` —— 实际写法用显式 kwargs 组装 dict 再 `**kwargs` 展开。
- replace 路径同理传给 `self.store.replace(...)`（None 时继承旧值，Task 2 已实现）。

tools/list 中 `memory_add` 的 inputSchema properties 追加：

```python
                                    "importance": {
                                        "type": "number",
                                        "description": "Importance 1-10 (default 5). 9-10 hard constraints, 7-8 key decisions, 5-6 ordinary facts",
                                    },
                                    "tier": {
                                        "type": "string",
                                        "enum": ["pinned", "normal"],
                                        "description": "pinned = injected every session; normal = scored competition",
                                    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全量 PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/mcp_server.py evolvmem/config.py tests/test_integration.py
git commit -m "feat: memory_add accepts importance/tier; config persists injection params"
```

---

### Task 7: 真实数据库验证 + README 更新

**Files:**
- Modify: `README.md`（注入策略一节）
- 生产数据：`~/.claude/evolvmem/memory.db`（只读验证 + 一次真实迁移）

**Interfaces:**
- Consumes: Task 1-6 全部。

- [ ] **Step 1: 备份真实数据库**

```bash
cp ~/.claude/evolvmem/memory.db ~/.claude/evolvmem/memory.db.bak-20260728
```

- [ ] **Step 2: 跑真实迁移并验证注入块**

```bash
cd /home/jiangli/hermes-memory-plugin
PYTHONPATH=. .venv/bin/python -c "
from evolvmem.hooks import get_session_start_block
block = get_session_start_block()
print('总字符数:', len(block))
print('---前1200字---')
print(block[:1200])
print('---末尾400字---')
print(block[-400:])
"
```

Expected:
- 无异常；`importance`/`tier` 列迁移成功，老数据按 category 回填
- 输出含 `### 常驻记忆 (pinned)` 段，约束/偏好类在列
- 总字符数 ≤ inject_max_chars + inject_index_max_chars + 头部（约 9500）
- 采购大文档单条独占问题可见（若仍刺眼，执行 Step 3）

- [ ] **Step 3: 处理超长大记忆（数据治理，手动确认后执行）**

`project:purchase:context:full`（约 4k 字符）超出 value 硬上限，降 tier 并截断或保持可检索：

```bash
PYTHONPATH=. .venv/bin/python -c "
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
with MemoryStore(Config.from_file()) as s:
    rows = s._execute(\"SELECT id, key, length(value) AS n FROM memories WHERE status='active' ORDER BY n DESC LIMIT 5\")
    for r in rows: print(dict(r))
"
```

确认后对超过 2000 字符的条目执行 `memory_replace` 为摘要版本（由用户确认摘要内容后再改，不自动截断）。

- [ ] **Step 4: README 更新**

在 README.md 的 Features 列表中把 `L0 Active Memory` 一行改为：

```markdown
- **L0 Active Memory**: Three-layer SessionStart injection — pinned memories always injected, normal memories ranked by importance+recency+frequency score, the rest listed as a searchable index (progressive disclosure)
```

并在 Tools 表 `memory_add` 描述中补充 `importance`/`tier` 参数。

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: three-layer injection strategy in README"
```

---

## Self-Review 记录

- **Spec coverage**: pinned 必注入（Task 1/4）、三因子评分（Task 3/4）、索引层渐进披露（Task 4）、extractor 打分方案 A（Task 5）、MCP 透传（Task 6）、老库迁移与真实验证（Task 1/7）、update_access 污染修复（Task 2）、单条超长记忆治理（Task 5 写入侧预防 + Task 7 存量处理）。consolidation（记忆合并）按用户决定列为二期，不在本计划。
- **Placeholder scan**: 所有代码步骤均含完整代码；Task 5 Step 1 要求先读现有测试文件对齐类名/fixture，属合理前置。
- **Type consistency**: `importance: float`（1-10）、`tier: str ∈ {"pinned","normal"}` 在 Task 1-6 间一致；`compute_score(memory, config, now=None)` 签名在 Task 3 定义、Task 4 以 `compute_score(m, config)` 调用，一致。
