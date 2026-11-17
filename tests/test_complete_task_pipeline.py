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
    worker._assign(service,org.task_view(service,task['id']),materialize=False)
    unit=org.unit_views(service,task['id'])[0]
    assert unit['project']=='demo' and unit['decision']=='auto'
    assert unit['business_analysis']['projects']==['demo']
    assert unit['item_id'] is None
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
    observed=[]
    def suggest(service, units):
        observed.extend(units)
        return {}
    monkeypatch.setattr(ownership_reasoning,'analyze',suggest)
    worker._assign(service,org.task_view(service,task['id']))
    units=org.unit_views(service,task['id'])
    current=next(u for u in units if u['decision']=='review')
    assert current['project']==''
    assert current['task_record']['confirmed_background'][0]['projects']==['other']
    assert all('在 Demo 增加写入开关。' not in u['text'] for u in observed)
    assert units[0]['decision']=='manual' and units[0]['project']=='other'
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
    from evolvmem.organization_review_decisions import archive_one
    unit=archive_one(service,{'task_id':task['id'],'digest':unit['digest'],'expected_revision':unit['revision'],'action':'include','project':'demo'})['unit']
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


def test_merging_fully_reviewed_fragments_preserves_their_common_decision(service, monkeypatch):
    old, unit = legacy_unit(service)
    second = craft(service, session=SESSION, start=5, end=8,
                   text='同一开关更新失败，修复后仍待实际验收。')
    other = org.enqueue(service, [f'archive:{second.id}'])['items'][0]
    row = org._task_row(service, other['id'])
    text = row['source_snapshot']
    org.OrganizationWorker(service.config)._store_units(service, row, [{
        'source_start':0, 'source_end':len(text), 'text':text, 'title':'已确认的后续修复',
        'category':'task_requirement', 'project_hint':'', 'evidence_quote':''}])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='demo',projects='[\"demo\"]' WHERE task_id IN (?,?)",
            (old['id'], other['id']))
    task, worker, calls = prepare(service, monkeypatch)
    current = org.unit_views(service, task['id'])[0]
    assert current['decision'] == 'manual'
    assert json.loads(current['projects']) == ['demo']
    worker._assign(service, org.task_view(service, task['id']))
    assert len(calls) == 2, 'already reviewed content must not be classified again'


def test_reviewed_requirement_keeps_ownership_when_task_adds_assistant_progress(service, monkeypatch):
    old, unit = legacy_unit(service)
    # Legacy segmentation confirmed the user's request, but left the following
    # assistant update outside its range (the production executor-heartbeat case).
    end = unit['text'].index('\n\nAI：')
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='demo',projects='[\"demo\"]',"
            "text=?,source_end=? WHERE task_id=?", (unit['text'][:end], end, old['id']))
    task = conversation_tasks.enqueue(service, [old['source_key']])['items'][0]
    row = org._task_row(service, task['id'])
    worker = org.OrganizationWorker(service.config)
    worker._store_units(service, row, [{
        'source_start':0, 'source_end':len(row['source_snapshot']),
        'source_ranges':[[0,len(row['source_snapshot'])]],
        'text':row['source_snapshot'], 'title':'原需求及后续处理',
        'category':'task_requirement', 'project_hint':'', 'evidence_quote':''}])
    conversation_tasks.protect_prior_decisions(service, row)
    current = org.unit_views(service, task['id'])[0]
    assert current['decision'] == 'manual'
    assert json.loads(current['projects']) == ['demo']
    assert current['task_record']['prior_decisions'][0]['project'] == 'demo'
    monkeypatch.setattr(ownership_reasoning, 'analyze', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('an already reviewed task must not go back to ownership AI')))
    worker._assign(service, org.task_view(service, task['id']))
    from evolvmem.organization_review import groups
    assert groups(service, {'task_id':task['id']})['total'] == 0


