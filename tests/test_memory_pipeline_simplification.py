import pytest
from tests.test_history_qa_memory import service, archive
from tests.test_experience_service import experiences, case, proof
from evolvmem.knowledge_api import dispatch


def test_one_extraction_editor_saves_validation_atomically(service):
    skills=dispatch(service,'GET','skills')['skills']
    assert [s['id'] for s in skills]==['ownership','cleaning','extraction']
    old=dispatch(service,'GET','skills/extraction')
    saved=dispatch(service,'POST','skills/extraction',{'expected_revision':old['revision'],
        'instructions':'历史生成项目摘要，经验必须核对实际验证。','admission_instructions':'来源明确才可使用。',
        'settings':{'min_chars':20}})
    assert saved['admission_instructions']=='来源明确才可使用。'
    assert saved['settings']['min_chars']==20
    with pytest.raises(ValueError,match='revision_conflict'):
        dispatch(service,'POST','skills/extraction',{'expected_revision':old['revision'],'instructions':'不能覆盖新规则。'})


def test_collaboration_disabled_without_deleting_existing_rules(service):
    learning=service.learning()
    before=learning.settings()
    assert learning.context('evo')==''
    assert learning.skill('evo')['status']=='disabled'
    result=learning.analyze({'project':'evo','automatic':True},llm=lambda p:pytest.fail('disabled workflow called model'))
    assert result['status']=='disabled'
    with pytest.raises(ValueError,match='collaboration_disabled'):
        dispatch(service,'POST','learning/analyze',{'project':'evo'})
    assert learning.settings()==before


def test_unverified_method_is_not_searchable_but_history_remains(service):
    from evolvmem.memory_eligibility import eligible
    kb=service.knowledge()
    raw=kb.create({'title':'已知历史','body':'此前讨论保留会话来源，之后再核对项目摘要。','project':'evo','action':'publish','content_type':'reference'})
    method=kb.create({'title':'待验证方法','body':'使用这个方法应该能够解决全部查询速度问题。','project':'evo','action':'publish','content_type':'reference'})
    method=service.learning().classify(method['id'],{'expected_revision':method['revision'],'category':'experience'})
    assert eligible(service.store,raw['id'])
    assert not eligible(service.store,method['id'])
    assert kb.detail(method['id'])['body']
    from evolvmem.context_models import ContextSearchRequest
    hits=service.search(ContextSearchRequest(project='evo',query='查询速度'))
    assert method['id'] not in [r.id for r in hits]
    from evolvmem.qa_memory import record, detail
    with service.store.transaction():
        record(service,method['id'],{'question':'怎样解决查询速度？','answer':method['body']},approved=True)
    assert not detail(service,method['id'])['effective']
    assert method['id'] not in {d.item_id for d in service.store.list_vector_documents()}
    assert raw['id'] in {d.item_id for d in service.store.list_vector_documents()}


def test_verified_case_and_history_both_enter_vector_documents(experiences):
    from evolvmem.context_models import ContextItemDraft,ContextLayers,ContextContentType,ContextStatus
    from evolvmem.memory_eligibility import eligible
    good=experiences.record(case(),evidence=proof(experiences))
    pending=experiences.record(case(problem='另一条尚未核实的处理方法'))
    store=experiences.store
    with store.transaction():
        history=store.create_item(ContextItemDraft(identity_key='project:demo:progress:log:history',
            content_type=ContextContentType.SESSION_SUMMARY,project='demo',status=ContextStatus.ACTIVE,
            layers=ContextLayers('项目历史摘要','当时讨论了查询速度。','保留来源核对。','test')))
    assert eligible(store,good['id']) and not eligible(store,pending['id'])
    ids={d.item_id for d in store.list_vector_documents()}
    assert good['id'] in ids and history.id in ids and pending['id'] not in ids
