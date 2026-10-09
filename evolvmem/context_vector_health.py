"""Shared, read-only checks for Context retrieval and cache recovery."""
from dataclasses import dataclass

from evolvmem import vector_provenance


@dataclass(frozen=True)
class ContextVectorState:
    dirty: bool = False
    count: int = 0
    reason: str = ''
    # None means the cache passed its normal checks. A set restricts a cache
    # awaiting recovery to individually verified, still-eligible vectors.
    verified_ids: frozenset[int] | None = None

    def available(self, engine):
        return ((not self.reason or bool(self.verified_ids)) and self.count > 0 and engine is not None
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
        documents = store.list_vector_documents() if documents is None else documents
        ids = set(index.ids())
        proofs, compatible = vector_provenance.load_with_contract(config)
        if compatible is False:
            return ContextVectorState(dirty, count, 'context_vector_contract_mismatch')
        reason = ''
        if dirty:
            reason = 'context_vector_dirty'
        elif count != len(documents):
            reason = 'context_vector_count_mismatch'
        elif ids != {d.item_id for d in documents}:
            reason = 'context_vector_ids_mismatch'
        elif any(isinstance(proofs.get(str(d.item_id)), dict) and
                 proofs[str(d.item_id)].get('text') != vector_provenance.text_digest(d.l0)
                 for d in documents):
            reason = 'context_vector_content_mismatch'
        if not reason:
            return ContextVectorState(False, count)
        verified = set()
        if compatible is True:
            for document in documents:
                proof = proofs.get(str(document.item_id))
                if (document.item_id not in ids or not isinstance(proof, dict) or
                        proof.get('text') != vector_provenance.text_digest(document.l0)):
                    continue
                vector = index.vector_copy(document.item_id)
                if vector is not None and proof.get('vector') == vector_provenance.vector_digest(vector):
                    verified.add(document.item_id)
        return ContextVectorState(dirty, count, reason, frozenset(verified))
    except Exception:
        return ContextVectorState(dirty, count, 'context_vector_unavailable')
