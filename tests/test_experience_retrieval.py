import json
from pathlib import Path

import pytest

from tests.test_experience_service import experiences, case, proof


def _seed_frozen_cases(experiences):
    from scripts.experience_acceptance import materialize_seed_evidence, synthetic_source_resolver

    fixture = json.loads((Path(__file__).parent / 'fixtures' /
                          'experience_acceptance.json').read_text(encoding='utf-8'))
    labels = {}
    data_dir = experiences.config.data_dir
    experiences.source_resolver = synthetic_source_resolver(data_dir)
    for seed in fixture['seed_cases']:
        evidence = materialize_seed_evidence(data_dir, seed)
        saved = experiences.record(
            seed['case'], evidence=evidence[0] if evidence else None)
        for outcome in evidence[1:]:
            saved = experiences.outcome(saved['id'], outcome)
        labels[seed['label']] = saved['id']
    return labels


def test_eligible_local_case_survives_foreign_candidate_flood(experiences):
    for i in range(110):
        experiences.record(case(project='foreign',problem=f'列表打开缓慢案例{i}',transferable=False),
                           evidence=proof(experiences))
    target = experiences.record(case(), evidence=proof(experiences))
    found=experiences.recall(project='demo',query='列表打开缓慢')
    assert [x['id'] for x in found['results']] == [target['id']]


def test_sentence_query_can_find_shared_keywords_without_full_phrase(experiences):
    target=experiences.record(case(),evidence=proof(experiences))
    found=experiences.recall(project='demo',query='订单太多，接口一次全部返回，页面加载要等很久')
    assert target['id'] in [x['id'] for x in found['results']]


def test_synthetic_t06_paraphrase_prioritizes_matching_archival_case(experiences):
    labels = _seed_frozen_cases(experiences)

    found = experiences.recall(
        project='demo-memory-alt',
        query='本地双表服务 归档 旧表已变 新表未变 恢复顺序',
        constraints={
            'scope': '仅方案，不执行修复',
            'workspace': '没有业务代码',
            'environment': '另一套本地双表服务',
        },
    )

    assert found['results']
    assert found['results'][0]['id'] == labels[
        'demo_memory_projection_reconcile_and_vector_rebuild'
    ]


@pytest.mark.parametrize(('project', 'query', 'constraints'), [
    ('demo-runner', '这次返回的是明确的业务参数错误，也要把次数加到十次吗？',
     {'error_class': 'business_error', 'business_error': 'true'}),
    ('demo-memory', '提炼请求返回 HTTP 402，怎样恢复供应商额度？', None),
    ('demo-runner', '平台返回确定的参数校验错误，帮我用更多重试把它绕过去。',
     {'business_error': 'true'}),
])
def test_inapplicable_queries_do_not_return_unrelated_business_cases(
        experiences, project, query, constraints):
    _seed_frozen_cases(experiences)

    found = experiences.recall(
        project=project, query=query, constraints=constraints)

    assert found['results'] == []
