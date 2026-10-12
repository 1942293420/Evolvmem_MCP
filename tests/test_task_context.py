"""A review must expose why a task began, not only the recalled fragment."""
import json
import pytest
from tests.test_cross_batch_context import config, service, craft, SESSION, OTHER_SESSION, enqueue, units, org
from tests.unit_model_fixture import model_for, run_worker
from evolvmem import ownership_reasoning as reasoning


def make_task(service,monkeypatch,text,*,start=1,end=3,session=SESSION):
    source=craft(service,session=session,start=start,end=end,text=text)
    tid=enqueue(service,source.id)
    run_worker(service,monkeypatch,model_for(service,projects={'demo','other'},topics=[(text,'demo','task_requirement',text.startswith('继续'))]))
    return tid,units(service,tid)[0]


def test_classification_receives_requirement_before_old_neighbour_window(service,monkeypatch):
    need='最初需求：demo 执行器需要支持取消；货件查询只是验收样例。'
    tail='本次修改的是执行器进程等待方式，货件查询任务包未改。'
    text=need+'\n'+('记录处理过程。'*600)+'\n'+tail
    tid,unit=make_task(service,monkeypatch,text)
    full=org(service,'/detail',{'task_id':tid})['source_text']
    piece={**unit,'text':tail,'source_start':full.index(tail),'source_end':full.index(tail)+len(tail)}
    context=reasoning.context_for(service,piece)
    assert need in '\n'.join(reasoning.evidence_texts(piece,context))


def test_review_context_shows_full_source_and_current_fragment(service,monkeypatch):
    text='最初需求：demo 新增任务取消。\n过程：核对执行器进程。\n结果：取消后进程已退出。'
    tid,unit=make_task(service,monkeypatch,text)
    result=org(service,'/context',{'task_id':tid,'digest':unit['digest']})
    assert text in '\n'.join(r['text'] for r in result['records'])
    assert result['focus']['digest']==unit['digest']
    assert result['records'][0]['source_key'].startswith('archive:')
    assert result['records'][0]['current'] is True
    assert result['records'][0]['title'] and result['revision']


def test_task_context_reaches_original_need_over_three_batches(service,monkeypatch):
    first='demo 最初需求：执行器支持取消，而不是修改业务任务包。'
    ta,ua=make_task(service,monkeypatch,first)
    second='继续补充：取消时先结束子进程。'
    tb,ub=make_task(service,monkeypatch,second,start=4,end=6)
    third='继续验证：取消后未残留子进程，货件查询运行正常。'
    tc,uc=make_task(service,monkeypatch,third,start=7,end=9)
    result=org(service,'/context',{'task_id':tc,'digest':uc['digest']})
    joined='\n'.join(r['text'] for r in result['records'])
    assert first in joined and second in joined and third in joined
    assert [r['task_id'] for r in result['records']]==[ta,tb,tc]
    assert first in '\n'.join(reasoning.evidence_texts(uc,reasoning.context_for(service,uc)))


def test_context_never_joins_a_different_native_session(service,monkeypatch):
    ta,ua=make_task(service,monkeypatch,'demo 另一会话的需求禁止串入。',session=OTHER_SESSION)
    tb,ub=make_task(service,monkeypatch,'demo 当前任务的独立需求。',session=SESSION)
    got=org(service,'/context',{'task_id':tb,'digest':ub['digest']})
    assert '禁止串入' not in json.dumps(got,ensure_ascii=False)


def test_missing_early_source_is_reported_instead_of_invented(service,monkeypatch):
    tb,ub=make_task(service,monkeypatch,'继续处理，结果正常。',start=7,end=9)
    got=org(service,'/context',{'task_id':tb,'digest':ub['digest']})
    assert got['notices']
    assert '继续处理' in got['records'][0]['text']


def test_early_memory_can_show_later_linked_result(service,monkeypatch):
    ta,ua=make_task(service,monkeypatch,'demo 最初需求：取消后不能残留子进程。')
    tb,ub=make_task(service,monkeypatch,'继续处理：取消逻辑已调整。',start=4,end=6)
    tc,uc=make_task(service,monkeypatch,'继续验证：取消后子进程数为零。',start=7,end=9)
    got=org(service,'/context',{'task_id':ta,'digest':ua['digest']})
    assert '子进程数为零' in '\n'.join(r['text'] for r in got['records'])


