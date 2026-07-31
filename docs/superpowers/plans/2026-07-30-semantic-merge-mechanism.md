# Automatic Key/Contradiction Unification Mechanism Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「同主题记忆散落/新旧矛盾共存」从手工修复变为自动机制：写入侧语义合并（新值自动 supersede 高相似旧值）+ SessionStart 定期自动 consolidation。

**Architecture:** 新建 `evolvmem/semantic_merge.py` 共享助手（encode → vidx 近邻 → 阈值判定 → replace supersede）；接入 `mcp_server._memory_add`（冲突检测后）与 `kimi_hooks.session_end`（persist 循环内）；hooks.py 加 `_maybe_run_consolidation`（复用 forgetting 的节流模式，周频、阈值 0.97）。

**Tech Stack:** 现有组件（VectorIndex/EmbeddingEngine/store.replace）。无新增依赖。

## Global Constraints

- 不新增第三方依赖；测试 `.venv/bin/python -m pytest tests/ -q`（基线 135 passed, 2 skipped）。
- commit feat:/fix:/docs: 前缀。
- 合并阈值必须保守：默认 `add_merge_threshold=0.95`（写入侧）、auto consolidation 用 0.97。注意口径 similarity=(1+cos)/2。
- reference tier 永不参与合并（consolidator 既有规则，写入侧语义合并同样豁免——长文档不 supersede 别人也不被 supersede）。
- 真实库只读验证；写测试用临时库。

---

### Task 1: semantic_merge 助手 + memory_add 接入

**Files:**
- Create: `evolvmem/semantic_merge.py`
- Modify: `evolvmem/mcp_server.py`、Modify: `evolvmem/config.py`
- Test: `tests/test_semantic_merge.py`（新建）

**Interfaces:**
- Produces:
  - `find_semantic_match(store, vidx, engine, value, threshold, exclude_id=None) -> dict | None`：返回最相似的 active 且非 reference 记录（含 similarity 字段），无则 None
  - Config：`add_merge_threshold: float = 0.95`（入 save()）
  - `_memory_add`：同 key 冲突检测之后、store.add 之前加语义合并——`find_semantic_match` 命中时走 `store.replace(key=match["key"], new_value=value, importance=?, tier=?)`（importance/tier 传 None 继承旧值），返回 `{"status": "merged", "merged_into": match["id"], "key": match["key"], "similarity": s}`；命中即不再走 add。engine 未加载时跳过（行为同现状）。

- [ ] **Step 1: Write the failing test**

新建 tests/test_semantic_merge.py（FakeEngine 风格照 tests/test_consolidator.py 的 md5 定种子 FakeEngine）：

```python
"""semantic_merge tests."""
import numpy as np
import pytest
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.semantic_merge import find_semantic_match


class FakeEngine:
    is_loaded = True
    def __init__(self, dim=8):
        self.dim = dim
    def encode_document(self, text):
        import hashlib
        seed = int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "big")
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def setup(test_config):
    store = MemoryStore(test_config); store.initialize()
    vidx = VectorIndex(test_config); vidx.initialize(dim=8)
    engine = FakeEngine()
    yield store, vidx, engine
    vidx.close(); store.close()


def _add(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


class TestFindSemanticMatch:
    def test_identical_value_matches(self, setup):
        store, vidx, engine = setup
        mid = _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "数据库选用 PostgreSQL", 0.95)
        assert hit is not None and hit["id"] == mid
        assert hit["similarity"] > 0.99

    def test_different_value_no_match(self, setup):
        store, vidx, engine = setup
        _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "用户偏好中文界面", 0.95)
        assert hit is None

    def test_reference_tier_never_matches(self, setup):
        store, vidx, engine = setup
        _add(store, vidx, engine, "p:t:arch:doc", "长文档内容", tier="reference")
        hit = find_semantic_match(store, vidx, engine, "长文档内容", 0.95)
        assert hit is None

    def test_engine_not_loaded_returns_none(self, setup):
        store, vidx, engine = setup
        engine.is_loaded = False
        _add(store, vidx, engine, "p:t:fact:a", "任意内容")
        assert find_semantic_match(store, vidx, engine, "任意内容", 0.95) is None

    def test_exclude_id_skips_self(self, setup):
        store, vidx, engine = setup
        mid = _add(store, vidx, engine, "p:t:fact:a", "数据库选用 PostgreSQL")
        hit = find_semantic_match(store, vidx, engine, "数据库选用 PostgreSQL", 0.95, exclude_id=mid)
        assert hit is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_semantic_merge.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Write minimal implementation**

新建 `evolvmem/semantic_merge.py`：

```python
"""Write-time semantic merge: find an existing active memory that is
semantically the same fact, so the new value can supersede it instead of
coexisting as a fragmented duplicate."""

import numpy as np

from evolvmem.vector_index import VectorIndex
from evolvmem.memory_store import MemoryStore


