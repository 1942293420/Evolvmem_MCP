# EvolvMem Extraction Quality Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-subagent-driven-development (recommended) or superpowers-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在继续使用单次 DeepSeek V4 Flash 非思考请求的前提下，为 EvolvMem 增加发送前凭据脱敏、中文与长期价值门控、确定性去重排序，以及摘要与原子记忆的 SQLite 原子写入。

**Architecture:** 新增纯函数策略模块 `evolvmem/extraction_policy.py`，由它完成脱敏、安全判断、质量判断和排序；`auto_extractor.py` 只维护中文模型协议；`kimi_hooks.py` 负责编排并在 SQLite 提交后同步向量；`memory_store.py` 提供可嵌套使用的外层事务。正常路径仍只调用一次模型，现有上下文超限分块、网络重试和 stale-session 状态语义保持不变。

**Tech Stack:** Python 3.11、pytest、SQLite/FTS5、`urllib.request`、`dataclasses`、现有 EvolvMem 模块；不增加第三方依赖。

## Global Constraints

- 以 `docs/superpowers/specs/2026-08-03-extraction-quality-hardening-design.md` 为验收依据，仅实现范围 A；不增加模型自动路由、二次 LLM 审核或持久化评测平台。
- 工作区已有用户认可但尚未提交的 DeepSeek provider、完整会话优先和 stale-session 相关修改。执行时必须保留这些修改；每次提交只暂存当前任务列出的文件，禁止使用 `git add .`。
- 测试中的 Token、JWT、密码、私钥和 URL 凭据必须全部为明显的合成值，不得复制本机真实配置。
- 策略和日志不得输出候选 `value`、命中的敏感片段或脱敏前消息。
- 所有功能改动遵循 red → green → refactor：先运行新增测试并确认因预期原因失败，再写最小实现，再运行相关回归。
- 完成修复后，按 `/home/jiangli/fix-records/README.md` 在 `/home/jiangli/fix-records/records/2026-08-04-evolvmem-extraction-quality-hardening.md` 如实记录症状、排查过程、根因、修复内容、验证和遗留事项。未通过的验收不得写成已修复。

---

## Task 1: 将模型提炼协议改为中文长期记忆合约

**Files:**

- Modify: `evolvmem/auto_extractor.py`
- Test: `tests/test_auto_extractor.py`

- [ ] **Step 1: 写一个会失败的中文提示词契约测试**

在 `tests/test_auto_extractor.py` 增加测试，检查提示词同时表达中文输出、长期保留、短期丢弃、摘要独立和 JSON Object 合约：

```python
def test_extraction_prompt_requires_chinese_durable_memories():
    prompt = AutoExtractor().build_extraction_prompt([
        {"role": "user", "content": "请记住这个长期约束"},
    ])

    assert "value 必须使用中文" in prompt
    assert "长期" in prompt
    assert "临时密码" in prompt
    assert "测试通过" in prompt
    assert "部署完成" in prompt
    assert '"memories"' in prompt
    assert "SESSION_SUMMARY" in prompt
    assert "SESSION_SUMMARY 不占 8 条原子记忆配额" in prompt
```

- [ ] **Step 2: 运行测试并确认它因当前英文提示词失败**

Run:

```bash
.venv/bin/pytest -q tests/test_auto_extractor.py::test_extraction_prompt_requires_chinese_durable_memories
```

Expected: FAIL；至少第一个中文合约断言失败，不应是导入错误或 fixture 错误。

- [ ] **Step 3: 用中文重写 `EXTRACTION_PROMPT`，保持协议兼容**

提示词必须明确包含以下规则：

```text
你是 EvolvMem 长期记忆提炼器。请审阅完整会话，只提炼跨会话仍有价值的信息。

保留：用户长期偏好和画像、硬约束与安全开关、业务规则、架构或技术决策及原因、废弃方案及替代原因、可复用故障根因和防复发规则。
丢弃：临时密码、等待输入或稍后确认、一次性测试、单次测试通过或测试数量、单次部署完成、纯提交号、已完成且没有长期决策或原因的待办、可直接从代码或 git 获得的事实。

所有 value 必须使用中文；说明性 tags 也使用中文。稳定 key、代码标识符、产品名和必要缩写可以保留英文。
只返回一个 JSON 对象，顶层字段必须且只能为 "memories"。
必须包含且只包含一个 key 为 SESSION_SUMMARY 的会话摘要；即使没有原子记忆也不能省略。SESSION_SUMMARY 不占 8 条原子记忆配额。
```

保留现有字段名、字段类型、200 字限制、attribute 枚举、tier 枚举和稳定 key 格式。不要删除 `parse_response()` 对旧顶层数组的兼容。

- [ ] **Step 4: 运行提示词和解析器回归**

Run:

```bash
.venv/bin/pytest -q tests/test_auto_extractor.py
```

Expected: PASS。

- [ ] **Step 5: 暂存并提交本任务文件**

```bash
git add evolvmem/auto_extractor.py tests/test_auto_extractor.py
git commit -m "feat: require Chinese durable memory extraction"
```

---

## Task 2: 新增发送前脱敏和摘要清洗策略

**Files:**

- Create: `evolvmem/extraction_policy.py`
- Create: `tests/test_extraction_policy.py`

- [ ] **Step 1: 先用行为断言证明策略模块尚不可用**

在 `tests/test_extraction_policy.py` 增加：

```python
import importlib.util


def test_extraction_policy_module_is_available():
    assert importlib.util.find_spec("evolvmem.extraction_policy") is not None
```

Run:

```bash
.venv/bin/pytest -q tests/test_extraction_policy.py::test_extraction_policy_module_is_available
```

Expected: FAIL；失败是明确的 `assert None is not None`，不把 collection 阶段的 `ModuleNotFoundError` 当作有效 RED。

- [ ] **Step 2: 创建无行为模块骨架并让模块可用性测试变绿**

