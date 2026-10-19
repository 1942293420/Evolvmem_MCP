"""Real candidate selection, using deterministic model vectors in an isolated DB."""
import pytest
from tests.test_review_result_cache import service, base_service, setup_job
from evolvmem import organization_semantic_review as semantic


class RankingModel:
    is_loaded = True
    reranker_loaded = True

    def __init__(self):
        self.documents = []
        self.reranked = []

    def encode_document(self, text):
        self.documents.append(text)
        return [1., 0.] if any(f'条目{i:02d}' in text for i in range(5, 35)) else [0., 1.]

    def encode_query(self, text):
        return [1., 0.]

    def rerank(self, query, documents):
        self.reranked.extend(documents)
        import re
        return [int(re.search(r'条目(\d+)', text)[1])/40 for text in documents]

    def close(self):pass


def test_retrieval_filters_then_reranks_and_judges_only_one_small_batch(service, monkeypatch):
    _, request, calls = setup_job(service, monkeypatch, count=40)
    service.embedding_engine = model = RankingModel()
    job = semantic.start(service, request)
    semantic.tick(service)
    result = semantic.get(service, job['id'])
    assert result['status'] == 'completed'
    p = result['proposal']
    assert p['pool_count'] == 40 and p['recalled_count'] == 30
    assert len(calls) == 20 and p['remaining'] == 10
    assert '条目34' in p['eligible'][0]['title']
    assert all('条目00' not in text for text in model.reranked)
    assert [u['relevance'] for u in p['eligible']] == sorted([u['relevance'] for u in p['eligible']], reverse=True)
    first_calls = set(calls)
    next_job = semantic.start(service, {**request, 'continue_job_id':job['id']})
    semantic.tick(service)
    assert len(calls) == 30 and len(set(calls)) == 30
    assert not first_calls.intersection(calls[20:])
    assert len(model.documents) == 40, 'cached content must not be embedded again'
    assert len(model.reranked) == 30, 'next batch reuses the frozen candidate order'


def test_unavailable_similarity_service_never_falls_back_to_full_ai_scan(service, monkeypatch):
    _, request, calls = setup_job(service, monkeypatch, count=6)
    service.embedding_engine = None
    job = semantic.start(service, request)
    semantic.tick(service)
    result = semantic.get(service, job['id'])
    assert result['status'] == 'failed'
    assert result['error'] == 'review_retrieval_unavailable'
    assert calls == []


@pytest.mark.parametrize('initial', ['startup_failure', 'embedding_failure', 'reranker_failure'])
def test_review_recovers_when_shared_model_returns_without_restarting_worker(service, monkeypatch, initial):
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    from evolvmem.lan_http_client import JsonClient
    service.config.embedding_http_url = 'http://shared-model.test'
    available = False
    def model_request(client, body):
        if not available:
            raise RuntimeError('lan_http_unavailable')
        if client.url.endswith('/embedding/status'):
            return dict(available=True, dimension=service.config.embedding_dim,
                        query_prefix=service.config.embedding_query_prefix,
                        document_prefix=service.config.embedding_doc_prefix, reranker_available=True)
        if client.url.endswith('/embedding'):
            return {'vector':[1.] + [0.] * (service.config.embedding_dim-1)}
        if client.url.endswith('/rerank'):
            return {'scores':[.9] * len(body['documents'])}
        raise AssertionError(client.url)
    monkeypatch.setattr(JsonClient, 'post', model_request)
    task_id, request, calls = setup_job(service, monkeypatch, count=2)
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    try:
        available = initial != 'startup_failure'
        client = worker._connect()
        if initial == 'startup_failure':
            assert client.embedding_engine is None
        else:
            # Exercise the real provider's failure state, not a replacement flag.
            available = False
            with pytest.raises(RuntimeError):
                if initial == 'embedding_failure': client.embedding_engine.encode_query('模拟查询')
                else: client.embedding_engine.rerank('模拟查询',['模拟正文'])
        failed = semantic.start(service, request)
        worker.tick()
        assert semantic.get(service,failed['id'])['error'] == 'review_retrieval_unavailable'
        assert calls == []
        available = True
        retried = semantic.start(service, request)
        worker.tick()
        result = semantic.get(service,retried['id'])
        assert result['status'] == 'completed', result['error']
        assert len(calls) == 2
        assert client.store._connection().execute(
            "SELECT COUNT(*) FROM organization_units WHERE task_id=? AND decision='manual'", (task_id,)
        ).fetchone()[0] == 0, 'recovered AI suggestions must not archive material'
    finally:
        worker.stop()


def test_stop_keeps_finished_judgments_and_never_starts_another_call(service, monkeypatch):
    _, request, calls = setup_job(service, monkeypatch, count=8)
    job=semantic.start(service,request)
    original=semantic.judge
    def stopped(*args):
        result=original(*args)
        semantic.stop(service,job['id'])
        return result
    monkeypatch.setattr(semantic,'judge',stopped)
    semantic.tick(service)
    result=semantic.get(service,job['id'])
    assert result['status']=='stopped' and len(calls)==4
    assert len(result['proposal']['eligible'])==4
    assert not semantic.tick(service)
