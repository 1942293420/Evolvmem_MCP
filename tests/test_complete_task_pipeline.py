import json
from tests.test_cross_batch_context import config, service, craft, SESSION
from evolvmem import auto_organization as org, conversation_tasks, ownership_reasoning
from evolvmem.topic_segmentation import message_spans, unit_messages


def prepare(service,monkeypatch):
    a=craft(service,session=SESSION,start=1,end=4,text='在 Demo 增加写入开关。')
    b=craft(service,session=SESSION,start=5,end=8,text='同一开关更新失败，修复后仍待实际验收。')
    task=conversation_tasks.enqueue(service,[f'archive:{a.id}',f'archive:{b.id}'])['items'][0]
    calls=[]
    def provider(prompt,*args,**kwargs):
        calls.append(prompt)
        if '完整任务识别员' in prompt:
            return json.dumps({'segments':[{'task':'new','start_id':1,'end_id':4,'title':'增加写入开关并修复更新','need_quote':'在 Demo 增加写入开关。'}]})
        if '任务资料整理员' in prompt:
            return json.dumps({'requirement':{'text':'增加写入开关','evidence':[{'message':1,'quote':'增加写入开关'}]},
                'analysis':None,'steps':[{'text':'更新失败后继续修复','evidence':[{'message':3,'quote':'同一开关更新失败'}]}],
                'result':{'text':'仍待验收','evidence':[{'message':3,'quote':'仍待实际验收'}]},
                'state':'open','outstanding':['实际验收']})
        assert '业务资料归类核对员' in prompt
        assert 'organized_task' in prompt and '仍待验收' in prompt
        return json.dumps({'items':[{'id':0,'decision':'include','reason':'修改 Demo 的写入开关',
             'quote':'在 Demo 增加写入开关。','chain':{'need':'在 Demo 增加写入开关。','result':'仍待实际验收'},
             'targets':[{'project':'demo','role':'owner','quote':'在 Demo 增加写入开关。'}]}]})
    monkeypatch.setattr(org,'_load_llm',lambda:({},provider))
    worker=org.OrganizationWorker(service.config)
    worker._segment(service,task)
    return task,worker,calls


def test_worker_organizes_before_classifying_and_keeps_full_sources(service,monkeypatch):
    task,worker,calls=prepare(service,monkeypatch)
    assert len(calls)==2
    units=org.unit_views(service,task['id'])
    assert len(units)==1 and units[0]['task_record']['result']['text']=='仍待验收'
    assert org.task_view(service,task['id'])['stage']=='assignment'
    worker._assign(service,org.task_view(service,task['id']))
    unit=org.unit_views(service,task['id'])[0]
    assert unit['project']=='demo' and unit['decision']=='auto'
    from evolvmem.task_context import read
    context=read(service,{'task_id':task['id'],'digest':unit['digest']})
    assert len(context['records'])==2
    assert '增加写入开关' in context['records'][0]['text']
    assert '实际验收' in context['records'][1]['text']
    assert len(calls)==3


def test_disjoint_task_extraction_never_includes_intervening_goal():
    spans=message_spans([{'role':'user','content':'需求甲'},{'role':'user','content':'需求乙'},
                         {'role':'assistant','content':'甲处理结果'}])
    unit={'source_ranges':[[spans[0]['start'],spans[0]['end']],[spans[2]['start'],spans[2]['end']]]}
    assert [m['content'] for m in unit_messages(unit,spans)]==['需求甲','甲处理结果']


def test_long_complete_task_uses_narrative_instead_of_refusing_length(service,monkeypatch):
    task,worker,calls=prepare(service,monkeypatch)
    unit=org.unit_views(service,task['id'])[0]
    unit['text']+='\n重复日志'*12000
    result=ownership_reasoning.analyze(service,[unit])[f"{task['id']}:{unit['digest']}"]
    assert result['decision']=='include'
    assert '重复日志' not in calls[-1]


def test_context_revision_invalidates_when_earlier_archive_is_deleted(service,monkeypatch):
    task,worker,_=prepare(service,monkeypatch)
    first=conversation_tasks.members(service,task['source_key'])[0]
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET state='expired' WHERE id=?",(int(first.split(':')[1]),))
    assert not org.task_view(service,task['id'])['source_current']


