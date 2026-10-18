"""Separate reviewed source ranges from new requirements without a model call."""
import json

from evolvmem import knowledge_identity as identity, topic_segmentation
from evolvmem.conversation_tasks import _covered_decision


def split(service, task, units, messages):
    parts = identity._parts(service, task)
    conn = service.store._connection()
    coverage = []
    for part in parts:
        for raw in conn.execute('SELECT b.*,k.decision_json,t.source_snapshot FROM knowledge_source_bindings b '
                'JOIN knowledge_objects k ON k.knowledge_id=b.knowledge_id AND k.retired=0 '
                'JOIN organization_tasks t ON t.id=b.task_id WHERE b.source_key=? AND b.approved=1', (part['source_key'],)):
            row = dict(raw)
            decision = json.loads(row['decision_json'])
            if decision.get('action') not in ('include','defer') or identity._hash(row['source_snapshot']) != row['snapshot_hash']:
                continue
            quote, pos = row['quote'], row['source_start']
            if part['text'][pos:pos+len(quote)] != quote:
                if not quote or part['text'].count(quote) != 1:
                    continue
                pos = part['text'].index(quote)
            coverage.append((part['start']+pos, part['start']+pos+len(quote), row['knowledge_id'], decision, quote))
    result = []
    body = task['source_snapshot']
    for original in units:
        unit = dict(original)
        ranges = topic_segmentation.unit_ranges(unit) or [[unit['source_start'],unit['source_end']]]
        if not identity._anchors(task, unit, parts):
            result.append(unit)
            continue
        relevant = [c for c in coverage if any(a<c[1] and b>c[0] for a,b in ranges)]
        confirmed = [(c[3], [[c[0],c[1]]]) for c in relevant]
        # A reviewed request followed only by assistant progress is still the
        # same task. Its existing role-aware inheritance remains authoritative.
        if not relevant or _covered_decision(body, ranges, confirmed, messages=messages):
            result.append(unit)
            continue
        segments = []
        for start,end in ranges:
            bounds = sorted({start,end,*[max(start,min(end,p)) for c in relevant for p in c[:2]]})
            for a,b in zip(bounds,bounds[1:]):
                if not body[a:b].strip():
                    continue
                owners = {c[2] for c in relevant if c[0]<=a and b<=c[1]}
                owner = next(iter(owners)) if len(owners)==1 else ''
                if segments and segments[-1][0]==owner and not body[segments[-1][1][-1][1]:a].strip():
                    segments[-1][1][-1][1]=b
                else:
                    segments.append((owner,[[a,b]]))
        if not any(owner for owner,_ in segments) or len(segments)<2:
            result.append(unit)
            continue
        background = [{'knowledge_id':c[2], 'text':c[4], 'projects':c[3].get('projects',[]),
                       'action':c[3]['action']} for c in relevant]
        background = list({json.dumps(x,sort_keys=True):x for x in background}.values())
        record = unit.get('task_record') or {}
        if isinstance(record,str):
            record = json.loads(record)
        for owner, spans in segments:
            text = '\n\n'.join(body[a:b] for a,b in spans)
            # Whole-task summaries cannot describe a new fragment. Preserve
            # only source-backed facts/relations wholly supported by this text.
            fragment = {'version':1, 'new_content':not bool(owner), 'confirmed_background':background if not owner else []}
            for field in ('facts','relations'):
                fragment[field] = [f for f in record.get(field,[]) if f.get('evidence') and
                                   all(e.get('quote') and e['quote'] in text for e in f['evidence'])]
            result.append({**unit, 'source_start':spans[0][0], 'source_end':spans[-1][1], 'source_ranges':spans,
                           'text':text, 'cleaned_text':text, 'title':text.strip().split('\n')[0][:100],
                           'task_record':fragment, 'ownership_analysis':{}, 'project_hint':'',
                           'disposition':'keep', 'disposition_reason':'', 'evidence_quote':'', 'continues_context':False})
    return result


def prepare(service, body):
    """Upgrade only the visible pending tasks; no historical/model replay."""
    from evolvmem import auto_organization as org
    ids = body.get('task_ids')
    if not isinstance(ids,list) or len(ids)>20 or any(type(i) is not int for i in ids):
        raise ValueError('invalid_items')
    changed = 0
    for task_id in set(ids):
        with service.store.transaction():
            task = org._task_row(service, task_id)
            if task['status'] not in ('review','completed','failed'):
                continue
            if not org.task_view(service,task_id)['source_current']:
                continue
            units = [dict(r) for r in service.store._connection().execute(
                'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,))]
            if not any(u['decision']=='review' for u in units):
                continue
            identity.capture_existing(service, task)
            for u in units:
                for key in ('task_record','source_ranges','ownership_analysis'):
                    u[key] = json.loads(u[key])
            pieces = split(service, task, units, org.source_messages(service,task['source_key']))
            if pieces == units:
                continue
            org.OrganizationWorker(service.config)._store_units(service,task,pieces)
            # This on-demand upgrade does not run the extraction worker. Link
            # retained manual fragments now, so the obsolete aggregate cannot
            # make their already archived knowledge disappear from recall.
            from evolvmem.unit_derivations import record_derivation
            for u in service.store._connection().execute(
                    "SELECT * FROM organization_units WHERE task_id=? AND decision='manual' AND disposition='keep' AND item_id IS NOT NULL",
                    (task_id,)).fetchall():
                record_derivation(service,task_id,u['digest'],u['item_id'],kind='history',project=u['project'])
            # Keep completed extraction of unchanged fragments intact.
            for u in units:
                service.store._connection().execute(
                    'UPDATE organization_units SET extraction_stage=?,extraction_signature=? WHERE task_id=? AND digest=?',
                    (u['extraction_stage'],u['extraction_signature'],task_id,u['digest']))
            org._refresh_task_state(service,task_id)
            changed += 1
    return {'changed':changed}
