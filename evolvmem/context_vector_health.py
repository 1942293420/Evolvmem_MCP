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
    document_count: int | None = None
    obsolete_count: int | None = None

    def available(self, engine):
        return ((not self.reason or bool(self.verified_ids)) and self.count > 0 and engine is not None
                and bool(getattr(engine, 'is_loaded', False)))

    def public_snapshot(self, config, engine):
        import json
        import math
        import re
        indexed = self.count if not self.reason else len(self.verified_ids or ())
        loaded = engine is not None and bool(getattr(engine, 'is_loaded', False))
        state = ('unavailable' if not loaded or self.document_count is None
                 else 'pending' if self.reason else 'empty' if not self.document_count else 'ready')
        result = dict(state=state, document_count=self.document_count,
                      indexed_count=indexed,
                      searchable_count=indexed if loaded else 0,
                      pending_count=None if self.document_count is None else max(0, self.document_count-indexed),
                      obsolete_count=self.obsolete_count, last_recovery=None)
        try:
            raw = json.loads(config.context_vector_path.with_suffix('.recovery.json').read_text())
            safe = {}
            if raw.get('status') in ('staged', 'failed', 'fts_only'):
                safe['status'] = raw['status']
            stamp = raw.get('occurred_at')
            if isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', stamp):
                safe['occurred_at'] = stamp
            for key in ('document_count', 'encoded_operations', 'duration_ms', 'pending_ms'):
                value = raw.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    safe[key] = value
            result['last_recovery'] = safe or None
        except (OSError, ValueError, AttributeError):
            pass
        return result


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
        coverage = dict(document_count=len(documents), obsolete_count=len(ids-{d.item_id for d in documents}))
        proofs, compatible = vector_provenance.load_with_contract(config)
        if compatible is False:
            return ContextVectorState(dirty, count, 'context_vector_contract_mismatch', **coverage)
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
            return ContextVectorState(False, count, **coverage)
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
        return ContextVectorState(dirty, count, reason, frozenset(verified), **coverage)
    except Exception:
        return ContextVectorState(dirty, count, 'context_vector_unavailable')
