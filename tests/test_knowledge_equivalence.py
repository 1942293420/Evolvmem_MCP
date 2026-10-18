"""A person confirms equivalent expressions; similarity never grants ownership."""
import pytest
from tests.test_knowledge_identity import config, service, material, save, view, rebuild, ingest
from tests.test_cross_batch_context import OTHER_SESSION, THIRD_SESSION
from evolvmem.knowledge_api import dispatch
from evolvmem.memory_recall import recall

ORIGINAL = 'Demo 的运行系统是 Linux。'
ALIAS = 'Demo 运行于 Linux 系统。'


def pair(service, *, category='environment'):
    first, _ = material(service, text=ORIGINAL, category=category)
    saved = save(service, first['id'])
    second, unit = material(service, text=ALIAS, session=OTHER_SESSION, category=category)
    return first, saved, second, unit


def confirm(service, target, task, unit, **extra):
    return dispatch(service, 'POST', 'equivalences/confirm', {
        'task_id':task['id'], 'digest':unit['digest'], 'expected_revision':unit['revision'],
        'target_knowledge_id':target['knowledge_id'],
        'target_revision':service.store._connection().execute(
            'SELECT revision FROM knowledge_objects WHERE knowledge_id=?',(target['knowledge_id'],)).fetchone()[0],
        **extra})


def test_confirmed_expression_reuses_decision_and_shows_one_knowledge_with_all_sources(service):
    first, saved, second, unit = pair(service)
    suggestions = dispatch(service,'GET','equivalences/candidates',{'task_id':second['id'],'digest':unit['digest']})
    assert suggestions['items'][0]['knowledge_id'] == saved['knowledge_id']
    assert view(service,second['id'])[0]['decision'] == 'review'
    result = confirm(service,saved,second,unit)
    assert result['unit']['project'] == 'demo'
    third, repeated = material(service,text=ALIAS,session=THIRD_SESSION)
    assert repeated['decision'] == 'manual' and repeated['project'] == 'demo'
    ingest(service,third['id'])
    items = dispatch(service,'GET','directory',{'project':'demo'})['items']
    assert len(items) == 1
    assert len({s['archive_id'] for s in items[0]['sources'] if s.get('archive_id')}) == 3
    assert ORIGINAL in items[0]['body']
    assert any(ALIAS in x['text'] for x in items[0]['expressions'])
    assert recall(service,{'project':'demo','kind':'knowledge'})['knowledge'] == items


def test_cancel_equivalence_separates_display_but_keeps_human_projects_and_sources(service):
    _, saved, second, unit = pair(service)
    result = confirm(service,saved,second,unit)
    record = result['equivalence']
    dispatch(service,'POST','equivalences/revoke',{'knowledge_id':record['source_knowledge_id'],'expected_revision':record['revision']})
    items = dispatch(service,'GET','directory',{'project':'demo'})['items']
    assert len(items) == 2 and all(i['sources'] for i in items)
    assert view(service,second['id'])[0]['project'] == 'demo'
    assert service.knowledge().detail(saved['item_id'])['project'] == 'demo'


def test_equivalence_never_overwrites_a_conflicting_human_project(service):
    _, saved, second, _ = pair(service)
    other = save(service,second['id'],project='other')
    with pytest.raises(ValueError,match='equivalence_scope_conflict'):
        confirm(service,saved,second,other)
    assert view(service,second['id'])[0]['project'] == 'other'


def test_stale_confirmation_is_atomic(service):
    _, saved, second, unit = pair(service)
    with pytest.raises(ValueError,match='revision_conflict'):
        confirm(service,saved,second,unit,target_revision=999)
    assert view(service,second['id'])[0]['decision'] == 'review'
    assert not dispatch(service,'GET','equivalences',{})['items']


def test_new_condition_is_not_the_confirmed_expression(service):
    _, saved, second, unit = pair(service)
    confirm(service,saved,second,unit)
    _, expanded = material(service,text=ALIAS+'新增：生产环境改为 Windows。',session=THIRD_SESSION)
    assert expanded['decision'] == 'review' and not expanded['project']


def test_confirmed_reference_expression_can_repeat_but_unknown_generic_expression_cannot(service):
    _, saved, second, unit = pair(service,category='reference')
    confirm(service,saved,second,unit)
    _, repeated = material(service,text=ALIAS,session=THIRD_SESSION,category='reference')
    assert repeated['decision'] == 'manual'
    _, generic = material(service,text='运行于 Linux 系统。',session='generic-other',category='reference')
    assert generic['decision'] == 'review'


def test_link_does_not_move_a_later_manual_project_change(service):
    _, saved, second, unit = pair(service)
    confirm(service,saved,second,unit)
    save(service,second['id'],project='other')
    demo=dispatch(service,'GET','directory',{'project':'demo'})['items']
    other=dispatch(service,'GET','directory',{'project':'other'})['items']
    assert len(demo)==len(other)==1
    assert not demo[0].get('expressions') and ALIAS in other[0]['body']


def test_confirm_does_not_allow_a_cycle_or_reparent_other_expressions(service):
    _, saved, second, unit = pair(service)
    result=confirm(service,saved,second,unit)
    with pytest.raises(ValueError,match='equivalence_choose_original'):
        confirm(service,result['unit'],{'id':saved['task_id']},saved)


def test_query_or_source_filter_returns_the_whole_confirmed_group(service):
    from evolvmem.project_knowledge import directory
    _, saved, second, unit = pair(service)
    result=confirm(service,saved,second,unit)
    items=directory(service.store,'demo',item_ids=[result['unit']['item_id']])['items']
    assert len(items)==1 and len(items[0]['item_ids'])==2
    assert items==directory(service.store,'demo',q='运行于')['items']


def test_old_manual_record_can_be_selected_without_global_reprocessing(service):
    first,_=material(service,text=ORIGINAL)
    saved=save(service,first['id'])
    with service.store.transaction():
        c=service.store._connection()
        c.execute("UPDATE organization_units SET knowledge_id='' WHERE task_id=?",(first['id'],))
        c.execute('DELETE FROM knowledge_decisions WHERE knowledge_id=?',(saved['knowledge_id'],))
        c.execute('DELETE FROM knowledge_source_bindings WHERE knowledge_id=?',(saved['knowledge_id'],))
        c.execute('DELETE FROM knowledge_objects WHERE knowledge_id=?',(saved['knowledge_id'],))
    second,unit=material(service,text=ALIAS,session=OTHER_SESSION)
    before=service.knowledge().detail(saved['item_id'])
    items=dispatch(service,'GET','equivalences/candidates',{'task_id':second['id'],'digest':unit['digest']})['items']
    target=next(x for x in items if ORIGINAL in x['text'])
    assert target['knowledge_id'].startswith('legacy:')
    assert not view(service,first['id'])[0]['knowledge_id'], 'reading candidates must not adopt history'
    result=dispatch(service,'POST','equivalences/confirm',{'task_id':second['id'],'digest':unit['digest'],
        'expected_revision':unit['revision'],'target_knowledge_id':target['knowledge_id'],'target_revision':target['revision']})
    assert result['unit']['project']=='demo'
    assert service.knowledge().detail(saved['item_id'])==before
    assert len(dispatch(service,'GET','directory',{'project':'demo'})['items'])==1
