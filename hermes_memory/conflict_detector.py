"""冲突检测——新记忆写入前与已有 active 记忆比对。"""

import re
from dataclasses import dataclass
from hermes_memory.memory_store import MemoryStore


@dataclass
class ConflictDecision:
    """冲突检测结果。"""
    action: str   # "add" | "skip" | "replace" | "conflict"
    reason: str
    existing_id: int | None = None


class ConflictDetector:
    """候选记忆写入前的冲突检测器。

    决策树:
    1. key 不存在 → add
    2. value 相同 → skip（重复）
    3. 用户明确说放弃旧方案 → replace
    4. 新信息更具体（来自当前 session 且明显更详细）→ replace
    5. 无法判断 → conflict（不写，保留旧值）
    """

    def __init__(self, memory_store: MemoryStore):
        self.store = memory_store

    def check(self, candidate_key: str, candidate_value: str,
              user_override: bool = False) -> ConflictDecision:
        """检测候选记忆与已有记忆的冲突。"""
        existing = self._get_active(candidate_key)

        # 1. key 不存在 → 直接新增
        if existing is None:
            return ConflictDecision(
                action="add",
                reason=f"新 key '{candidate_key}'，直接新增",
            )

        # 2. value 相同 → 跳过
        if existing["value"].strip() == candidate_value.strip():
            return ConflictDecision(
                action="skip",
                reason=f"key '{candidate_key}' 的 value 未变化",
                existing_id=existing["id"],
            )

        # 3. 用户明确覆盖 → 替换
        if user_override:
            return ConflictDecision(
                action="replace",
                reason="用户明确指示放弃旧方案",
                existing_id=existing["id"],
            )

        # 4. 新信息明显更具体 → 替换
        if self._is_significantly_more_specific(
            existing["value"], candidate_value
        ):
            return ConflictDecision(
                action="replace",
                reason="新信息明显更具体详细，替换旧值",
                existing_id=existing["id"],
            )

        # 5. 无法判断 → 冲突
        return ConflictDecision(
            action="conflict",
            reason=f"key '{candidate_key}' 已存在不同值，"
                   f"无法自动判断哪个更可信",
            existing_id=existing["id"],
        )

    def _get_active(self, key: str) -> dict | None:
        results = self.store.get_by_key(key)
        for r in results:
            if r["status"] == "active":
                return r
        return None

    def _is_significantly_more_specific(self, old_value: str,
                                        new_value: str) -> bool:
        """判断新值是否明显比旧值更具体。"""
        if len(new_value) >= len(old_value) * 1.5:
            return True
        # 新值包含具体数据（数字、日期、人名等）
        specificity_markers = [
            r'\d+',             # 数字
            r'\d{4}-\d{2}',     # 日期
            r'http|https|\.com', # 链接
            r'具体|明确|确认',    # 中文确定性标记
        ]
        old_specificity = sum(
            1 for p in specificity_markers
            if re.search(p, old_value)
        )
        new_specificity = sum(
            1 for p in specificity_markers
            if re.search(p, new_value)
        )
        return new_specificity > old_specificity
