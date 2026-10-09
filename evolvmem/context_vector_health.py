"""Shared, read-only checks for Context retrieval and cache recovery."""
from dataclasses import dataclass

from evolvmem import vector_provenance


@dataclass(frozen=True)
class ContextVectorState:
    dirty: bool = False
    count: int = 0
    reason: str = ''

    def available(self, engine):
        return (not self.reason and self.count > 0 and engine is not None
                and bool(getattr(engine, 'is_loaded', False)))


def inspect_context_vector(config, store, index, documents=None):
    """Compare cache identity and known L0 proofs, without encoding or writing.

    Count equality alone cannot detect a missing ID replaced by an obsolete
    one. Existing provenance also detects content changes with unchanged IDs.
    Absent provenance remains compatible with older caches; recovery only
    reuses vectors that have full text/vector proofs.
    """
    dirty = False
    count = 0
    try:
        if index.path != config.context_vector_path.resolve():
            return ContextVectorState(reason='context_vector_path_mismatch')
        dirty = bool(index.is_dirty())
        count = index.count()
        if dirty:
            return ContextVectorState(True, count, 'context_vector_dirty')
        documents = store.list_vector_documents() if documents is None else documents
        if count != len(documents):
            return ContextVectorState(False, count, 'context_vector_count_mismatch')
        if set(index.ids()) != {d.item_id for d in documents}:
            return ContextVectorState(False, count, 'context_vector_ids_mismatch')
        proofs = vector_provenance.load(config)
        for document in documents:
            proof = proofs.get(str(document.item_id))
            if isinstance(proof, dict) and proof.get('text') != vector_provenance.text_digest(document.l0):
                return ContextVectorState(False, count, 'context_vector_content_mismatch')
        return ContextVectorState(False, count)
    except Exception:
        return ContextVectorState(dirty, count, 'context_vector_unavailable')
