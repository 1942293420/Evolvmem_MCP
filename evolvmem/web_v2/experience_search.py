"""Web experience query validation and warm encoder/shared SQL integration."""
import json
import re
import time

from evolvmem.experience_v2.embedding_client import EmbeddingClient
from evolvmem.experience_v2.hybrid_search import hybrid_search, _validate
from evolvmem.web_v2.common import V2ApiError


def search_notes(conn, *, owner_id, params, status, category, origin, kind, limit, offset):
    query = params.get("q", "")
    project = params.get("project", "")
    constraints = params.get("constraints", {})
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except (ValueError, TypeError):
            raise V2ApiError("invalid_recall_constraints", 400) from None
    try:
        _validate([owner_id], query, None, None, constraints, status, limit, offset,
                  None, .72, project)
    except ValueError as exc:
        raise V2ApiError(str(exc), 400) from None
    deadline = time.monotonic() + 3
    vector, contract, error = None, None, ""
    try:
        client = EmbeddingClient.from_environment()
        if client is None:
            error = "embedding_unconfigured"
        else:
            contract = client.retrieval_contract_id
            vector = client.encode_query(query.strip(), deadline=deadline - .4)
    except Exception as exc:
        code = getattr(exc, "code", "") or str(exc)
        error = code if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code) else "embedding_unavailable"
    try:
        result = hybrid_search(conn, owner_ids=[owner_id], query=query,
            query_vector=vector, contract_id=contract, project=project,
            constraints=constraints, status=status, category=category or None,
            origin=origin or None, kind=kind or None, limit=limit, offset=offset,
            include_total=True, deadline=deadline)
    except ValueError as exc:
        raise V2ApiError(str(exc), 400) from None
    except TimeoutError:
        raise V2ApiError("experience_search_timeout", 503) from None
    if error:
        result["diagnostics"]["embedding_error"] = error
    return result
