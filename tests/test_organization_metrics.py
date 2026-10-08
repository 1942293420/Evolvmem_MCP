import pytest
from tests.test_auto_organization import service, base_service, org
from tests.test_review_groups import group_source
from tests.unit_model_fixture import model_for, run_worker
from evolvmem import organization_guidance


def test_metrics_define_unassessed_and_count_current_review_groups(service,monkeypatch):
    tid,_=group_source(service,monkeypatch)
    m=org(service,'/metrics')
    assert m['review_units']==12 and m['review_groups']==1
    assert m['automatic_rate']==0 and m['settled_tasks']==1
    assert m['feedback']=={'total':0,'correct':0,'incorrect':0,'error_rate':None}


def test_only_actual_guidance_assignment_counts_and_manual_decision_is_separate(service,monkeypatch):
    organization_guidance.record(service,guidance='导出行归 Evo',scope='future',project='evo',condition='导出行')
    tid,_=group_source(service,monkeypatch)
    assert org(service,'/metrics')['guidance_reused_units']==12
    assert org(service,'/metrics')['automatic_completed']==1
    u=org(service,'/units',{'task_id':tid})['items'][0]
    org(service,'/correct',{'task_id':tid,'digest':u['digest'],'expected_revision':u['revision'],'project':'evo'})
    m=org(service,'/metrics')
    assert m['guidance_reused_units']==11 and m['manual_completed']==1
    assert m['automatic_completed']==0


def test_feedback_requires_exact_current_unit_and_replay_is_one_observation(service,monkeypatch):
    tid,_=group_source(service,monkeypatch)
    u=org(service,'/units',{'task_id':tid})['items'][0]
    body={'task_id':tid,'digest':u['digest'],'expected_revision':u['revision'],'verdict':'incorrect'}
    org(service,'/feedback',body);org(service,'/feedback',body)
    assert org(service,'/metrics')['feedback']=={'total':1,'correct':0,'incorrect':1,'error_rate':1.0}
    org(service,'/correct',{**body,'project':'evo'})
    with pytest.raises(ValueError,match='revision_conflict'):org(service,'/feedback',body)
    assert org(service,'/metrics')['feedback']['total']==0


def test_completed_metrics_never_decrypt_old_payload_just_to_count(service,monkeypatch):
    from tests.test_progress_history import run_progress
    from evolvmem.session_archive import SessionArchiver
    run_progress(service,monkeypatch,[{'role':'assistant','content':'进度：正在检查按钮。'}])
    # Legacy archives can predate the cleaned-history projection; counting
    # their completed task must not decode the potentially huge raw payload.
    with service.store.transaction():
        service.store._connection().execute('DELETE FROM conversation_history')
    def forbidden(*args,**kwargs):
        raise AssertionError('a count must not expand an entire old archive')
    monkeypatch.setattr(SessionArchiver,'read_payload',forbidden)
    assert org(service,'/metrics')['automatic_completed']==1