创建只含模块 docstring 的 `evolvmem/extraction_policy.py`，重新运行 Step 1 命令并确认 PASS。此时尚未实现任何策略行为。

- [ ] **Step 3: 写发送前脱敏的参数化失败测试**

在 `tests/test_extraction_policy.py` 使用明显的合成凭据：

```python
import copy

import pytest

from evolvmem import extraction_policy as policy


@pytest.mark.parametrize("content,secret,marker", [
    ("Authorization: Bearer synthetic-token-123456", "synthetic-token-123456", "[已脱敏:token]"),
    ("api_key = sk-synthetic-abcdefghijklmnop", "sk-synthetic-abcdefghijklmnop", "[已脱敏:api_key]"),
    ("access_token: synthetic-access-123456", "synthetic-access-123456", "[已脱敏:token]"),
    ("refresh token 是 synthetic-refresh-123456", "synthetic-refresh-123456", "[已脱敏:token]"),
    ("password set to Synthetic-Pass-123!", "Synthetic-Pass-123!", "[已脱敏:password]"),
    ("密码为 合成口令-仅测试-123", "合成口令-仅测试-123", "[已脱敏:password]"),
    ("postgres://synthetic_user:synthetic_pass@example.invalid/db", "synthetic_pass", "[已脱敏:url凭据]"),
    ("密码保存在 /tmp/synthetic-credential.txt", "/tmp/synthetic-credential.txt", "[已脱敏:凭据位置]"),
])
def test_redact_messages_covers_credential_forms(content, secret, marker):
    original = [{"role": "user", "content": content}]
    snapshot = copy.deepcopy(original)

    redacted, count = policy.redact_messages(original)

    assert count >= 1
    assert secret not in redacted[0]["content"]
    assert marker in redacted[0]["content"]
    assert original == snapshot
    assert redacted is not original
    assert redacted[0] is not original[0]
```

再增加 PEM、JWT、摘要清洗和普通密码策略不误报测试：

```python
def test_redact_messages_removes_private_key_block_and_jwt():
    content = (
        "-----BEGIN PRIVATE KEY-----\nSYNTHETICKEYDATA\n-----END PRIVATE KEY-----\n"
        "jwt=eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl"
    )
    redacted, count = policy.redact_messages([{"role": "user", "content": content}])
    value = redacted[0]["content"]
    assert count == 2
    assert "SYNTHETICKEYDATA" not in value
    assert "eyJhbGci" not in value


def test_password_policy_sentence_is_not_a_secret():
    text = "密码策略要求至少 12 位，并开启多因素认证。"
    assert policy.contains_sensitive_text(text) is False
    redacted, count = policy.redact_messages([{"role": "user", "content": text}])
    assert count == 0
    assert redacted[0]["content"] == text


def test_sanitize_summary_redacts_local_secret_and_requires_chinese():
    summary, count = policy.sanitize_summary(
        "确定采用新架构，password: Synthetic-Pass-123!，并保留回滚路径。"
    )
    assert count == 1
    assert summary is not None
    assert "Synthetic-Pass-123!" not in summary
    assert policy.contains_cjk(summary)


@pytest.mark.parametrize("value", ["", "only English summary", "[已脱敏:password]"])
def test_sanitize_summary_rejects_empty_low_information_or_non_chinese(value):
    summary, _ = policy.sanitize_summary(value)
    assert summary is None
```

- [ ] **Step 4: 运行新测试并确认策略行为尚未实现**

Run:

```bash
.venv/bin/pytest -q tests/test_extraction_policy.py
```

Expected: FAIL；测试已正常收集，失败原因为缺少 `redact_messages` 等公开行为，不是导入或 fixture 错误。

- [ ] **Step 5: 实现无副作用的敏感模式和脱敏 API**

在 `evolvmem/extraction_policy.py` 定义以下公开接口：

```python
from __future__ import annotations

import re

from evolvmem.auto_extractor import CandidateMemory


def contains_cjk(text: str) -> bool:
    return re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text) is not None


def contains_sensitive_text(text: str) -> bool:
    return any(pattern.search(text) for _, pattern, _ in _SENSITIVE_RULES)


def redact_messages(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int]:
    redacted_messages: list[dict[str, str]] = []
    count = 0
    for message in messages:
        copied = dict(message)
        copied["content"], replacements = _redact_text(
            str(message.get("content", ""))
        )
        redacted_messages.append(copied)
        count += replacements
    return redacted_messages, count


def sanitize_summary(value: str) -> tuple[str | None, int]:
    sanitized, count = _redact_text(value.strip())
    informative = re.sub(r"\[已脱敏:[^\]]+\]|[\s，。；：、.!?]", "", sanitized)
    if not informative or not contains_cjk(sanitized):
        return None, count
    return sanitized, count
```

内部使用一组集中定义的、带类别标记的预编译规则。匹配顺序从结构最强的 PEM、URL、Bearer/JWT，到键值赋值和凭据位置，避免通用规则吞掉更精确的替换：

```python
_SENSITIVE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
        r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        re.IGNORECASE | re.DOTALL,
    ), "[已脱敏:private_key]"),
    ("url_credentials", re.compile(
        r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
        re.IGNORECASE,
    ), r"\g<scheme>[已脱敏:url凭据]@"),
    ("bearer", re.compile(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE
    ), "Bearer [已脱敏:token]"),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    ), "[已脱敏:token]"),
    ("api_key", re.compile(
        r"(?i)\b(?:api[_ -]?key|secret[_ -]?key)\s*(?:=|:|is|是|为)\s*"
        r"[^\s，。；,]{8,}"
    ), "api_key=[已脱敏:api_key]"),
    ("token", re.compile(
        r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|token)\s*"
        r"(?:=|:|is|是|为)\s*[^\s，。；,]{8,}"
    ), "token=[已脱敏:token]"),
    ("password", re.compile(
        r"(?i)(?:\bpassword\b|\bpasswd\b|\bpwd\b|密码)\s*"
        r"(?:=|:|set\s+to|is|是|为)\s*[^\s，。；,]{4,}"
    ), "password=[已脱敏:password]"),
    ("credential_location", re.compile(
        r"(?i)(?:密码|口令|密钥|token|credential)s?\s*"
        r"(?:存放|保存|位于|写入|stored?|saved?|located?)\s*(?:在|于|to|at|in)?\s*"
        r"[^\s，。；,]+"
    ), "凭据[已脱敏:凭据位置]"),
)
```

