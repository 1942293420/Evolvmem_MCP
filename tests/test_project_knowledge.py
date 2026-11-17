"""The project directory and Agent recall read the same admitted knowledge."""
import json
from tests.test_knowledge_facts import config, service, add, api, save, SESSION, OTHER_SESSION, THIRD_SESSION
from tests.test_knowledge_identity import material
from evolvmem.knowledge_api import dispatch
from evolvmem.memory_recall import recall


def directory(service, **options):
    return dispatch(service, 'GET', 'directory', {'project': 'demo', **options})


def test_directory_and_recall_share_archived_facts_sources_and_relations(service):
    task, _ = add(service, relations=[{'type': '验证', 'target': 'other', 'quote': 'Demo 执行器在生产使用 Windows。'}])
    assert directory(service)['items'] == []
    saved = save(service, task['id'])
    page = directory(service)
    agent = recall(service, {'project': 'demo', 'kind': 'knowledge', 'query': ''})
    assert agent['knowledge'] == page['items']
    item = page['items'][0]
    assert item['current'][0]['value'] == 'Windows'
    assert item['versions'][0]['sources'][0]['quote'] == 'Demo 执行器在生产使用 Windows。'
    assert item['item_ids'] == [saved['item_id']]
    assert item['section'] == '运行与部署'
    assert item['relations'][0]['type'] == '验证'
    assert directory(service, project='other')['items'] == []
    assert 'Windows' in agent['block'] and '验证' in agent['block']


def test_new_unarchived_conflict_is_warning_not_current_project_knowledge(service):
    task, _ = add(service)
    save(service, task['id'])
    add(service, 'Linux', session=OTHER_SESSION)
    item = directory(service)['items'][0]
    assert [v['value'] for v in item['versions']] == ['Windows']
    assert item['warnings']
    assert item['alternatives'][0]['value'] == 'Linux'
    assert item['alternatives'][0]['archive_state'] == 'not_in_project'
    assert item['alternatives'][0]['sources'][0]['quote']
    agent = recall(service, {'project': 'demo', 'kind': 'knowledge'})
    assert '未归入本项目' in agent['block'] and '分歧' in agent['block']


def test_time_and_conditions_preserve_current_confirmed_and_history(service):
    first, _ = add(service, 'Windows', since='2026-10-10')
    save(service, first['id'])
    fact = api(service)['items'][0]
    api(service, '/confirm', {'id': fact['id'], 'expected_revision': fact['revision'], 'version_id': fact['versions'][0]['id']})
    old, _ = add(service, 'Linux', since='2026-09-01', session=OTHER_SESSION)
    save(service, old['id'])
    assert directory(service)['items'][0]['current'][0]['value'] == 'Windows'
    assert directory(service, at='2026-09-20')['items'][0]['current'][0]['value'] == 'Linux'
    other, _ = add(service, 'Mac', condition='测试', session=THIRD_SESSION)
    save(service, other['id'])
    assert {v['value'] for v in directory(service)['items'][0]['current']} == {'Windows', 'Mac'}
    assert {v['value'] for v in directory(service, conditions={'环境':'测试'})['items'][0]['versions']} == {'Mac'}


def test_withdrawal_and_project_correction_remove_directory_and_recall(service):
    task, _ = add(service)
    save(service, task['id'])
    assert directory(service)['total'] == 1
    save(service, task['id'], action='defer')
    assert directory(service)['total'] == 0
    assert recall(service, {'project':'demo', 'kind':'knowledge'})['knowledge'] == []
    save(service, task['id'], project='other')
    assert directory(service)['total'] == 0
    assert directory(service, project='other')['total'] == 1


def test_unenriched_human_archive_visible_without_model_or_qa(service):
    task, _ = material(service, text='Demo 归档必须由用户选择项目后保存。', category='project_convention')
    unit = save(service, task['id'])
    page = directory(service)
    assert page['total'] == 1
    item = page['items'][0]
    assert item['kind'] == 'material' and item['item_ids'] == [unit['item_id']]
    assert item['section'] == '业务规则'
    assert item['sources']
    agent = recall(service, {'project':'demo','kind':'knowledge','query':'用户选择项目'})
    assert agent['knowledge'] == page['items']
    assert agent['used_chars'] <= 4000


