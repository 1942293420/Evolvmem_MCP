"""自动记忆提取——分析对话，提取值得持久化的候选记忆。"""

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidateMemory:
    """候选记忆——从对话中提取的待持久化信息。"""
    key: str
    value: str
    category: str = "fact"
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5


class AutoExtractor:
    """自动记忆提取器。

    编排 Claude 通过 prompt 审视对话并产出候选记忆。
    实际推理由 Claude Code 的 Stop Hook 中的 Claude 完成；
    本模块负责构建 prompt 和解析响应。
    """

    EXTRACTION_PROMPT = """你是一个记忆管理助手。请审视以下对话，提取需要持久化的信息。

## 保留规则
以下内容应该提取为记忆：
- 用户明确表达的偏好、决策、约束
- 项目架构选型、技术决策及理由
- 被推翻的旧方案（保留历史，标记 superseded）
- 业务逻辑规则和例外
- 重要的"为什么"——决策背后的业务理由

## 丢弃规则
以下内容不应提取：
- 临时任务、一次性路径、已完成的 todo
- 普通闲聊和问候
- 可以从代码/git 直接获取的事实
- 纯技术实现细节

## 稳定 key 格式
key 使用格式: `{{项目}}:{{领域}}:{{类型}}:{{主题}}`
例如:
- `project:shop:decision:after_sales` — 售后决策
- `user:preference:communication:language` — 语言偏好
- `project:hermes:arch:embedding_model` — 架构选型

## 输出格式
返回 JSON 数组，每条包含:
- key: 稳定标识符
- value: 记忆内容（一句话描述清楚）
- category: decision | preference | fact | constraint | user_profile
- tags: 相关标签列表
- confidence: 0.0-1.0 的置信度

如果没有值得持久化的信息，返回空数组 `[]`。

## 对话
{conversation}

## 输出
只返回 JSON 数组，不要有其他内容："""

    def build_extraction_prompt(self,
                                messages: list[dict[str, str]]) -> str:
        """构建提取 prompt。"""
        conversation = "\n".join(
            f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
            for m in messages
        )
        return self.EXTRACTION_PROMPT.format(conversation=conversation)

    def parse_response(self, response_text: str) -> list[CandidateMemory]:
        """解析 Claude 返回的 JSON，提取候选记忆列表。"""
        # 提取 JSON 块
        json_match = re.search(
            r'```(?:json)?\s*(\[.*?\])\s*```',
            response_text, re.DOTALL,
        )
        if json_match:
            json_str = json_match.group(1)
        else:
            # 尝试直接解析整个文本
            json_str = response_text.strip()

        try:
            items = json.loads(json_str)
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
            if not key or not value:
                continue
            candidates.append(CandidateMemory(
                key=key,
                value=value,
                category=item.get("category", "fact"),
                tags=item.get("tags", []),
                confidence=float(item.get("confidence", 0.5)),
            ))
        return candidates

    def should_persist(self, candidate: CandidateMemory) -> bool:
        """判断候选记忆是否值得持久化。"""
        # 置信度过低 → 不保留
        if candidate.confidence < 0.3:
            return False
        # 闲聊类 → 不保留
        if candidate.category in ("chat", "greeting", "small_talk"):
            return False
        # key 或 value 太短 → 不保留
        if len(candidate.key) < 5 or len(candidate.value) < 5:
            return False
        return True

    def build_key(self, project: str, domain: str, category: str,
                  topic: str) -> str:
        """构建符合规范的稳定 key。"""
        parts = [project, domain, category, topic]
        # 转小写，空格替换为下划线，只保留字母数字和下划线
        sanitized = []
        for p in parts:
            p = p.lower().strip()
            p = re.sub(r'[^\w一-鿿-]', '_', p)
            p = re.sub(r'_+', '_', p)
            sanitized.append(p.strip('_'))
        return ":".join(sanitized)
