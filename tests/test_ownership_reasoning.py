"""Business ownership must survive names used as examples, callers and dependencies."""
import json
import pytest
from tests.test_auto_organization import service, base_service, org
from tests.unit_model_fixture import model_for, run_worker, many_topic_archive


def test_project_name_and_hint_alone_cannot_assign_reference(service):
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    unit = {'text': '用 Evo 演示项目复现任务启动失败，业务规则没有修改。',
            'evidence_quote': 'Evo 演示项目', 'project_hint': 'evo'}
    project, _, decision = worker._decide(service, unit, 'evo', service.knowledge().rules.read(), {'evo','dsh'})
    assert (project, decision) == ('', 'review')


def test_unvalidated_automatic_predecessor_cannot_seed_another_batch(service):
    from evolvmem.organization_context import _prior_project_verified
    assert not _prior_project_verified(service, {'decision':'auto','project':'evo',
        'text':'Evo 演示项目用来复现，真正要调整的是运行程序。','context_basis':''})


def test_review_rejects_related_only_project_even_when_ai_says_include(service, monkeypatch):
    from evolvmem import organization_semantic_review as semantic
    text='货件查询仅用于验证执行器修复，货件查询任务包没有变更。'
    unit={'task_id':0,'digest':'synthetic','text':text,'title':'执行器修复'}
    def call(*a,**k):
        return json.dumps({'items':[{'id':0,'decision':'include','reason':'作为测试样例', 'quote':text,
            'chain':{'change':text},'targets':[{'project':'evo','role':'related','quote':text}]}]},ensure_ascii=False)
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,call))
    result=semantic.judge(service,[unit],'货件查询任务包的业务变更','',{'projects':['evo'],'knowledge_scope':'project'})
    assert result['0:synthetic']['decision'] != 'include'


def test_ingestion_name_match_never_bypasses_business_ownership(service):
    got=service.knowledge().preview({'body':'evo 只是本次验收样例，实际修改的是通用执行能力。','source':True})
    assert got['action']=='review' and got['project']==''


def test_explicit_validated_owner_survives_related_project_mentions(service):
    got=service.knowledge().preview({'project':'dsh','body':'dsh 的执行能力修复后用 evo 验证结果，evo 业务规则没有变化。','source':True})
    assert got['action']=='auto' and got['project']=='dsh'


def response(text, projects=('evo',), role='owner', **extra):
    return {'decision':'include','reason':'实际修改执行能力，查询任务只是验证场景。',
            'quote':text,'chain':{'change':text},
            'targets':[{'project':p,'role':role,'quote':text} for p in projects],**extra}


@pytest.mark.parametrize('extra',[
 {'chain':{'result':'不存在的测试结果'}},
 {'targets':[{'project':'unknown','role':'owner','quote':'修复执行能力'}]},
 {'chain':{}}, {'quote':'不存在的原话'},
 {'targets':[{'project':'evo','role':'related','quote':'修复执行能力'}]},
])
def test_invalid_or_related_evidence_cannot_assign(service, extra):
    from evolvmem.ownership_reasoning import validate
    text='修复执行能力'
    got=validate(response(text,**extra),{'text':text},{},{'evo'}, {'project':'evo'})
    assert got['decision']=='uncertain' and not got['projects']


def test_shared_applicability_and_independent_changes_are_distinct():
    from evolvmem.ownership_reasoning import validate
    text='两个项目的规则'
    shared=validate(response(text,('evo','dsh'),'applicable'),{'text':text},{},{'evo','dsh'})
    changed=validate(response(text,('evo','dsh'),'owner'),{'text':text},{},{'evo','dsh'})
    assert shared['decision']=='include' and shared['projects']==['evo','dsh']
    assert changed['decision']=='uncertain' and not changed['projects']


def test_general_scope_needs_positive_evidence_not_missing_project():
    from evolvmem.ownership_reasoning import validate
    text='这是个人通用的开发习惯'
    data=response(text,projects=())
    assert validate(data,{'text':text},{},set())['decision']=='uncertain'
    data.update(knowledge_scope='global',general_quote=text)
    result=validate(data,{'text':text},{},set())
    assert result['decision']=='include' and result['knowledge_scope']=='global'


def test_multiple_selected_projects_need_evidence_for_each():
    from evolvmem.ownership_reasoning import validate
    text='规则只适用于本项目'
    result=validate(response(text),{'text':text},{},{'evo','dsh'},{'projects':['evo','dsh']})
    assert result['decision']=='uncertain'


def test_semantic_ownership_reaches_history_without_related_project_leak(service,monkeypatch):
    from tests.unit_model_fixture import UnitProvider
    from evolvmem import history_memory
    text='evo 执行器修复通用启动参数，dsh 任务仅用于复现和验证，任务包未改。'
    class Provider(UnitProvider):
        def classify(self,prompt):
            rows=json.loads(prompt.split('待判断资料：',1)[1])
            return json.dumps({'items':[{'id':r['id'],**response(text),
                'targets':[{'project':'evo','role':'owner','quote':text},{'project':'dsh','role':'related','quote':text}]} for r in rows]},ensure_ascii=False)
    source=many_topic_archive(service,text=text)
    tid=org(service,'/tasks',{'items':[{'key':f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service,monkeypatch,Provider(topics=[(text,'dsh','task_requirement')]))
    unit=org(service,'/units',{'task_id':tid})['items'][0]
    assert unit['decision']=='auto' and unit['project']=='evo'
    assert unit['business_analysis']['related_projects']==['dsh']
    assert history_memory.sessions(service,'evo')
    assert not history_memory.sessions(service,'dsh')


def test_requirement_only_can_be_assigned_without_inventing_result():
    from evolvmem.ownership_reasoning import validate
    text='evo 下周要设计取消任务功能，尚未实现'
    data=response(text,chain={'need':text,'result':''})
    result=validate(data,{'text':text},{},{'evo'})
    assert result['decision']=='include' and 'result' not in result['chain']
