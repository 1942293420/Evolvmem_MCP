# Memory System Phase 2+3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 二期（活路径长度防线、索引层按分排序、小修复、迁移数据重打分）+ 三期（relevance 注入因子、consolidation 合并压缩、记忆时效失效），把 evolvmem 从「注入选得准」推进到「记忆库自我维持 + 注入因项目而异」。

**Architecture:** 二期为点对点修复；三期新增 `evolvmem/consolidator.py`（向量近重复检测 + 合并）、scoring 增加第四因子 relevance（cwd 项目匹配）、schema 增加 `expires_at` 列（幂等迁移）。全部沿用既有模式：Config 字段 + save()、TDD、幂等迁移。

**Tech Stack:** Python 3.10+, SQLite, usearch, pytest。无新增依赖。

**明确排除（含理由）:** LangMem 式 procedural memory（直接改写 system prompt）。理由：需要改动宿主 system prompt 生成链，风险大于收益；pinned 层已覆盖其「规则必现」的实际价值。记忆类型分治的实际收益（constraint/preference 必注入）已由 tier 机制实现。

## Global Constraints

- schema 变更幂等；老库（`~/.claude/evolvmem/memory.db`）无损。
- 不新增第三方依赖。
- 测试：`.venv/bin/python -m pytest tests/ -q`，项目根 `/home/jiangli/hermes-memory-plugin`。
- 既有测试全绿（当前基线 89 passed, 2 skipped）。
- commit 信息 `feat:`/`fix:`/`docs:` 前缀。
- 前置状态：一期已合并（97846a0..aac9fec，10 commits）。`compute_score(memory, config, now=None)`；hooks 三层注入；Config 含 inject_* 全部 14 个字段。

---

### Task 1: memory_add 活路径 value 长度防线

**Files:**
- Modify: `evolvmem/config.py`（新字段）
- Modify: `evolvmem/mcp_server.py`（`_memory_add`、`_memory_replace`）
- Test: `tests/test_integration.py`

**Interfaces:**
- Produces: Config 新字段 `value_max_chars: int = 500`（入 save()）。`memory_add`/`memory_replace` 在 `len(value) > value_max_chars` 时返回 `{"error": "value too long (N > 500 chars); split or summarize before adding"}`，不落库。

- [ ] **Step 1: Write the failing test**

按 tests/test_integration.py 现有 server fixture 模式追加：

```python
    def test_memory_add_rejects_overlong_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:long", "value": "x" * 501,
        })
        assert "error" in result
        assert "too long" in result["error"]
        assert server.store.count_active() == 0

    def test_memory_add_accepts_value_at_limit(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:ok", "value": "x" * 500,
        })
        assert result["status"] == "added"

    def test_memory_replace_rejects_overlong_value(self, server):
        server.handle_tool_call("memory_add", {"key": "p:t:fact:r", "value": "short"})
        result = server.handle_tool_call("memory_replace", {
            "key": "p:t:fact:r", "value": "y" * 501,
        })
        assert "error" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_integration.py -q`
Expected: FAIL（未被拒绝）

- [ ] **Step 3: Write minimal implementation**

config.py 字段区加（并入 save()）：

```python
    value_max_chars: int = 500  # memory_add/replace 的 value 长度硬上限
```

mcp_server.py `_memory_add` 在参数读取后、conflict detection 前加：

```python
        if len(value) > self.config.value_max_chars:
            return {"error": f"value too long ({len(value)} > {self.config.value_max_chars} chars); "
                             "split or summarize before adding"}
```

