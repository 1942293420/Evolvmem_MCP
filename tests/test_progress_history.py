import json
import pytest
from tests.test_auto_organization import service, base_service, org
from tests.unit_model_fixture import UnitProvider, run_worker
from evolvmem.session_archive import SessionArchiver


class ProgressProvider(UnitProvider):
    def segment(self, prompt):
        result=json.loads(super().segment(prompt))
        for unit in result['units']:
            unit.update(disposition='history_only',disposition_reason='只是助手执行进度，没有新的要求或可复用结论')
        return json.dumps(result,ensure_ascii=False)


def run_progress(service,monkeypatch,messages,category='reference'):
    source=SessionArchiver(service.config,service.store).archive_session(
        '', 'kimi','progress-demo',json.dumps({'messages':messages},ensure_ascii=False))
    tid=org(service,'/tasks',{'items':[{'key':f'archive:{source.id}'}]})['items'][0]['id']
    model=ProgressProvider(topics=[('进度','','reference')])
    if category!='reference':model.topics=[('进度','',category)]
    run_worker(service,monkeypatch,model)
    return tid,model


def test_unassigned_progress_searchable_without_review_or_extraction(service,monkeypatch):
    tid,model=run_progress(service,monkeypatch,[{'role':'assistant','content':'进度：正在核对上一轮的测试，稍后继续。'}])
    detail=org(service,'/detail',{'task_id':tid})
    assert detail['status']=='completed' and detail['review_count']==0
    assert detail['units'][0]['disposition']=='history_only'
    assert not detail['units'][0]['project'] and not model.extract_calls
    result=org(service,'/progress',{'query':'上一轮'})
    assert result['total']==1 and result['items'][0]['task_id']==tid
    assert org(service,'/groups')['unit_count']==0
    # The UI restore action returns this real source to normal review.
    u=detail['units'][0]
    org(service,'/disposition',{'task_id':tid,'digest':u['digest'],'expected_revision':u['revision'],'disposition':'keep'})
    assert org(service,'/detail',{'task_id':tid})['status']=='review'
    assert org(service,'/progress')['total']==0


@pytest.mark.parametrize('roles,category',[
    (['user'],'reference'),(['assistant','user'],'reference'),
    (['assistant'],'experience'),(['assistant'],'decision')])
def test_model_cannot_hide_user_mixed_evidence_or_decision(service,monkeypatch,roles,category):
    tid,model=run_progress(service,monkeypatch,[{'role':r,'content':('进度：' if i==0 else '')+'必须保留原始编号。'} for i,r in enumerate(roles)],category)
    units=org(service,'/units',{'task_id':tid})['items']
    assert all(u['disposition']=='review' for u in units)
    assert org(service,'/detail',{'task_id':tid})['review_count']>0


def test_assistant_progress_with_two_project_signals_keeps_conflict_visible(service,monkeypatch):
    tid,_=run_progress(service,monkeypatch,[{'role':'assistant','content':'进度：Evo 演示项目和 DSH 项目之间正在切换。'}])
    assert org(service,'/detail',{'task_id':tid})['status']=='review'
    assert org(service,'/progress')['total']==0
