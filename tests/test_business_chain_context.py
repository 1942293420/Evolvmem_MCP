"""Related requirements participate in the proof and in cache invalidation."""
import json
import pytest
from tests.test_cross_batch_context import config,service,craft,SESSION,enqueue,units,org
from tests.unit_model_fixture import model_for,run_worker
from evolvmem import ownership_reasoning as reasoning, organization_semantic_review as semantic


def prepared(service,monkeypatch):
    a='用户要求：demo 项目修复执行器启动参数，other 只作测试样例。'
    b='继续补充：启动参数还要支持取消。'
    # The first assignment is explicitly human-confirmed for the context tests.
    first=craft(service,session=SESSION,start=1,end=3,text=a)
    ta=enqueue(service,first.id)
    run_worker(service,monkeypatch,model_for(service,projects={'demo','other'},topics=[(a,'','task_requirement')]))
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET project='demo',decision='manual',revision=revision+1 WHERE task_id=?",(ta,))
    second=craft(service,session=SESSION,start=4,end=6,text=b)
    tb=enqueue(service,second.id)
    run_worker(service,monkeypatch,model_for(service,projects={'demo','other'},topics=[(b,'','task_requirement')]))
    return ta,tb,units(service,tb)[0]


def test_reviewer_gets_verified_predecessor_and_changed_parent_invalidates_cache(service,monkeypatch):
    ta,tb,unit=prepared(service,monkeypatch)
    def judge(prompt,*a,**k):
        rows=json.loads(prompt.split('待判断资料：',1)[1]);r=rows[0]
        prior=r['context']['predecessor']['basis']
        assert prior['project']=='demo' and '启动参数' in prior['quote']
        return json.dumps({'items':[{'id':0,'decision':'include','reason':'延续已确认的执行器需求。',
            'quote':r['text'],'chain':{'need':prior['quote'],'change':r['text']},
            'targets':[{'project':'demo','role':'owner','quote':prior['quote']}]}]},ensure_ascii=False)
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,judge))
    group=org(service,'/groups')['items'][0]
    request={'group_id':group['id'],'selection':'all','projects':['demo'],'condition':'执行器启动能力的需求与改动'}
    job=semantic.start(service,request);semantic.tick(service)
    before=semantic.get(service,job['id'])['proposal'];assert len(before['eligible'])==1
    assert before['eligible'][0]['chain']['need']
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET project='other',revision=revision+1 WHERE task_id=?",(ta,))
    after=semantic.get(service,job['id'])['proposal']
    assert not after['eligible'] and after['excluded'][0]['reason_code']=='review_changed'
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':before})


def test_parent_correction_while_model_answers_cannot_write_stale_ownership(service,monkeypatch):
    ta,tb,unit=prepared(service,monkeypatch)
    def judge(prompt,*a,**k):
        r=json.loads(prompt.split('待判断资料：',1)[1])[0]
        with service.store.transaction():
            service.store._connection().execute("UPDATE organization_units SET project='other',revision=revision+1 WHERE task_id=?",(ta,))
        return json.dumps({'items':[{'id':0,'decision':'include','reason':'旧需求的结论','quote':r['text'],
            'chain':{'change':r['text']},'targets':[{'project':'demo','role':'owner','quote':r['text']}]}]},ensure_ascii=False)
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,judge))
    result=reasoning.analyze(service,[unit])[f'{tb}:{unit["digest"]}']
    assert result['decision']=='uncertain' and not result['projects']
