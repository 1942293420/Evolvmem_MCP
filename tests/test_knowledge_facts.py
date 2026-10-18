"""Fact organization must never grant ownership or discard original claims."""
import json
import pytest
from urllib.parse import urlsplit, parse_qs

from tests.test_cross_batch_context import config, service, SESSION, OTHER_SESSION, THIRD_SESSION
from tests.test_knowledge_identity import material, save, view
from evolvmem import auto_organization as org
from evolvmem.knowledge_api import dispatch


def api(service, route='', body=None):
    url = urlsplit('facts' + route)
    return dispatch(service, 'GET' if body is None else 'POST', url.path,
                    {k: v[0] for k, v in parse_qs(url.query).items()} if body is None else body)


def add(service, value='Windows', *, condition='生产', since='', session=SESSION, relations=None, exclusive=True, subject="Demo 执行器"):
    text = f'{subject}在{condition}使用 {value}。' + (f'生效日期 {since}。' if since else '')
    task, unit = material(service, text=text, session=session)
    fact = dict(exclusive=exclusive, subject=subject, property='运行系统', value=value,
                conditions={'环境': condition}, valid_from=since, valid_until='',
                evidence=[{'quote': text, 'role': 'user'}])
    full = org.find_unit(service, task['id'], unit['digest'])
    full['source_ranges'] = json.loads(full['source_ranges'])
    full['ownership_analysis'] = json.loads(full['ownership_analysis'])
    full['task_record'] = {'facts': [fact], 'relations': relations or []}
    org.OrganizationWorker(service.config)._store_units(service, org._task_row(service, task['id']), [full])
    return task, view(service, task['id'])[0]


def test_conditions_coexist_and_query_cannot_mix_environments(service):
    add(service, 'Linux', condition='Linux 主机')
    add(service, 'Windows', condition='Windows 主机', session=OTHER_SESSION)
    result = api(service, '?conditions=' + json.dumps({'环境': 'Linux 主机'}))
    assert [v['value'] for f in result['items'] for v in f['versions']] == ['Linux']
    assert api(service)['issues'] == []
    assert all(u['decision'] == 'review' for u in org.unit_views(service, 1))


def test_repeated_conflict_one_issue_and_human_resolution_survives_replay(service):
    first, u = add(service, 'Windows')
    saved = save(service, first['id'])
    add(service, 'Linux', session=OTHER_SESSION)
    add(service, 'Linux', session=THIRD_SESSION)
    data = api(service)
    assert len(data['items']) == len(data['issues']) == 1
    fact = data['items'][0]
    assert len(fact['versions']) == 2
    linux = next(v for v in fact['versions'] if v['value'] == 'Linux')
    assert len(linux['sources']) == 2
    issue = data['issues'][0]
    api(service, '/resolve', dict(id=issue['id'], expected_revision=issue['revision'], action='choose', version_id=linux['id']))
    add(service, 'Linux', session=THIRD_SESSION)
    result = api(service)
    assert result['issues'] == []
    assert result['items'][0]['current']['value'] == 'Linux'
    assert view(service, first['id'])[0]['project'] == 'demo'
    assert view(service, first['id'])[0]['knowledge_id'] == saved['knowledge_id']
    with pytest.raises(ValueError, match='revision_conflict'):
        api(service, '/resolve', dict(id=issue['id'], expected_revision=issue['revision'], action='defer'))


def test_late_old_version_does_not_replace_confirmed_new_version(service):
    add(service, 'Windows', since='2026-10-10')
    fact = api(service)['items'][0]
    api(service, '/confirm', {'id': fact['id'], 'expected_revision': fact['revision'], 'version_id': fact['versions'][0]['id']})
    add(service, 'Linux', since='2026-09-01', session=OTHER_SESSION)
    assert api(service)['items'][0]['current']['value'] == 'Windows'
    old = api(service, '?at=2026-09-20')['items'][0]
    assert old['current']['value'] == 'Linux'
    assert old['current']['sources']
    assert api(service)['issues'] == []


def test_splitting_suggested_group_preserves_sources_and_human_projects(service):
    first, _ = add(service, 'Windows')
    saved1 = save(service, first['id'])
    second, _ = add(service, 'Linux', session=OTHER_SESSION)
    saved2 = save(service, second['id'], project='other')
    fact = api(service)['items'][0]
    api(service, '/split', {'id': fact['id'], 'knowledge_id': saved2['knowledge_id'], 'expected_revision': fact['revision']})
    add(service, 'Linux', session=OTHER_SESSION)
    result = api(service)
    assert len(result['items']) == 2
    assert result['issues'] == []
    assert {v['value'] for f in result['items'] for v in f['versions']} == {'Linux', 'Windows'}
    assert view(service, first['id'])[0]['project'] == 'demo'
    assert view(service, second['id'])[0]['project'] == 'other'
    assert saved1['knowledge_id'] != saved2['knowledge_id']