def test_retriever_exposes_same_fact_state_not_unqualified_old_summary(service):
    from evolvmem.context_models import ContextSearchRequest
    first, _ = add(service)
    saved = save(service, first['id'])
    second, _ = add(service, 'Linux', session=OTHER_SESSION)
    save(service, second['id'])
    results = service.retriever.search(ContextSearchRequest(query='Demo',project='demo'))
    hit = next(r for r in results if r.id == saved['item_id'] or saved['item_id'] in r.duplicate_ids)
    assert hit.knowledge == tuple(directory(service)['items'])
    assert '分歧' in hit.l0


def test_unrelated_search_and_small_budget_never_emit_partial_conflict(service):
    for session, value in [(SESSION,'Windows'),(OTHER_SESSION,'Linux')]:
        task, _ = add(service, value, session=session)
        save(service, task['id'])
    assert directory(service,q='不存在的卫星')['items'] == []
    tiny = recall(service, {'project':'demo','kind':'knowledge','max_chars':40})
    assert tiny['knowledge'] == [] and tiny['used_chars'] <= 40
    normal = recall(service, {'project':'demo','kind':'knowledge'})
    assert 'Windows' in normal['block'] and 'Linux' in normal['block'] and '分歧' in normal['block']


def test_resolved_conflict_no_longer_warns_but_preserves_old_sources(service):
    for session, value in [(SESSION,'Windows'),(OTHER_SESSION,'Linux')]:
        task, _ = add(service, value, session=session)
        save(service, task['id'])
    data=api(service); issue=data['issues'][0]; v=data['items'][0]['versions'][0]
    api(service,'/resolve',{'id':issue['id'],'expected_revision':issue['revision'],'action':'choose','version_id':v['id']})
    item=directory(service)['items'][0]
    assert item['warnings'] == []
    assert len(item['versions']) == 2 and item['current'][0]['id'] == v['id']


def test_auto_recall_answers_current_knowledge_and_conditions_never_fall_back_to_raw_material(service):
    task, _ = add(service)
    save(service, task['id'])
    assert recall(service, {'project':'demo','query':'Demo 执行器怎么部署'})['knowledge']
    assert directory(service, conditions={'环境':'未登记环境'})['items'] == []
    assert not recall(service, {'project':'demo','kind':'knowledge','conditions':{'环境':'未登记环境'}})['knowledge']


def test_project_context_recall_carries_conflict_instead_of_old_raw_l1(service):
    from evolvmem.context_models import ContextSessionStartRequest
    for session, value in [(SESSION,'Windows'),(OTHER_SESSION,'Linux')]:
        task,_=add(service,value,session=session)
        save(service,task['id'])
    result=service.session_start(ContextSessionStartRequest(project='demo',query='Demo',max_chars=4000),project_only=True)
    assert '分歧' in result.block and 'Windows' in result.block and 'Linux' in result.block


def test_pending_ownership_cannot_enter_directory_or_knowledge_recall(service):
    task,_=add(service)
    saved=save(service,task['id'])
    with service.store.transaction():
        service.store._connection().execute("UPDATE context_project_resolutions SET review_state='pending', decision_source='automatic' WHERE item_id=?",(saved['item_id'],))
    assert directory(service)['items']==[]
    assert recall(service,{'project':'demo','kind':'knowledge'})['knowledge']==[]
    from evolvmem.context_models import ContextSearchRequest
    assert not service.retriever.search(ContextSearchRequest(project='demo',query='Demo'))


def test_content_choice_does_not_implicitly_archive_but_remains_visible_as_warning(service):
    first,_=add(service)
    save(service,first['id'])
    add(service,'Linux',session=OTHER_SESSION)
    data=api(service);issue=data['issues'][0]
    new=next(v for v in data['items'][0]['versions'] if v['value']=='Linux')
    api(service,'/resolve',{'id':issue['id'],'expected_revision':issue['revision'],'action':'choose','version_id':new['id']})
    item=directory(service)['items'][0]
    assert item['current'][0]['value']=='Windows'
    assert item['alternatives'][0]['value']=='Linux'
    assert item['warnings'][0]['kind']=='unarchived_choice'


def test_material_without_known_conditions_cannot_claim_to_match_specific_environment(service):
    task,_=material(service,text='Demo 部署在默认环境，未注明操作系统。')
    save(service,task['id'])
    assert directory(service)['total']==1
    assert directory(service,conditions={'系统':'Linux'})['total']==0
