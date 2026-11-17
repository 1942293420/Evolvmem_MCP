"""Authorized SQL lexical/pgvector retrieval; never writes impressions or notes."""
from __future__ import annotations

import json
import math
import re
import time

_FIELDS = (
    "id owner_id category problem solution reusable_experience origin kind misbelief "
    "scope evidence occurrence_count importance pinned status confidence_a confidence_b "
    "stability last_used_at use_count valid_from valid_until proposal_uid source_actor "
    "source_device_id source_agent source_model revision deleted_at created_at updated_at"
).split()
_LOW_SIGNAL = {"任务", "使用", "处理", "数据", "检查", "测试", "状态", "结果",
               "系统", "请求", "调用", "验证", "项目", "返回", "出现", "问题",
               "这个", "那个", "一下", "帮我", "我们", "现在", "如何", "怎么"}
_STATUSES = {"active", "challenger", "dormant", "contested", "retired", "all"}
_BLOB = "(n.problem || E'\\n' || n.solution || E'\\n' || n.reusable_experience || E'\\n' || COALESCE(n.misbelief,''))"
_SHA = ("encode(sha256(convert_to(n.category || E'\\n' || n.problem || E'\\n' || "
        "n.solution || E'\\n' || n.reusable_experience,'UTF8')),'hex')")


def _validate(owner_ids, query, query_vector, contract_id, constraints, status, limit,
              offset, deadline, min_similarity, project):
    if (not isinstance(owner_ids, list) or not 1 <= len(owner_ids) <= 100
            or any(not isinstance(o, str) or not o.strip() or len(o) > 300 for o in owner_ids)):
        raise ValueError("invalid_authorized_owners")
    if (not isinstance(query, str) or not query.strip() or len(query) > 4000
            or any(ord(c) < 32 and c not in "\n\r\t" for c in query)):
        raise ValueError("invalid_recall_query")
    if (not isinstance(constraints, dict) or len(constraints) > 20
            or any(not isinstance(k, str) or not k or len(k) > 100
                   or not isinstance(v, str) or len(v) > 1000 for k, v in constraints.items())):
        raise ValueError("invalid_recall_constraints")
    if (not isinstance(project, str) or len(project) > 300 or status not in _STATUSES
            or type(limit) is not int or not 1 <= limit <= 200
            or type(offset) is not int or not 0 <= offset <= 5000):
        raise ValueError("invalid_recall_options")
    if type(min_similarity) not in (int, float) or not 0 < min_similarity <= 1:
        raise ValueError("invalid_semantic_threshold")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("invalid_recall_deadline")
    if query_vector is not None:
        if (not isinstance(query_vector, (list, tuple)) or len(query_vector) != 1024
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in query_vector)
                or not any(v != 0 for v in query_vector)):
            raise ValueError("invalid_retrieval_vector")
        if not isinstance(contract_id, str) or not contract_id.strip() or len(contract_id) > 256:
            raise ValueError("invalid_retrieval_contract")


def _terms(query):
    terms = set()
    for word in re.findall(r"[a-z0-9_.-]{2,}|[一-鿿]+", query.lower()):
        if re.fullmatch("[一-鿿]+", word):
            terms.update(word[i:i+2] for i in range(len(word)-1))
        else:
            terms.add(word)
    return sorted(terms - _LOW_SIGNAL, key=lambda t: (-len(t), t))[:64]


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(owner_ids, project, constraints, status, category, origin, kind):
    clauses = ["n.owner_id = ANY(%s::text[])", "jsonb_typeof(n.scope)='object'",
               "n.scope @> %s::jsonb",
               "(n.valid_from IS NULL OR n.valid_from <= CURRENT_TIMESTAMP)",
               "(n.valid_until IS NULL OR n.valid_until > CURRENT_TIMESTAMP)"]
    params = [list(dict.fromkeys(owner_ids)),
              json.dumps(constraints, ensure_ascii=False, sort_keys=True)]
    if project.strip():
        for field in ("project", "repo"):
            clauses.append(f"lower(btrim(COALESCE(n.scope->>'{field}',''))) IN ('',lower(btrim(%s)))")
            params.append(project)
    if status == "all":
        clauses.append("(n.deleted_at IS NULL OR n.status='retired')")
    elif status == "retired":
        clauses.append("n.status='retired'")
    else:
        clauses.extend(["n.deleted_at IS NULL", "n.status=%s"])
        params.append(status)
    for field, value in (("category", category), ("origin", origin), ("kind", kind)):
        if value is not None:
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError("invalid_recall_filter")
            clauses.append(f"n.{field}=%s")
            params.append(value)
    return " AND ".join(clauses), params


