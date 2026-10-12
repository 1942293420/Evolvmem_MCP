"""One authoritative knowledge item can serve several explicitly chosen projects."""
import pytest
from tests.test_review_groups import service, base_service, group_source, org
from tests.unit_model_fixture import model_for, run_worker
from evolvmem import qa_memory, memory_recall, history_memory


def recalled(service, project):
    return memory_recall.recall(service, {'project': project, 'kind': 'experience', 'query': '导出编号'})['qa']


def test_shared_qa_edit_keeps_one_id_in_both_projects(service):
    row = qa_memory.save(service, {'question': '导出编号怎么保留？', 'answer': '导出编号需要保留原始值。',
        'projects': ['evo', 'dsh'], 'knowledge_scope': 'project', 'action': 'publish'})
    assert row['projects'] == ['dsh', 'evo']
    assert [r['id'] for r in recalled(service, 'evo')] == [row['id']]
    assert [r['id'] for r in recalled(service, 'dsh')] == [row['id']]
    assert recalled(service, 'shop') == []
    changed = qa_memory.save(service, {'question': row['question'], 'answer': '导出编号需要保留原始值和前导零。',
        'expected_revision': row['revision'], 'action': 'publish'}, item_id=row['id'])
    for project in ('evo', 'dsh'):
        assert recalled(service, project)[0]['answer'] == changed['answer']
        assert qa_memory.list_items(service, {'project': project})['items'][0]['id'] == row['id']
    assert service.store._connection().execute('SELECT COUNT(*) FROM knowledge_qa').fetchone()[0] == 1


@pytest.mark.parametrize('scope,projects', [('global', []), ('project', ['evo', 'dsh'])])
def test_review_scope_survives_extraction_and_recall(service, monkeypatch, scope, projects):
    tid, source = group_source(service, monkeypatch, count=1)
    unit = org(service, '/units', {'task_id': tid})['items'][0]
    group = org(service, '/groups')['items'][0]
    preview = org(service, '/groups/preview', {'group_id': group['id'], 'selection': 'item',
        'task_id': tid, 'digest': unit['digest'], 'knowledge_scope': scope, 'projects': projects})
    assert preview['knowledge_scope'] == scope
    assert org(service, '/groups/apply', {'proposal': preview})['succeeded'] == 1
    run_worker(service, monkeypatch, model_for(service))
    saved = org(service, '/units', {'task_id': tid})['items'][0]
    assert saved['decision'] == 'manual' and saved['extraction_stage'] == 'done'
    results = {p: recalled(service, p) for p in ('evo', 'dsh', 'shop')}
    assert results['evo'] and results['dsh']
    assert results['evo'][0]['id'] == results['dsh'][0]['id']
    assert bool(results['shop']) == (scope == 'global')
    if scope == 'project':
        assert history_memory.sessions(service, 'evo')[0]['source_ids'] == history_memory.sessions(service, 'dsh')[0]['source_ids']
        assert history_memory.read(service, 'dsh', source.id)['available']


def test_empty_project_selection_is_not_general(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=1)
    unit = org(service, '/units', {'task_id': tid})['items'][0]
    with pytest.raises(ValueError, match='project_required'):
        org(service, '/groups/preview', {'group_id': org(service, '/groups')['items'][0]['id'],
            'selection': 'item', 'task_id': tid, 'digest': unit['digest'],
            'knowledge_scope': 'project', 'projects': []})


def test_shared_scope_conflict_and_removal_are_checked(service):
    first = qa_memory.save(service, {'question': '导出编号怎么保留？', 'answer': '导出编号保留十进制原始值。',
        'projects': ['evo','dsh'], 'knowledge_scope':'project', 'action':'publish'})
    with pytest.raises(ValueError, match='qa_conflict_requires_confirmation'):
        qa_memory.save(service, {'question': first['question'], 'answer':'导出编号改用十六进制表示。',
            'project':'evo', 'action':'publish'})
    changed = qa_memory.save(service, {'question':first['question'], 'answer':first['answer'],
        'projects':['evo'], 'knowledge_scope':'project', 'expected_revision':first['revision'], 'action':'publish'}, item_id=first['id'])
    assert recalled(service, 'dsh') == []
    assert recalled(service, 'evo')[0]['id'] == changed['id']
    with pytest.raises(ValueError, match='revision_conflict'):
        qa_memory.save(service, {'question':first['question'], 'answer':first['answer'],
            'expected_revision': first['revision'], 'action':'publish'}, item_id=first['id'])


