# Memory Quality & Real-Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复用户复盘发现的三个逻辑问题：①「热门榜」按被动检索命中排序导致低质数据霸榜；② memory_add 活路径无质量门，空摘要类低质数据可入库；③ 缺少真正的物理删除。并批量清理存量低质迁移数据。

**Architecture:** 热门榜改按 `importance × (access_count+1)` 综合排序并改语义标签；`memory_add` 增加规则式质量门（低信息模式拒收）；Web 控制台加 hard_delete（物理删除行）；存量 importance ≤2 迁移数据批量软删。

**Tech Stack:** Python stdlib + 现有 evolvmem 组件。无新增依赖。

## Global Constraints

- 不新增第三方依赖；测试 `.venv/bin/python -m pytest tests/ -q`（基线 125 passed, 2 skipped）。
- commit 用 feat:/fix: 前缀。
- 真实库（~/.claude/evolvmem/memory.db）写操作只允许 Task 4 的批量软删；验证用临时库。
- **工作区已有未提交的半成品改动**（属本计划基线，由 Task 1/2 补完并随任务提交）：
  - `evolvmem/web_server.py`：已加 `api_hard_delete` + `_MEM_ACTION_RE` 含 hard_delete + do_POST 分支；`api_stats` 的 top_accessed 已改综合排序并带 importance 字段
  - `evolvmem/web_static/index.html`：已加「彻底删除」按钮（尚无 JS handler）

---

### Task 1: 彻底删除（hard delete）收尾

**Files:**
- Modify: `evolvmem/web_static/index.html`（JS handler）
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: 基线中已有的 `api_hard_delete` 与路由。
- Produces: 前端「彻底删除」按钮完整可用；`api_hard_delete(store, mem_id) -> dict` 物理 DELETE 行。

- [ ] **Step 1: Write the failing test**

追加到 tests/test_web_server.py（fixture 模式照该文件现有）：

```python
def test_hard_delete_removes_row_permanently(test_config):
    from evolvmem.web_server import api_hard_delete
    s = MemoryStore(test_config); s.initialize()
    mid = s.add(key="p:t:temp", value="临时记忆")
    result = api_hard_delete(s, mid)
    assert result["ok"] is True
    assert s.get_by_id(mid) is None  # 物理消失，不是 status 标记
    s.close()

def test_hard_delete_not_found(test_config):
    from evolvmem.web_server import api_hard_delete
    s = MemoryStore(test_config); s.initialize()
    assert api_hard_delete(s, 999)["ok"] is False
    s.close()
```

（import MemoryStore 方式照该文件现有写法。）

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_server.py -q`
Expected: 若基线路由/函数完整则应直接 PASS——此时改为验证通过即可（TDD 对已完成基线不适用，如实记录）。

- [ ] **Step 3: 前端 JS handler**

index.html 的 tbody click handler 中（`act === "delete"` 分支后）加：

```javascript
      else if (act === "hard_delete") {
        if (confirm(`确认彻底删除 #${id}？\n物理删除数据库行，不可恢复！`)) {
          await post(id, "hard_delete"); toast("已彻底删除"); refresh();
        }
      }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿（125 + 2 新增）

- [ ] **Step 5: Commit**

```bash
git add evolvmem/web_server.py evolvmem/web_static/index.html tests/test_web_server.py
git commit -m "feat: hard delete permanently removes memory row (with confirm)"
```

---

### Task 2: 热门榜语义修正 — 综合热度 + 标签澄清

**Files:**
- Modify: `evolvmem/web_static/index.html`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: 基线中 `api_stats` 的 top_accessed（已按 `importance * (access_count+1)` 排序、含 importance 字段）。
- Produces: 前端热门榜展示综合热度，调用次数显示语义改「检索命中」。

- [ ] **Step 1: Write the failing test**

```python
def test_top_accessed_ranked_by_composite_heat(test_config):
    from evolvmem.web_server import api_stats
    s = MemoryStore(test_config); s.initialize()
    # 高频低分：4 次命中但 importance 1 → 综合 1*(4+1)=5
    junk = s.add(key="p:t:junk", value="空摘要", importance=1.0)
    for _ in range(4): s.update_access(junk)
    # 低频高分：1 次命中 importance 9 → 综合 9*(1+1)=18
    gem = s.add(key="p:t:gem", value="核心规则", importance=9.0)
    s.update_access(gem)
    top = api_stats(s)["top_accessed"]
    assert top[0]["id"] == gem  # 高分低频应排在高频低分之前
    s.close()
```

- [ ] **Step 2: Run test to verify it fails / passes**

基线后端已实现综合排序——直接验证 PASS 并记录。若 FAIL 则修正 api_stats 排序表达式。

- [ ] **Step 3: 前端语义修正**