`_redact_text()` 必须使用 `pattern.subn(replacement, value)` 累加替换次数，不捕获或返回原值。URL 规则的替换要保留 scheme，以免破坏其余语义，但不得保留用户名或密码。

- [ ] **Step 6: 运行策略测试并按失败样例收紧正则**

Run:

```bash
.venv/bin/pytest -q tests/test_extraction_policy.py
```

Expected: PASS；尤其确认“密码策略要求至少 12 位”不命中。

- [ ] **Step 7: 暂存并提交策略实现**

```bash
git add evolvmem/extraction_policy.py tests/test_extraction_policy.py
git commit -m "feat: redact credentials before memory extraction"
```

---

## Task 3: 实现候选门控、同 key 去重和稳定排序

**Files:**

- Modify: `evolvmem/extraction_policy.py`
- Modify: `tests/test_extraction_policy.py`

- [ ] **Step 1: 写安全、中文和短期状态门控失败测试**

增加构造函数和参数化测试：

```python
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.extraction_policy import evaluate_candidate, rank_candidates


def candidate(value: str, **overrides) -> CandidateMemory:
    values = {
        "key": "project:test:fact:item",
        "value": value,
        "attribute": "fact",
        "importance": 5.0,
        "confidence": 0.8,
        "tier": "normal",
    }
    values.update(overrides)
    return CandidateMemory(**values)


@pytest.mark.parametrize("item,reason", [
    (candidate("临时密码为 Synthetic-Pass-123!"), "sensitive"),
    (candidate("Waiting for user confirmation"), "language"),
    (candidate("等待用户稍后确认输入。"), "ephemeral"),
    (candidate("本次 184 个测试全部通过。"), "ephemeral"),
    (candidate("本次部署已经完成。"), "ephemeral"),
    (candidate("本次部署已经完成。", attribute="decision"), "ephemeral"),
    (candidate("提交 commit abcdef123456 已完成。"), "ephemeral"),
])
def test_evaluate_candidate_rejects_unsafe_or_ephemeral_items(item, reason):
    assert evaluate_candidate(item).reason == reason
    assert evaluate_candidate(item).accepted is False


def test_evaluate_candidate_keeps_durable_broker_constraint():
    item = candidate(
        "Broker 完成初始化前禁止任何写操作，这是持续有效的安全约束。",
        attribute="constraint",
        importance=10,
        tier="pinned",
    )
    assert evaluate_candidate(item).accepted is True


def test_evaluate_candidate_keeps_password_policy_without_password_value():
    item = candidate(
        "密码策略要求至少 12 位并启用多因素认证。",
        attribute="constraint",
    )
    assert evaluate_candidate(item).accepted is True
```

- [ ] **Step 2: 写去重和排序失败测试**

```python
def test_rank_candidates_deduplicates_same_key_by_quality_tuple():
    low = candidate("采用方案甲。", key="project:x:decision:api", confidence=0.7)
    high = candidate(
        "采用方案乙，因为它避免重复写入。",
        key="PROJECT:X:DECISION:API",
        confidence=0.9,
        importance=8,
    )
    assert rank_candidates([low, high]) == [high]


def test_rank_candidates_promotes_pinned_even_when_model_puts_it_last():
    ordinary = [
        candidate(
            f"第 {index} 条长期架构决定保留原因。",
            key=f"project:x:decision:{index}",
            importance=9,
            confidence=0.9,
        )
        for index in range(9)
    ]
    pinned = candidate(
        "用户长期偏好使用中文沟通。",
        key="user:preference:communication:language",
        attribute="preference",
        tier="pinned",
        importance=7,
    )
    ranked = rank_candidates([*ordinary, pinned], limit=8)
    assert ranked[0] is pinned
    assert len(ranked) == 8


def test_rank_candidates_uses_importance_confidence_then_original_order():
    first = candidate("保留第一项架构决定。", key="a", importance=7, confidence=0.8)
    second = candidate("保留第二项架构决定。", key="b", importance=8, confidence=0.6)
    third = candidate("保留第三项架构决定。", key="c", importance=7, confidence=0.8)
    assert rank_candidates([first, second, third]) == [second, first, third]
```

- [ ] **Step 3: 运行新测试并确认公开接口尚不存在**

Run:

```bash
.venv/bin/pytest -q tests/test_extraction_policy.py
```

Expected: FAIL，原因为 `evaluate_candidate` 或 `rank_candidates` 尚未定义。

- [ ] **Step 4: 实现稳定原因码和组合式短期判断**

新增：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str = ""


_EPHEMERAL_PATTERNS = (
    re.compile(r"(?:等待|稍后|之后).{0,12}(?:用户|输入|确认)"),
    re.compile(r"(?:临时|暂时|一次性).{0,12}(?:密码|设置|测试|任务)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:测试|用例).{0,12}(?:通过|成功|完成|\d+\s*个)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:部署|发布|上线).{0,8}(?:通过|成功|完成|结束)"),
    re.compile(r"(?:commit|提交)\s*[0-9a-f]{7,40}", re.IGNORECASE),
)
_DURABLE_CONTEXT_RE = re.compile(
    r"长期|持续|永久|约束|规则|决定|原因|因为|防止|禁止|必须"
)


