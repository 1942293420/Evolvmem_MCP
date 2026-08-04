"""Auto memory extraction — analyzes conversations, extracts candidate memories worth persisting."""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidateMemory:
    """Candidate memory — information extracted from conversation that may be persisted."""
    key: str
    value: str
    attribute: str = "fact"
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 5.0
    tier: str = "normal"


class AutoExtractor:
    """Automatic memory extractor.

    Prompts the configured provider to review conversations and produce
    candidate memories; this module builds the prompt and parses responses.
    """

    EXTRACTION_PROMPT = """你是 EvolvMem 长期记忆提炼器。请审阅完整会话，只提炼跨会话仍有价值的信息。

## 保留规则
保留：用户长期偏好和画像、硬约束与安全开关、业务规则、架构或技术决策及原因、废弃方案及替代原因、可复用故障根因和防复发规则。

## 丢弃规则
丢弃：临时密码、等待输入或稍后确认、一次性测试、单次测试通过或测试数量、单次部署完成、纯提交号、已完成且没有长期决策或原因的待办、可直接从代码或 git 获得的事实。

所有 value 必须使用中文；说明性 tags 也使用中文。稳定 key、代码标识符、产品名和必要缩写可以保留英文。

## 稳定 key 格式
使用格式：`{{project}}:{{domain}}:{{type}}:{{topic}}`
示例：
- `project:shop:decision:after_sales` — 售后决策
- `user:preference:communication:language` — 语言偏好
- `project:evolvmem:arch:embedding_model` — 架构选择

## 输出格式
只返回一个 JSON 对象，顶层字段必须且只能为 "memories"。
"memories" 的值必须是数组；数组条目包含：
- key：稳定标识符
- value：记忆内容，必须为单句且最多 200 个字符；更长内容必须拆分或压缩。
- attribute：decision | preference | fact | constraint | user_profile
- tags：相关标签列表
- confidence：0.0-1.0 的置信度
- importance：1-10 的整数。9-10 为硬约束或成败关键决策；7-8 为重要架构或业务决策；5-6 为普通偏好和事实；3-4 为边缘参考资料。
- tier：若该记忆必须在每个会话可见（约束、长期用户偏好、用户画像）则为 "pinned"；若为只应在相关时通过 memory_search 获取、绝不注入的长参考资料则为 "reference"；否则为 "normal"。

## 会话摘要条目
必须包含且只包含一个 key 为 SESSION_SUMMARY 的会话摘要；即使没有原子记忆也不能省略。SESSION_SUMMARY 不占 8 条原子记忆配额。
- key：字面量 `SESSION_SUMMARY`（调用方会重写）
- value：最多 200 个字符，叙述会话涉及的项目、完成的事项和当前状态。
- attribute："fact"；importance：5-6；tier："normal"；tags：["日志", "分类:<project>"]

## 会话内容
{conversation}

## 输出
只返回 JSON 对象，不要输出其他内容："""

    def build_extraction_prompt(self,
                                messages: list[dict[str, str]]) -> str:
        """Build the extraction prompt."""
        conversation = "\n".join(
            f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
            for m in messages
        )
        return self.EXTRACTION_PROMPT.format(conversation=conversation)

    def parse_response(self, response_text: str) -> list[CandidateMemory]:
        """Parse provider JSON and extract the candidate memory list."""
        # Extract JSON block
        json_match = re.search(
            r'```(?:json)?\s*([\[{].*[\]}])\s*```',
            response_text, re.DOTALL,
        )
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try parsing the entire text directly
            json_str = response_text.strip()

        try:
            payload = json.loads(json_str)
            if isinstance(payload, dict):
                items = payload.get("memories", [])
            else:
                items = payload
            if not isinstance(items, list):
                return []
        except json.JSONDecodeError:
            return []

        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key", "")
            value = item.get("value", "")
            if (not isinstance(key, str) or not isinstance(value, str)
                    or not key or not value):
                continue
            try:
                importance = float(item.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            if math.isnan(importance):  # min(10.0, nan) 返回 10.0，必须先拦截
                importance = 5.0
            importance = max(1.0, min(10.0, importance))
            tier = item.get("tier", "normal")
            if tier not in ("pinned", "normal", "reference"):
                tier = "normal"
            tags = item.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            else:
                tags = [tag for tag in tags if isinstance(tag, str)]
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                raise ValueError("invalid candidate confidence") from None
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ValueError("invalid candidate confidence")
            candidates.append(CandidateMemory(
                key=key,
                value=value,
                attribute=item.get("attribute", "fact"),
                tags=tags,
                confidence=confidence,
                importance=importance,
                tier=tier,
            ))
        return candidates

    def should_persist(self, candidate: CandidateMemory) -> bool:
        """Check whether a candidate memory is worth persisting."""
        # Confidence too low → skip
        if candidate.confidence < 0.3:
            return False
        # Value too long → skip (extraction prompt requires <= 200 chars; hard cap 500)
        if len(candidate.value) > 500:
            return False
        # Casual chat type → skip
        if candidate.attribute in ("chat", "greeting", "small_talk"):
            return False
        # Key or value too short → skip
        if len(candidate.key) < 5 or len(candidate.value) < 5:
            return False
        return True

    def build_key(self, project: str, domain: str, attribute: str,
                  topic: str) -> str:
        """Build a standards-compliant stable key."""
        parts = [project, domain, attribute, topic]
        # Lowercase, replace spaces with underscores, keep only alphanumeric and underscores
        sanitized = []
        for p in parts:
            p = p.lower().strip()
            p = re.sub(r'[^\w一-鿿-]', '_', p)
            p = re.sub(r'_+', '_', p)
            sanitized.append(p.strip('_'))
        return ":".join(sanitized)
