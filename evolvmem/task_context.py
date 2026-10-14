"""Read original task sources from their beginning, with verifiable provenance.

Project ownership is not inferred here. A same-session predecessor is background;
only the reasoning step decides whether it belongs to the current task.
"""
import hashlib
import json

VERSION = 'task-context-v1'
MODEL_MAX_CHARS = 48000


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _record(service, task, unit, *, focus=False):
    from evolvmem import auto_organization as org, organization_context as ctx
    current=org._source_current(service,task)
    info=ctx.archive_info(service,task['source_key']) or {}
    archive=service.store.get_session_archive(info['archive_id']) if info else None
    if not archive and task['source_key'].startswith('archive:'):
        archive=service.store.get_session_archive(int(task['source_key'].split(':')[1]))
    return {'task_id':task['id'],'source_key':task['source_key'],
            'title':task['source_title'] or task['source_key'],
            'adapter':(archive or {}).get('adapter','资料'),
            'created_at':(archive or {}).get('created_at',task['created_at']),
            'session_id':info.get('session_id',''),
            'start_line':info.get('start_line'), 'end_line':info.get('end_line'),
            'text':task['source_snapshot'], 'current':current, 'focus':focus,
            'unit_digest':unit['digest'],'unit_start':unit['source_start'],'unit_end':unit['source_end'],
            'unit_revision':0 if focus else unit.get('revision',0),'source_revision':task['source_revision'],
            'relation':'current' if focus else 'predecessor'}


def _session_background(service, focus_source, records, notices):
    """Read unlinked original batches without borrowing their project decisions.

    Human review must not depend on an earlier batch already being classified.
    These records explain the conversation but cannot certify task continuity.
    """
    from evolvmem import auto_organization as org, organization_context as ctx
    info=ctx.archive_info(service,focus_source)
    if not info:return
    from evolvmem.history_organization import _unassigned_archives
    visible={r['id'] for r in _unassigned_archives(service)}
    policy=service.knowledge().rules.read()
    existing={r['source_key'] for r in records}
    spans=[(r['start_line'],r['end_line']) for r in records if r.get('start_line')]
    rows=service.store._connection().execute(ctx._sibling_sql(),ctx._sibling_params(info)).fetchall()
    for row in rows:
        key=f'archive:{row["id"]}'
        if key in existing:continue
        other=ctx.archive_info(service,key)
        if not other or other['head']!=info['head']:continue
        if any(other['start_line']<=end and other['end_line']>=start for start,end in spans):
            notices.append('原始会话存在重叠批次，重叠部分未自动拼接。');continue
        try:source,text=org.source_snapshot(service,key,visible=visible,policy=policy)
        except ValueError:
            notices.append('同会话的部分原文暂时不可读取。');continue
        archive=service.store.get_session_archive(row['id']) or {}
        title=(str(source.get('source') or '')+' · '+text.strip().split('\n',1)[0][:80]).strip(' ·')[:200]
        records.append({'task_id':0,'source_key':key,'title':title,
            'adapter':archive.get('adapter',''),'created_at':archive.get('created_at',''),
            'session_id':other['session_id'],'start_line':other['start_line'],'end_line':other['end_line'],
            'text':text,'current':True,'focus':False,'relation':'session_background',
            'evidence_eligible':False,'source_revision':source.get('expected_revision','')})
        spans.append((other['start_line'],other['end_line']))