def _budget(conn, deadline):
    if deadline is None:
        return
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("experience_recall_deadline_exceeded")
    conn.execute("SELECT set_config('statement_timeout',%s,true), set_config('lock_timeout',%s,true)",
                 (str(remaining), str(min(100, remaining))))


def _execute(conn, sql, params, deadline):
    _budget(conn, deadline)
    return conn.execute(sql, params)


def _keyword(query, terms):
    # Literal patterns stay parameters. Chinese uses overlapping bigrams, with a
    # quality gate before popularity can rank a candidate.
    patterns = [_like(term) for term in terms]
    matched = f"(SELECT count(*) FROM unnest(%s::text[]) t(pattern) WHERE {_BLOB} ILIKE t.pattern ESCAPE '\\')"
    phrase = f"({_BLOB} ILIKE %s ESCAPE '\\')"
    score = f"(CASE WHEN {phrase} THEN 1.0 ELSE {matched}::float / %s END)"
    score_params = [_like(query), patterns, max(1, len(terms))]
    quality = f"({phrase} OR ({matched} >= %s AND {matched}::float / %s >= 0.25))"
    quality_params = [_like(query), patterns, min(2, len(terms)), patterns, max(1, len(terms))]
    return score, score_params, quality, quality_params


def _vector_filter(contract_id):
    return ("n.retrieval_embedding IS NOT NULL AND n.retrieval_embedding_contract=%s "
            f"AND n.retrieval_embedding_content_sha256={_SHA}"), [contract_id]


def _dict_rows(cursor):
    names = [d.name for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]



def _cosine_expression(literal, vector_where, vector_params):
    if literal is None:
        return "NULL::float", []
    return (f"CASE WHEN {vector_where} THEN "
            "1-(n.retrieval_embedding <=> %s::public.vector) END"), [*vector_params, literal]


def _complete_page(conn, *, where, params, keyword, vector, min_similarity,
                   limit, offset, deadline):
    """Count and page the same complete match relation with a stable SQL order.

    ANN/lexical candidate budgets remain useful for hook previews. A paginated
    result must not depend on those changing budgets or ANN tie traversal order.
    """
    score, score_params, quality, quality_params = keyword
    cosine, cosine_params = vector
    fields = ",".join(_FIELDS)
    order = "score DESC,cardinality(match_types) DESC,pinned DESC,importance DESC,owner_id,id"
    sql = f"""WITH raw AS NOT MATERIALIZED (
        SELECT {','.join('n.'+f for f in _FIELDS)},
               {score} AS raw_keyword_score,{quality} AS keyword_eligible,
               {cosine} AS raw_cosine
        FROM experience_notes n WHERE {where}
    ), eligible AS NOT MATERIALIZED (
        SELECT {fields},keyword_eligible,
               CASE WHEN keyword_eligible THEN raw_keyword_score ELSE 0.0 END AS keyword_score,
               CASE WHEN raw_cosine BETWEEN %s AND 1.000001 THEN raw_cosine END AS cosine
        FROM raw
    ), matches AS MATERIALIZED (
        SELECT {fields},keyword_score,cosine,
               GREATEST(keyword_score,COALESCE(cosine,0.0)) AS score,
               array_remove(ARRAY[CASE WHEN keyword_eligible THEN 'keyword' END,
                   CASE WHEN cosine IS NOT NULL THEN 'vector' END]::text[],NULL) AS match_types
        FROM eligible WHERE keyword_eligible OR cosine IS NOT NULL
    ), page AS (
        SELECT * FROM matches ORDER BY {order} LIMIT %s OFFSET %s
    ), totals AS (SELECT count(*) AS total FROM matches)
    SELECT totals.total,page.* FROM totals LEFT JOIN page ON TRUE
    ORDER BY page.score DESC,cardinality(page.match_types) DESC,
             page.pinned DESC,page.importance DESC,page.owner_id,page.id"""
    rows = _dict_rows(_execute(conn, sql,
        [*score_params,*quality_params,*cosine_params,*params,min_similarity,limit,offset], deadline))
    total = int(rows[0]["total"])
    items = []
    for row in rows:
        row.pop("total")
        if row["id"] is not None:
            row["keyword_score"] = float(row["keyword_score"])
            items.append(row)
    _budget(conn, deadline)
    return {"items": items, "total": total}

