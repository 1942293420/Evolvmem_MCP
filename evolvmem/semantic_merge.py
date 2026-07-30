"""Write-time semantic merge: find an existing active memory that is
semantically the same fact, so the new value can supersede it instead of
coexisting as a fragmented duplicate."""

import numpy as np

from evolvmem.vector_index import VectorIndex
from evolvmem.memory_store import MemoryStore


def find_semantic_match(store: MemoryStore, vidx: VectorIndex, engine,
                        value: str, threshold: float,
                        exclude_id: int | None = None) -> dict | None:
    """Return the most similar active, non-reference memory (with similarity),
    or None. similarity uses the consolidator convention: 1 - distance/2."""
    if not getattr(engine, "is_loaded", False):
        return None
    try:
        vec = np.array(engine.encode_document(value), dtype=np.float32)
        hits = vidx.search(vec, 5)
    except Exception:
        return None
    best: dict | None = None
    for h in hits:
        if exclude_id is not None and h["id"] == exclude_id:
            continue
        similarity = max(0.0, 1.0 - h["distance"] / 2.0)
        if similarity < threshold:
            continue
        rec = store.get_by_id(h["id"])
        if not rec or rec["status"] != "active":
            continue
        if rec.get("tier") == "reference":
            continue
        if best is None or similarity > best["similarity"]:
            best = {**rec, "similarity": similarity}
    return best
