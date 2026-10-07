"""待确认问答显示实际入库原因，并按原因分组筛选。

只覆盖最小范围：detail 的 review_reason、list_items 的 review_reason_counts 与
reason_code 筛选。使用真实临时 SQLite 库和现有提炼/资格检查路径。
"""
from tests.test_history_qa_memory import extracted_qa, service  # noqa: F401  复用真实临时库夹具


def counts(result):
    return {code: value['count'] for code, value in result['review_reason_counts'].items()}


def test_identity_conflict_candidate_shows_real_reason_not_generic(service):
    from evolvmem import qa_memory
    extracted_qa(service, question='界面改动前先确认什么？', answer='修改界面前先明确验收条件。', key='same', session='first')
    second, _ = extracted_qa(service, question='界面改动后如何复核？', answer='界面改动后复核实际操作入口。',
                             key='same', session='second')
    row = qa_memory.detail(service, second.candidates[0].context_id)
    assert row['status'] == 'candidate'
    # 兼容字段保留原泛化文案
    assert row['reason'] == '推断、来源或入库状态需要确认'
    assert row['review_reason']['code'] == 'conflict'
    assert row['review_reason']['label'] == '冲突待确认'
    assert '同一标识已有不同内容' in row['review_reason']['detail']


def test_missing_evidence_candidate_shows_actual_reason(service):
    from evolvmem import qa_memory
    item, _ = extracted_qa(service, question='如何开始调研？', answer='调研时先比较三种可行方案。',
                           basis='inferred', key='research', session='inferred')
    row = qa_memory.detail(service, item.candidates[0].context_id)
    assert row['status'] == 'candidate'
    assert row['review_reason']['code'] == 'evidence'
    assert row['review_reason']['detail'] == '推断或缺少用户明确依据，待确认'


def test_effective_qa_has_no_review_reason_and_is_not_counted(service):
    from evolvmem import qa_memory
    item, _ = extracted_qa(service)
    row = qa_memory.detail(service, item.candidates[0].context_id)
    assert row['effective'] and row['review_reason'] is None
    result = qa_memory.list_items(service, {'project': 'evo'})
    assert result['items'][0]['review_reasons'] == []
    assert result['items'][0]['reason_codes'] == []
    assert result['review_reason_counts'] == {}


def test_stale_source_is_not_overridden_by_old_ingestion_reason(service):
    from evolvmem import qa_memory
    item, _ = extracted_qa(service)
    item_id = item.candidates[0].context_id
    with service.store.transaction():
        service.store._connection().execute('UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?',
            ('同一标识已有不同内容，需要明确补充或替代关系', item_id))
    assert qa_memory.detail(service, item_id)['review_reason'] is None
    source = service.knowledge().detail(item_id)
    service.knowledge().update(item_id, {'expected_revision': source['revision'], 'body': '用户修正了界面流程要求。'})
    row = qa_memory.detail(service, item_id)
    assert row['status'] == 'stale'
    assert row['review_reason']['code'] == 'source'
    assert '已改变' in row['review_reason']['detail']
    assert '同一标识' not in row['review_reason']['detail']


def test_withdrawn_source_keeps_current_source_reason(service):
    from evolvmem import qa_memory, unit_derivations
    item, _ = extracted_qa(service)
    item_id = item.candidates[0].context_id
    unit_derivations.supersede(service, item_id, '归属已撤回，先停止展示')
    row = qa_memory.detail(service, item_id)
    assert row['status'] == 'candidate'
    assert row['review_reason']['code'] == 'source'
    assert '失效' in row['review_reason']['detail']
    assert '归属已撤回' not in row['review_reason']['detail']


def test_unverified_experience_is_verification_not_source(service):
    from evolvmem import qa_memory
    case = service.experiences().record({'project': 'evo', 'problem': '缺少验证的经验能否复用？',
        'conditions': {'场景': '复用旧方法'}, 'steps': ['先核对实际结果']})
    row = qa_memory.detail(service, case['id'])
    assert row['status'] == 'candidate' and not row['effective']
    assert row['review_reason']['code'] == 'verification'
    assert '验证依据' in row['review_reason']['detail']


def test_verification_reason_is_not_guessed_from_category(service):
    from evolvmem import qa_memory
    item, _ = extracted_qa(service, question='旧方法如何复用？', answer='复用前先核对实际验证结果。',
                           basis='inferred', key='method', session='method')
    item_id = item.candidates[0].context_id
    with service.store.transaction():
        service.store._connection().execute('UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?',
            ('经验方法须绑定实际验证结果，提炼和人工入库不能替代验证', item_id))
    row = qa_memory.detail(service, item_id)
    # 非经验类型，不能凭文案或分类猜成验证原因
    assert row['review_reason']['code'] == 'other'
    assert row['review_reason']['detail'] == '经验方法须绑定实际验证结果，提炼和人工入库不能替代验证'