def evaluate_candidate(candidate: CandidateMemory) -> PolicyDecision:
    value = candidate.value.strip()
    if contains_sensitive_text(value):
        return PolicyDecision(False, "sensitive")
    if not contains_cjk(value):
        return PolicyDecision(False, "language")
    is_ephemeral = any(pattern.search(value) for pattern in _EPHEMERAL_PATTERNS)
    if is_ephemeral and not _DURABLE_CONTEXT_RE.search(value):
        return PolicyDecision(False, "ephemeral")
    return PolicyDecision(True)
```

不得添加“前”“直到”“完成”等单关键词拒绝，也不得仅因模型把候选标成 `constraint`、`decision`、`preference` 或 `user_profile` 就绕过短期门控。纯测试、部署完成和提交记录无论 attribute 为何都拒绝；只有正文同时包含长期决定、约束或原因的明确证据时才保留。

- [ ] **Step 5: 实现先去重、再排序、最后截断**

```python
def _quality_tuple(candidate: CandidateMemory, original_index: int) -> tuple:
    return (
        candidate.tier == "pinned",
        candidate.importance,
        candidate.confidence,
        -original_index,
    )


def rank_candidates(
    candidates: list[CandidateMemory], limit: int = 8,
) -> list[CandidateMemory]:
    best_by_key: dict[str, tuple[CandidateMemory, int]] = {}
    for index, candidate in enumerate(candidates):
        normalized_key = candidate.key.strip().casefold()
        current = best_by_key.get(normalized_key)
        if current is None or _quality_tuple(candidate, index) > _quality_tuple(*current):
            best_by_key[normalized_key] = (candidate, index)
    ranked = sorted(
        best_by_key.values(),
        key=lambda item: _quality_tuple(*item),
        reverse=True,
    )
    return [candidate for candidate, _ in ranked[:limit]]
```

`rank_candidates()` 不做门控；调用方必须先用 `evaluate_candidate()` 过滤，这样原因计数保持可见。SESSION_SUMMARY 不传入该函数。

- [ ] **Step 6: 运行策略测试和提炼器回归**

Run:

```bash
.venv/bin/pytest -q tests/test_extraction_policy.py tests/test_auto_extractor.py
```

Expected: PASS。

- [ ] **Step 7: 暂存并提交候选策略**

```bash
git add evolvmem/extraction_policy.py tests/test_extraction_policy.py
git commit -m "feat: gate and rank durable memory candidates"
```

---

## Task 4: 为 MemoryStore 增加外层原子事务

**Files:**

- Modify: `evolvmem/memory_store.py`
- Modify: `tests/test_memory_store.py`

- [ ] **Step 1: 写事务提交、回滚和 replace 嵌套失败测试**

在 `tests/test_memory_store.py` 复用现有临时 Config fixture，并增加：

```python
@pytest.fixture
def store(test_config):
    with MemoryStore(test_config) as instance:
        yield instance


def test_transaction_commits_all_writes(store):
    with store.transaction():
        first = store.add("project:x:fact:first", "第一条长期记录内容。")
        second = store.add("project:x:fact:second", "第二条长期记录内容。")
    assert store.get_by_id(first)["status"] == "active"
    assert store.get_by_id(second)["status"] == "active"


def test_transaction_rolls_back_all_writes(store):
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with store.transaction():
            store.add("project:x:fact:first", "第一条长期记录内容。")
            store.add("project:x:fact:second", "第二条长期记录内容。")
            raise RuntimeError("synthetic failure")
    assert store.get_by_key("project:x:fact:first") == []
    assert store.get_by_key("project:x:fact:second") == []


def test_replace_joins_outer_transaction_and_rolls_back(store):
    old_id = store.add("project:x:decision:api", "采用旧接口方案。")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with store.transaction():
            store.replace(
                "project:x:decision:api",
                "采用新接口方案，因为它避免重复写入。",
            )
            raise RuntimeError("synthetic failure")
    assert store.get_by_id(old_id)["status"] == "active"
    records = store.get_by_key("project:x:decision:api")
    active_ids = [record["id"] for record in records if record["status"] == "active"]
    assert active_ids == [old_id]
```

- [ ] **Step 2: 运行事务测试并确认 `transaction()` 尚不存在**

Run:

```bash
.venv/bin/pytest -q tests/test_memory_store.py -k transaction
```

Expected: FAIL，原因为 `MemoryStore` 没有 `transaction`。

- [ ] **Step 3: 实现事务深度和条件提交**

在 `MemoryStore.__init__` 设置：

```python
self._transaction_depth = 0
```

新增：

```python
from contextlib import contextmanager
from collections.abc import Iterator


def _commit_if_outermost(self) -> None:
    if self._transaction_depth == 0:
        self._conn.commit()


@contextmanager
def transaction(self) -> Iterator["MemoryStore"]:
    outermost = self._transaction_depth == 0
    if outermost:
        self._conn.execute("BEGIN IMMEDIATE")
    self._transaction_depth += 1
    try:
        yield self
        if outermost:
            self._conn.commit()
    except Exception:
        if outermost:
            self._conn.rollback()
        raise
    finally:
        self._transaction_depth -= 1
```

把 `add()` 及其他普通写 API 的直接 `self._conn.commit()` 改为 `_commit_if_outermost()`，保持未使用事务时的自动提交行为。

- [ ] **Step 4: 让 replace 复用外层事务而不重复 BEGIN**

将现有 `replace()` 的 SQL 主体提取为 `_replace_no_commit()`；公开方法按是否已经在事务内选择路径：

```python
def replace(self, key: str, new_value: str, **kwargs) -> int:
    if self._transaction_depth:
        return self._replace_no_commit(key, new_value, **kwargs)
    with self.transaction():
        return self._replace_no_commit(key, new_value, **kwargs)
