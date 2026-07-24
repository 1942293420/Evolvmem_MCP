"""Access-decay forgetting engine — auto-archives long-unaccessed low-activity memories."""

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore


class ForgettingEngine:
    """Access-frequency-based forgetting engine.

    Rules:
    - last_accessed older than forget_days_threshold days
    - access_count <= forget_access_count_threshold
    - both conditions met → downgrade to archived
    - same memory downgraded at most once per forget_rate_limit_days
    """

    def __init__(self, config: Config, memory_store: MemoryStore):
        self.config = config
        self.store = memory_store

    def find_candidates(self) -> list[dict]:
        """Find candidate memories eligible for downgrade."""
        return self.store.get_forgetting_candidates(
            days_threshold=self.config.forget_days_threshold,
            access_threshold=self.config.forget_access_count_threshold,
            rate_limit_days=self.config.forget_rate_limit_days,
        )

    def archive(self, mem_id: int) -> None:
        """Downgrade the specified memory to archived."""
        self.store.archive(mem_id)

    def run(self) -> int:
        """Run one forgetting check, return number of archived memories."""
        candidates = self.find_candidates()
        for c in candidates:
            self.archive(c["id"])
        return len(candidates)
