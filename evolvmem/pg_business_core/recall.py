"""Internal fixed-candidate read slice. No FTS, ANN or HTTP tool registration.

Candidates must be prepared by trusted code for this exact library/query. This
module shares SQLite's ranker but does not claim its candidate-generation or
backfill semantics. One control lease spans two independent content snapshots
and publication; current authorization and raw dependencies are never receipts.
"""
from dataclasses import dataclass
from copy import deepcopy
import math

from evolvmem.context_models import ContextContentType, ContextLayer, ContextSearchHit, ContextSearchRequest
from evolvmem.context_retriever import ContextRetriever
from evolvmem.library_foundation import FoundationError
from .recall_store import PgRecallStore


@dataclass(frozen=True)
class CandidateBatch:
    library_id: str
    request: ContextSearchRequest
    lexical: tuple
    vector: tuple

    def checked(self):
        if (not isinstance(self.request, ContextSearchRequest) or type(self.request.top_k) is not int
                or not 1 <= self.request.top_k <= 50 or not isinstance(self.lexical, tuple)
                or not isinstance(self.vector, tuple) or len(self.lexical) > 2160 or len(self.vector) > 240):
            raise FoundationError('invalid_recall_candidates')
        if any(not isinstance(h, ContextSearchHit) or type(h.item_id) is not int or h.item_id < 1
                or not math.isfinite(h.score) or not h.match_layers
                or any(layer not in (ContextLayer.L0, ContextLayer.L1) for layer in h.match_layers)
                for h in self.lexical):
            raise FoundationError('invalid_recall_candidates')
        vectors = []
        for h in self.vector:
            if (not isinstance(h, dict) or set(h) != {'id', 'distance'} or type(h['id']) is not int
                    or h['id'] < 1 or type(h['distance']) not in (float, int)
                    or not math.isfinite(h['distance']) or not 0 <= h['distance'] <= 2):
                raise FoundationError('invalid_recall_candidates')
            vectors.append(dict(h))
        if len({h.item_id for h in self.lexical} | {h['id'] for h in vectors}) > 240:
            raise FoundationError('recall_candidate_limit')
        return CandidateBatch(self.library_id, self.request, self.lexical, tuple(vectors))


class _ReadRetriever(ContextRetriever):
    """Only storage seams differ; rank, family, time and DTO code is canonical."""
    def _backing_allows(self, item_id):
        return self.store.eligible(item_id)

    def _project_results(self, results, request):
        # PgRecallStore refuses the whole operation if knowledge_objects or any
        # unsupported qualification domain exists. No known projection is hidden.
        return tuple(results)


class PgContextRecall:
    def __init__(self, manager, config, *, clock=None):
        self.manager, self.config, self.clock = manager, config, clock

    def read_candidates(self, route, batch, *, publish=None):
        if not isinstance(batch, CandidateBatch): raise FoundationError('invalid_recall_candidates')
        batch = batch.checked()
        if route.action != 'read' or route.library.library_id != batch.library_id:
            raise FoundationError('recall_library_mismatch')
        if (not batch.request.content_types or any(t not in (ContextContentType.REFERENCE, ContextContentType.FACT)
                for t in batch.request.content_types)):
            raise FoundationError('unsupported_recall_content_type')
        config = deepcopy(self.config)
        if config.context_reranker_model_dir:
            raise FoundationError('unsupported_recall_reranking')

        def check_config():
            if self.config != config: raise FoundationError('recall_config_changed')

        def read(session):
            check_config()
            try:
                store = PgRecallStore(session)
                retriever = _ReadRetriever(config, store, None, None, clock=self.clock)
                results = retriever.rank_candidates(batch.request, batch.lexical, batch.vector, allow_rerank=False)
                proof = store.proof()
            except (KeyError, ValueError, TypeError, OverflowError):
                raise FoundationError('unsupported_context_shape') from None
            check_config()
            return results, proof

        def final(session, initial):
            current = read(session)
            # Scores contain continuous recency decay. Preserve the initial DTO,
            # but independently rerun time/family gates and compare raw proof.
            before = tuple((r.id, r.duplicate_ids) for r in initial[0])
            after = tuple((r.id, r.duplicate_ids) for r in current[0])
            if initial[1] != current[1] or before != after:
                raise FoundationError('recall_selected_changed')
            return initial[0]

        def checked_publish(result):
            check_config()
            if publish is not None: publish(result)

        return self.manager.run_many((route,), read, finalize=final,
            combine=lambda gathered: gathered[0][1], publish=checked_publish)
