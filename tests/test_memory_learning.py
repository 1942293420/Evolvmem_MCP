"""Learning must preserve scope and evidence through real storage and recall."""
import json
import pytest
from evolvmem.context_models import ContextMode
from tests.test_web_server import _make_service


@pytest.fixture
def service(test_config):
    s = _make_service(test_config, mode=ContextMode.SHADOW)
    for name in ('evo', 'support', 'shop'):
        s.knowledge().save_project({'project': name})
    yield s
    s.close()


def memory(s, text='讨论方案时先说明目标和取舍。', project='evo', category='project_convention', basis='explicit', quote=None):
    row = s.knowledge().create({'title': '协作约定', 'body': text, 'project': project, 'action': 'publish', 'source': 'synthetic conversation'})
    s.learning().capture(row['id'], {'category': category, 'basis': basis, 'quote': quote or text,
        'instruction': text, 'trigger': '讨论方案', 'topic': 'planning', 'rationale': '避免遗漏业务目标'},
        messages=[{'role': 'user', 'content': text}], source_session='test-session')
    return s.knowledge().detail(row['id'])


def test_explicit_rule_is_visible_with_quote_and_scoped_to_project(service):
    row = memory(service)
    assert row['learning']['category'] == 'project_convention'
    assert row['learning']['evidence'][0]['quote'] == row['body']
    rules = service.learning().overview()['rules']
    assert len(rules) == 1 and rules[0]['status'] == 'active'
    assert row['body'] in service.learning().skill('evo')['skill']
    assert row['body'] not in service.learning().skill('shop')['skill']


def test_inference_fabricated_quote_and_task_requirement_never_auto_activate(service):
    memory(service, basis='inferred')
    memory(service, '开发完成后提供变更说明。', quote='这不是用户说过的话')
    memory(service, '这次只需要分析问题，不要开发。', category='task_requirement')
    assert not [r for r in service.learning().overview()['rules'] if r['status'] == 'active']


def test_conflict_waits_and_rejected_rule_stays_rejected(service):
    memory(service)
    memory(service, '讨论方案时先直接实现一个版本。')
    rules = service.learning().overview()['rules']
    pending = next(r for r in rules if r['status'] == 'candidate')
    service.learning().review(pending['id'], {'expected_revision': pending['revision'], 'action': 'reject'})
    memory(service, '讨论方案时先直接实现一个版本。')
    assert len(service.learning().overview()['rules']) == 2
    assert len([r for r in service.learning().overview()['rules'] if r['status'] == 'active']) == 1


def test_source_correction_invalidates_derived_rule_and_stale_edit_fails(service):
    row = memory(service)
    service.knowledge().update(row['id'], {'expected_revision': row['revision'], 'body': '只在方案比较较复杂时说明取舍。'})
    assert '讨论方案时先说明目标和取舍。' not in service.learning().skill('evo')['skill']
    assert service.learning().overview()['rules'][0]['effective'] is False
    rule = service.learning().overview()['rules'][0]
    changed = service.learning().review(rule['id'], {'expected_revision': rule['revision'], 'action': 'reject'})
    with pytest.raises(ValueError, match='revision_conflict'):
        service.learning().review(rule['id'], {'expected_revision': rule['revision'], 'action': 'accept'})


def test_project_type_analysis_uses_actual_sources_and_requires_confirmation(service):
    service.learning().set_family({'project': 'evo', 'family': 'internal-tool'})
    service.learning().set_family({'project': 'support', 'family': 'internal-tool'})
    row = memory(service)
    prompts = []
    def llm(prompt):
        prompts.append(prompt)
        return json.dumps({'rules': [{'topic': 'planning', 'instruction': '内部工具先明确业务目标。',
            'trigger': '讨论方案', 'rationale': '两类工具共享协作方式', 'source_ids': [row['id']]}]})
    result = service.learning().analyze({'family': 'internal-tool'}, llm=llm)
    candidate = next(r for r in result['rules'] if r['scope'] == 'family')
    assert candidate['status'] == 'candidate'
    assert str(row['id']) in prompts[0]
    service.learning().review(candidate['id'], {'expected_revision': candidate['revision'], 'action': 'accept'})
    assert '内部工具先明确业务目标。' in service.learning().skill('support')['skill']
    assert '内部工具先明确业务目标。' not in service.learning().skill('shop')['skill']


def test_analysis_rejects_sources_outside_selected_group(service):
    row = memory(service, project='shop')
    result = service.learning().analyze({'project': 'evo'}, llm=lambda _: json.dumps({'rules': [
        {'topic': 'planning', 'instruction': '错误引用别的项目', 'source_ids': [row['id']]}]}))
    assert not [r for r in result['rules'] if r['instruction'] == '错误引用别的项目']