def test_model_relations_never_create_project_ownership(service):
    text = 'Demo 执行器在生产使用 Windows。'
    _, u = add(service, relations=[
        {'type': '修改', 'target': 'Demo 执行器', 'quote': text},
        {'type': '验证', 'target': 'other', 'quote': text},
        {'type': '属于项目', 'target': 'other', 'quote': text}])
    data = api(service)
    assert {r['type'] for r in data['items'][0]['relations']} == {'修改', '验证'}
    assert all(r['state'] == 'suggested' for r in data['items'][0]['relations'])
    assert u['project'] == '' and u['decision'] == 'review'
    assert api(service, '?project=other')['items'] == []


def test_unlocatable_fact_evidence_is_not_saved(service):
    task, u = material(service)
    full = org.find_unit(service, task['id'], u['digest'])
    full['source_ranges'] = json.loads(full['source_ranges'])
    full['ownership_analysis'] = json.loads(full['ownership_analysis'])
    full['task_record'] = {'facts': [dict(subject='Demo', property='运行系统', value='Linux',
                                       conditions={}, evidence=[{'quote': '并不存在的原话'}])]}
    org.OrganizationWorker(service.config)._store_units(service, org._task_row(service, task['id']), [full])
    assert api(service)['items'] == []


def test_conditions_resolution_keeps_original_claims_and_no_repeat_issue(service):
    add(service, 'Windows')
    add(service, 'Linux', session=OTHER_SESSION)
    data = api(service); issue = data['issues'][0]
    mapping = {v['id']: {'环境': v['value']+' 主机'} for v in data['items'][0]['versions']}
    api(service, '/resolve', dict(id=issue['id'], expected_revision=issue['revision'], action='conditions', conditions=mapping))
    add(service, 'Linux', session=OTHER_SESSION)
    result = api(service, '?conditions='+json.dumps({'环境':'Linux 主机'}))
    assert result['issues'] == []
    assert result['items'][0]['current']['value'] == 'Linux'
    assert result['items'][0]['current']['original_conditions'] == {'环境':'生产'}


def test_future_dated_claim_is_not_current(service):
    add(service, 'Windows', since='2099-01-01')
    assert api(service)['items'][0]['current'] is None


def test_real_organization_parser_carries_checked_facts_and_roles(service):
    from evolvmem.task_narrative import organize
    text = 'Demo 执行器修改启动方式，使用 other 项目验证。'
    def model(prompt):
        return json.dumps(dict(requirement=None, analysis=None, steps=[], result=None,
            outstanding=[], state='open', facts=[dict(subject='Demo 执行器', property='修改对象',
            value='启动方式', conditions={}, evidence=[{'source':'e1','quote':text}])],
            relations=[dict(type='修改',target='Demo 执行器',evidence=[{'source':'e1','quote':text}]),
                       dict(type='验证',target='other',evidence=[{'source':'e1','quote':text}])]))
    record = organize([{'role':'user','content':text}], model)
    task, u = material(service, text=text)
    full = org.find_unit(service, task['id'], u['digest'])
    full.update(source_ranges=json.loads(full['source_ranges']), ownership_analysis={}, task_record=record)
    org.OrganizationWorker(service.config)._store_units(service, org._task_row(service, task['id']), [full])
    data = api(service)
    assert data['items'][0]['versions'][0]['sources'][0]['role'] == 'user'
    assert {r['type'] for r in data['items'][0]['relations']} == {'修改','验证'}
    assert view(service,task['id'])[0]['project'] == ''