def find_semantic_match(store: MemoryStore, vidx: VectorIndex, engine,
                        value: str, threshold: float,
                        exclude_id: int | None = None) -> dict | None:
    """Return the most similar active, non-reference memory (with similarity),
    or None. similarity uses the consolidator convention: 1 - distance/2."""
    if not getattr(engine, "is_loaded", False):
        return None
    try:
        vec = np.array(engine.encode_document(value), dtype=np.float32)
        hits = vidx.search(vec, 5)
    except Exception:
        return None
    best: dict | None = None
    for h in hits:
        if exclude_id is not None and h["id"] == exclude_id:
            continue
        similarity = max(0.0, 1.0 - h["distance"] / 2.0)
        if similarity < threshold:
            continue
        rec = store.get_by_id(h["id"])
        if not rec or rec["status"] != "active":
            continue
        if rec.get("tier") == "reference":
            continue
        if best is None or similarity > best["similarity"]:
            best = {**rec, "similarity": similarity}
    return best
```

config.py（入 save()）：

```python
    add_merge_threshold: float = 0.95  # 写入侧语义合并阈值（≥即 supersede 而非新增）
```

mcp_server.py `_memory_add`：在同 key 冲突检测的 `else`（add 路径）分支前加语义检查（conflict 决策为 add 时）：

```python
        else:
            # decision.action == "add": 同 key 无冲突 → 再做跨 key 语义合并
            if self.engine.is_loaded:
                match = find_semantic_match(
                    self.store, self.vidx, self.engine, value,
                    self.config.add_merge_threshold)
                if match:
                    new_id = self.store.replace(
                        key=match["key"], new_value=value,
                        importance=importance, tier=tier)
                    if self.engine.is_loaded:
                        try:
                            vec = self.engine.encode_document(value)
                            import numpy as np
                            self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                            self.vidx.save()
                        except Exception as e:
                            self._log(f"Vector update failed (id={new_id}): {e}")
                    return {"status": "merged", "merged_into": match["id"],
                            "key": match["key"],
                            "similarity": match["similarity"],
                            "new_id": new_id}
            mem_id = self.store.add(...)
```

（importance/tier 变量在该函数中已存在；传 None 时 replace 继承旧值——注意现有代码 extra dict 只在非 None 时传，保持同样语义。）

test_integration.py 追加（server fixture 的 engine 恒未加载，测跳过路径即可——端到端合并路径由 test_semantic_merge 覆盖）：

```python
    def test_memory_add_skips_merge_when_engine_not_loaded(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:x", "value": "供应商合同必须双人复核后归档",
        })
        assert result["status"] == "added"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add evolvmem/semantic_merge.py evolvmem/mcp_server.py evolvmem/config.py tests/test_semantic_merge.py tests/test_integration.py
git commit -m "feat: write-time semantic merge — new values supersede near-identical memories"
```

---

### Task 2: session_end 提炼链路接入语义合并

**Files:**
- Modify: `evolvmem/kimi_hooks.py`
- Test: `tests/test_kimi_hooks.py`（新建，fake 全部外部依赖）

**Interfaces:**
- Consumes: Task 1 的 `find_semantic_match`。
- Produces: session_end 的 persist 循环里，同 key 冲突检测为 add 时同样做语义合并（命中 → replace 旧值；阈值用 config.add_merge_threshold）。

- [ ] **Step 1: Write the failing test**

tests/test_kimi_hooks.py：构造 session_end 的 persist 段单元测试——直接测 `_persist_candidates`（若不存在，把 persist 循环抽成该函数再测）：

```python
"""kimi_hooks persist tests."""
import numpy as np
import pytest
from evolvmem.kimi_hooks import _persist_candidates
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from tests.test_semantic_merge import FakeEngine


def _add(store, vidx, engine, key, value, **kw):
    mid = store.add(key=key, value=value, **kw)
    vidx.add(mid, np.array(engine.encode_document(value), dtype=np.float32))
    return mid


def test_semantically_similar_candidate_merges(test_config):
    store = MemoryStore(test_config); store.initialize()
    vidx = VectorIndex(test_config); vidx.initialize(dim=8)
    engine = FakeEngine()
    old = _add(store, vidx, engine, "p:t:decision:db", "数据库选用 MySQL")

    cands = [CandidateMemory(key="other:key:fact:x",
                             value="数据库选用 MySQL",
                             attribute="fact", importance=8.0)]
    n = _persist_candidates(test_config, store, vidx, engine, cands, "sess")
    assert n == 1
    # 旧 key 被 supersede，而不是新增一条并列记忆
    assert store.count_active() == 1
    active = store.get_active()[0]
    assert active["key"] == "p:t:decision:db"
    assert active["value"] == "数据库选用 MySQL"
    assert store.get_by_id(old)["status"] == "superseded"
    vidx.close(); store.close()
