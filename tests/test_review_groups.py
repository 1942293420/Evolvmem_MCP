"""Grouped review is a previewed decision over current sources, never a guessed project."""
import json
import pytest
from tests.test_auto_organization import service, base_service, org
from tests.unit_model_fixture import model_for, run_worker
from evolvmem.session_archive import SessionArchiver


def group_source(service, monkeypatch, *, session='group-demo', count=12):
    messages = [{'role':'user', 'content':f'条目{i:02d}：导出行必须保留原始编号。' +
                 ('其他平台需要单独判断。' if i >= count-2 else '')} for i in range(count)]
    source = SessionArchiver(service.config,service.store).archive_session(
        '', 'kimi', session, json.dumps({'messages':messages},ensure_ascii=False))
    tid = org(service,'/tasks',{'items':[{'key':f'archive:{source.id}'}]})['items'][0]['id']
    model = model_for(service,topics=[(f'条目{i:02d}','','task_requirement') for i in range(count)])
    run_worker(service,monkeypatch,model)
    assert len(org(service,'/units',{'task_id':tid})['items'])==count
    return tid, source


def proposal(service, group_id):
    return org(service,'/groups/preview',{'group_id':group_id,'project':'evo',
        'condition':'导出行','exceptions':'其他平台','scope':'future',
        'guidance':'导出行规范归 Evo，其他平台除外'})


def test_preview_ten_matches_two_exceptions_then_single_guidance(service,monkeypatch):
    tid,source=group_source(service,monkeypatch)
    before=service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]
    groups=org(service,'/groups')['items']
    assert len(groups)==1 and groups[0]['count']==12
    p=proposal(service,groups[0]['id'])
    assert len(p['eligible'])==10 and len(p['excluded'])==2
    assert all(x['reason']=='命中例外：其他平台' for x in p['excluded'])
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before
    r=org(service,'/groups/apply',{'proposal':p})
    assert r['succeeded']==10 and r['failed']==0
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before+1
    units=org(service,'/units',{'task_id':tid})['items']
    assert sum(u['decision']=='manual' and u['project']=='evo' for u in units)==10
    assert sum(u['decision']=='review' and not u['project'] for u in units)==2
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before+1


def test_preview_changes_refuse_all_writes(service,monkeypatch):
    tid,_=group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    with service.store.transaction():
        service.store._connection().execute('update organization_units set revision=revision+1 where task_id=? and ordinal=0',(tid,))
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert not any(u['decision']=='manual' for u in org(service,'/units',{'task_id':tid})['items'])


def test_preview_tampering_cannot_expand_members_or_change_scope(service,monkeypatch):
    group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    p['exceptions']=''
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==0


def test_groups_keep_sessions_separate_and_manual_rows_out(service,monkeypatch):
    tid,_=group_source(service,monkeypatch,session='first',count=4)
    group_source(service,monkeypatch,session='second',count=4)
    groups=org(service,'/groups')['items']
    assert len(groups)==2 and sorted(g['count'] for g in groups)==[4,4]
    u=org(service,'/units',{'task_id':tid})['items'][0]
    org(service,'/correct',{'task_id':tid,'digest':u['digest'],'expected_revision':u['revision'],'project':'evo'})
    groups=org(service,'/groups')['items']
    assert all(not any(x['digest']==u['digest'] for x in g['examples']) for g in groups)


def test_narrow_condition_and_explicit_preview_required(service,monkeypatch):
    group_source(service,monkeypatch)
    gid=org(service,'/groups')['items'][0]['id']
    with pytest.raises(ValueError,match='review_condition_required'):
        org(service,'/groups/preview',{'group_id':gid,'project':'evo','condition':'资料'})
    with pytest.raises(ValueError,match='review_preview_required'):
        org(service,'/groups/apply',{'group_id':gid,'project':'evo'})


def test_rules_change_invalidates_preview(service,monkeypatch):
    group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    rules=service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision':rules['revision'],'instructions':rules['instructions']+'\n新的核对条件。'})
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
