"""Regression cases for relevance and exact identifiers in natural queries."""
from tests.test_context_retriever import (
    store, add_item, make_retriever, request_for, update_metadata,
    FakeVectorIndex, QueryEmbeddingEngine,
)
from evolvmem.context_models import ContextContentType, ContextTier


class RankedEngine(QueryEmbeddingEngine):
    reranker_loaded = True

    def __init__(self, scores):
        super().__init__([1., 0., 0.])
        self.scores = scores

    def rerank(self, query, documents):
        return [max((score for text, score in self.scores.items() if text in document), default=.001) for document in documents]


def test_project_candidate_pool_survives_global_neighbors_before_reranking(store, test_config):
    from evolvmem.context_models import ContextScope
    neighbors = [add_item(store, f'global-{i}', l0=f'global generic policy {i}',
                         scope=ContextScope.GLOBAL, project='', content_type=ContextContentType.CONSTRAINT)
                 for i in range(55)]
    target = add_item(store, 'local-answer', l0='页面业务说明',
                      l1='后端已自动处理的内容，不应逐条强制人工审核。')
    index = FakeVectorIndex(test_config, [{'id':i.id,'distance':.3} for i in neighbors]
                            + [{'id':target.id,'distance':.5}])
    engine = RankedEngine({'不应逐条强制人工审核': .9})
    hits = make_retriever(test_config, store, index, engine).search(
        request_for('are repeated manual approvals necessary?', top_k=1))
    assert hits[0].id == target.id
    assert len(engine.queries) == 1, 'expanding candidate search must reuse the query embedding'


def test_reranking_reads_bounded_detail_but_returns_only_summary(store, test_config):
    test_config.context_l1_max_chars = 3000
    target = add_item(store, 'detailed-answer', l0='页面设置',
                      l1='切页后必须保留查询条件和草稿。' + '正常说明' * 500)
    index = FakeVectorIndex(test_config, [{'id':target.id,'distance':.714}])
    engine = RankedEngine({})
    def rank(query, documents):
        assert '切页后必须保留查询条件和草稿。' in documents[0]
        assert len(documents[0]) <= 1400
        return [.9]
    engine.rerank = rank
    hits = make_retriever(test_config, store, index, engine).search(request_for('switch tabs safely'))
    assert [hit.id for hit in hits] == [target.id]
    assert hits[0].l0 == '页面设置' and not hasattr(hits[0], 'l1')


def test_more_relevant_history_beats_popular_high_priority_neighbor(store, test_config):
    test_config.context_vector_min_similarity = .7
    target = add_item(store, 'actual-answer', l0='页面修改后需要实际截图验证',
        content_type=ContextContentType.PREFERENCE, importance=6, confidence=1)
    neighbor = add_item(store, 'general-policy', l0='处理异常需要保留检查点',
        content_type=ContextContentType.CONSTRAINT, tier=ContextTier.PINNED,
        importance=10, confidence=1)
    update_metadata(store, neighbor.id, access_count=1000)
    index = FakeVectorIndex(test_config, [
        {'id':target.id,'distance':.44}, {'id':neighbor.id,'distance':.50}])
    engine = RankedEngine({'页面修改后需要实际截图验证': .8, '处理异常需要保留检查点': .1})
    hits = make_retriever(test_config, store, index, engine).search(
        request_for('how to verify the page', top_k=1))
    assert [hit.id for hit in hits] == [target.id]


def test_reranking_recovers_weak_bilingual_candidate_without_filling_unrelated_results(store, test_config):
    target = add_item(store, 'smooth-scroll', l0='避免无限动画造成滚动卡顿')
    noise = add_item(store, 'noise', l0='会议时间改到下午')
    index = FakeVectorIndex(test_config, [
        {'id': noise.id, 'distance': .7}, {'id': target.id, 'distance': .714}])
    engine = RankedEngine({'避免无限动画造成滚动卡顿': .22})
    retriever = make_retriever(test_config, store, index, engine)
    assert [h.id for h in retriever.search(request_for('keep scrolling smooth'))] == [target.id]
    engine.scores = {}
    assert retriever.search(request_for('moons of Neptune')) == ()


def test_reranker_failure_restores_original_similarity_gate(store, test_config):
    target = add_item(store, 'weak', l0='滚动性能的历史约束')
    index = FakeVectorIndex(test_config, [{'id': target.id, 'distance': .714}])
    engine = RankedEngine({})
    def unavailable(*args):
        raise RuntimeError('unavailable')
    engine.rerank = unavailable
    assert make_retriever(test_config, store, index, engine).search(request_for('smooth scrolling')) == ()


def test_exact_error_identifier_inside_sentence_has_lexical_candidate(store, test_config):
    target = add_item(store, 'bot', l0='应用可用范围缺少人员导致 HTTP400 code41050')
    results = make_retriever(test_config, store).search(request_for(
        'Feishu bot sends HTTP 400 code 41050. Which application setting should be checked?'))
    assert [h.id for h in results] == [target.id]


def test_weak_reranked_quantity_match_needs_evidence_for_named_subject(store, test_config):
    target = add_item(store, 'progress-count', l0='采集结果已经从十七条增加到十九条')
    engine = RankedEngine({'采集结果已经从十七条增加到十九条': .95})
    index = FakeVectorIndex(test_config, [{'id':target.id,'distance':.74}])
    retriever = make_retriever(test_config, store, index, engine)
    for subject in ('Neptune', 'Aldebaran', 'NovaProduct'):
        assert retriever.search(request_for(f'How many items belong to {subject}?')) == ()
    assert retriever.search(request_for('How many items were collected?'))
    # Strong semantic support still permits translated names; the extra check
    # applies only to weak neighbors that the reranker alone would admit.
    index._results[0]['distance'] = .4
    assert retriever.search(request_for('How many moons does Neptune have?'))


def test_full_text_duplicates_share_one_slot_but_keep_both_source_ids(store, test_config):
    a = add_item(store, 'source-a', l0='同一份配置约束', l1='同一份完整配置约束', l2='用户明确要求的完整配置约束')
    b = add_item(store, 'source-b', l0=a.layers.l0, l1=a.layers.l1, l2=a.layers.l2)
    different = add_item(store, 'different-body', l0=a.layers.l0, l2='另一个条件下的配置约束')
    hits = make_retriever(test_config, store).search(request_for('配置约束'))
    assert len(hits) == 2
    same = next(h for h in hits if h.id in (a.id, b.id))
    assert {same.id, *same.duplicate_ids} == {a.id, b.id}
    assert any(h.id == different.id for h in hits)