def hybrid_search(conn, *, owner_ids: list[str], query: str, query_vector: list | None,
                  contract_id: str | None, project: str = "", constraints: dict | None = None,
                  status: str = "active", category: str | None = None, origin: str | None = None,
                  kind: str | None = None, limit: int = 3, offset: int = 0,
                  include_total: bool = False, deadline: float | None = None,
                  min_similarity: float = 0.72) -> dict:
    """Search all eligible notes in SQL, then fuse bounded relevant candidates.

    owner_ids are a server-resolved authorization set, never request-supplied ACL.
    include_total counts and pages the complete SQL match relation in one snapshot;
    bounded ANN candidate pools never determine page membership or totals.
    """
    constraints = {} if constraints is None else constraints
    _validate(owner_ids, query, query_vector, contract_id, constraints, status, limit,
              offset, deadline, min_similarity, project)
    where, params = _where(owner_ids, project, constraints, status, category, origin, kind)
    terms = _terms(query)
    diagnostics = {"mode": "hybrid" if query_vector is not None else "keywords_only",
                   "keyword_candidates": 0, "vector_candidates": 0,
                   "total_complete": bool(include_total)}
    if not terms and query_vector is None:
        _budget(conn, deadline)
        return {"items": [], "total": 0 if include_total else None, "diagnostics": diagnostics}
    score, score_params, quality, quality_params = _keyword(query.strip(), terms)
    if not terms:
        quality, quality_params = "FALSE", []
    budget = min(6000, max(80, (offset + limit) * 4))
    lexical = _execute(conn,
        f"SELECT n.id,n.owner_id,{score} AS keyword_score FROM experience_notes n "
        f"WHERE {where} AND {quality} ORDER BY keyword_score DESC,n.id ASC LIMIT %s",
        [*score_params, *params, *quality_params, budget], deadline).fetchall()
    candidates = {(owner, nid): {"keyword_score": float(s), "cosine": None,
                                "match_types": ["keyword"]} for nid, owner, s in lexical}
    diagnostics["keyword_candidates"] = len(lexical)
    literal = None
    vector_where, vector_params = _vector_filter(contract_id)
    if query_vector is not None:
        literal = "[" + ",".join(format(float(v), ".9g") for v in query_vector) + "]"
        _execute(conn, "SELECT set_config('hnsw.ef_search',%s,true),"
            "set_config('hnsw.iterative_scan','strict_order',true),"
            "set_config('hnsw.max_scan_tuples','20000',true),"
            "set_config('hnsw.scan_mem_multiplier','4',true)",
            ["1000"], deadline)
        # Explicit ANN lane: tiny owner sets otherwise choose Btree + Sort.
        # Savepoint rollback restores flags after a SQL failure; successful work
        # restores caller values before the lexical/count/final stages continue.
        with conn.transaction():
            flags = conn.execute("SELECT current_setting('enable_seqscan'),current_setting('enable_sort')").fetchone()
            _execute(conn, "SELECT set_config('enable_seqscan','off',true),set_config('enable_sort','off',true)", [], deadline)
            nearest = _execute(conn,
                "SELECT n.id,n.owner_id,1-(n.retrieval_embedding <=> %s::public.vector) AS cosine "
                f"FROM experience_notes n WHERE {where} AND {vector_where} "
                "ORDER BY n.retrieval_embedding <=> %s::public.vector LIMIT %s",
                [literal, *params, *vector_params, literal, budget], deadline).fetchall()
            conn.execute("SELECT set_config('enable_seqscan',%s,true),set_config('enable_sort',%s,true)", flags)
        if not include_total and sum(c is not None and math.isfinite(c) and c >= min_similarity for _, _, c in nearest) < offset + limit:
            # ANN can underfill filtered populations (including equal-vector
            # clusters). Complete that short page in SQL within the same budget.
            # +0 prevents another ANN plan while retaining owner Btree filtering.
            exact = _execute(conn,
                "SELECT n.id,n.owner_id,1-(n.retrieval_embedding <=> %s::public.vector) AS cosine "
                f"FROM experience_notes n WHERE {where} AND {vector_where} "
                "AND (1-(n.retrieval_embedding <=> %s::public.vector)) >= %s "
                "ORDER BY (n.retrieval_embedding <=> %s::public.vector)+0 LIMIT %s",
                [literal, *params, *vector_params, literal, min_similarity, literal, budget], deadline).fetchall()
            nearest = list({(o,i):(i,o,c) for i,o,c in [*nearest,*exact]}.values())
            diagnostics["vector_fallback"] = "filtered_exact_underfill"
        for nid, owner, cosine in nearest:
            if cosine is None or not math.isfinite(cosine) or cosine < min_similarity:
                continue
            item = candidates.setdefault((owner, nid),
                {"keyword_score": 0.0, "cosine": None, "match_types": []})
            item["cosine"] = float(cosine)
            item["match_types"].append("vector")
            diagnostics["vector_candidates"] += 1
    if include_total:
        complete = _complete_page(conn, where=where, params=params,
            keyword=(score,score_params,quality,quality_params),
            vector=_cosine_expression(literal,vector_where,vector_params),
            min_similarity=min_similarity,limit=limit,offset=offset,deadline=deadline)
        diagnostics["pagination"] = "stable_exact_sql"
        return {**complete, "diagnostics": diagnostics}
    total = None
    if not candidates:
        return {"items": [], "total": total, "diagnostics": diagnostics}
    # Fresh eligibility/projection check before any result leaves the core.
    cosine_sql, cosine_params = _cosine_expression(literal,vector_where,vector_params)
    rows = _dict_rows(_execute(conn,
        f"SELECT {','.join('n.'+f for f in _FIELDS)},{score} AS keyword_score,"
        f"{quality} AS keyword_eligible,{cosine_sql} AS cosine "
        f"FROM experience_notes n WHERE {where} AND n.id=ANY(%s::text[])",
        [*score_params, *quality_params, *cosine_params, *params,
         sorted({nid for _, nid in candidates})], deadline))
    fused = []
    for row in rows:
        key = (row["owner_id"], row["id"])
        if key not in candidates:
            continue
        matched = []
        if row.pop("keyword_eligible"):
            matched.append("keyword")
        else:
            row["keyword_score"] = 0.0
        cosine = row["cosine"]
        if cosine is not None and math.isfinite(cosine) and cosine >= min_similarity:
            matched.append("vector")
        else:
            row["cosine"] = None
        if not matched:
            continue
        row["keyword_score"] = float(row["keyword_score"])
        row["match_types"] = matched
        row["score"] = max(row["keyword_score"], row["cosine"] or 0)
        fused.append(row)
    fused.sort(key=lambda r: (-r["score"], -len(r["match_types"]), -bool(r["pinned"]),
                              -r["importance"], r["owner_id"], r["id"]))
    _budget(conn, deadline)
    return {"items": fused[offset:offset + limit], "total": total, "diagnostics": diagnostics}