`_memory_replace` 同样位置加同款检查。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`（全量）
Expected: PASS。注意：若既有集成测试里有 >500 字符的 value 需同步调整——检查并在报告中说明。

- [ ] **Step 5: Commit**

```bash
git add evolvmem/config.py evolvmem/mcp_server.py tests/test_integration.py
git commit -m "feat: enforce value length cap on live memory_add/replace path"
```

---

### Task 2: 索引层按 score 降序

**Files:**
- Modify: `evolvmem/hooks.py`（`get_session_start_block` 索引层一段）
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `compute_score`（一期 Task 3）。
- Produces: 索引层候选 `omitted` 在进 `_take_budget` 前按 `compute_score(m, config)` 降序排序。

- [ ] **Step 1: Write the failing test**

```python
    def test_index_lines_ordered_by_score(self, test_config):
        test_config.inject_max_chars = 200      # 精选层只留 1 条
        test_config.inject_index_max_chars = 60  # 索引层只留 1 行
        with MemoryStore(test_config) as store:
            # 三条都落选精选层（首条占满预算）；索引层应只显示分最高者
            store.add(key="p:t:fact:first", value="x" * 150, importance=9.0)
            store.add(key="p:t:fact:low", value="低分", importance=1.0)
            store.add(key="p:t:fact:high", value="高分", importance=8.0)

        result = get_session_start_block(config=test_config)

        assert "- p:t:fact:high" in result
        assert "- p:t:fact:low" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q`
Expected: FAIL（当前 omitted 顺序为 pinned_omit + normal_omit + quota_overflow，normal_omit 在前，low 可能先入索引）

- [ ] **Step 3: Write minimal implementation**

`get_session_start_block` 中：

```python
    omitted = pinned_omit + normal_omit + quota_overflow