@pytest.mark.parametrize('scope,projects', [('global', []), ('project', ['evo','dsh'])])
def test_future_guidance_reuses_exact_scope(service, monkeypatch, scope, projects):
    tid, _ = group_source(service, monkeypatch, count=3)
    preview = org(service, '/groups/preview', {'group_id':org(service, '/groups')['items'][0]['id'],
        'condition':'导出行', 'scope':'future', 'knowledge_scope':scope, 'projects':projects})
    assert org(service, '/groups/apply', {'proposal':preview})['succeeded'] == 3
    rule = org(service, '/guidance')['items'][0]
    org(service, '/guidance', {'id':rule['id'], 'expected_revision':rule['revision'], 'matching_mode':'semantic', 'condition':'导出数据保留原始编号的要求', 'projects':projects, 'knowledge_scope':scope})
    tid2, _ = group_source(service, monkeypatch, session='future-scope', count=1)
    unit = org(service, '/units', {'task_id':tid2})['items'][0]
    assert unit['decision'] == 'auto' and unit['knowledge_scope'] == scope
    from evolvmem import knowledge_scope as ks
    assert ks.projects(unit) == sorted(projects)


def test_context_search_filters_use_secondary_project(service):
    from evolvmem.context_models import ContextSearchRequest
    row = qa_memory.save(service, {'question':'导出编号怎么保留？', 'answer':'导出编号需要保留原始值。',
        'projects':['evo','dsh'], 'knowledge_scope':'project', 'action':'publish'})
    for project in ('evo','dsh'):
        assert row['id'] in [m.id for m in service.retriever.search(ContextSearchRequest(query='导出编号', project=project))]
    assert row['id'] not in [m.id for m in service.retriever.search(ContextSearchRequest(query='导出编号', project='shop'))]


def test_semantic_preview_requires_reasons_and_real_quotes(service, monkeypatch):
    from evolvmem import organization_semantic_review as semantic
    tid, _ = group_source(service, monkeypatch, count=3)
    units = org(service, '/units', {'task_id':tid})['items']
    def model(prompt, *_a, **_k):
        import json
        return json.dumps({'items':[
            {'id':0,'decision':'include','reason':'这段规定了导出数据的保真要求。','quote':units[0]['text'],'chain':{'need':units[0]['text']},'targets':[{'project':p,'role':'applicable','quote':units[0]['text']} for p in ('evo','dsh')]},
            {'id':1,'decision':'exclude','reason':'资料明确要求另作判断。','quote':'其他平台需要单独判断。','chain':{'need':'其他平台需要单独判断。'},'targets':[]},
            {'id':2,'decision':'include','reason':'虽然包含同样词语但引用无法定位。','quote':'不存在的原话'}]})
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,model))
    request={'group_id':org(service, '/groups')['items'][0]['id'],'selection':'group',
        'projects':['evo','dsh'],'knowledge_scope':'project','matching_mode':'semantic',
        'condition':'管理报表导出时保持数据完整性和可追溯性的要求，不含其他平台专属要求。',
        'exceptions':'涉及其他平台且要求单独判断的内容','scope':'future'}
    job=semantic.start(service,request)
    assert job['status']=='pending'
    assert semantic.tick(service)
    result=semantic.get(service,job['id'])
    assert result['status']=='completed'
    p=result['proposal']
    assert len(p['eligible'])==1 and len(p['excluded'])==2
    assert p['eligible'][0]['reason'] and p['eligible'][0]['evidence_quote'] == units[0]['text']
    assert p['excluded'][1]['reason_code']=='ai_uncertain'
    assert org(service, '/groups/apply', {'proposal':p})['succeeded']==1
    assert org(service, '/guidance')['items'][0]['matching_mode']=='semantic'


