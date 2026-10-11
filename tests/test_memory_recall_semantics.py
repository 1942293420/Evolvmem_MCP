"""Public knowledge recall must use semantics while preserving ingestion gates."""
from tests.test_history_qa_memory import service, extracted_qa
from tests.test_retrieval_regressions import RankedEngine
from evolvmem import memory_recall, qa_memory


def ranked_service(service, scores):
    engine = RankedEngine(scores)
    service.embedding_engine = engine
    service.retriever.embedding_engine = engine
    return engine


def test_qa_paraphrase_matches_question_and_trigger_with_no_shared_words(service):
    row = qa_memory.save(service, {'project':'evo','question':'变更完成怎样验收？',
        'answer':'打开实际页面并保存对比截图。','trigger':'界面更新后','action':'publish'})
    qa_memory.save(service, {'project':'evo','question':'什么时候开会？',
        'answer':'每周三下午开例会。','action':'publish'})
    ranked_service(service, {'变更完成怎样验收？\n打开实际页面并保存对比截图。\n界面更新后': .9})
    result = memory_recall.recall(service, {'project':'evo','kind':'experience','query':'How should we verify a UI change?'})
    assert [r['id'] for r in result['qa']] == [row['id']]
    assert 'rerank' in result['recall_channels']


def test_factual_unrelated_query_does_not_return_recent_qa_or_history(service):
    extracted_qa(service)
    qa_memory.save(service, {'project':'evo','question':'怎样做页面验收？',
        'answer':'用真实截图检查呈现。','action':'publish'})
    result = memory_recall.recall(service, {'project':'evo','kind':'both','query':'氦氖激光器如何调节谐振腔长度'})
    assert result['qa'] == [] and result['history'] == [] and result['block'] == ''


def test_question_about_unknown_named_subject_cannot_use_qa_quantity_as_answer(service):
    qa_memory.save(service, {'project':'evo','question':'采集了多少商品？',
        'answer':'已采集十九个商品。','action':'publish'})
    ranked_service(service, {'采集了多少商品？\n已采集十九个商品。\n': .95})
    result = memory_recall.recall(service, {'project':'evo','kind':'experience',
        'query':'How many moons does Neptune have?'})
    assert result['qa'] == []
    known = memory_recall.recall(service, {'project':'evo','kind':'experience',
        'query':'How many items were collected in Evo?'})
    assert known['qa'], 'the canonical project also supports a named subject'


def test_qa_semantic_match_never_bypasses_project_or_draft_eligibility(service):
    row = qa_memory.save(service, {'project':'shop','question':'怎样做页面验收？',
        'answer':'用真实截图检查呈现。','action':'publish'})
    draft = qa_memory.save(service, {'project':'evo','question':'怎样做页面验收？',
        'answer':'用真实截图检查呈现。','action':'draft'})
    ranked_service(service, {'怎样做页面验收？\n用真实截图检查呈现。\n': .99})
    result = memory_recall.recall(service, {'project':'evo','kind':'experience','query':'How should we verify a UI change?'})
    assert result['qa'] == []


def test_history_summary_semantic_recall_uses_current_project_sources(service):
    _, a = extracted_qa(service)
    from evolvmem import history_memory
    summary = history_memory.sessions(service, 'evo')[0]['summary']
    ranked_service(service, {summary: .9})
    result = memory_recall.recall(service, {'project':'evo','kind':'history','query':'What did we decide about UI acceptance?'})
    assert [r['archive_id'] for r in result['history']] == [a.id]
    assert result['used_chars'] <= 4000


def test_active_qa_with_pending_ownership_is_not_usable_even_if_reranker_confident(service):
    row = qa_memory.save(service, {'project':'evo','question':'怎样做页面验收？',
        'answer':'用真实截图检查呈现。','action':'publish'})
    with service.store.transaction():
        service.store._connection().execute("UPDATE context_project_resolutions SET resolution_state='unresolved',review_state='pending',decision_source='automatic' WHERE item_id=?", (row['id'],))
    ranked_service(service, {'怎样做页面验收？\n用真实截图检查呈现。\n': .99})
    assert not qa_memory.detail(service, row['id'])['effective']
    assert not memory_recall.recall(service, {'project':'evo','kind':'experience','query':'页面验收'})['qa']
