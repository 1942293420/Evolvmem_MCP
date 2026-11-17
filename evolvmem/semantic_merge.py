"""Write-time semantic merge: find an existing active memory that is
semantically the same fact, so the new value can supersede it instead of
coexisting as a fragmented duplicate."""

import json

import numpy as np

from evolvmem.vector_index import VectorIndex
from evolvmem.memory_store import MemoryStore


def find_semantic_match(store: MemoryStore, vidx: VectorIndex, engine,
                        value: str, threshold: float,
                        exclude_id: int | None = None, *, key: str = "",
                        attribute: str = "fact", tags=()) -> dict | None:
    """Return the most similar active, non-reference memory (with similarity),
    or None. similarity uses the consolidator convention: 1 - distance/2."""
    identity = semantic_identity(key, attribute, tags)
    if identity is None:
        return None
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
        if semantic_identity(rec["key"], rec.get("attribute", "fact"), rec.get("tags", ())) != identity:
            continue
        if rec.get("tier") == "reference":
            continue
        if best is None or similarity > best["similarity"]:
            best = {**rec, "similarity": similarity}
    return best


def semantic_identity(key: str, attribute: str = "fact", tags=()) -> tuple | None:
    """Only explicit, matching project/entity/type identities permit merging.

    Canonical project:p:domain:entity and legacy p:domain:entity are aliases.
    Ambiguous names and conflicting category metadata never authorize a merge.
    """
    parts = key.strip().lower().split(":")
    if parts[0] == "project":
        parts = parts[1:]
    if len(parts) < 3 or any(not part for part in parts):
        return None
    scope = "global" if parts[0] in {"user", "global"} else "project"
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, TypeError):
            tags = tags.split(",")
    projects = {t.split(":", 1)[1].lower() for t in (tags or ())
                if isinstance(t, str) and t.startswith("分类:")}
    if scope == "project" and projects and projects != {parts[0]}:
        return None
    return scope, tuple(parts), attribute or "fact"
