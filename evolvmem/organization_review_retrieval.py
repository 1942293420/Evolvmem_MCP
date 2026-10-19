"""Persistent document vectors, bounded similarity recall, then local reranking."""
import hashlib
import json

import numpy as np

BATCH_SIZE = 20
RECALL_LIMIT = 100
VERSION = 'review-retrieval-v1'


def key(unit):
    return f"{unit['task_id']}:{unit['digest']}"


def document(unit):
    from evolvmem.task_narrative import read, render
    narrative = read(unit.get('task_record'))
    return (unit['title'] + '\n' + (render(narrative) if narrative else unit['text']))[:4000]


def rank(service, units, request, progress=lambda **kw: None):
    if not units:
        progress(phase='judging',pool_count=0,recalled_count=0,ranked_count=0)
        return []
    engine = service.embedding_engine
    reconnect = engine is None or not engine.is_loaded or not engine.reranker_loaded
    if reconnect:
        if not service.config.embedding_http_url:
            raise ValueError('review_retrieval_unavailable')
        # A worker can outlive a model-server restart or an HTTP failure. Its
        # cached availability flags are not a permanent verdict. Recheck only
        # for this requested review, without loading another local model or
        # changing the shared client's lifecycle under other operations.
        from evolvmem.embedding import EmbeddingEngine
        engine = EmbeddingEngine(service.config)
    try:
        if reconnect:
            engine.initialize()
            if not engine.is_loaded or not engine.reranker_loaded:
                raise ValueError('review_retrieval_unavailable')
        return _rank(service, units, request, progress, engine)
    finally:
        if reconnect:
            engine.close()


def _rank(service, units, request, progress, engine):
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('CREATE TABLE IF NOT EXISTS organization_review_vectors('
                     'cache_key TEXT PRIMARY KEY, vector BLOB NOT NULL)')
    profile = [VERSION, str(service.config.model_path), service.config.embedding_dim,
               service.config.embedding_http_url, service.config.embedding_doc_prefix]
    vectors = []
    documents = [document(u) for u in units]
    for index, text in enumerate(documents):
        cache_key = hashlib.sha256(json.dumps([profile, text], ensure_ascii=False).encode()).hexdigest()
        row = conn.execute('SELECT vector FROM organization_review_vectors WHERE cache_key=?', (cache_key,)).fetchone()
        vector = np.frombuffer(row[0], dtype=np.float32).copy() if row else np.asarray(engine.encode_document(text), dtype=np.float32)
        if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError('review_retrieval_unavailable')
        if row is None:
            with service.store.transaction():
                conn.execute('INSERT OR REPLACE INTO organization_review_vectors VALUES(?,?)', (cache_key, vector.tobytes()))
        vectors.append(vector / np.linalg.norm(vector))
        progress(phase='retrieval', indexed=index+1, pool_count=len(units))
    names = {p['project']:p['display_name'] or p['project'] for p in service.knowledge().registry()}
    query = (request['condition'] + '\n适用范围：' + '、'.join(names.get(p,p) for p in request.get('projects',[])))[:4000]
    vector = np.asarray(engine.encode_query(query), dtype=np.float32)
    if vector.ndim != 1 or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
        raise ValueError('review_retrieval_unavailable')
    similarities = (np.asarray(vectors) @ (vector / np.linalg.norm(vector)) + 1) / 2
    order = [int(i) for i in np.argsort(-similarities, kind='stable')
             if similarities[i] >= service.config.context_vector_min_similarity][:RECALL_LIMIT]
    progress(phase='reranking', recalled_count=len(order), pool_count=len(units))
    scores = []
    for offset in range(0,len(order),8):
        progress(phase='reranking',recalled_count=len(order),reranked_count=len(scores))
        scores.extend(engine.rerank(query,[documents[i] for i in order[offset:offset+8]]))
    from evolvmem.reranking import valid_scores
    if not valid_scores(scores, len(order)):
        raise ValueError('review_retrieval_unavailable')
    ranked = [{'key':key(units[i]), 'similarity':float(similarities[i]), 'relevance':float(score)}
              for i, score in zip(order, scores) if score >= service.config.context_reranker_min_score]
    ranked.sort(key=lambda row:row['relevance'], reverse=True)
    progress(phase='judging', recalled_count=len(order), ranked_count=len(ranked))
    return ranked


def select(base, ranking):
    """Freeze a bounded ordered pool; only this batch may reach the reasoner."""
    entries = {key(u):u for u in base['excluded'] if u.get('reason_code') in ('ai_pending','batch_limit')}
    ordered = []
    for row in ranking:
        if row['key'] not in entries:continue
        entry = {**entries[row['key']], 'similarity':row['similarity'], 'relevance':row['relevance']}
        entry.update(reason_code='ai_pending' if len(ordered)<BATCH_SIZE else 'batch_limit',
                     reason='等待 AI 判断' if len(ordered)<BATCH_SIZE else '按相关度排序，尚未判断')
        ordered.append(entry)
    # Preparation problems remain visible, but previously rejected scopes are
    # not fresh candidates. Existing saved previews still permit undo.
    problems = [u for u in base['excluded'] if u.get('reason_code') in ('rule_changed','value_review')]
    return {**base, 'excluded':ordered+problems, 'eligible':[],
            'remaining':max(0,len(ordered)-BATCH_SIZE), 'ranked_count':len(ordered),
            'batch_size':BATCH_SIZE, 'retrieval_limit':RECALL_LIMIT}
