"""访问衰减遗忘引擎——长期未访问的低活跃记忆自动归档。"""

from hermes_memory.config import Config
from hermes_memory.memory_store import MemoryStore


class ForgettingEngine:
    """基于访问频率的遗忘引擎。

    规则:
    - last_accessed 距今超过 forget_days_threshold 天
    - access_count ≤ forget_access_count_threshold
    - 同时满足 → 降级为 archived
    - 同一记忆 forget_rate_limit_days 天内最多降级一次
    """

    def __init__(self, config: Config, memory_store: MemoryStore):
        self.config = config
        self.store = memory_store

    def find_candidates(self) -> list[dict]:
        """查找可降级的候选记忆。"""
        return self.store.get_forgetting_candidates(
            days_threshold=self.config.forget_days_threshold,
            access_threshold=self.config.forget_access_count_threshold,
            rate_limit_days=self.config.forget_rate_limit_days,
        )

    def archive(self, mem_id: int) -> None:
        """将指定记忆降级为 archived。"""
        self.store.archive(mem_id)

    def run(self) -> int:
        """执行一次遗忘检查，返回被降级的记忆数量。"""
        candidates = self.find_candidates()
        for c in candidates:
            self.archive(c["id"])
        return len(candidates)