def test_retry_reuses_completed_segmentation_and_narratives(service,monkeypatch):
    task,worker,calls=prepare(service,monkeypatch)
    assert len(calls)==2
    worker._segment(service,org.task_view(service,task['id']))
    assert len(calls)==2
    assert len(org.unit_views(service,task['id']))==1


def legacy_unit(service):
    src=craft(service,session=SESSION,start=1,end=4,text='在 Demo 增加写入开关。')
    task=org.enqueue(service,[f'archive:{src.id}'])['items'][0]
    row=org._task_row(service,task['id']);text=row['source_snapshot']
    org.OrganizationWorker(service.config)._store_units(service,row,[{
        'source_start':0,'source_end':len(text),'text':text,'title':'原人工判断',
        'category':'task_requirement','project_hint':'','evidence_quote':'在 Demo 增加写入开关。'}])
    return task,org.find_unit(service,task['id'],org.unit_views(service,task['id'])[0]['digest'])


def test_extending_a_manually_classified_fragment_requires_review(service,monkeypatch):
    old,unit=legacy_unit(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET decision='manual',project='other',projects='[\"other\"]' WHERE task_id=?",(old['id'],))
    task,worker,calls=prepare(service,monkeypatch)
    worker._assign(service,org.task_view(service,task['id']))
    current=org.unit_views(service,task['id'])[0]
    assert current['decision']=='review' and current['project']==''
    assert current['task_record']['prior_decisions'][0]['project']=='other'
    assert org.find_unit(service,old['id'],unit['digest'])['decision']=='manual'
    assert len(calls)==2, 'must not automatically broaden a human decision'


def test_original_exclusion_still_blocks_merged_task_from_same_target(service,monkeypatch):
    from evolvmem import organization_exclusions as exclusions
    old,unit=legacy_unit(service)
    full={**unit,**{k:old[k] for k in ('source_key','source_revision','rule_revision')}}
    with service.store.transaction():
        service.store._connection().execute('INSERT INTO organization_scope_exclusions VALUES(?,?,?,?,1,?)',
            (old['source_key'],unit['digest'],'project:demo',exclusions._stamp(full),'2026-10-13'))
    task,worker,_=prepare(service,monkeypatch)
    current=org.find_unit(service,task['id'],org.unit_views(service,task['id'])[0]['digest'])
    assert exclusions.active(service,current,{'projects':['demo'],'knowledge_scope':'project'})
    assert not exclusions.active(service,current,{'projects':['other'],'knowledge_scope':'project'})


def test_project_history_links_every_contributing_archive(service,monkeypatch):
    task,worker,_=prepare(service,monkeypatch)
    worker._assign(service,org.task_view(service,task['id']))
    unit=org.unit_views(service,task['id'])[0]
    sources=service.store._connection().execute('SELECT archive_id FROM context_sources WHERE item_id=? AND archive_id IS NOT NULL',(unit['item_id'],)).fetchall()
    assert {r['archive_id'] for r in sources}=={int(k.split(':')[1]) for k in conversation_tasks.members(service,task['source_key'])}


def test_new_arrival_does_not_summarize_unchanged_task_again(service,monkeypatch):
    task,worker,_=prepare(service,monkeypatch)
    previous=org.unit_views(service,task['id'])[0]['task_record']
    source=craft(service,session=SESSION,start=9,end=12,text='另一个需求：调整日期格式。')
    current=conversation_tasks.enqueue(service,[f'archive:{source.id}'])['items'][0]
    calls=[]
    def provider(prompt,*a,**kw):
        calls.append(prompt)
        if '完整任务识别员' in prompt:
            records=json.loads(prompt.split('本批原文：')[1])
            assert records[0]['id']==5 and len(records)==2
            return json.dumps({'segments':[{'task':'new','start_id':5,'end_id':6,'title':'调整日期','need_quote':'另一个需求：调整日期格式。'}]})
        assert '增加写入开关' not in prompt
        return json.dumps({'requirement':{'text':'调整日期','evidence':[{'message':1,'quote':'调整日期格式'}]},'analysis':None,'steps':[],'result':None,'outstanding':[],'state':'open'})
    monkeypatch.setattr(org,'_load_llm',lambda:({},provider))
    worker._segment(service,current)
    units=org.unit_views(service,current['id'])
    assert len(units)==2 and units[0]['task_record']==previous
    assert len(calls)==2
