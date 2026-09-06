"""Access-decay forgetting engine — auto-archives long-unaccessed low-activity memories."""

from evolvmem.config import Config
from evolvmem.legacy_compat import LegacyCompatibilityFacade
from evolvmem.memory_store import MemoryStore, _now_iso
from evolvmem.summary_retention import is_session_summary_projection


class ForgettingEngine:
    """Access-frequency-based forgetting engine.

    Rules:
    - last_accessed older than forget_days_threshold days
    - access_count <= forget_access_count_threshold
    - both conditions met → downgrade to archived
    - same memory downgraded at most once per forget_rate_limit_days

    The store is the legacy compatibility facade in production (every archive
    routes through ContextService onto both mapped sides); isolated tests may
    still pass a raw MemoryStore. No raw SQL is issued here.
    """

    def __init__(self, config: Config,
                 memory_store: "MemoryStore | LegacyCompatibilityFacade"):
        self.config = config
        self.store = memory_store

    def find_candidates(self) -> list[dict]:
        """Find candidate memories eligible for downgrade."""
        candidates = self.store.get_forgetting_candidates(
            days_threshold=self.config.forget_days_threshold,
            access_threshold=self.config.forget_access_count_threshold,
            rate_limit_days=self.config.forget_rate_limit_days,
        )
        return [
            candidate for candidate in candidates
            if not is_session_summary_projection(candidate)
        ]

    def archive(self, mem_id: int) -> None:
        """Downgrade the specified memory to archived."""
        self.store.archive(mem_id)

    def run(self) -> int:
        """Run one forgetting check, return number of archived memories.

        Expired ordinary memories are archived first, then the regular
        access-decay rules run on the rest. Session summaries are excluded
        from both paths because SummaryRetention owns their coverage gate.
        """
        expired = self._expired_ids()
        for mem_id in expired:
            self.store.archive(mem_id)
        candidates = self.find_candidates()
        for c in candidates:
            self.archive(c["id"])
        return len(expired) + len(candidates)

    def _expired_ids(self) -> list[int]:
        """Active IDs past expires_at, in id order, via narrow facade reads.

        Equivalent to the old private query (status='active' AND expires_at
        IS NOT NULL AND expires_at <= now); the facade exposes no
        get_expired_ids yet, so the predicate is evaluated over its
        all_ids/get_by_ids reads instead of any raw SQL escape hatch.
        """
        now = _now_iso()
        rows = self.store.get_by_ids(self.store.all_ids())
        return sorted(
            row["id"]
            for row in rows
            if row["status"] == "active"
            and row.get("expires_at") is not None
            and row["expires_at"] <= now
            and not is_session_summary_projection(row)
        )