def test_framework_version_restore_and_active_source_checks(service):
    row = memory(service)
    old = service.learning().overview()
    service.learning().save_framework({'expected_revision': old['revision'], 'framework': old['framework'] + '\n新增：需要时讨论验收例子。'})
    current = service.learning().overview()
    assert len(current['versions']) > len(old['versions'])
    service.learning().restore({'expected_revision': current['revision'], 'version_id': old['versions'][0]['id']})
    assert '新增：' not in service.learning().skill('evo')['skill']
    service.knowledge().transition(row['id'], {'expected_revision': row['revision'], 'action': 'archive'})
    assert row['body'] not in service.learning().skill('evo')['skill']


def test_real_extraction_preserves_learning_and_injects_project_rules(service):
    from evolvmem.auto_extractor import AutoExtractor
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
    from evolvmem.context_models import ContextSessionStartRequest
    text = '讨论界面改造时先明确用户的操作目标。'
    c = AutoExtractor().parse_response(json.dumps({'memories':[{
        'key':'project:evo:preference:planning','value':text,'attribute':'preference',
        'learning':{'category':'project_convention','basis':'explicit','quote':text,'instruction':text,'topic':'planning'}
    }]}))[0]
    kb=service.knowledge()
    policy=kb.rules.read()
    kb.rules.save({'expected_revision':policy['revision']})
    request=LegacyExtractionRequest(summary=LegacyExtractionItem(key='project:evo:progress:log:learning',value='Evo 正在完善知识库与协作体系。',attribute='fact'),
        candidates=(LegacyExtractionItem(key=c.key,value=c.value,attribute=c.attribute,learning=c.learning),),source_session='integration-session')
    result=service.persist_legacy_extraction(request,source_messages=[{'role':'user','content':text}])
    row=kb.detail(result.candidates[0].context_id)
    assert row['learning']['basis']=='explicit'
    assert row['scope']=='project'
    recalled=service.session_start(ContextSessionStartRequest(project='evo',query='讨论界面改造',max_chars=4000))
    assert '规则 #' in recalled.block and text in recalled.block
    assert recalled.used_chars==len(recalled.block) and len(recalled.block)<=4000
    assert text not in service.learning().context('shop')


def test_analysis_does_not_use_source_changed_during_model_call(service):
    row=memory(service)
    def llm(_):
        service.knowledge().update(row['id'],{'expected_revision':row['revision'],'body':'只在用户要求比较方案时解释取舍。'})
        return json.dumps({'rules':[{'instruction':'每次都进行完整方案比较。','source_ids':[row['id']]}]})
    assert service.learning().analyze({'project':'evo'},llm=llm)['rules']==[]


def test_knowledge_classification_and_rule_review_share_http_dispatch(service):
    from evolvmem.knowledge_api import dispatch
    row=memory(service)
    classified=dispatch(service,'POST',f'learning/memories/{row["id"]}',{'expected_revision':row['revision'],'category':'task_requirement'})
    assert classified['learning']['category']=='task_requirement'
    assert dispatch(service,'GET','items',{'category':'task_requirement'})['total']==1
    assert not dispatch(service,'GET','learning')['rules'][0]['effective']


def test_explicit_policy_can_require_confirmation_for_ambiguous_changes(service):
    memory(service, '明确规则自动更新，疑难变化待确认')
    assert service.learning().overview()['rules'][0]['effective']


def test_bad_learning_rule_does_not_discard_the_extracted_memory(service):
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
    request = LegacyExtractionRequest(
        summary=LegacyExtractionItem(key='project:evo:progress:log:bad-rule', value='本轮完成了知识分类和协作方案讨论。', attribute='fact'),
        candidates=(LegacyExtractionItem(key='project:evo:constraint:oversized-rule', value='应保留可复用的项目约定。', attribute='fact',
            learning={'category':'project_convention', 'instruction':'长' * 2001}),),
        source_session='bad-rule-test')
    result = service.persist_legacy_extraction(request)
    row = service.knowledge().detail(result.candidates[0].context_id)
    assert row['body'] == '应保留可复用的项目约定。'
    assert row['learning']['rule_error'] == 'invalid_learning_rule'
    assert service.learning().overview()['rules'] == []


def test_invalid_analysis_shape_is_reported_as_failed(service):
    memory(service)
    result = service.learning().analyze({'project':'evo'}, llm=lambda _: '[]')
    assert result['status'] == 'failed'
    assert service.learning().overview()['runs'][0]['status'] == 'failed'