def collect(service, unit, *, include_background=True):
    """Current source plus the complete verified predecessor chain, oldest first.

    Keep entire original sources: the first demand can be outside the selected
    unit or a former fixed-size neighbour/tail window. Related topics remain
    labelled source context, never silently treated as the current project.
    """
    from evolvmem import organization_context as ctx
    conn=service.store._connection()
    row=conn.execute('SELECT * FROM organization_tasks WHERE id=?',(unit.get('task_id'),)).fetchone()
    if row is None:
        return {'records':[],'notices':['未找到关联的原始会话，请核对资料来源。'],'revision':_hash(['missing',unit.get('task_id')])}
    from evolvmem.history_organization import source_excluded
    if source_excluded(service,row['source_key']):
        return {'records':[],'notices':['这份来源是系统内部检查副本，已停止作为业务原文使用。原始档案与人工判断仍保留，请从真实会话核查。'],
                'source_excluded':True,'revision':_hash(['excluded',row['source_key']])}
    from evolvmem import conversation_tasks, topic_segmentation
    if conversation_tasks.is_conversation(row['source_key']):
        from evolvmem import auto_organization as org, task_narrative
        task=dict(row)
        source=conversation_tasks.snapshot(service,task['source_key'])
        current=org._source_current(service,task)
        ranges=topic_segmentation.unit_ranges(unit)
        records=[]
        for part in source['records']:
            overlaps=[[max(a,part['start'])-part['start'],min(b,part['end'])-part['start']]
                      for a,b in ranges if a<part['end'] and b>part['start']]
            if not include_background and not overlaps:continue
            records.append({**part,'task_id':task['id'],'current':current,'focus':bool(overlaps),
                            'unit_digest':unit['digest'],'unit_ranges':overlaps,
                            'unit_start':overlaps[0][0] if overlaps else 0,
                            'unit_end':overlaps[0][1] if overlaps else 0,
                            'relation':'current' if overlaps else 'session_background',
                            'evidence_eligible':bool(overlaps)})
        return {'records':records,'notices':[] if current else ['原文或整理规则已更新，请重新整理后核对。'],
                'task_record':task_narrative.read(unit.get('task_record')),
                'focus':{'task_id':task['id'],'digest':unit['digest'],'text':unit['text']},
                'revision':_hash([VERSION,task['source_revision'],records])}
    task=dict(row);current_unit=dict(unit);records=[];notices=[];seen=set()
    while True:
        identity=(task['id'],current_unit['digest'])
        if identity in seen:
            notices.append('任务关联出现循环，已停止拼接；当前原文仍可核对。');break
        seen.add(identity)
        record=_record(service,task,current_unit,focus=not records)
        records.append(record)
        if not record['current']:
            notices.append('部分来源已更新，下面保留当时原文；归类前需要重新整理。');break
        # Explicit unit dependency first. A still-unclassified unit may inspect
        # the task's verified predecessor without inheriting its project.
        basis=ctx.decode(current_unit.get('context_basis'))
        if not basis and record['focus'] and current_unit.get('decision')!='auto':
            basis=ctx.build_basis(service,task)
        if not basis or basis.get('state')!=ctx.STATE_READY or not ctx.validate(service,basis):
            info=ctx.archive_info(service,task['source_key'])
            if info and info['start_line']>1:
                notices.append('尚未关联到更早的任务原文，不能据此认定需求从这里开始。')
            break
        identity=ctx.archive_info(service,task['source_key'])
        if not identity or identity['head']!=basis.get('session'):
            notices.append('关联记录与当前会话不一致，未合并为同一任务。');break
        parent=conn.execute('SELECT * FROM organization_tasks WHERE id=?',(basis['task_id'],)).fetchone()
        prior=conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                           (basis['task_id'],basis['unit_digest'])).fetchone()
        if parent is None or prior is None:
            notices.append('关联的前文暂时不可读取，任务上下文尚不完整。');break
        task,current_unit=dict(parent),dict(prior)
    records.reverse()
    # A memory saved during the requirement phase can also have later work and
    # results. Follow only recorded, still-valid continuation links, not names.
    queue=[(r['task_id'],r['unit_digest']) for r in records]
    while queue:
        parent_id,parent_digest=queue.pop(0)
        children=conn.execute("SELECT u.* FROM organization_context_basis b JOIN organization_units u "
            "ON u.task_id=b.unit_task_id AND u.digest=b.unit_digest "
            "WHERE b.prior_task_id=? AND b.prior_digest=? AND b.state='active' AND u.decision='auto'",
            (parent_id,parent_digest)).fetchall()
        for child in children:
            child=dict(child);identity=(child['task_id'],child['digest'])
            if identity in seen:continue
            seen.add(identity)
            basis=ctx.decode(child.get('context_basis'))
            if not ctx.validate(service,basis):continue
            child_task=conn.execute('SELECT * FROM organization_tasks WHERE id=?',(child['task_id'],)).fetchone()
            if child_task is None:continue
            info=ctx.archive_info(service,child_task['source_key'])
            if not info or info['head']!=basis.get('session'):continue
            added=_record(service,dict(child_task),child)
            if not added['current']:continue
            added['relation']='successor';records.append(added);queue.append(identity)
    focus_source=next(r['source_key'] for r in records if r['focus'])
    if include_background:
        _session_background(service,focus_source,records,notices)
    if any(r['relation']=='session_background' and r.get('start_line',0)<next((f.get('start_line') or 0 for f in records if f['focus']),0) for r in records):
        notices=[n.replace('尚未关联到更早的任务原文，不能据此认定需求从这里开始。',
                           '已显示同会话的早期原文，任务关联尚待核对。') for n in notices]
    records.sort(key=lambda r:(r.get('start_line') or 0,r['task_id']))
    value={'records':records,'notices':list(dict.fromkeys(notices)),
           'focus':{'task_id':unit['task_id'],'digest':unit['digest'],'text':unit['text']}}
    value['revision']=_hash([VERSION,records,value['notices']])
    return value