index.html：
- 热门榜标题改：`🔥 综合热度 Top 10（重要性 × 检索命中）`
- 热门榜每行末尾在调用次数后追加重要性：`<span class="cnt">${t.access_count}次/${Number(t.importance).toFixed(0)}分</span>`，条形图宽度改用综合值 `t.importance * (t.access_count + 1)` 对最大值归一
- 表格「调用次数」列表头改「检索命中」；「只看技能候选」文案改「只看技能候选（≥3 次命中）」

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add evolvmem/web_server.py evolvmem/web_static/index.html tests/test_web_server.py
git commit -m "fix: hot list ranks by composite heat (importance x hits), clarify 检索命中 semantics"
```

---

### Task 3: memory_add 活路径质量门

**Files:**
- Modify: `evolvmem/mcp_server.py`
- Modify: `evolvmem/config.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Produces: Config 新字段 `value_min_chars: int = 10`（入 save()）；`_memory_add` 在长度上限检查旁加低信息拒收。

- [ ] **Step 1: Write the failing test**

```python
    def test_memory_add_rejects_trivial_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:trivial", "value": "等待用户指令。",
        })
        assert "error" in result
        assert server.store.count_active() == 0

    def test_memory_add_rejects_too_short(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:short", "value": "短",
        })
        assert "error" in result

    def test_memory_add_accepts_normal_value(self, server):
        result = server.handle_tool_call("memory_add", {
            "key": "p:t:fact:ok", "value": "供应商合同必须双人复核后归档",
        })
        assert result["status"] == "added"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_integration.py -q`
Expected: FAIL（当前可入库）

- [ ] **Step 3: Write minimal implementation**

config.py（value_max_chars 旁，入 save()）：

```python
    value_min_chars: int = 10  # memory_add 的 value 长度下限（低于视为无信息）
```

mcp_server.py 顶部加低信息模式常量与检查函数：

```python
# 低信息过渡语：自动摘要里常见的"零价值"句式（命中即拒收）
_LOW_INFO_PATTERNS = (
    "等待用户", "会话继续", "等待下一步", "等待用户确认",
    "等待用户后续", "no action required",
)


def _is_low_info(value: str) -> bool:
    v = value.strip()
    return any(p in v for p in _LOW_INFO_PATTERNS)
```

`_memory_add` 中 value 长度上限检查之后加：

```python
        if len(value.strip()) < self.config.value_min_chars:
            return {"error": f"value too short ({len(value.strip())} < {self.config.value_min_chars} chars); "
                             "no information content"}
        if _is_low_info(value):
            return {"error": "value looks like a low-information placeholder "
                             "(transitional/chatter); not persisting"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿。注意既有用例若有 <10 字符或命中模式的 value 需调整，报告中说明。

- [ ] **Step 5: Commit**

```bash
git add evolvmem/mcp_server.py evolvmem/config.py tests/test_integration.py
git commit -m "feat: quality gate on live memory_add path — reject trivial/short values"
```

---

### Task 4: 存量低质数据批量清理（数据治理，非代码任务）

**Files:** 无代码改动。

- [ ] **Step 1: 备份真实库**（sqlite3 backup API → `~/.claude/evolvmem/memory.db.bak-quality`）

- [ ] **Step 2: 预览清理范围**

```bash
cd /home/jiangli/hermes-memory-plugin && PYTHONPATH=. .venv/bin/python -c "
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
with MemoryStore(Config.from_file()) as s:
    rows = s._execute(\"SELECT COUNT(*) c FROM memories WHERE status='active' AND importance <= 2.0 AND tier='normal'\")
    print('将软删:', rows[0]['c'], '条')
"
```

- [ ] **Step 3: 批量软删（archived，可恢复）**

```bash
PYTHONPATH=. .venv/bin/python -c "
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore, _now_iso
with MemoryStore(Config.from_file()) as s:
    s._conn.execute(\"UPDATE memories SET status='archived', updated_at=? WHERE status='active' AND importance <= 2.0 AND tier='normal'\", (_now_iso(),))
    s._conn.commit()
    print('archived, 剩余 active:', s.count_active())
"
```

- [ ] **Step 4: 验证注入块与 stats**（active 数下降、注入块正常）并写入报告。无 commit。

---

### Task 5: 验证 + README + 服务重启

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 全量测试** `.venv/bin/python -m pytest tests/ -q` 绿色
- [ ] **Step 2: 重启服务** `systemctl --user restart evolvmem-web`，curl 验证 /api/stats 热门榜为综合排序、hard_delete 路由 404 不报（随便点个不存在 id 应返回 not found JSON）
- [ ] **Step 3: README 更新**：Tools/功能说明加 hard_delete 与质量门一段；Web Console 一节描述综合热度榜
- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: quality gate, hard delete and composite heat ranking"
```

---

## Self-Review 记录

- **Spec coverage**: 热门榜误导（T2）、活路径无质量门（T3）、无物理删除（T1）、存量低质数据（T4）——用户复盘的三问题全覆盖。
- **access_count 语义**：本计划不改计数机制本身（检索命中仍计数，作为频率信号保留），只改展示语义与排序权重；「是否停止被动命中计数」留给用户后续决策。
- **Placeholder scan**: T1/T2 因基线半成品存在，Step 2 允许"验证通过"替代"确认失败"，已如实标注。
- **Type consistency**: `api_hard_delete(store, mem_id) -> dict` 与既有 api_delete 同形；Config.value_min_chars 与 value_max_chars 同模式。
