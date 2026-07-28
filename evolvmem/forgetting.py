"""Access-decay forgetting engine — auto-archives long-unaccessed low-activity memories."""

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore, _now_iso


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
        """Run one forgetting check, return number of archived memories.

        Expired memories (expires_at <= now) are archived first, then the
        regular access-decay rules run on the rest.
        """
        expired = self.store._execute(
            "SELECT id FROM memories WHERE status='active' "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (_now_iso(),),
        )
        for row in expired:
            self.store.archive(row["id"])
        candidates = self.find_candidates()
        for c in candidates:
            self.archive(c["id"])
        return len(expired) + len(candidates)
