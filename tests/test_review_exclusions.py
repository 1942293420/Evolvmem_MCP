"""A human exclusion is scoped to a target, never a deletion or global dismissal."""
import pytest
from tests.test_review_result_cache import setup_job, service, base_service
from tests.test_review_groups import org
from evolvmem import organization_semantic_review as semantic


def prepare(service,monkeypatch):
    tid,request,calls=setup_job(service,monkeypatch,count=3)
    job=semantic.start(service,request);semantic.tick(service)
    p=semantic.get(service,job['id'])['proposal']
    return tid,request,calls,job,p


def decide(service,p,unit,action='exclude'):
    return org(service,'/groups/exclusion',{'proposal':p,'task_id':unit['task_id'],'digest':unit['digest'],'action':action})


def test_exclusion_removes_recommendation_without_changing_knowledge_and_survives_retry(service,monkeypatch):
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0]
    before=dict(service.store._connection().execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',(tid,unit['digest'])).fetchone())
    decide(service,p,unit)
    after=dict(service.store._connection().execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',(tid,unit['digest'])).fetchone())
    assert before==after
    current=semantic.get(service,job['id'])['proposal']
    assert len(current['eligible'])==2
    assert current['excluded'][0]['reason_code']=='manual_excluded'
    assert current['excluded'][0]['excluded_projects']==['evo']
    assert len(calls)==3
    retry=semantic.start(service,request)
    assert retry['status']=='completed' and len(calls)==3
    assert retry['proposal']['excluded'][0]['reason_code']=='manual_excluded'
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_guidance').fetchone()[0]==0


def test_exclusion_is_project_specific_and_changes_in_criteria_do_not_erase_it(service,monkeypatch):
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    other=semantic.start(service,{**request,'project':'dsh'});semantic.tick(service)
    assert len(semantic.get(service,other['id'])['proposal']['eligible'])==3
    changed=semantic.start(service,{**request,'condition':'新的业务判断依据'});semantic.tick(service)
    assert len(semantic.get(service,changed['id'])['proposal']['eligible'])==2


def test_undo_restores_original_ai_judgment_without_calling_model(service,monkeypatch):
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    current=semantic.get(service,job['id'])['proposal']
    decide(service,current,current['excluded'][0],'undo')
    restored=semantic.get(service,job['id'])['proposal']
    assert len(restored['eligible'])==3 and not restored['excluded'] and len(calls)==3


def test_manual_exclusion_blocks_single_confirmation_and_stale_batch_apply(service,monkeypatch):
    from evolvmem import organization_review as review
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    group=review.detail(service,unit)
    single=review.preview(service,{**unit,'group_id':group['id'],'selection':'item','project':'evo'})
    assert not single['eligible'] and single['excluded'][0]['reason_code']=='manual_excluded'
    with pytest.raises(ValueError,match='review_preview_changed'):
        review.apply(service,{'proposal':p})
    with pytest.raises(ValueError,match='review_excluded'):
        org(service,'/correct',{'task_id':tid,'digest':unit['digest'],'expected_revision':unit['revision'],'project':'evo'})
    assert review.apply(service,{'proposal':semantic.get(service,job['id'])['proposal']})['succeeded']==2


def test_exclusion_respects_multi_project_subset_and_personal_general_scope(service,monkeypatch):
    from evolvmem import organization_review as review
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    common={**unit,'group_id':p['group_id'],'selection':'item'}
    together=review.preview(service,{**common,'projects':['evo','dsh'],'knowledge_scope':'project'})
    assert together['excluded'][0]['excluded_projects']==['evo']
    assert review.preview(service,{**common,'project':'dsh'})['eligible']
    general=review.preview(service,{**common,'knowledge_scope':'global','projects':[],'project':''})
    assert general['eligible'];decide(service,general,general['eligible'][0])
    assert review.preview(service,{**common,'knowledge_scope':'global','projects':[],'project':''})['excluded']
    assert review.preview(service,{**common,'project':'dsh'})['eligible']


def test_stale_or_tampered_exclusion_writes_nothing(service,monkeypatch):
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0]
    tampered={**p,'projects':['dsh'],'project':'dsh'}
    with pytest.raises(ValueError,match='review_preview_changed'):decide(service,tampered,unit)
    with service.store.transaction():service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=? AND digest=?',(tid,unit['digest']))
    with pytest.raises(ValueError,match='review_preview_changed'):decide(service,p,unit)
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0]==0


def test_automatic_project_decision_cannot_override_human_exclusion(service,monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker,find_unit
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    worker=OrganizationWorker(service.config)
    worker._update_unit(service,tid,unit['digest'],project='evo',decision='auto',reason='自动建议',expected_revision=find_unit(service,tid,unit['digest']))
    saved=find_unit(service,tid,unit['digest'])
    assert saved['decision']=='review' and saved['project']==''


def test_personal_general_auto_assignment_cannot_override_exclusion(service,monkeypatch):
    from evolvmem import organization_review as review
    from evolvmem.auto_organization import OrganizationWorker,find_unit
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0]
    target={'project':'','projects':[],'knowledge_scope':'global'}
    general=review.preview(service,{**unit,**target,'group_id':p['group_id'],'selection':'item'})
    decide(service,general,general['eligible'][0])
    OrganizationWorker(service.config)._update_unit(service,tid,unit['digest'],project='',decision='auto',reason='自动归为通用',expected_revision=find_unit(service,tid,unit['digest']),knowledge_target=target)
    saved=find_unit(service,tid,unit['digest'])
    assert saved['decision']=='review' and saved['knowledge_scope']!='global'


def test_blocked_automatic_assignment_does_not_record_inherited_dependency(service,monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker,find_unit
    from evolvmem import organization_context
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];prior=p['eligible'][1]
    org(service,'/correct',{'task_id':tid,'digest':prior['digest'],'expected_revision':prior['revision'],'project':'evo'})
    p=semantic.get(service,job['id'])['proposal'];unit=p['eligible'][0];decide(service,p,unit)
    worker=OrganizationWorker(service.config)
    worker._update_unit(service,tid,unit['digest'],project='evo',decision='auto',expected_revision=find_unit(service,tid,unit['digest']))
    worker._record_dependency(service,tid,unit['digest'],{'state':organization_context.STATE_READY,'task_id':tid,'unit_digest':prior['digest'],'source_key':prior['source_key'],'project':'evo'})
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_context_basis WHERE unit_task_id=? AND unit_digest=?',(tid,unit['digest'])).fetchone()[0]==0


def test_undo_after_new_preview_restores_cached_result_and_refresh_resume(service,monkeypatch):
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    repeated=semantic.start(service,request)
    current=repeated['proposal']
    restored=decide(service,current,current['excluded'][0],'undo')['proposal']
    assert len(restored['eligible'])==3 and not restored['excluded'] and len(calls)==3
    assert semantic.recent(service)['job']['proposal']==restored


def test_exclusion_remains_undoable_after_other_recommendations_are_saved(service,monkeypatch):
    from evolvmem import organization_review as review
    tid,request,calls,job,p=prepare(service,monkeypatch)
    unit=p['eligible'][0];decide(service,p,unit)
    current=semantic.get(service,job['id'])['proposal']
    assert review.apply(service,{'proposal':current})['succeeded']==2
    resumed=semantic.recent(service)['job']
    assert resumed is not None
    restored=decide(service,resumed['proposal'],resumed['proposal']['excluded'][0],'undo')['proposal']
    assert len(restored['eligible'])==1 and len(calls)==3
