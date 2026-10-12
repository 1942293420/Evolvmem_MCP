"""Completed classification survives unrelated arrivals and retries."""
import json
import pytest
from tests.test_review_groups import service, base_service, group_source, org
from evolvmem import organization_semantic_review as semantic


def setup_job(service, monkeypatch, count=8):
    tid, _ = group_source(service, monkeypatch, count=count)
    calls=[]
    def judge(_service, units, *_):
        calls.extend(semantic._key(u) for u in units)
        return {semantic._key(u):dict(decision='include',reason='符合数据保真要求',evidence_quote=u['text']) for u in units}
    monkeypatch.setattr(semantic, 'judge', judge)
    request=dict(group_id=org(service,'/groups')['items'][0]['id'],selection='all',project='evo',condition='导出数据保真要求',scope='batch')
    return tid,request,calls


def test_repeated_click_reuses_pending_and_completed_job(service, monkeypatch):
    _,request,calls=setup_job(service,monkeypatch)
    first=semantic.start(service,request)
    assert semantic.start(service,request)['id']==first['id']
    semantic.tick(service)
    again=semantic.start(service,request)
    assert again['status']=='completed' and again['id']==first['id']
    assert len(again['proposal']['eligible'])==8 and len(calls)==8


def test_unrelated_arrival_during_judgment_does_not_discard_result(service, monkeypatch):
    _,request,calls=setup_job(service,monkeypatch)
    job=semantic.start(service,request)
    original=semantic.judge
    def arriving(*args):
        result=original(*args)
        if len(calls)==4:
            # New review material arriving while a model call is in flight.
            group_source(service,monkeypatch,session='unrelated-arrival',count=1)
        return result
    monkeypatch.setattr(semantic,'judge',arriving)
    semantic.tick(service)
    result=semantic.get(service,job['id'])
    assert result['status']=='completed'
    assert len(result['proposal']['eligible'])==8
    assert org(service,'/groups/apply',{'proposal':result['proposal']})['succeeded']==8


def test_changed_unit_is_shown_and_only_that_unit_is_rejudged(service, monkeypatch):
    tid,request,calls=setup_job(service,monkeypatch)
    job=semantic.start(service,request);semantic.tick(service)
    old=semantic.get(service,job['id'])['proposal']
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=? AND ordinal=0',(tid,))
    restored=semantic.get(service,job['id'])['proposal']
    assert len(restored['eligible'])==7
    assert [u['reason_code'] for u in restored['excluded']]==['review_changed']
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':old})
    new=semantic.start(service,request);semantic.tick(service)
    assert len(calls)==9
    assert len(semantic.get(service,new['id'])['proposal']['eligible'])==8


@pytest.mark.parametrize('field,value',[('condition','另一个业务依据'),('project','dsh'),('exceptions','新增例外')])
def test_changed_business_criteria_does_not_reuse_verdicts(service,monkeypatch,field,value):
    _,request,calls=setup_job(service,monkeypatch)
    semantic.start(service,request);semantic.tick(service)
    semantic.start(service,{**request,field:value});semantic.tick(service)
    assert len(calls)==16


def test_partial_results_survive_failure_and_retry(service,monkeypatch):
    _,request,calls=setup_job(service,monkeypatch)
    original=semantic.judge
    def interrupted(*args):
        if len(calls)==4:raise RuntimeError('provider failed')
        return original(*args)
    monkeypatch.setattr(semantic,'judge',interrupted)
    job=semantic.start(service,request);semantic.tick(service)
    assert semantic.get(service,job['id'])['status']=='failed'
    monkeypatch.setattr(semantic,'judge',original)
    retry=semantic.start(service,request);semantic.tick(service)
    assert len(calls)==8
    assert len(semantic.get(service,retry['id'])['proposal']['eligible'])==8


def test_legacy_completed_result_can_be_restored_without_new_model_calls(service,monkeypatch):
    _,request,calls=setup_job(service,monkeypatch)
    from evolvmem import organization_review as review
    from evolvmem.context_store import _now_iso
    request={**request,'matching_mode':'semantic','job_id':'legacy-job'}
    base=review.preview(service,request,_judgments={})
    units=[u for g in review._groups(service) for u in g['members']]
    judgments=semantic.judge(service,units,'','',request)
    with service.store.transaction():
        service.store._connection().execute('INSERT INTO organization_review_jobs(id,status,request,base_revision,judgments,processed,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',('legacy-job','completed',json.dumps(request),base['revision'],json.dumps(judgments),8,8,_now_iso(),_now_iso()))
    newer,_=group_source(service,monkeypatch,session='legacy-new-arrival',count=1)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET created_at='2999-01-01' WHERE task_id=?",(newer,))
    # Restore the old frozen eight even though the current pool now has nine.
    recovered=semantic.get(service,'legacy-job')
    assert len(recovered['proposal']['eligible'])==8
    restored=semantic.start(service,request)
    semantic.tick(service)
    restored=semantic.get(service,restored['id'])
    assert restored['status']=='completed' and len(calls)==9
    assert restored['id']!='legacy-job'  # only the new arrival requires AI