def test_progress_inheritance_requires_reviewed_turn_and_agreement():
    from evolvmem.conversation import render
    messages = [{'role':'user','content':'修复 Demo 心跳刷新'},
                {'role':'assistant','content':'已补上进入页面和切回窗口刷新'}]
    spans = message_spans(messages)
    text = render(messages)
    decision = {'project':'demo','projects':'["demo"]','knowledge_scope':'project','disposition':'keep'}
    confirmed = [(decision, [[0,spans[0]['end']]])]
    ranges = [[0,len(text)]]
    assert conversation_tasks._covered_decision(text, ranges, confirmed, messages=messages) == decision
    # Reviewing a whole reply establishes ownership of its original request too.
    assert conversation_tasks._covered_decision(text, ranges, [(decision, [[spans[1]['start'],spans[1]['end']]])], messages=messages) == decision
    conflicting = {**decision,'project':'other','projects':'["other"]'}
    assert conversation_tasks._covered_decision(text, ranges, [*confirmed,(conflicting,[[spans[1]['start'],spans[1]['end']]])], messages=messages) is None
    for role in ('user','unknown'):
        changed = [messages[0], {'role':role,'content':'AI：另一个项目也需要修改'}]
        changed_text = render(changed)
        assert conversation_tasks._covered_decision(changed_text, [[0,len(changed_text)]], confirmed, messages=changed) is None


def test_reviewed_reply_cannot_cover_new_requests_unknown_authors_or_other_tasks():
    from evolvmem.conversation import render
    decision = {'projects':['demo'], 'knowledge_scope':'project', 'disposition':'keep'}
    original = [{'role':'user','content':'采集进度卡住，请修复'},
                {'role':'assistant','content':'已恢复采集，进度继续增长'}]
    for role in ('user', 'unknown'):
        messages = [*original, {'role':role,'content':'还需要修改另一个项目'},
                    {'role':'assistant','content':'正在处理新的要求'}]
        spans = message_spans(messages); text = render(messages)
        confirmed = [(decision, [[spans[1]['start'],spans[1]['end']]])]
        assert conversation_tasks._covered_decision(text, [[0,len(text)]], confirmed, messages=messages) is None
    messages = [original[0], {'role':'user','content':'另一件事：改报表'}, original[1]]
    spans = message_spans(messages); text = render(messages)
    confirmed = [(decision, [[spans[2]['start'],spans[2]['end']]])]
    ranges = [[0,spans[0]['end']], [spans[2]['start'],spans[2]['end']]]
    assert conversation_tasks._covered_decision(text, ranges, confirmed, messages=messages) is None
    messages = original; spans = message_spans(messages); text = render(messages)
    confirmed = [(decision, [[spans[1]['start']+4,spans[1]['end']]])]
    assert conversation_tasks._covered_decision(text, [[0,len(text)]], confirmed, messages=messages) is None
    confirmed = [(decision, [[spans[1]['start'],spans[1]['end']]])]
    assert conversation_tasks._covered_decision(text, [[0,spans[0]['end']]], confirmed, messages=messages) is None


def test_reviewed_reply_preserves_multi_project_and_personal_scope():
    from evolvmem.conversation import render
    messages = [{'role':'user','content':'排查导出问题'},
                {'role':'assistant','content':'已经修复并通过验证'},
                {'role':'assistant','content':'补充：真实页面也已恢复'}]
    spans = message_spans(messages); text = render(messages)
    for scope, projects in [('project',['demo','other']),('global',[])]:
        decision = {'knowledge_scope':scope,'projects':projects,'disposition':'keep'}
        confirmed = [(decision, [[spans[1]['start'],spans[1]['end']]])]
        assert conversation_tasks._covered_decision(text, [[0,len(text)]], confirmed, messages=messages) == decision


def merged_reviewed_reply(service):
    old, unit = legacy_unit(service)
    start = unit['text'].index('AI：')
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='demo',projects='[\"demo\"]',"
            "text=?,source_start=?,reason='人工确认处理结果属于 Demo' WHERE task_id=?",
            (unit['text'][start:],start,old['id']))
    task = conversation_tasks.enqueue(service,[old['source_key']])['items'][0]
    row = org._task_row(service,task['id']); text = row['source_snapshot']
    org.OrganizationWorker(service.config)._store_units(service,row,[{
        'source_start':0,'source_end':len(text),'source_ranges':[[0,len(text)]],
        'text':text,'title':'同一需求与已确认处理结果','category':'task_requirement',
        'project_hint':'','evidence_quote':''}])
    return row