def test_duplicate_sources_with_different_reasons_merge_and_count_once_per_group(service):
    from evolvmem import qa_memory
    extracted_qa(service, question='界面调整前先确认什么？', answer='先确认验收条件再动手。',
                 basis='inferred', key='dup-a', session='dup-a')
    qa_memory.save(service, {'project': 'evo', 'question': '界面调整前先确认什么？',
        'answer': '先确认验收条件再动手。', 'category': 'project_convention', 'trigger': '修改界面时', 'action': 'draft'})
    result = qa_memory.list_items(service, {'project': 'evo'})
    groups = [g for g in result['items'] if g['question'] == '界面调整前先确认什么？']
    assert len(groups) == 1
    group = groups[0]
    assert len(group['source_ids']) == 2
    assert set(group['reason_codes']) == {'evidence', 'other'}
    assert len(group['reason_codes']) == len(set(group['reason_codes']))
    assert counts(result) == {'evidence': 1, 'other': 1}
    assert qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence'})['total'] == 1
    assert qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'other'})['total'] == 1
    assert qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'conflict'})['total'] == 0


def test_reason_counts_are_computed_before_reason_filter_and_pagination(service):
    from evolvmem import qa_memory
    for index in range(3):
        extracted_qa(service, question=f'调研步骤 {index} 如何开始？', answer=f'调研先比较三种方案 {index}。',
                     basis='inferred', key=f'p{index}', session=f'p{index}')
    result = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence'})
    assert counts(result) == {'evidence': 3}
    assert result['total'] == 3
    page1 = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence', 'page': 1, 'page_size': 2})
    page2 = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence', 'page': 2, 'page_size': 2})
    assert page1['total'] == 3 and len(page1['items']) == 2
    assert page2['total'] == 3 and len(page2['items']) == 1
    empty = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'conflict'})
    assert empty['total'] == 0
    assert counts(empty) == {'evidence': 3}


def test_reason_filter_recounts_sources_after_filter(service):
    from evolvmem import qa_memory
    # 一个有效问答（无待确认原因）+ 一个缺证据候选，筛选 evidence 后只统计被显示组的来源
    extracted_qa(service, question='测试一该先确认什么？', answer='测试一先确认验收条件。',
                 key='active-one', session='active-one')
    extracted_qa(service, question='测试二该先确认什么？', answer='测试二先确认验收条件。',
                 basis='inferred', key='candidate-one', session='candidate-one')
    assert qa_memory.list_items(service, {'project': 'evo'})['record_count'] == 2
    result = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence'})
    assert result['total'] == 1
    assert result['record_count'] == sum(len(g['source_ids']) for g in result['items']) == 1
    # 计数仍按原因筛选前的显示组统计（有效组无待确认原因，不计入）
    assert counts(result) == {'evidence': 1}
    page = qa_memory.list_items(service, {'project': 'evo', 'reason_code': 'evidence',
                                          'page': 1, 'page_size': 1})
    assert page['record_count'] == 1


def test_source_priority_never_reports_verification(service):
    from evolvmem import qa_memory
    row = {'content_type': 'experience', 'experience_payload': {'proof': 'present'},
           'status': 'active', 'success_count': 1, 'ingestion_reason': '旧入库说明'}
    result = qa_memory._review_reason(row, '来源暂存、归属已变更，或方法尚无验证依据，暂不用于召回',
                                      effective=False, priority='source')
    assert result['code'] == 'source'
    assert result['label'] == '来源待确认'
    assert result['detail'] == '来源暂存、归属已变更，或方法尚无验证依据，暂不用于召回'


def test_verified_experience_withdrawn_is_source_not_verification(service):
    from evolvmem import qa_memory, unit_derivations
    case = service.experiences().record({'project': 'evo', 'problem': '已验证的方法被撤下后如何标记？',
        'conditions': {'场景': '来源撤下'}, 'steps': ['先核对实际结果']})
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_items SET status='active',success_count=1 WHERE id=?", (case['id'],))
    unit_derivations.supersede(service, case['id'], '归属已撤回，先停止展示')
    row = qa_memory.detail(service, case['id'])
    assert row['status'] == 'candidate'
    assert row['review_reason']['code'] == 'source'
    assert row['reason'] == '来源暂存、归属已变更，或方法尚无验证依据，暂不用于召回'