def read(service, body):
    from evolvmem import auto_organization as org, ownership_reasoning as reasoning
    unit=org.find_unit(service,int(body.get('task_id') or 0),str(body.get('digest') or ''))
    result=collect(service,unit)
    if result.get('source_excluded'):
        result.update(analysis={},stages=[])
        return result
    analysis=reasoning.validated(unit)
    result['analysis']=analysis
    from evolvmem import task_narrative
    narrative=task_narrative.read(unit.get('task_record'))
    stages=[]
    for key,label in reasoning.STAGES.items():
        field={'need':'requirement','analysis':'analysis','change':'steps','result':'result'}[key]
        entries=narrative.get(field) or []
        if isinstance(entries,dict):entries=[entries]
        refs=[]
        for entry in entries:
            for ref in entry.get('evidence',[]):
                if ref.get('quote') and ref['quote'] in unit['text'] and ref not in refs:refs.append(ref)
        # Task reconstruction precedes classification. An undecided project or
        # manual classification need not have an ownership chain at all.
        quote='\n\n'.join(dict.fromkeys(r['quote'] for r in refs))
        if not quote:quote=analysis.get('chain',{}).get(key,'')
        stages.append({'key':key,'title':label,'quote':quote,'evidence':refs,
                       'state':'recorded' if quote else 'not_identified'})
    result['stages']=stages
    return result


def for_item(service, item_id):
    """Follow actual derivations/source IDs; never search by similar wording."""
    from evolvmem import auto_organization as org
    item=service.knowledge().detail(item_id)
    conn=service.store._connection()
    links=conn.execute('SELECT DISTINCT d.unit_task_id,d.unit_digest FROM unit_derivations d '
                       'JOIN organization_tasks t ON t.id=d.unit_task_id '
                       'WHERE d.item_id=? ORDER BY t.id DESC',(item_id,)).fetchall()
    contexts=[];seen=set()
    for link in links:
        try:context=read(service,{'task_id':link['unit_task_id'],'digest':link['unit_digest']})
        except ValueError:continue
        source=next((r['source_key'] for r in context['records'] if r['focus']),None)
        if source in seen:continue
        seen.add(source);contexts.append(context)
    if not contexts:
        for source in item['sources']:
            aid=source.get('archive_id')
            if not aid or aid in seen:continue
            seen.add(aid)
            try:
                row,text=org.source_snapshot(service,f'archive:{aid}')
                archive=service.store.get_session_archive(aid) or {}
                contexts.append({'records':[{'source_key':f'archive:{aid}','title':archive.get('adapter','会话')+' · 原始对话',
                    'adapter':archive.get('adapter',''),'created_at':archive.get('created_at',''),
                    'text':text,'current':True,'focus':True,'unit_start':-1,'unit_end':-1}],
                    'notices':['这条记忆尚未关联到具体任务片段，以下为来源会话原文。'],'stages':[]})
            except ValueError:
                contexts.append({'records':[],'notices':['关联的原始会话已不可读取。'],'stages':[]})
    if not contexts:
        contexts=[{'records':[{'source_key':f'item:{item_id}','title':item['title'],'text':item['body'],
                    'current':True,'focus':True,'unit_start':-1,'unit_end':-1}],
                   'notices':['这条资料没有关联原始会话；目前只能核对已保存正文，不能补出需求和结果。'],'stages':[]}]
    return {'title':item['title'],'contexts':contexts}