def test_reviewed_reply_restores_complete_task_without_ai_or_repeated_review(service,monkeypatch):
    task = merged_reviewed_reply(service)
    conversation_tasks.protect_prior_decisions(service,task)
    current = org.unit_views(service,task['id'])[0]
    assert current['decision']=='manual' and json.loads(current['projects'])==['demo']
    monkeypatch.setattr(ownership_reasoning,'analyze',lambda *a,**kw: (_ for _ in ()).throw(
        AssertionError('confirmed replies must not be sent back for project judgment')))
    org.OrganizationWorker(service.config)._assign(service,org.task_view(service,task['id']))
    from evolvmem.organization_review import groups
    assert groups(service,{'task_id':task['id']})['total']==0
    saved = org.unit_views(service,task['id'])[0]
    assert saved['item_id']
    conversation_tasks.protect_prior_decisions(service,task)
    assert org.unit_views(service,task['id'])[0]==saved


def test_current_exclusion_blocks_inheritance_from_reviewed_reply(service):
    from evolvmem import organization_exclusions as exclusions
    task = merged_reviewed_reply(service)
    current = org.find_unit(service,task['id'],org.unit_views(service,task['id'])[0]['digest'])
    full={**current,**{k:task[k] for k in ('source_key','source_revision','rule_revision')}}
    with service.store.transaction():
        exclusions.write_decision(service,full,{'projects':['demo'],'knowledge_scope':'project'},'exclude')
    conversation_tasks.protect_prior_decisions(service,task)
    current = org.unit_views(service,task['id'])[0]
    assert current['decision']=='review'
    assert exclusions.active(service,current,{'projects':['demo'],'knowledge_scope':'project'})


def test_old_exclusion_is_kept_when_reviewed_reply_matches_its_target(service):
    from evolvmem import organization_exclusions as exclusions
    task = merged_reviewed_reply(service)
    old = dict(service.store._connection().execute(
        'SELECT u.*,t.source_key,t.source_revision,t.rule_revision FROM organization_units u '
        'JOIN organization_tasks t ON t.id=u.task_id WHERE t.id<? ORDER BY t.id DESC LIMIT 1',
        (task['id'],)).fetchone())
    with service.store.transaction():
        exclusions.write_decision(service,old,{'projects':['demo'],'knowledge_scope':'project'},'exclude')
    conversation_tasks.protect_prior_decisions(service,task)
    current = org.unit_views(service,task['id'])[0]
    assert current['decision']=='review'
    assert exclusions.active(service,current,{'projects':['demo'],'knowledge_scope':'project'})


def test_changed_old_source_cannot_transfer_reviewed_reply(service):
    task = merged_reviewed_reply(service)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET source_snapshot='已发生修改的旧原文' WHERE id<?", (task['id'],))
    conversation_tasks.protect_prior_decisions(service,task)
    assert org.unit_views(service,task['id'])[0]['decision']=='review'


def test_progress_inheritance_respects_disjoint_tasks_and_shared_scopes():
    from evolvmem.conversation import render
    messages = [{'role':'user','content':'修复心跳刷新'},
                {'role':'user','content':'另一个任务：创建报表'},
                {'role':'assistant','content':'心跳刷新修复完成'}]
    spans = message_spans(messages)
    ranges = [[0,spans[0]['end']],[spans[2]['start'],spans[2]['end']]]
    for scope, projects in [('project',['demo','other']),('global',[])]:
        decision = {'knowledge_scope':scope,'projects':projects,'disposition':'keep'}
        confirmed = [(decision, [[0,spans[0]['end']]])]
        assert conversation_tasks._covered_decision(render(messages), ranges, confirmed, messages=messages) == decision