```

`_replace_no_commit()` 中删除 `BEGIN IMMEDIATE`、`commit()` 和 `rollback()`，但保留旧记录 supersede、新记录 add、关系迁移和 FTS 更新的原有顺序。确保它调用的 `add()` 因事务深度非零而不会提前提交。

- [ ] **Step 5: 运行 MemoryStore 全量测试**

Run:

```bash
.venv/bin/pytest -q tests/test_memory_store.py
```

Expected: PASS；现有 add、replace、remove、metadata 和关系测试均不回归。

- [ ] **Step 6: 暂存并提交事务支持**

```bash
git add evolvmem/memory_store.py tests/test_memory_store.py
git commit -m "feat: support atomic memory store batches"
```

---

## Task 5: 将脱敏、摘要合约和候选排序接入 SessionEnd

**Files:**

- Modify: `evolvmem/kimi_hooks.py`
- Modify: `tests/test_kimi_hooks.py`

- [ ] **Step 1: 写发送给模型的是脱敏副本且原输入不变的失败测试**

按 `tests/test_kimi_hooks.py` 现有 monkeypatch 风格创建临时 wire，拦截 `_extract_candidates`：

```python
def test_session_end_redacts_before_llm_without_mutating_wire(
    self, monkeypatch, tmp_path, test_config,
):
    secret = "Synthetic-Pass-For-Redaction-123!"
    wire = self._wire_session(
        monkeypatch,
        tmp_path,
        test_config,
        text=f"请分析长期规则。password: {secret}。" + "甲" * 250,
    )
    monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
    original = wire.read_text(encoding="utf-8")
    assert secret in original
    seen = {}

    def fake_extract(messages, llm_config):
        seen["messages"] = messages
        assert secret not in repr(messages)
        return [CandidateMemory(
            key="SESSION_SUMMARY",
            value="本次确认了长期架构约束并完成安全检查。",
            tags=["日志", "分类:test"],
        )]

    monkeypatch.setattr(hooks, "_extract_candidates", fake_extract)
    result = hooks.session_end({"session_id": "synthetic"})

    assert result.status == "completed"
    assert wire.read_text(encoding="utf-8") == original
    assert "[已脱敏:password]" in repr(seen["messages"])
```

fixture 中凭据必须使用 `password: Synthetic-Pass-For-Redaction-123!` 这种合成文本。

- [ ] **Step 2: 写无效摘要整批 retry 且不写库的失败测试**

```python
@pytest.mark.parametrize("summary_value", [
    "English-only session summary",
    "password: Synthetic-Pass-Only-123!",
])
def test_session_end_retries_without_writes_when_summary_is_invalid(
    self, monkeypatch, tmp_path, test_config, summary_value,
):
    self._wire_session(monkeypatch, tmp_path, test_config)
    monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
    monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
        CandidateMemory(key="SESSION_SUMMARY", value=summary_value),
        CandidateMemory(
            key="project:x:constraint:safe",
            value="这是应当保留的长期安全约束。",
            attribute="constraint",
        ),
    ])

    result = hooks.session_end({"session_id": "synthetic"})

    assert result.status == "retry"
    with MemoryStore(test_config) as store:
        assert store.count_active() == 0
```

第二个样例在脱敏后只剩标记，必须由低信息摘要规则拒绝。

- [ ] **Step 3: 写过滤原因、pinned 排序和摘要不占配额测试**

构造 10 条普通候选，并把 pinned 中文偏好放到模型结果最后；混入敏感、纯测试和纯英文候选。断言：

```python
assert result.status == "completed"
assert result.persisted == 9  # 1 条摘要 + 8 条原子记忆
with MemoryStore(test_config) as store:
    records = store.get_active()
assert len(records) == 9
summaries = [record for record in records if ":progress:log:" in record["key"]]
atomics = [record for record in records if ":progress:log:" not in record["key"]]
assert len(summaries) == 1
assert len(atomics) == 8
assert any(
    record["key"] == "user:preference:communication:language"
    for record in atomics
)
assert all("Synthetic-Pass" not in record["value"] for record in records)
```

拦截 `_log` 并断言只出现统计字段：

```python
stats_line = next(line for line in logs if "rejected_sensitive=" in line)
assert "provider=deepseek" in stats_line
assert "redacted=" in stats_line
assert "accepted=8" in stats_line
assert "rejected_ephemeral=1" in stats_line
assert "rejected_language=1" in stats_line
assert "Synthetic-Pass" not in "\n".join(logs)
```

- [ ] **Step 4: 运行新增测试并确认当前实现会泄露输入或按模型顺序截断**

Run:

```bash
.venv/bin/pytest -q tests/test_kimi_hooks.py -k "redacts_before_llm or summary_is_invalid or pinned_sorting"
```

Expected: FAIL，且失败来自尚未调用策略，而不是配置读取或 wire 定位。

- [ ] **Step 5: 在 LLM 调用前制作脱敏副本**

在 `session_end()` 读取并做长度判断后、调用 `_extract_candidates()` 前：

```python
from collections import Counter

from evolvmem.extraction_policy import (
    evaluate_candidate,
    rank_candidates,
    redact_messages,
    sanitize_summary,
)

try:
    model_messages, redacted_count = redact_messages(messages)
except Exception as error:
    _log(f"extraction deferred: redaction failed: {type(error).__name__}")
    return ExtractionResult("retry", reason="redaction failed")

candidates = _extract_candidates(model_messages, llm_config)
```

错误日志只能记录异常类型，不能拼接异常消息，因为异常可能意外包含原文。

- [ ] **Step 6: 摘要先清洗并执行中文合约**

拆出 summary 后立即执行：

```python
summary_value, summary_redactions = sanitize_summary(summary.value)
redacted_count += summary_redactions
if summary_value is None:
    _log("extraction deferred: unsafe or non-Chinese SESSION_SUMMARY")
    return ExtractionResult("retry", reason="invalid SESSION_SUMMARY")
summary.value = summary_value
```

只有摘要通过后才能加载 embedding 或打开 MemoryStore。

- [ ] **Step 7: 门控普通候选、记录原因并在限制前排序**

```python
rejections: Counter[str] = Counter()
accepted = []
for candidate in candidates:
    decision = evaluate_candidate(candidate)
    if decision.accepted:
        accepted.append(candidate)
    else:
        rejections[decision.reason] += 1