def test_evidence_counts_real_events_not_repeated_narration(service, tmp_path):
    from evolvmem.experience_sources import ExperienceSourceResolver
    from evolvmem.experience_service import ExperienceService
    from tests.test_experience_service import case
    from evolvmem.unit_derivations import record_derivation
    task, u = add(service, 'Windows')
    archived = save(service, task['id'])
    root = tmp_path/'proof'; root.mkdir()
    exp = ExperienceService(service, source_resolver=ExperienceSourceResolver(codex_roots=(root,)))
    proof = 'Demo 执行器在生产使用 Windows。'
    evidence = dict(task_id='fact-task-1',event_id='check-1',outcome='success',level='technical',
                    source_kind='tool_result',quote=proof,note=proof,conditions={'环境':'生产'})
    for i in (1,2):
        path = root/f'rollout-2026-fact-task-{i}.jsonl'
        path.write_text(json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':proof}})+'\n')
        saved = exp.record(case(problem='Demo 执行器系统核验',conditions={'环境':'生产'}), evidence={**evidence,'task_id':f'fact-task-{i}','source_ref':str(path)+'#1'})
    with service.store.transaction():
        record_derivation(service,task['id'],u['digest'],saved['id'],kind='knowledge',project='demo')
    first = api(service)['items'][0]['versions'][0]
    assert first['verification_count'] == 2
    for session in (OTHER_SESSION,THIRD_SESSION):
        add(service,'Windows',session=session)
    # Rephrase the same verification event with a new caller event label.
    exp.outcome(saved['id'], {**evidence,'event_id':'retold-again','source_ref':str(root/'rollout-2026-fact-task-1.jsonl')+'#1'})
    result = api(service)['items'][0]['versions'][0]
    assert result['verification_count'] == 2
    assert len(result['sources']) == 3
    assert result['state'] == 'suggested'  # Archiving is not fact confirmation.


def test_nonexclusive_facts_are_not_conflicts(service):
    add(service, 'Windows', exclusive=False)
    add(service, 'Linux', session=OTHER_SESSION, exclusive=False)
    assert api(service)['issues'] == []


def test_confirmations_are_independent_for_different_conditions(service):
    add(service,'Windows',condition='桌面')
    add(service,'Linux',condition='服务器',session=OTHER_SESSION)
    fact = api(service)['items'][0]
    for v in fact['versions']:
        current = api(service)['items'][0]
        api(service,'/confirm',dict(id=current['id'],expected_revision=current['revision'],version_id=v['id']))
    result = api(service)['items'][0]
    assert all(v['state']=='confirmed' for v in result['versions'])


def test_timestamp_is_not_invented_and_overlapping_claims_need_resolution(service):
    task,u = add(service,'Windows',since='2026-09-01')
    full = org.find_unit(service,task['id'],u['digest'])
    record=json.loads(full['task_record']);record['facts'][0]['valid_from']='2099-01-01'
    full.update(task_record=record,source_ranges=json.loads(full['source_ranges']),ownership_analysis={})
    org.OrganizationWorker(service.config)._store_units(service,org._task_row(service,task['id']),[full])
    assert len(api(service)['items'][0]['versions'])==1


def test_failure_during_fact_decision_rolls_back_human_and_fact_ledgers(service, monkeypatch):
    from evolvmem import knowledge_facts
    first,_ = add(service,'Windows');save(service,first['id'])
    add(service,'Linux',session=OTHER_SESSION)
    before=api(service);issue=before['issues'][0]
    monkeypatch.setattr(knowledge_facts,'_log',lambda *a: (_ for _ in ()).throw(RuntimeError('simulated disk failure')))
    with pytest.raises(RuntimeError):
        api(service,'/resolve',dict(id=issue['id'],expected_revision=issue['revision'],action='choose',version_id=before['items'][0]['versions'][0]['id']))
    assert api(service)==before
    assert view(service,first['id'])[0]['project']=='demo'


def test_ownership_reasoning_publishes_modification_and_validation_links_only(service):
    from evolvmem import ownership_reasoning as reasoning
    task,u=add(service,'Windows')
    full=org.find_unit(service,task['id'],u['digest'])
    quote='Demo 执行器在生产使用 Windows。'
    analysis=dict(version=reasoning.VERSION,text_hash=reasoning.digest(full['text']),
        chain={'change':quote,'result':quote},targets=[{'project':'demo','role':'owner','quote':quote},
        {'project':'other','role':'related','quote':quote}])
    org.OrganizationWorker(service.config)._update_unit(service,task['id'],u['digest'],project='',decision='review',ownership_analysis=analysis)
    result=api(service)['items'][0]
    assert {(r['type'],r['target']) for r in result['relations']}=={('修改','demo'),('验证','other')}
    assert view(service,task['id'])[0]['project']==''


def test_inferred_end_date_cannot_erase_an_evidenced_old_fact(service):
    text='Demo 执行器在生产使用 Linux，生效日期 2026-09-01。'
    task,u=material(service,text=text)
    full=org.find_unit(service,task['id'],u['digest'])
    record={'facts':[dict(subject='Demo 执行器',property='系统',value='Linux',conditions={'环境':'生产'},
        valid_from='2026-09-01',valid_until='2026-10-10',evidence=[{'quote':text}])]}
    full.update(task_record=record,source_ranges=json.loads(full['source_ranges']),ownership_analysis={})
    org.OrganizationWorker(service.config)._store_units(service,org._task_row(service,task['id']),[full])
    assert len(api(service)['items'][0]['versions'])==1
    assert api(service)['items'][0]['versions'][0]['valid_until']==''


def test_relations_are_attached_to_their_subject_not_every_fact_in_task(service):
    task,u=material(service,text='Demo 执行器使用 Windows。Demo 浏览器使用 Chromium。')
    full=org.find_unit(service,task['id'],u['digest'])
    full.update(source_ranges=json.loads(full['source_ranges']),ownership_analysis={},task_record={
        'facts':[dict(subject='Demo 执行器',property='运行系统',value='Windows',evidence=[{'quote':'Demo 执行器使用 Windows。'}]),
                 dict(subject='Demo 浏览器',property='内核',value='Chromium',evidence=[{'quote':'Demo 浏览器使用 Chromium。'}])],
        'relations':[dict(subject='Demo 浏览器',type='使用',target='Chromium',quote='Demo 浏览器使用 Chromium。')]})
    org.OrganizationWorker(service.config)._store_units(service,org._task_row(service,task['id']),[full])
    data=api(service)['items']
    assert next(f for f in data if f['subject']=='Demo 执行器')['relations']==[]
    assert next(f for f in data if f['subject']=='Demo 浏览器')['relations'][0]['target']=='Chromium'


def test_many_conditions_do_not_display_one_as_the_universal_current_fact(service):
    add(service,'Windows',condition='桌面');add(service,'Linux',condition='服务器',session=OTHER_SESSION)
    fact=api(service)['items'][0]
    for v in fact['versions']:
        now=api(service)['items'][0]
        api(service,'/confirm',dict(id=now['id'],expected_revision=now['revision'],version_id=v['id']))
    assert api(service)['items'][0]['current'] is None
    assert api(service,'?conditions='+json.dumps({'环境':'桌面'}))['items'][0]['current']['value']=='Windows'


def test_new_effective_version_needs_choice_and_preserves_prior_confirmation(service):
    add(service,'Linux',since='2026-09-01')
    f=api(service)['items'][0];old=f['versions'][0]
    api(service,'/confirm',dict(id=f['id'],expected_revision=f['revision'],version_id=old['id']))
    add(service,'Windows',since='2026-10-10',session=OTHER_SESSION)
    data=api(service);f=data['items'][0];issue=data['issues'][0]
    assert issue['kind']=='change' and f['current']['value']=='Linux'
    new=next(v for v in f['versions'] if v['value']=='Windows')
    api(service,'/resolve',dict(id=issue['id'],expected_revision=issue['revision'],action='choose',version_id=new['id']))
    assert api(service)['items'][0]['current']['value']=='Windows'
    past=api(service,'?at=2026-09-20')['items'][0]['current']
    assert past['value']=='Linux' and past['state']=='previously_confirmed'


def test_relation_only_subject_is_queryable_without_inventing_another_fact(service):
    text='Demo 自动化平台修改启动能力，并用 Demo 蓝鲸验证。'
    task,u=material(service,text=text)
    full=org.find_unit(service,task['id'],u['digest'])
    full.update(source_ranges=json.loads(full['source_ranges']),ownership_analysis={},task_record={
        'facts':[], 'relations':[dict(subject='Demo 自动化平台',type='验证',target='Demo 蓝鲸',evidence=[{'quote':text}])]})
    org.OrganizationWorker(service.config)._store_units(service,org._task_row(service,task['id']),[full])
    data=api(service)
    assert any(r['type']=='验证' and r['target']=='Demo 蓝鲸' for f in data['items'] for r in f['relations'])
    assert all(s['quote']==text for f in data['items'] for v in f['versions'] for s in v['sources'])


def test_model_comparison_flag_change_does_not_reopen_resolved_content(service):
    task,u=add(service,'Windows');add(service,'Linux',session=OTHER_SESSION)
    data=api(service);issue=data['issues'][0]
    api(service,'/resolve',dict(id=issue['id'],expected_revision=issue['revision'],action='defer'))
    full=org.find_unit(service,task['id'],u['digest']);record=json.loads(full['task_record'])
    record['facts'][0]['exclusive']=False
    full.update(source_ranges=json.loads(full['source_ranges']),ownership_analysis={},task_record=record)
    org.OrganizationWorker(service.config)._store_units(service,org._task_row(service,task['id']),[full])
    assert len(api(service)['items'][0]['versions'])==2
    assert api(service)['issues']==[]


def test_resolving_conditions_checks_new_overlap_and_keeps_current_choices(service):
    a,_=add(service,'Windows');b,_=add(service,'Linux',session=OTHER_SESSION)
    add(service,'macOS',condition='桌面',session=THIRD_SESSION)
    data=api(service);f=data['items'][0];issue=data['issues'][0]
    members=[v for v in f['versions'] if v['id'] in json.loads(issue['members_json'])]
    api(service,'/resolve',dict(id=issue['id'],expected_revision=issue['revision'],action='conditions',
        conditions={v['id']:{'环境':'桌面' if v['value']=='Windows' else '服务器'} for v in members}))
    data=api(service)
    assert len(data['issues'])==1
    values={v['value'] for v in data['items'][0]['versions'] if v['id'] in json.loads(data['issues'][0]['members_json'])}
    assert values=={'Windows','macOS'}
    with pytest.raises(ValueError,match='revision_conflict'):
        api(service,'/confirm',{'id':f['id'],'expected_revision':None,'version_id':members[0]['id']})
