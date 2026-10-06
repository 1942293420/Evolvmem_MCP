"""Reviewer acceptance cases using synthetic data only."""
import json
import pytest
from tests.conftest import temp_dir, test_config
from tests.test_history_qa_memory import service, archive
from evolvmem import organization_guidance as guidance
from evolvmem.knowledge_api import dispatch

def test_future_guidance_does_not_match_generic_words(service):
    with service.store.transaction():
        guidance.record(service, guidance='以后蓝鲸商品采集的资料归蓝鲸项目', scope='future',
                        project='evo', condition='蓝鲸商品采集', exceptions='通用执行器')
    assert guidance.match(service, {'text':'这是另一个项目的资料，需要整理。'}) is None

def test_future_guidance_exceptions_are_excluded(service):
    with service.store.transaction():
        guidance.record(service, guidance='蓝鲸商品采集归蓝鲸', scope='future',
                        project='evo', condition='蓝鲸商品采集', exceptions='通用执行器')
    result=guidance.match(service, {'text':'蓝鲸商品采集只是例子，本文仅讨论通用执行器。'})
    assert result is None or result.get('negative') or result.get('conflict')

def test_uncleaned_source_can_enter_auto_pipeline(service, monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem import kimi_hooks
    a=archive(service,session='review-new-raw',project='',text='Evo 演示项目需要保留原始资料。')
    dispatch(service,'POST','organization/tasks',{'items':[{'key':f'archive:{a.id}'}]})
    calls=[]
    def model(prompt,*args,**kw):
        calls.append(prompt)
        if '完整正文：\n' in prompt:
            body=prompt.split('完整正文：\n',1)[1]
            return json.dumps({'units':[{'title':'项目资料','body':body,'category':'reference','project_hint':'evo'}]},ensure_ascii=False)
        return json.dumps({'cleaned_text':'Evo 演示项目需要保留原始资料。','category':'reference','recommended_action':'keep','reason':'保留'},ensure_ascii=False)
    monkeypatch.setattr(kimi_hooks,'_load_llm_config',lambda **kw:object())
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    w=OrganizationWorker(service.config)
    try:
        w.tick()
        task=w.tasks()[0]
        assert task['error_code'] != 'organization_failed' or 'cleaning_confirmation_required' not in task['error_detail']
        assert calls, 'A raw source must be processed without requiring the old manual cleaning confirmation'
    finally:
        w.stop()