selected = rank_candidates(accepted, limit=_MAX_MEMORIES_PER_SESSION)
```

把 `selected` 而不是原始 `candidates` 传给持久化。删除 `_persist_candidates()` 内部基于 `len(added_ids)` 的 8 条截断；摘要仍单独调用，不经过 `rank_candidates()`。

- [ ] **Step 8: 输出无正文单行统计并保持 ExtractionResult 兼容**

持久化完成后输出：

```python
_log(
    f"provider={llm_config.provider} redacted={redacted_count} "
    f"accepted={len(selected)} "
    f"rejected_sensitive={rejections['sensitive']} "
    f"rejected_ephemeral={rejections['ephemeral']} "
    f"rejected_language={rejections['language']} persisted={n}"
)
```

不要给 `ExtractionResult` 增加必填字段；stale-session worker 继续只依赖 `status`、`persisted`、`reason` 和 `rate_limited`。

- [ ] **Step 9: 运行 hook 相关回归**

Run:

```bash
.venv/bin/pytest -q tests/test_kimi_hooks.py tests/test_extract_stale_sessions.py
```

Expected: PASS；完整会话优先、400/422 上下文超限分块、408/429/5xx、网络超时、240 秒预算以及 terminal 状态推进规则均不回归。

- [ ] **Step 10: 暂存并提交编排策略**

```bash
git add evolvmem/kimi_hooks.py tests/test_kimi_hooks.py
git commit -m "feat: apply extraction policy before persistence"
```

---

## Task 6: 原子持久化摘要和原子候选，并在提交后同步向量

**Files:**

- Modify: `evolvmem/kimi_hooks.py`
- Modify: `tests/test_kimi_hooks.py`
- Modify: `tests/test_integration.py`

- [ ] **Step 1: 写第三次写入失败时整批回滚测试**

让模型返回 1 条摘要和至少 3 条合法中文候选，包装 `MemoryStore.add` 在第三次调用前抛出合成异常：

```python
def test_session_end_rolls_back_summary_and_atomics_on_third_write_failure(
    self, monkeypatch, tmp_path, test_config,
):
    self._wire_session(monkeypatch, tmp_path, test_config)
    monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
    monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
        CandidateMemory(
            key="SESSION_SUMMARY",
            value="本次确认了三项长期架构规则。",
            tags=["日志"],
        ),
        CandidateMemory(key="project:x:decision:first", value="采用第一项长期架构决定。"),
        CandidateMemory(key="project:x:decision:second", value="采用第二项长期架构决定。"),
        CandidateMemory(key="project:x:constraint:third", value="必须遵守第三项长期安全约束。"),
    ])
    real_add = MemoryStore.add
    calls = 0

    def fail_on_third(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError("synthetic third write failure")
        return real_add(self, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "add", fail_on_third)
    result = hooks.session_end({"session_id": "synthetic"})

    assert result.status == "retry"
    with MemoryStore(test_config) as store:
        assert store.count_active() == 0
```

在测试文件顶部增加 `import sqlite3`。

- [ ] **Step 2: 写数据库提交后向量失败不回滚测试**

将向量同步抽成独立函数后用 fake engine/index：

```python
class FailingVectorIndex:
    def add(self, memory_id, embedding):
        raise RuntimeError("synthetic vector failure")

    def save(self):
        raise AssertionError("save must not run after add failure")


class LoadedFakeEngine:
    is_loaded = True

    def encode_document(self, value):
        return [0.0, 1.0]


def test_vector_failure_after_commit_keeps_sqlite_records(
    monkeypatch, test_config,
):
    logs = []
    monkeypatch.setattr(hooks, "_log", logs.append)
    with MemoryStore(test_config) as store:
        memory_id = store.add(
            "project:x:decision:api",
            "采用统一接口，因为它减少重复实现。",
        )
        hooks._sync_candidate_vectors(
            store, FailingVectorIndex(), LoadedFakeEngine(), [memory_id]
        )
        assert store.get_by_id(memory_id)["status"] == "active"
    assert logs == ["vector sync skipped: RuntimeError"]
    assert "统一接口" not in "\n".join(logs)
```

同时拦截日志并断言没有记录正文。

- [ ] **Step 3: 运行新增测试并确认当前逐条提交行为导致回滚断言失败**

Run:

```bash
.venv/bin/pytest -q tests/test_kimi_hooks.py -k "third_write_failure or vector_failure_after_commit"
```

Expected: FAIL；回滚测试可观察到已有部分记录，向量辅助函数尚不存在。

- [ ] **Step 4: 将 `_persist_candidates` 收窄为纯 SQLite 写入**

签名改为：

```python
def _persist_candidates(
    config,
    store,
    vidx,
    engine,
    candidates,
    session_id: str,
) -> list[int]:
```

函数继续完成 `should_persist`、长度、低信息、同 key conflict 和跨 key semantic merge，但：

- 返回新增或替换后的 ID 列表，而不是数量；
- 不在内部截断候选；
- 不在内部调用 `vidx.add()` 或 `vidx.save()`；
- 所有 `store.replace()` 路径都传 `source_session=session_id`，保留来源信息；
- semantic merge 仍可在事务内只读向量索引来决定替换目标。

- [ ] **Step 5: 增加提交后向量同步辅助函数**

```python
def _sync_candidate_vectors(store, vidx, engine, memory_ids: list[int]) -> None:
    if not memory_ids or vidx is None or engine is None:
        return
    if not getattr(engine, "is_loaded", False):
        return
    try:
        import numpy as np
        for memory_id in memory_ids:
            record = store.get_by_id(memory_id)
            if record:
                embedding = engine.encode_document(record["value"])
                vidx.add(memory_id, np.array(embedding, dtype=np.float32))
        vidx.save()
    except Exception as error:
        _log(f"vector sync skipped: {type(error).__name__}")
```

异常日志只写类型；SQLite 已经提交，不能在这里改变 `ExtractionResult.status` 或删除记录。

- [ ] **Step 6: 用单个事务包住摘要和原子候选**

`session_end()` 的持久化块改为：

```python
with MemoryStore(config) as store:
    with store.transaction():
        memory_ids = _persist_candidates(
            config, store, vidx, engine, [summary], session_id
        )
        memory_ids.extend(_persist_candidates(
            config, store, vidx, engine, selected, session_id
        ))
    _sync_candidate_vectors(store, vidx, engine, memory_ids)
    n = len(memory_ids)
```

SQLite 异常仍由现有 `except` 转为 `ExtractionResult("retry")`。stale-session worker 因 retry 不推进 mtime；completed/skipped 行为不变。

- [ ] **Step 7: 增加一条状态回归，证明 retry 不推进、completed 才推进**

扩展 `tests/test_extract_stale_sessions.py::test_batch_advances_only_completed_or_skipped_sessions`，给 retry 会话放入旧 mtime，证明失败后保持旧进度，而 completed 覆盖为本轮 mtime：

```python
state = {
    "session_retry": {
        "mtime": 5.0,
        "via": "offline-fallback",
        "status": "completed",
    },
}
stale.process_batch(
    [(30.0, "session_done"), (10.0, "session_retry")],
    state,
    hooks,
)
assert state["session_retry"]["mtime"] == 5.0
assert state["session_done"]["mtime"] == 30.0
```

复用当前 fake hook/status fixture，不新建第二套 worker 实现。

- [ ] **Step 8: 运行事务、hook、worker 和集成回归**

Run:

```bash
.venv/bin/pytest -q tests/test_memory_store.py tests/test_kimi_hooks.py tests/test_extract_stale_sessions.py tests/test_integration.py
```

Expected: PASS。

- [ ] **Step 9: 暂存并提交原子批次实现**

```bash
git add evolvmem/kimi_hooks.py tests/test_kimi_hooks.py tests/test_integration.py tests/test_extract_stale_sessions.py
git commit -m "feat: persist extraction batches atomically"
```

---

## Task 7: 文档、真实 Flash 只读验收和最终验证

**Files:**

- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-03-extraction-quality-hardening-design.md` only if implementation diverged for an approved reason
- Create: `/home/jiangli/fix-records/records/2026-08-04-evolvmem-extraction-quality-hardening.md`

- [ ] **Step 1: 更新 README 的提炼行为说明**

在现有 provider/自动提炼章节记录：

- DeepSeek V4 Flash 非思考模式仍为默认，正常路径为一次调用；
- 外发消息先在内存副本中脱敏，不改 wire；
- 摘要和原子候选正文要求中文；
- 先过滤、同 key 去重和排序，再截取最多 8 条原子记忆，摘要不占配额；
- SQLite 批次原子提交，向量索引在提交后同步；
- retry 不推进 stale-session mtime；
- 日志只输出计数，不输出候选正文或敏感片段。

不要把 Kimi 写成自动 fallback，也不要声称存在 Flash/Pro 自动路由。

- [ ] **Step 2: 运行不落库的真实 Flash active-only 验收**

验收代码必须：

1. 用 SQLite URI `mode=ro` 打开本机 Claude-mem 数据库；
2. SQL 明确包含 `WHERE status = 'active'`，不读取 archived/superseded；
3. 提炼数据只经过 `redact_messages()`、`_extract_candidates()`、`sanitize_summary()`、`evaluate_candidate()` 和 `rank_candidates()`；允许使用只读的配置加载和摘要拆分辅助函数；
4. 不构造 `MemoryStore`，不调用 `_persist_candidates()` 或 `session_end()`；
5. 只打印数量、原因码计数和接纳 key，不打印输入、summary value 或候选 value；
6. 断言脱敏后的外发文本不含测试注入的合成凭据，摘要和所有接纳 value 均含中文，接纳候选不含敏感内容；
7. 断言最多 8 条且结果覆盖至少一个 pinned 偏好或用户画像、一个 decision、一个 constraint；
8. 人工核对接纳 key 中没有 archived onboarding、临时密码、测试数量、部署完成或纯提交记录。

Run the following local read-only acceptance command using network approval for the configured provider endpoint:

```bash
.venv/bin/python - <<'PY'
from collections import Counter
from pathlib import Path
import re
import sqlite3

from evolvmem.extraction_policy import (
    contains_cjk,
    contains_sensitive_text,
    evaluate_candidate,
    rank_candidates,
    redact_messages,
    sanitize_summary,
)
from evolvmem.kimi_hooks import (
    _extract_candidates,
    _load_llm_config,
    _split_summary_candidate,
)

database = Path.home() / ".claude" / "evolvmem" / "memory.db"
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
records = connection.execute(
    "SELECT key, value, attribute, tier, importance FROM memories "
    "WHERE status = 'active' AND source_session = ? ORDER BY id",
    ("claude-mem-migration",),
).fetchall()
connection.close()
assert records, "no active Claude-mem migration records"

messages = [
    {"role": "user", "content": f"[{record['key']}] {record['value']}"}
    for record in records
]
synthetic_secret = "Synthetic-Acceptance-Pass-123!"
messages.append({
    "role": "user",
    "content": f"安全验收注入 password: {synthetic_secret}",
})
model_messages, redacted = redact_messages(messages)
assert synthetic_secret not in repr(model_messages)

config = _load_llm_config()
assert config is not None, "extraction provider unavailable"
raw_candidates = _extract_candidates(model_messages, config)
summary, atomics = _split_summary_candidate(raw_candidates)
assert summary is not None
safe_summary, summary_redactions = sanitize_summary(summary.value)
assert safe_summary is not None and contains_cjk(safe_summary)

rejections = Counter()
accepted = []
for candidate in atomics:
    decision = evaluate_candidate(candidate)
    if decision.accepted:
        accepted.append(candidate)
    else:
        rejections[decision.reason] += 1
selected = rank_candidates(accepted, limit=8)

assert len(selected) <= 8
assert all(contains_cjk(candidate.value) for candidate in selected)
assert all(not contains_sensitive_text(candidate.value) for candidate in selected)
assert any(
    candidate.tier == "pinned"
    and candidate.attribute in {"preference", "user_profile"}
    for candidate in selected
)
assert any(candidate.attribute == "decision" for candidate in selected)
assert any(candidate.attribute == "constraint" for candidate in selected)
noise = re.compile(
    r"临时密码|\d+\s*个测试|测试.{0,10}(?:通过|成功)|"
    r"部署.{0,10}(?:完成|成功)|(?:commit|提交)\s*[0-9a-f]{7,40}",
    re.IGNORECASE,
)
assert all(not noise.search(candidate.value) for candidate in selected)
assert all("onboarding" not in candidate.key.casefold() for candidate in selected)

print(
    f"provider={config.provider} active_inputs={len(records)} "
    f"redacted={redacted + summary_redactions} "
    f"rejected_sensitive={rejections['sensitive']} "
    f"rejected_ephemeral={rejections['ephemeral']} "
    f"rejected_language={rejections['language']} selected={len(selected)}"
)
for candidate in selected:
    print(candidate.key)
PY
```

Expected: 进程退出码 0；输出仅包含 provider、active 输入条数、redacted 数、各拒绝原因计数和最多 8 个 key。

如果真实 active 数据不能产出 preference/decision/constraint 三类覆盖，真实验收即未通过：如实记录样本与输出并继续修正提示词或策略，不得降低成功标准、读取 archived 数据或写生产数据库。

- [ ] **Step 3: 运行完整自动化验证**

Run:

```bash
.venv/bin/pytest -q
python -m compileall -q evolvmem scripts
git diff --check
```

Expected: 所有测试通过、compileall 退出码 0、`git diff --check` 无输出。

- [ ] **Step 4: 检查范围和安全不变量**

Run:

```bash
rg -n "auto.*route|second.*review|benchmark.*platform" evolvmem tests README.md
rg -n "_log\(.*value|print\(.*value|logger\..*value" evolvmem
rg -n "Synthetic-Pass|synthetic-token|SYNTHETICKEYDATA" evolvmem
```

Expected:

- 第一条没有新增自动路由、二次审核或评测平台实现；
- 第二条没有新增候选正文日志；
- 第三条在生产代码中无测试凭据，合成值只存在测试文件。

- [ ] **Step 5: 按本机规范写修复记录**

创建 `/home/jiangli/fix-records/records/2026-08-04-evolvmem-extraction-quality-hardening.md`，必须包含：

```markdown
# EvolvMem 提炼质量与安全强化

## 症状

真实历史提炼会保留临时状态、测试数量和部署结果，英文输出占主导；模型曾复述历史临时测试密码，后排 pinned 偏好还可能被前 8 条截断，逐条提交存在部分落库风险。

## 排查过程

记录 Flash/Pro 真实 A/B 证据、当前提示词语言、候选截断位置、MemoryStore 逐条 commit，以及被排除的“仅换强模型即可解决安全问题”假设。

## 根因

提炼管线缺少发送前确定性脱敏、返回后质量与语言门控、限制前排序，以及覆盖摘要和原子候选的外层事务。

## 修复内容

记录中文协议、extraction_policy、hook 编排、MemoryStore 事务和提交后向量同步的实际修改文件与行为。

## 验证

记录本计划 Step 2 和 Step 3 实际执行的命令、退出码和真实测试汇总；只有全部通过才能写“已修复”。

## 遗留事项

记录真实凭据模式仍需随新格式扩展；向量同步失败依赖启动一致性修复；Kimi/Pro 只支持手动配置且未增加自动路由。
```

- [ ] **Step 6: 进行完成前审查**

使用 `superpowers-requesting-code-review` 审查设计覆盖、敏感信息泄漏、事务边界、测试有效性和非目标越界；修正所有阻断问题后，重新执行 Step 3。

- [ ] **Step 7: 暂存最终文档和修复记录并提交**

```bash
git add README.md docs/superpowers/specs/2026-08-03-extraction-quality-hardening-design.md docs/superpowers/plans/2026-08-03-extraction-quality-hardening.md
git commit -m "docs: document extraction hardening verification"
```

`/home/jiangli/fix-records` 已确认不是 Git 仓库，修复记录只创建和核对，不对它执行 `git add`。不得把仓库外的绝对路径强行暂存到 `hermes-memory-plugin`。

---

## Final Acceptance Checklist

- [ ] 正常路径仍为单次 DeepSeek V4 Flash 非思考请求。
- [ ] 发送给模型的消息是脱敏副本，原 wire 和原消息对象未修改。
- [ ] 合成凭据不会进入模型 prompt、SQLite 或日志。
- [ ] SESSION_SUMMARY 安全且含中文；无效摘要导致 retry 和零写入。
- [ ] 普通候选逐条门控，单个拒绝不影响合法候选。
- [ ] 过滤后同 key 去重，pinned/importance/confidence/原顺序排序，再截取 8 条。
- [ ] SESSION_SUMMARY 不占 8 条原子记忆配额。
- [ ] 摘要和原子候选在单个 SQLite 事务中提交，中途失败全部回滚。
- [ ] 向量同步只发生在 SQLite 提交后，失败不回滚数据库。
- [ ] retry 不推进 stale-session mtime，completed/skipped 继续推进。
- [ ] 400/422 上下文降级、429/超时重试和 240 秒预算回归通过。
- [ ] 真实 Flash active-only 验收只读且不落库。
- [ ] 完整 pytest、compileall 和 `git diff --check` 通过。
- [ ] 本机修复记录已按规范如实创建。
