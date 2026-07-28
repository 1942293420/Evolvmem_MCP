"""Memory consolidation — near-duplicate detection and merge.

Uses the existing HNSW vector index: for each active memory, find its
nearest neighbor; pairs above the similarity threshold are merge candidates.
The higher-scored memory (compute_score) is kept, the other is archived.
"""

import numpy as np

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.scoring import compute_score


class Consolidator:
    """Detects and merges near-duplicate active memories."""

    def __init__(self, config: Config, store: MemoryStore,
                 vidx: VectorIndex, engine) -> None:
        self.config = config
        self.store = store
        self.vidx = vidx
        self.engine = engine

    def find_candidates(self, threshold: float | None = None) -> list[dict]:
        """Return merge candidate pairs: [{keep, drop, similarity}]."""
        if threshold is None:
            threshold = self.config.consolidate_similarity_threshold
        if not getattr(self.engine, "is_loaded", False):
            return []

        actives = self.store.get_active()
        seen: set[frozenset] = set()
        pairs: list[dict] = []
        for m in actives:
            try:
                vec = np.array(self.engine.encode_document(m["value"]),
                               dtype=np.float32)
                hits = self.vidx.search(vec, 4)
            except Exception:
                continue
            for h in hits:
                other_id = h["id"]
                if other_id == m["id"]:
                    continue
                similarity = max(0.0, 1.0 - h["distance"] / 2.0)
                if similarity < threshold:
                    continue
                pair_key = frozenset({m["id"], other_id})
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                other = self.store.get_by_id(other_id)
                if not other or other["status"] != "active":
                    continue
                keep, drop = self._order(m, other)
                pairs.append({"keep": keep, "drop": drop,
                              "similarity": round(similarity, 4)})
        return pairs

    def consolidate(self, dry_run: bool = True,
                    threshold: float | None = None) -> dict:
        """Merge near-duplicates. dry_run=True only reports."""
        pairs = self.find_candidates(threshold)
        if dry_run:
            return {"dry_run": True, "pairs": pairs, "merged": 0}
        merged = 0
        for p in pairs:
            drop = p["drop"]
            current = self.store.get_by_id(drop["id"])
            if not current or current["status"] != "active":
                continue  # already archived by an earlier pair in this run
            self.store.update_access(p["keep"]["id"])
            self.store.archive(drop["id"])
            merged += 1
        return {"dry_run": False, "pairs": pairs, "merged": merged}

    def _order(self, a: dict, b: dict) -> tuple[dict, dict]:
        """Higher compute_score wins as keep."""
        sa = compute_score(a, self.config)
        sb = compute_score(b, self.config)
        return (a, b) if sa >= sb else (b, a)