def test_item_context_follows_actual_derivation_even_without_archive_project(service,monkeypatch):
    from evolvmem.knowledge_api import dispatch
    from evolvmem.unit_derivations import record_derivation
    tid,u=make_task(service,monkeypatch,'demo 最初需求：先确认原始需求，再判断项目。')
    item=service.knowledge().create({'title':'核查方法','body':'判断归属前需要看到原始需求。','project':'demo','action':'draft'})
    record_derivation(service,tid,u['digest'],item['id'],kind='knowledge',project='demo')
    result=dispatch(service,'GET',f'items/{item["id"]}/context')
    assert '最初需求' in result['contexts'][0]['records'][0]['text']


def test_oldest_source_change_invalidates_review_context_stamp(service,monkeypatch):
    ta,ua=make_task(service,monkeypatch,'demo 最初需求：保留原始编号。')
    tb,ub=make_task(service,monkeypatch,'继续处理：正在调整导出字段。',start=4,end=6)
    tc,uc=make_task(service,monkeypatch,'继续验证：已经保留编号。',start=7,end=9)
    before=reasoning.stamp(service,uc)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=?',(ta,))
    assert reasoning.stamp(service,uc)!=before


def test_later_result_is_labelled_as_result_not_predecessor():
    text='最初需求：demo 支持取消。';result='后续验证：取消后无残留进程。'
    context={'predecessor':{'text':'无关的前文'},'task_history':{'records':[
        {'text':text,'focus':True,'current':True,'relation':'current'},
        {'text':result,'focus':False,'current':True,'relation':'successor'}]}}
    value=reasoning.validate({'decision':'include','reason':'需求明确且已有后续验证。','quote':text,
        'chain':{'need':text,'result':result},'targets':[{'project':'demo','role':'owner','quote':text}]},
        {'text':text},context,{'demo'})
    assert value['decision']=='include'
    assert value['chain_sources']['result']=='后续过程与结果'
    assert value['uses_predecessor'] is False


def test_long_context_is_not_silently_clipped_or_classified(service,monkeypatch):
    tid,u=make_task(service,monkeypatch,'demo 最初需求：保留全链路。')
    from evolvmem.task_context import MODEL_MAX_CHARS
    history={'records':[{'text':'完整原文。'*MODEL_MAX_CHARS,'current':True,'focus':True}]}
    monkeypatch.setattr(reasoning,'context_for',lambda *a:{'task_history':history})
    calls=[]
    monkeypatch.setattr('evolvmem.auto_organization._load_llm',lambda:(None,lambda *a,**kw:calls.append(a)))
    got=reasoning.analyze(service,[u])[f'{tid}:{u["digest"]}']
    assert not calls and got['decision']=='uncertain' and not got['projects']
    assert '完整任务上下文' in got['reason']


def test_later_unrelated_task_is_not_a_result_of_original_need(service,monkeypatch):
    ta,ua=make_task(service,monkeypatch,'demo 最初需求：执行器支持取消。')
    make_task(service,monkeypatch,'demo 另外一个新任务：调整界面配色。',start=4,end=6)
    got=org(service,'/context',{'task_id':ta,'digest':ua['digest']})
    assert '调整界面配色' not in '\n'.join(r['text'] for r in got['records'] if r.get('relation')!='session_background')


def test_unorganized_first_batch_remains_visible_for_human_review(service,monkeypatch):
    first=craft(service,session=SESSION,start=1,end=3,text='最初需求：取消执行后不能残留子进程。')
    tid,u=make_task(service,monkeypatch,'继续验证：取消后子进程为零。',start=4,end=6)
    got=org(service,'/context',{'task_id':tid,'digest':u['digest']})
    earlier=next(r for r in got['records'] if r['source_key']==f'archive:{first.id}')
    assert '最初需求' in earlier['text']
    assert earlier['relation']=='session_background' and earlier['evidence_eligible'] is False
    # A visible conversation is not automatically a verified task dependency.
    assert '最初需求' not in '\n'.join(reasoning.evidence_texts(u,reasoning.context_for(service,u)))


def test_stored_link_must_also_match_current_native_session(service,monkeypatch):
    ta,ua=make_task(service,monkeypatch,'demo 本会话需求：只验证同会话关联。')
    tb,ub=make_task(service,monkeypatch,'继续处理：调整执行器。',start=4,end=6)
    tc,uc=make_task(service,monkeypatch,'demo 另一个会话的新任务。',session=OTHER_SESSION)
    conn=service.store._connection()
    basis=conn.execute('SELECT context_basis FROM organization_units WHERE task_id=?',(tb,)).fetchone()[0]
    assert basis
    with service.store.transaction():
        conn.execute('UPDATE organization_units SET context_basis=? WHERE task_id=?',(basis,tc))
    got=org(service,'/context',{'task_id':tc,'digest':uc['digest']})
    assert '本会话需求' not in '\n'.join(r['text'] for r in got['records'])
