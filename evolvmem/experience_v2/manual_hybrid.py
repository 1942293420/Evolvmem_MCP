"""Shared projection search for the older manual HTTP/MCP read contract."""
import time
from evolvmem.experience_v2.embedding_client import EmbeddingClient, EmbeddingFailure
from evolvmem.experience_v2.hybrid_search import hybrid_search


def read_manual_hybrid(conn, *, result, owner, query, category, page, page_size, embedding_client=None):
    from evolvmem.experience_v2.pg_experience_store import CATEGORIES, _format_manual_note
    deadline = time.monotonic() + 3.0
    vector, contract, error = None, None, None
    try:
        client = embedding_client or EmbeddingClient.from_environment()
        if client is None:
            error = 'embedding_unconfigured'
        else:
            contract = client.retrieval_contract_id
            vector = client.encode_query(query, deadline=min(deadline-.5,time.monotonic()+.65))
    except EmbeddingFailure as exc:
        error = exc.code
    except Exception:
        error = 'embedding_unavailable'
    common = dict(owner_ids=[owner], query=query, query_vector=vector,
        contract_id=contract, origin='manual', deadline=deadline, include_total=True)
    out = hybrid_search(conn, **common, category=category or None,
                        limit=page_size, offset=(page-1)*page_size)
    result['search_mode'] = out['diagnostics']['mode']
    result['diagnostics'] = {**out['diagnostics'], 'embedding_error': error}
    result['total'] = out['total']
    result['rows'] = [_format_manual_note(row) for row in out['items']]
    for cat in CATEGORIES:
        cat_out = hybrid_search(conn, **common, category=cat, limit=1)
        result['summary']['categories'][cat] = cat_out['total']
    return result