```

改为：

```python
    omitted = pinned_omit + normal_omit + quota_overflow
    omitted.sort(key=lambda m: compute_score(m, config), reverse=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/hooks.py tests/test_hooks.py
git commit -m "feat: index layer lists omitted memories by score, highest first"
```

---

### Task 3: 小修复包 — replace 继承 tags/category、update_metadata、install.sh、README

**Files:**
- Modify: `evolvmem/memory_store.py`（`replace`、`update_metadata` 新方法）
- Modify: `install.sh`（config.json 模板补 inject_* 键）
- Modify: `README.md`（Configuration 补 forget_rate_limit_days、stop_hook_safe、value_max_chars）
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Produces:
  - `replace(key, new_value, ...)`：tags/category 未显式提供时继承旧记录（与 importance/tier 行为一致）；显式传空列表/空串则尊重调用方
  - `MemoryStore.update_metadata(mem_id: int, importance: float | None = None, tier: str | None = None) -> None`（Task 6/7 依赖）

- [ ] **Step 1: Write the failing test**

```python
    def test_replace_inherits_tags_and_category(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:decision:db", value="用 MySQL",
                  category="decision", tags=["db", "arch"],
                  importance=9.0, tier="pinned")
        new_id = store.replace(key="p:t:decision:db", new_value="改用 PostgreSQL")
        rec = store.get_by_id(new_id)
        assert rec["category"] == "decision"
        assert rec["tags"] == "db,arch"
        store.close()

    def test_replace_explicit_tags_override(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:fact:x", value="v1", tags=["old"])
        new_id = store.replace(key="p:t:fact:x", new_value="v2", tags=["new"])
        assert store.get_by_id(new_id)["tags"] == "new"
        store.close()

    def test_update_metadata(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:t:fact:m", value="v")
        store.update_metadata(mid, importance=8.5, tier="pinned")
        rec = store.get_by_id(mid)
        assert rec["importance"] == 8.5
        assert rec["tier"] == "pinned"
        store.update_metadata(mid, importance=3.0)
        rec2 = store.get_by_id(mid)
        assert rec2["importance"] == 3.0
        assert rec2["tier"] == "pinned"  # 未指定的字段不变
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -q`
Expected: FAIL（category/tags 重置为空、update_metadata 不存在）

- [ ] **Step 3: Write minimal implementation**

`replace` 中（现有 importance/tier 继承逻辑旁）：

```python
        category = kwargs.pop("category", None)
        if category is None:
            category = old["category"]
        if "tags" in kwargs:
            tag_str = ",".join(kwargs.pop("tags"))
        else:
            tag_str = old["tags"]
```

（替换原有的 `tag_str = ",".join(kwargs.pop("tags", [])) if "tags" in kwargs else ""` 与 `kwargs.pop("category", "")` 用法，并传入 `_insert_row`。）

新增方法：

```python
    def update_metadata(self, mem_id: int, importance: float | None = None,
                        tier: str | None = None) -> None:
        """Update importance/tier in place (used by batch rescoring)."""
        if importance is not None:
            self._conn.execute(
                "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                (importance, _now_iso(), mem_id),
            )
        if tier is not None:
            self._conn.execute(
                "UPDATE memories SET tier=?, updated_at=? WHERE id=?",
                (tier, _now_iso(), mem_id),
            )
        self._conn.commit()
```

install.sh 中生成 config.json 的部分补 inject_* 全部键（值与 config.py 默认值一致）；README.md Configuration 一节补 `forget_rate_limit_days`、`stop_hook_safe`、`value_max_chars` 三行（沿用现有表格格式）。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/memory_store.py tests/test_memory_store.py install.sh README.md
git commit -m "fix: replace inherits tags/category; add update_metadata; config template and README parity"
```

---

### Task 4: scoring 第四因子 — relevance（cwd 项目匹配）

**Files:**
- Modify: `evolvmem/scoring.py`
- Modify: `evolvmem/config.py`
- Modify: `evolvmem/hooks.py`（传 context）
- Test: `tests/test_scoring.py`、`tests/test_hooks.py`

**Interfaces:**
- Produces:
  - `compute_score(memory, config, now=None, context: dict | None = None) -> float`；context 形如 `{"project": "hermes-memory-plugin"}`，None 时 relevance 项为 0（不影响既有调用方）
  - Config：`inject_w_relevance: float = 0.3`、`inject_project_aliases: dict = field(default_factory=dict)`（目录名 → key 段映射，如 `{"hermes": "purchase"}`；注意 dataclass mutable default 用 field）
  - hooks 内 `_session_context() -> dict`：取 `os.getcwd()` basename 作为 project

- [ ] **Step 1: Write the failing test**

tests/test_scoring.py 追加：

```python
class TestRelevance:
    def test_project_match_boosts_score(self, test_config):
        m = dict(_mem(), key="project:purchase:fact:x")
        no_ctx = compute_score(m, test_config, now=NOW)
        with_ctx = compute_score(m, test_config, now=NOW,
                                 context={"project": "purchase"})
        assert with_ctx > no_ctx

    def test_no_context_relevance_is_zero(self, test_config):
        m = dict(_mem(), key="project:purchase:fact:x")
        assert compute_score(m, test_config, now=NOW) == \
               compute_score(m, test_config, now=NOW, context=None)

    def test_alias_mapping(self, test_config):
        test_config.inject_project_aliases = {"hermes": "purchase"}
        m = dict(_mem(), key="project:purchase:fact:x")
        boosted = compute_score(m, test_config, now=NOW,
                                context={"project": "hermes"})
        plain = compute_score(m, test_config, now=NOW)
        assert boosted > plain

    def test_non_matching_project_no_boost(self, test_config):
        m = dict(_mem(), key="project:purchase:fact:x")
        assert compute_score(m, test_config, now=NOW,
                             context={"project": "other"}) == \
               compute_score(m, test_config, now=NOW)
```

注意：`_mem()` 不含 key 字段，现有 compute_score 不读 key——测试用 `dict(_mem(), key=...)` 补。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scoring.py -q`
Expected: FAIL（context 参数不存在）

- [ ] **Step 3: Write minimal implementation**

config.py（并入 save()；dataclass 顶部 import field）：

```python
    inject_w_relevance: float = 0.3   # cwd 项目匹配加分权重
    inject_project_aliases: dict = field(default_factory=dict)  # 目录名 → key 段
```

scoring.py：

```python
def _relevance_norm(memory: dict, config: Config,
                    context: dict | None) -> float:
    """1.0 when the memory key mentions the current project (or its alias), else 0.0."""
    if not context:
        return 0.0
    project = context.get("project") or ""
    if not project:
        return 0.0
    alias = config.inject_project_aliases.get(project, project)
    key = memory.get("key") or ""
    return 1.0 if alias in key else 0.0
```

`compute_score` 签名加 `context: dict | None = None`，返回值末尾加 `+ config.inject_w_relevance * _relevance_norm(memory, config, context)`。docstring 更新。

hooks.py：

```python
import os

def _session_context() -> dict:
    """Weak query signal for relevance: the session's working directory name."""
    try:
        return {"project": os.path.basename(os.getcwd())}
    except Exception:
        return {}
```

`get_session_start_block` 开头取 `context = _session_context()`，两处 `compute_score(m, config)` 调用改为 `compute_score(m, config, context=context)`。

tests/test_hooks.py 追加：

```python
    def test_cwd_project_boosts_matching_memories(self, test_config, monkeypatch):
        monkeypatch.chdir(test_config.data_dir)  # basename = 临时目录名，不含 'purchase'
        with MemoryStore(test_config) as store:
            store.add(key="project:purchase:fact:a", value="采购记忆", importance=5.0)
            store.add(key="project:other:fact:b", value="其他记忆", importance=5.0)
        # 别名让任意目录都匹配 purchase
        test_config.inject_project_aliases = {
            test_config.data_dir.name: "purchase"}
        result = get_session_start_block(config=test_config)
        assert result.index("采购记忆") < result.index("其他记忆")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/scoring.py evolvmem/config.py evolvmem/hooks.py tests/test_scoring.py tests/test_hooks.py
git commit -m "feat: relevance factor boosts memories matching the session project"
```

---

### Task 5: consolidator — 近重复检测与合并

**Files:**
- Create: `evolvmem/consolidator.py`
- Modify: `evolvmem/mcp_server.py`（注册 `memory_consolidate` 工具）
- Modify: `evolvmem/config.py`
- Test: `tests/test_consolidator.py`（新建）

**Interfaces:**
- Consumes: `VectorIndex.search`、`EmbeddingEngine.encode_document`、`compute_score`、`store.archive`、`store.update_metadata`（Task 3）。
- Produces:
  - `Consolidator(config, store, vidx, engine)` 方法：
    - `find_candidates(threshold: float | None = None) -> list[dict]`：对每个 active 记忆取向量最近邻，cosine similarity ≥ threshold 的对（去重、不含自身、双方均 active），返回 `[{"keep": {...}, "drop": {...}, "similarity": float}]`，keep/drop 按 `compute_score` 分高者为 keep
    - `consolidate(dry_run: bool = True) -> dict`：dry_run 返回候选清单；`dry_run=False` 时对每对执行：keep 方 `update_access`，drop 方 `archive()`，返回 `{"merged": N, "pairs": [...]}`
  - Config：`consolidate_similarity_threshold: float = 0.92`（入 save()）
  - MCP 工具 `memory_consolidate`，参数 `dry_run`（boolean，默认 true）、`threshold`（number，可选）
  - embedding 未加载时 `find_candidates` 返回 `[]`，MCP 返回 `{"error": "embedding engine not loaded"}`

- [ ] **Step 1: Write the failing test**

tests/test_consolidator.py（fake engine：对相同文本返回相同向量、不同文本返回正交向量；参考 tests/test_integration.py 里 FakeEmbeddingEngine 的写法）：

```python
"""consolidator tests."""
import numpy as np
import pytest
from evolvmem.consolidator import Consolidator
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


class FakeEngine:
    is_loaded = True
    def __init__(self, dim=8):
        self.dim = dim
    def encode_document(self, text):
        # 相同文本 → 相同向量；不同文本 → 按 hash 散列后归一化
        rng = np.random.RandomState(hash(text) % (2**31))
        v = rng.rand(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def setup(test_config):
    store = MemoryStore(test_config)
    store.initialize()
    vidx = VectorIndex(test_config)
    vidx.initialize(dim=8)
    engine = FakeEngine()
    c = Consolidator(test_config, store, vidx, engine)
    yield store, vidx, engine, c
    vidx.close()
    store.close()


def _add_with_vector(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


class TestFindCandidates:
    def test_identical_values_flagged(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "b:y", "完全相同的内容")
        _add_with_vector(store, vidx, engine, "c:z", "完全不同的东西")
        pairs = c.find_candidates()
        assert len(pairs) == 1
        assert pairs[0]["similarity"] > 0.99

    def test_higher_score_is_keep(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "b:y", "相同内容", importance=9.0)
        pairs = c.find_candidates()
        assert pairs[0]["keep"]["importance"] == 9.0
        assert pairs[0]["drop"]["importance"] == 3.0

    def test_engine_not_loaded_returns_empty(self, test_config):
        store = MemoryStore(test_config); store.initialize()
        vidx = VectorIndex(test_config); vidx.initialize(dim=8)
        engine = FakeEngine(); engine.is_loaded = False
        c = Consolidator(test_config, store, vidx, engine)
        assert c.find_candidates() == []
        vidx.close(); store.close()


class TestConsolidate:
    def test_dry_run_changes_nothing(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容")
        _add_with_vector(store, vidx, engine, "b:y", "相同内容")
        result = c.consolidate(dry_run=True)
        assert len(result["pairs"]) == 1
        assert store.count_active() == 2

    def test_apply_archives_drop(self, setup):
        store, vidx, engine, c = setup
        _add_with_vector(store, vidx, engine, "a:x", "相同内容", importance=3.0)
        _add_with_vector(store, vidx, engine, "b:y", "相同内容", importance=9.0)
        result = c.consolidate(dry_run=False)
        assert result["merged"] == 1
        assert store.count_active() == 1
        active = store.get_active()[0]
        assert active["importance"] == 9.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_consolidator.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Write minimal implementation**

config.py（并入 save()）：

```python
    consolidate_similarity_threshold: float = 0.92  # 近重复合并的相似度阈值
```

新建 `evolvmem/consolidator.py`：

```python
"""Memory consolidation — near-duplicate detection and merge.

Uses the existing HNSW vector index: for each active memory, find its
nearest neighbor; pairs above the similarity threshold are merge candidates.
The higher-scored memory (compute_score) is kept, the other is archived.
"""

import numpy as np

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.scoring import compute_score


class Consolidator:
    """Detects and merges near-duplicate active memories."""

    def __init__(self, config: Config, store: MemoryStore,
                 vidx: VectorIndex, engine) -> None:
        self.config = config
        self.store = store
        self.vidx = vidx
        self.engine = engine

    def find_candidates(self, threshold: float | None = None) -> list[dict]:
        """Return merge candidate pairs: [{keep, drop, similarity}]."""
        if threshold is None:
            threshold = self.config.consolidate_similarity_threshold
        if not getattr(self.engine, "is_loaded", False):
            return []

        actives = self.store.get_active()
        seen: set[frozenset] = set()
        pairs: list[dict] = []
        for m in actives:
            try:
                vec = np.array(self.engine.encode_document(m["value"]),
                               dtype=np.float32)
                hits = self.vidx.search(vec, 4)
            except Exception:
                continue
            for h in hits:
                other_id = h["id"]
                if other_id == m["id"]:
                    continue
                similarity = max(0.0, 1.0 - h["distance"] / 2.0)
                if similarity < threshold:
                    continue
                pair_key = frozenset({m["id"], other_id})
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                other = self.store.get_by_id(other_id)
                if not other or other["status"] != "active":
                    continue
                keep, drop = self._order(m, other)
                pairs.append({"keep": keep, "drop": drop,
                              "similarity": round(similarity, 4)})
        return pairs

    def consolidate(self, dry_run: bool = True,
                    threshold: float | None = None) -> dict:
        """Merge near-duplicates. dry_run=True only reports."""
        pairs = self.find_candidates(threshold)
        if dry_run:
            return {"dry_run": True, "pairs": pairs, "merged": 0}
        merged = 0
        for p in pairs:
            drop = p["drop"]
            current = self.store.get_by_id(drop["id"])
            if not current or current["status"] != "active":
                continue  # already archived by an earlier pair in this run
            self.store.update_access(p["keep"]["id"])
            self.store.archive(drop["id"])
            merged += 1
        return {"dry_run": False, "pairs": pairs, "merged": merged}

    def _order(self, a: dict, b: dict) -> tuple[dict, dict]:
        """Higher compute_score wins as keep."""
        sa = compute_score(a, self.config)
        sb = compute_score(b, self.config)
        return (a, b) if sa >= sb else (b, a)
```

注意：实现时先读 `evolvmem/vector_index.py` 确认 `search()` 的返回格式（`[{"id":..., "distance":...}]` 以实际为准，不一致则适配）。

mcp_server.py：`__init__`/`initialize` 加 `self.consolidator = Consolidator(self.config, self.store, self.vidx, self.engine)`；handlers 注册 `"memory_consolidate"`：

```python
    def _memory_consolidate(self, args: dict) -> dict:
        if not self.engine.is_loaded:
            return {"error": "embedding engine not loaded"}
        dry_run = bool(args.get("dry_run", True))
        threshold = args.get("threshold")
        result = self.consolidator.consolidate(
            dry_run=dry_run,
            threshold=float(threshold) if threshold is not None else None,
        )
        # 压缩输出，避免把整条 value 灌回上下文
        for p in result.get("pairs", []):
            for side in ("keep", "drop"):
                m = p[side]
                p[side] = {"id": m["id"], "key": m["key"],
                           "preview": m["value"][:80],
                           "importance": m["importance"]}
        return result
```

tools/list 追加 schema：

```python
                        {
                            "name": "memory_consolidate",
                            "description": "Find and merge near-duplicate memories (vector similarity). dry_run=true (default) only reports candidates.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean", "default": True},
                                    "threshold": {"type": "number",
                                                  "description": "similarity threshold, default from config (0.92)"},
                                },
                            },
                        },
```

test_integration.py 追加（server fixture 的 engine 未加载时）：

```python
    def test_memory_consolidate_requires_embedding(self, server):
        result = server.handle_tool_call("memory_consolidate", {})
        assert "error" in result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evolvmem/consolidator.py evolvmem/mcp_server.py evolvmem/config.py tests/test_consolidator.py tests/test_integration.py
git commit -m "feat: consolidation — vector near-duplicate detection and merge (memory_consolidate)"
```

---

### Task 6: 记忆时效 — expires_at 列 + 过期归档

**Files:**
- Modify: `evolvmem/memory_store.py`（迁移 + add 参数 + get_active 过滤）
- Modify: `evolvmem/forgetting.py`（过期归档）
- Modify: `evolvmem/mcp_server.py`（memory_add 接受 expires_at）
- Test: `tests/test_memory_store.py`、`tests/test_forgetting.py`

**Interfaces:**
- Produces:
  - `memories.expires_at TEXT DEFAULT NULL`（幂等迁移，格式同 created_at）
  - `add(..., expires_at: str | None = None)`；MCP `memory_add` 接受 `expires_at`（"YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"）
  - `get_active()` 排除 `expires_at <= now`（过期即不可注入不可检索，但状态仍 active 直到 forgetting 归档）
  - `ForgettingEngine.run()` 先归档过期记忆，再走既有衰减规则；返回值含义不变

- [ ] **Step 1: Write the failing test**

test_memory_store.py：

```python
    def test_expired_memory_not_in_get_active(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:fact:temp", value="临时事实",
                  expires_at="2020-01-01 00:00:00")
        store.add(key="p:t:fact:durable", value="长期事实")
        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["key"] == "p:t:fact:durable"
        store.close()
```

test_forgetting.py（按该文件现有 fixture 模式）：

```python
    def test_run_archives_expired(self, test_config):
        from evolvmem.memory_store import MemoryStore
        from evolvmem.forgetting import ForgettingEngine
        with MemoryStore(test_config) as store:
            mid = store.add(key="p:t:fact:exp", value="已过期",
                            expires_at="2020-01-01 00:00:00")
            archived = ForgettingEngine(test_config, store).run()
            assert archived >= 1
            assert store.get_by_id(mid)["status"] == "archived"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py tests/test_forgetting.py -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

`_create_tables` 迁移块（一期 Task 1 的 PRAGMA 模式）追加：

```python
        if "expires_at" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN expires_at TEXT DEFAULT NULL"
            )
            migrated = True
```

（注意保持 `migrated` 标志语义：expires_at 不需要回填，`migrated` 仅控制 importance/tier 回填——把 expires_at 的 ALTER 放在回填判断之外独立执行。）

`_insert_row` 加 `expires_at: str | None = None` 参数并入 INSERT；`add` 透传；`replace` 继承旧 expires_at（同 importance 模式）。日期只接受字符串原样存储（SQLite 字符串比较即可排序），`"YYYY-MM-DD"` 形式补 ` 00:00:00` 后缀（在 add/replace 入口做：`if expires_at and len(expires_at) == 10: expires_at += " 00:00:00"`）。

`get_active`：

```python
        rows = self._execute(
            "SELECT * FROM memories WHERE status='active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY updated_at DESC",
            (_now_iso(),),
        )
```

forgetting.py `run` 开头加：

```python
        expired = self.store._execute(
            "SELECT id FROM memories WHERE status='active' "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (_now_iso(),),
        )
        for row in expired:
            self.store.archive(row["id"])
```

（forgetting.py 需要从 memory_store import _now_iso。）

mcp_server `_memory_add`：`expires_at = args.get("expires_at")` 透传进 add/replace 的 extra dict（None 时不传）。tools/list schema 加：

```python
                                    "expires_at": {
                                        "type": "string",
                                        "description": "Optional expiry, e.g. 2026-12-31; expired memories stop being injected and get archived",
                                    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS（注意检查既有 forgetting 测试不受"先归档过期"影响）

- [ ] **Step 5: Commit**

```bash
git add evolvmem/memory_store.py evolvmem/forgetting.py evolvmem/mcp_server.py tests/test_memory_store.py tests/test_forgetting.py
git commit -m "feat: memory expiry — expires_at column, injection filter, auto-archive"
```

---

### Task 7: 迁移数据批量重打分（数据治理，非代码任务）

**Files:**
- 无代码改动；产出 `~/.claude/evolvmem/` 数据库的 importance/tier 更新

**Interfaces:**
- Consumes: Task 3 的 `update_metadata`。

- [ ] **Step 1: 导出待评分清单**

```bash
cd /home/jiangli/hermes-memory-plugin && PYTHONPATH=. .venv/bin/python -c "
import json
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
with MemoryStore(Config.from_file()) as s:
    rows = [dict(r) for r in s._execute(
        \"SELECT id, key, value FROM memories WHERE status='active' AND importance=5.0 AND tier='normal'\")]
print(len(rows))
json.dump(rows, open('/tmp/rescore_input.json','w'), ensure_ascii=False, indent=1)
"
```

- [ ] **Step 2: LLM 批量评分（控制器编排）**

把 /tmp/rescore_input.json 按 ~50 条分片，派并行子代理，每个输出 JSON：`[{"id": N, "importance": 1-10, "tier": "pinned|normal", "reason": "一句话"}]` 写入 /tmp/rescore_part_N.json。评分标准沿用 extractor prompt 的 guide（9-10 硬约束/关键决策；7-8 重要决策；5-6 一般偏好事实；3-4 边缘参考；1-2 基本无价值）。

- [ ] **Step 3: 应用并校验**

```bash
cd /home/jiangli/hermes-memory-plugin && PYTHONPATH=. .venv/bin/python -c "
import json, glob
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
updates = []
for f in glob.glob('/tmp/rescore_part_*.json'):
    updates.extend(json.load(open(f)))
with MemoryStore(Config.from_file()) as s:
    for u in updates:
        imp = max(1.0, min(10.0, float(u['importance'])))
        tier = u['tier'] if u['tier'] in ('pinned','normal') else 'normal'
        s.update_metadata(int(u['id']), importance=imp, tier=tier)
    print('updated', len(updates))
    dist = s._execute(\"SELECT tier, ROUND(importance) i, COUNT(*) c FROM memories WHERE status='active' GROUP BY tier, i ORDER BY tier, i\")
    for r in dist: print(dict(r))
"
```

- [ ] **Step 4: 验证注入块改善**

重新生成 `get_session_start_block()`，确认精选层不再被 importance=5 的迁移摘要霸榜。无需 commit（数据变更）。

---

### Task 8: 真实库验证 + consolidation dry-run + README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1-7 全部。

- [ ] **Step 1: 备份真实库**（sqlite3 backup API，不用 cp——WAL 模式）：

```bash
cd /home/jiangli/hermes-memory-plugin && PYTHONPATH=. .venv/bin/python -c "
import sqlite3
src = sqlite3.connect('file:' + str(__import__('pathlib').Path.home()) + '/.claude/evolvmem/memory.db?mode=ro', uri=True)
dst = sqlite3.connect(str(__import__('pathlib').Path.home()) + '/.claude/evolvmem/memory.db.bak-phase23')
src.backup(dst); dst.close(); src.close()
print('backup done')
"
```

- [ ] **Step 2: 迁移验证 + 注入块再测**（expires_at 列出现、无报错、块结构正常）

- [ ] **Step 3: consolidation dry-run 报告**

通过 MCP server 进程内或直接脚本跑 `Consolidator.consolidate(dry_run=True)`（真实 embedding 引擎），输出候选对清单写入报告。**不执行真实合并**——候选清单交用户确认后再跑 `dry_run=False`。

- [ ] **Step 4: README 更新**：Features 加两行（consolidation、expiry + relevance）；Tools 表加 `memory_consolidate`；`memory_add` 描述补 `expires_at`。

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: consolidation, expiry and relevance in README"
```

---

## Self-Review 记录

- **Spec coverage**: 二期 4 项（活路径防线 T1、索引重排 T2、重打分 T7、小修复 T3）✓；三期 3 项（relevance T4、consolidation T5、时效 T6）✓；procedural 排除已声明。
- **Placeholder scan**: Task 5 的 VectorIndex.search 返回格式标注了"以实际为准"，因一期未直接暴露该 API 细节——实施者须先读源码适配。
- **Type consistency**: `update_metadata`（T3 定义）→ T7 使用；`compute_score(..., context=)`（T4 定义）→ hooks 使用；Consolidator 接口（T5 定义）→ mcp_server/T8 使用。一致。