def test_explicit_general_reference_is_available_in_context_search(service):
    from evolvmem.context_models import ContextSearchRequest
    row = qa_memory.save(service, {'question':'导出编号怎么保留？', 'answer':'导出编号需要保留原始值。',
        'projects':[], 'knowledge_scope':'global', 'action':'publish'})
    for project in ('evo','dsh'):
        assert row['id'] in [m.id for m in service.retriever.search(ContextSearchRequest(query='导出编号',project=project))]


@pytest.mark.parametrize('change', ['unit', 'rules'])
def test_ai_result_cannot_apply_after_source_or_rule_changes(service, monkeypatch, change):
    from evolvmem import organization_semantic_review as semantic
    tid, _ = group_source(service, monkeypatch, count=1)
    unit = org(service, '/units', {'task_id':tid})['items'][0]
    def model(*args, **kwargs):
        import json
        return json.dumps({'items':[{'id':0,'decision':'include','reason':'这是导出数据保真要求。','quote':unit['text']}]})
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,model))
    job=semantic.start(service,{'group_id':org(service,'/groups')['items'][0]['id'], 'selection':'group',
        'projects':['evo'], 'knowledge_scope':'project', 'condition':'导出数据保真要求', 'scope':'future'})
    semantic.tick(service)
    p=semantic.get(service,job['id'])['proposal']
    with service.store.transaction():
        if change=='unit':
            service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=?',(tid,))
        else:
            rules=service.knowledge().rules.read()
            service.knowledge().rules.save({'expected_revision':rules['revision'],'instructions':rules['instructions']+'\n新增核对约定。'})
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_guidance').fetchone()[0]==0
    assert org(service,'/units',{'task_id':tid})['items'][0]['decision']=='review'


@pytest.mark.parametrize('operation', ['qa', 'assign'])
def test_explicit_project_without_selection_rejected(service, operation):
    body={'question':'导出编号怎样处理？','answer':'保留原始编号。','action':'publish',
          'knowledge_scope':'project','projects':[]}
    with pytest.raises(ValueError,match='project_required'):
        if operation=='qa':
            qa_memory.save(service,body)
        else:
            row=service.knowledge().create({'title':'原始编号','body':'保留原始编号。','project':'evo'})
            service.knowledge().assign(row['id'],{**body,'expected_revision':row['revision']})


def test_direct_correction_can_save_business_criteria(service, monkeypatch):
    tid,_=group_source(service,monkeypatch,count=1)
    unit=org(service,'/units',{'task_id':tid})['items'][0]
    result=org(service,'/correct',{'task_id':tid,'digest':unit['digest'],'expected_revision':unit['revision'],
        'project':'evo','matching_mode':'semantic','scope':'future',
        'condition':'实际要求导出数据保留原始编号的资料，不包含概念介绍。'})
    assert result['guidance']['matching_mode']=='semantic'
    assert result['guidance']['condition'].startswith('实际要求')


def test_shared_item_remains_backed_by_secondary_project_source(service, monkeypatch):
    from evolvmem.unit_derivations import currently_backed
    tid,_=group_source(service,monkeypatch,count=1)
    unit=org(service,'/units',{'task_id':tid})['items'][0]
    org(service,'/correct',{'task_id':tid,'digest':unit['digest'],'expected_revision':unit['revision'],'project':'evo'})
    run_worker(service,monkeypatch,model_for(service))
    ids=[r[0] for r in service.store._connection().execute('SELECT item_id FROM unit_derivations WHERE unit_task_id=?',(tid,))]
    assert ids
    with service.store.transaction():
        for item in ids:
            service.store._connection().execute('UPDATE context_items SET project=?,projects=? WHERE id=?',('dsh','["dsh","evo"]',item))
    assert all(currently_backed(service.store,item) for item in ids)


def test_feedback_metrics_bind_the_full_applicability_revision(service, monkeypatch):
    tid,_=group_source(service,monkeypatch,count=1)
    unit=org(service,'/units',{'task_id':tid})['items'][0]
    org(service,'/feedback',{'task_id':tid,'digest':unit['digest'],'expected_revision':unit['revision'],'verdict':'correct'})
    assert org(service,'/metrics')['feedback']['total']==1