```

注意：需要把 kimi_hooks.py 中 session_end 的 persist 循环抽成 `_persist_candidates(config, store, vidx, engine, candidates, session_id) -> int` 以便测试。engine 未加载时 vidx/engine 传 None 跳过合并。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_kimi_hooks.py -q`
Expected: FAIL（_persist_candidates 不存在）

- [ ] **Step 3: Write minimal implementation**

kimi_hooks.py：把 session_end 中的 persist 循环抽成模块级 `_persist_candidates(config, store, vidx, engine, candidates, session_id)`，逻辑同现状，但在 `decision.action == "add"` 分支（无同 key 冲突）加：

```python
            if engine is not None and getattr(engine, "is_loaded", False):
                match = find_semantic_match(store, vidx, engine, value,
                                            config.add_merge_threshold)
                if match:
                    added_ids.append(store.replace(
                        key=match["key"], new_value=value,
                        importance=c.importance, tier=c.tier))
                    continue
```

session_end 改为调用 `_persist_candidates(config, store, vidx, engine, filtered_candidates, session_id)`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add evolvmem/kimi_hooks.py tests/test_kimi_hooks.py
git commit -m "feat: session-end extraction also merges semantically identical facts"
```

---

### Task 3: SessionStart 自动 consolidation（节流周频）

**Files:**
- Modify: `evolvmem/hooks.py`、`evolvmem/config.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Produces:
  - Config：`consolidate_auto_run_hours: int = 168`（入 save()；0=关闭）
  - hooks `_maybe_run_consolidation(config, store)`：复用 `_maybe_run_forgetting` 的 marker 节流模式（marker 文件 `.last_consolidate`），embedding 加载失败静默跳过；reference 永不合并（consolidator 既有保证）

- [ ] **Step 1: Write the failing test**

```python
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
```

（注意 embedding 未加载时函数应只 touch marker 后跳过，不报错。）

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

config.py（入 save()）：

```python
    consolidate_auto_run_hours: int = 168  # SessionStart 自动合并的最小间隔（小时），0=关闭
```

hooks.py（在 `_maybe_run_forgetting` 旁）：

```python
def _last_consolidate_path(config: Config) -> Path:
    return config.data_dir / ".last_consolidate"


def _maybe_run_consolidation(config: Config, store: MemoryStore) -> None:
    """Run auto-consolidation at most once per consolidate_auto_run_hours.

    Conservative threshold (0.97) — only near-identical pairs merge.
    Failures are swallowed — maintenance must never block session start.
    """
    if config.consolidate_auto_run_hours <= 0:
        return
    try:
        marker = _last_consolidate_path(config)
        interval_s = config.consolidate_auto_run_hours * 3600
        if marker.exists():
            if time.time() - marker.stat().st_mtime < interval_s:
                return
        marker.touch()
        from evolvmem.vector_index import VectorIndex
        from evolvmem.embedding import EmbeddingEngine
        from evolvmem.consolidator import Consolidator
        engine = EmbeddingEngine(config)
        try:
            engine.initialize()
        except Exception:
            return  # 无 embedding 时跳过，marker 已记避免每次都尝试
        vidx = VectorIndex(config)
        vidx.initialize(dim=config.embedding_dim)
        merged = Consolidator(config, store, vidx, engine).consolidate(
            dry_run=False, threshold=0.97)
        if merged.get("merged"):
            print(f"[evolvmem] auto-consolidation merged {merged['merged']} pairs",
                  file=sys.stderr, flush=True)
        vidx.close()
        engine.close()
    except Exception:
        pass
```

`get_session_start_block` 中 `_maybe_run_forgetting(config, store)` 之后加一行 `_maybe_run_consolidation(config, store)`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add evolvmem/hooks.py evolvmem/config.py tests/test_hooks.py
git commit -m "feat: weekly auto-consolidation at session start (threshold 0.97)"
```

---

### Task 4: README + 真实库验证

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README**：Configuration 表补 `add_merge_threshold`、`consolidate_auto_run_hours`；Features 加「写入侧语义合并 + 周频自动合并」一句
- [ ] **Step 2: 真实库验证**：跑一次全量测试；`PYTHONPATH=. .venv/bin/python -c` 调 get_session_start_block() 确认无报错（marker 会 touch，embedding 加载跑首次自动合并，dry 观察 stderr 输出）
- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: semantic merge and auto-consolidation in README"
```

---

## Self-Review 记录

- **Spec coverage**：入口合并（T1 MCP + T2 提炼链路，两条写入路径都覆盖）、存量定期合并（T3）、可配置（阈值/开关全入 config）。
- **一致性**：replace 继承语义（importance/tier 传 None 继承旧值）与 Task 2 传显式值的差异已注明：T1 传变量（None 时继承），T2 传 c.importance/c.tier（提炼器打分，允许更新）。
- **Placeholder scan**：T2 的 `_persist_candidates` 抽取签名在 Interfaces 与 Step 1 一致。