def test_hundred_results_remain_saveable_after_unrelated_arrival(service,monkeypatch):
    _,request,calls=setup_job(service,monkeypatch,count=100)
    job=semantic.start(service,request);semantic.tick(service)
    before=semantic.get(service,job['id'])['proposal']
    group_source(service,monkeypatch,session='after-hundred',count=1)
    after=semantic.get(service,job['id'])['proposal']
    assert before==after and len(after['eligible'])==100 and len(calls)==100
    assert org(service,'/groups/apply',{'proposal':after})['succeeded']==100


def test_extraction_bookkeeping_does_not_invalidate_review(service,monkeypatch):
    tid,request,calls=setup_job(service,monkeypatch)
    job=semantic.start(service,request);semantic.tick(service)
    before=semantic.get(service,job['id'])['proposal']
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET updated_at='2099-01-01',extraction_stage='pending' WHERE task_id=?",(tid,))
    assert semantic.get(service,job['id'])['proposal']==before
    assert semantic.start(service,request)['id']==job['id']
    assert len(calls)==8


def test_removed_source_is_excluded_without_losing_other_results(service,monkeypatch):
    tid,request,calls=setup_job(service,monkeypatch)
    second,_=group_source(service,monkeypatch,session='second-source',count=2)
    job=semantic.start(service,request);semantic.tick(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='superseded' WHERE id=?",(second,))
    p=semantic.get(service,job['id'])['proposal']
    assert len(p['eligible'])==8
    assert sum(u.get('reason_code')=='review_changed' for u in p['excluded'])==2
    assert org(service,'/groups/apply',{'proposal':p})['succeeded']==8


def test_reprocessed_source_leaves_stale_cache_without_rejudging_unchanged_items(service,monkeypatch):
    from evolvmem import organization_review as review
    from tests.unit_model_fixture import run_worker, model_for
    _,request,calls=setup_job(service,monkeypatch,count=4)
    tid,source=group_source(service,monkeypatch,session='old-rule-source',count=2)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET rule_revision='old-rule' WHERE id=?",(tid,))
    job=semantic.start(service,request);semantic.tick(service)
    saved=semantic.get(service,job['id'])['proposal']
    assert sum(u.get('reason_code')=='rule_changed' for u in saved['excluded'])==2
    task=review.reprocess(service,{'proposal':saved})['items'][0]
    run_worker(service,monkeypatch,model_for(service,topics=[('条目00','','task_requirement'),('条目01','','task_requirement')]))
    restored=semantic.get(service,job['id'])['proposal']
    assert not any(u.get('reason_code')=='rule_changed' for u in restored['excluded'])
    assert len(restored['eligible'])==4 and len(calls)==4
    assert all(u['task_id']!=tid for u in restored['excluded'])
    assert any(u['task_id']==task['id'] for u in restored['excluded'])
    before=service.store._connection().execute('SELECT COUNT(*) FROM organization_tasks').fetchone()[0]
    with pytest.raises(ValueError,match='review_reprocess_not_needed'):
        review.reprocess(service,{'proposal':restored})
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_tasks').fetchone()[0]==before


def test_next_batch_judges_unseen_items_and_repeated_preview_reuses_it(service,monkeypatch):
    _,request,calls=setup_job(service,monkeypatch,count=105)
    job=semantic.start(service,request);semantic.tick(service)
    assert len(calls)==100
    continued={**request,'continue_job_id':job['id']}
    next_job=semantic.start(service,continued);semantic.tick(service)
    result=semantic.get(service,next_job['id'])
    assert next_job['id']!=job['id'] and len(calls)==105
    assert len(result['proposal']['eligible'])==5
    assert semantic.start(service,continued)['id']==next_job['id']
    assert result['request']['continue_job_id']==job['id']


def test_next_batch_rejects_different_criteria(service,monkeypatch):
    _,request,calls=setup_job(service,monkeypatch,count=3)
    job=semantic.start(service,request);semantic.tick(service)
    with pytest.raises(ValueError,match='review_preview_changed'):
        semantic.start(service,{**request,'condition':'另一个业务依据','continue_job_id':job['id']})


def test_manual_confirmation_is_saved_and_removed_from_uncertain_batch(service,monkeypatch):
    from evolvmem import organization_review as review
    _,request,_=setup_job(service,monkeypatch,count=3)
    def uncertain(_service,units,*_):
        return {semantic._key(u):dict(decision='uncertain',reason='需要人工确认适用条件',evidence_quote=u['text']) for u in units}
    monkeypatch.setattr(semantic,'judge',uncertain)
    job=semantic.start(service,request);semantic.tick(service)
    p=semantic.get(service,job['id'])['proposal']
    assert not p['eligible'] and len(p['excluded'])==3
    unit=p['excluded'][0]
    group=review.detail(service,unit)
    single=review.preview(service,{**unit,'group_id':group['id'],'selection':'item','project':'evo'})
    assert review.apply(service,{'proposal':single})['succeeded']==1
    p=semantic.get(service,job['id'])['proposal']
    assert p['handled_count']==1 and len(p['excluded'])==2
