import json
import pytest
from tests.test_auto_organization_extraction import service, base_service, org, _task_for
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker


@pytest.mark.parametrize('phase',['model_request','model_parse','persistence'])
def test_failure_has_stage_type_and_time_without_provider_body(service,monkeypatch,phase):
    source=many_topic_archive(service,session='diagnostic')
    tid=_task_for(service,source)
    model=model_for(service)
    if phase=='model_request':model.fail_extraction='SECRET provider response and token'
    elif phase=='model_parse':monkeypatch.setattr(model,'extract',lambda p:'not JSON SECRET token')
    else:
        from evolvmem.context_service import ContextService
        original=ContextService.persist_legacy_extraction
        def broken(self,request,**kwargs):
            if request.candidates:raise RuntimeError('SECRET database payload')
            return original(self,request,**kwargs)
        monkeypatch.setattr(ContextService,'persist_legacy_extraction',broken)
    run_worker(service,monkeypatch,model)
    detail=org(service,'/detail',{'task_id':tid})
    failed=[u for u in detail['units'] if u['extraction_stage']=='failed']
    assert failed
    for unit in failed:
        diagnostic=json.loads(unit['extraction_diagnostic'])
        assert diagnostic['stage']==phase
        assert diagnostic['exception_type'] and diagnostic['occurred_at']
        assert 'SECRET' not in unit['extraction_diagnostic']
    assert 'SECRET' not in detail['error_detail']
    assert detail['error_detail'], 'task card exposes the readable failure summary'


def test_retry_keeps_history_and_clears_resolved_diagnostic(service,monkeypatch):
    source=many_topic_archive(service,session='diagnostic-retry')
    tid=_task_for(service,source)
    run_worker(service,monkeypatch,model_for(service,fail_extraction='SECRET'))
    before=[u['item_id'] for u in org(service,'/units',{'task_id':tid})['items']]
    org(service,'/retry',{'task_id':tid})
    run_worker(service,monkeypatch,model_for(service))
    units=org(service,'/units',{'task_id':tid})['items']
    assert before==[u['item_id'] for u in units]
    assert all(not u['extraction_diagnostic'] for u in units)
    assert not org(service,'/detail',{'task_id':tid})['error_detail']
