"""Background AI review of business criteria, with source-bound proposals."""
import json
import time
import uuid

from evolvmem import knowledge_scope as ks
from evolvmem.context_store import _now_iso

SCHEMA = """CREATE TABLE IF NOT EXISTS organization_review_jobs(
 id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
 base_revision TEXT NOT NULL, judgments TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', processed INTEGER NOT NULL DEFAULT 0,
 total INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def _key(unit):
    return f"{unit['task_id']}:{unit['digest']}"


def judge(service, units, criteria, exceptions, target):
    from evolvmem.ownership_reasoning import analyze
    return analyze(service, units, criteria=criteria, exceptions=exceptions, target=target)


# Only server-created snapshots are accepted; browser fields cannot seed verdicts.
_REQUEST_FIELDS = ('group_id', 'selection', 'task_id', 'digest', 'projects', 'project',
                   'knowledge_scope', 'condition', 'exceptions', 'scope', 'guidance', 'continue_job_id')


def _units(service):
    from evolvmem import organization_review as review
    return {_key(u):u for g in review._groups(service) for u in g['members']}


def _stamp(unit, service):
    from evolvmem.organization_review import _hash
    # Extraction progress and updated_at do not change the reviewed content.
    from evolvmem.ownership_reasoning import stamp
    return _hash([{k:unit[k] for k in ('revision','text','title','source_revision','rule_revision')}, stamp(service,unit)])


def _signature(service, request):
    from evolvmem.organization_review import _hash
    target=ks.normalize(service,request,required=True)
    names={p['project']:p['display_name'] or p['project'] for p in service.knowledge().registry()}
    from evolvmem.ownership_reasoning import VERSION
    from evolvmem.task_context import VERSION as CONTEXT_VERSION
    return _hash([VERSION, CONTEXT_VERSION, target, [names.get(p,p) for p in target['projects']],
                  str(request.get('condition','')).strip(), str(request.get('exceptions','')).strip(),
                  service.knowledge().rules.read()['revision']])


def _prepare(service, request, base, units):
    return {**request, '_preview':base, '_signature':_signature(service,request),
            '_snapshots':{_key(u):_stamp(units[_key(u)],service) for u in base['excluded']
                          if u.get('reason_code')=='ai_pending' and _key(u) in units}}


def _restore_request(service, row, units=None):
    """Old jobs can be adopted only when their original full revision still matches."""
    from evolvmem import organization_review as review
    request=json.loads(row['request'])
    if '_snapshots' not in request:
        request['_signature'] = 'legacy-business-evidence'
        base=review.preview(service,request,_judgments={})
        if base['revision']!=row['base_revision']:
            # Recover an old batch after new arrivals only if reconstruction
            # reproduces its exact stored hash. Dates alone never prove validity.
            groups=[{**g,'members':[u for u in g['members'] if u['created_at']<=row['created_at']]}
                    for g in review._groups(service)]
            base=review.preview(service,request,_judgments={},
                                _groups_snapshot=[g for g in groups if g['members']])
        if base['revision']!=row['base_revision']:
            raise ValueError('review_preview_changed')
        request=_prepare(service,request,base,units if units is not None else _units(service))
        request['_signature']='legacy-business-evidence'
        with service.store.transaction():
            service.store._connection().execute('UPDATE organization_review_jobs SET request=? WHERE id=?',
                                                (json.dumps(request,ensure_ascii=False),row['id']))
    return request


def _same_request(a,b):
    # Compare the normalized preview fields, not transient form fields or job IDs.
    keys=('group_id','selection','projects','knowledge_scope','condition','exceptions','scope','guidance')
    return all(a.get(k)==b.get(k) for k in keys)


def start(service, body, *, _cached_only=False):
    from evolvmem import organization_review as review
    request={k:body[k] for k in _REQUEST_FIELDS if k in body}
    request.update(matching_mode='semantic',job_id=uuid.uuid4().hex)
    conn=service.store._connection()
    with service.store.transaction():
        base=review.preview(service,request,_judgments={})
        units=_units(service)
        rows=[dict(r) for r in conn.execute('SELECT rowid AS job_order,* FROM organization_review_jobs ORDER BY created_at DESC,rowid DESC LIMIT 100').fetchall()]
        if request.get('continue_job_id'):
            parent=_row(service,request['continue_job_id'])
            previous=_restore_request(service,parent,units)
            if parent['status']!='completed' or not _same_request(base,previous['_preview']) or _signature(service,request)!=previous['_signature']:
                raise ValueError('review_preview_changed')
            cutoff=conn.execute('SELECT rowid FROM organization_review_jobs WHERE id=?',(parent['id'],)).fetchone()[0]
            skipped=set()
            for old in rows:
                previous=json.loads(old['request'])
                if old['job_order']>cutoff or previous.get('_signature')!=_signature(service,request):continue
                for key in json.loads(old['judgments']):
                    if key in units and previous.get('_snapshots',{}).get(key)==_stamp(units[key],service):skipped.add(key)
            request['_skip_keys']=sorted(skipped)
            groups=[{**g,'members':[u for u in g['members'] if _key(u) not in skipped]} for g in review._groups(service)]
            base=review.preview(service,request,_judgments={},_groups_snapshot=groups)
        request=_prepare(service,request,base,units)
        cached={}
        for old in rows:
            try:
                previous=_restore_request(service,old,units)
            except ValueError:
                continue
            if previous['_signature']!=request['_signature']:
                continue
            if (_same_request(base,previous['_preview']) and request['_snapshots']==previous['_snapshots']
                    and old['status'] in ('pending','running','completed') and not previous.get('_applied') and not _cached_only):
                return get(service,old['id'])
            for key,verdict in json.loads(old['judgments']).items():
                if key not in cached and key in request['_snapshots'] and previous['_snapshots'].get(key)==request['_snapshots'][key]:
                    cached[key]=verdict
        request['_reused']=len(cached)
        total=len(request['_snapshots'])
        if _cached_only and len(cached)!=total:
            return None
        status='completed' if len(cached)==total else 'pending'
        conn.execute('INSERT INTO organization_review_jobs(id,status,request,base_revision,judgments,processed,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (request['job_id'],status,json.dumps(request,ensure_ascii=False),base['revision'],
                      json.dumps(cached,ensure_ascii=False),len(cached),total,_now_iso(),_now_iso()))
    return get(service,request['job_id'])


def _row(service, job_id):
    row=service.store._connection().execute('SELECT * FROM organization_review_jobs WHERE id=?',(str(job_id),)).fetchone()
    if row is None:
        raise ValueError('review_job_not_found')
    return dict(row)


def get(service, job_id, *, _current_units=None):
    row=_row(service,job_id)
    result={k:row[k] for k in ('id','status','error','processed','total')}
    request=json.loads(row['request'])
    result['request']={k:request[k] for k in _REQUEST_FIELDS if k in request}
    if request.get('_signature') != _signature(service,request):
        result['request'].pop('continue_job_id',None)
    if row['status']=='completed':
        result['proposal']=proposal(service, {'job_id':row['id']}, _current_units=_current_units)
    return result


def recent(service):
    """Recover saved work on refresh, without starting any AI or applying changes."""
    rows=service.store._connection().execute('SELECT * FROM organization_review_jobs ORDER BY created_at DESC,rowid DESC LIMIT 20').fetchall()
    from evolvmem import organization_review as review
    # One request-local view: expired batches must not each re-read every
    # current conversation (and block all other Web requests behind the lock).
    groups=review._groups(service) if rows else []
    by_group={g['id']:g for g in groups}
    by_member={_key(u):g for g in groups for u in g['members']}
    by_source={u['source_key']:g for g in groups for u in g['members']}
    units={_key(u):u for g in groups for u in g['members']}
    for row in rows:
        request=json.loads(row['request'])
        if request.get('_applied'):continue
        group=by_group.get(request.get('group_id'))
        if group is None:
            # The anchor may have been saved while other frozen members still
            # need review. Only non-judged entries may follow a reprocessed source.
            entries=request.get('_preview',{}).get('excluded',[])
            group=next((by_member[_key(u)] for u in entries if _key(u) in by_member),None)
            if group is None:
                group=next((by_source[u['source_key']] for u in entries
                            if u.get('reason_code')!='ai_pending' and u.get('source_key') in by_source),None)
        if group is None:continue
        try:
            job = get(service,row['id'],_current_units=units)
            return {'job':job | {'resume_group_id':group['id']}}
        except ValueError:
            continue
    return {'job':None}


def proposal(service, body, *, _current_units=None):
    from evolvmem import organization_review as review
    row=_row(service,body.get('job_id'))
    if row['status']!='completed':
        raise ValueError('review_ai_not_ready')
    units=_units(service) if _current_units is None else _current_units
    request=_restore_request(service,row,units)
    current_signature=_signature(service,request)
    judgments=json.loads(row['judgments'])
    from evolvmem import organization_exclusions
    exclusions=organization_exclusions.rows(service)
    # Frozen membership: a new unrelated arrival must not expand or discard this batch.
    result=json.loads(json.dumps(request['_preview']))
    result['eligible'],result['excluded']=[],[]
    result['needs_recheck']=current_signature!=request.get('_signature')
    result['handled_count']=0
    result['ai_judged_count']=len(judgments)
    policy=service.knowledge().rules.read()['revision']
    seen=set()
    by_source={}
    for unit in units.values():by_source.setdefault(unit['source_key'],[]).append(unit)
    for entry in request['_preview']['excluded']:
        key=_key(entry)
        if key in seen:continue
        unit=units.get(key)
        excluded=organization_exclusions.active(service,unit,request,exclusions) if unit else None
        if excluded:
            seen.add(key)
            result['excluded'].append(review._brief(unit)|excluded)
            continue
        if entry.get('reason_code')!='ai_pending':
            # Judgment membership stays frozen; preparation status is live.
            # A superseded old unit must not keep offering the reprocess button.
            current=[units[key]] if key in units else by_source.get(entry['source_key'],[])
            for unit in current:
                candidate=_key(unit)
                if candidate in seen or candidate in request['_snapshots']:continue
                seen.add(candidate)
                excluded=organization_exclusions.active(service,unit,request,exclusions)
                if excluded:
                    result['excluded'].append(review._brief(unit)|excluded)
                    continue
                if unit['rule_revision']!=policy:
                    reason,code='这份资料使用旧版整理规则，需按最新规则重新整理。','rule_changed'
                elif unit['disposition']=='review':
                    reason,code='请先确认这条资料是否值得保留。','value_review'
                else:
                    reason,code='本条尚未进行归类判断，可继续判断下一批。','batch_limit'
                result['excluded'].append(review._brief(unit)|{'reason':reason,'reason_code':code})
            continue
        unit=units.get(key)
        verdict=judgments.get(key)
        valid=(unit is not None and _stamp(unit,service)==request['_snapshots'].get(key)
               and current_signature==request['_signature'])
        entry={k:v for k,v in entry.items() if k not in ('reason','reason_code')}
        if not valid:
            handled=service.store._connection().execute("SELECT 1 FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id WHERE u.task_id=? AND u.digest=? AND u.decision IN ('manual','auto') AND t.status!='superseded'",(entry['task_id'],entry['digest'])).fetchone()
            if handled:
                result['handled_count']+=1
                continue
            entry.update(reason='原文、整理结果或归类规则已变化；本条需重新判断，其余结果已保留。',reason_code='review_changed')
        elif not verdict:
            entry.update(reason='本条尚未完成判断，请继续预览；已完成的结果会复用。',reason_code='ai_pending')
        else:
            entry.update(reason=verdict['reason'],evidence_quote=verdict['evidence_quote'],
                         chain=verdict.get('chain',{}),chain_sources=verdict.get('chain_sources',{}),targets=verdict.get('targets',[]))
            if verdict['decision']!='include':
                entry['reason_code']='ai_excluded' if verdict['decision']=='exclude' else 'ai_uncertain'
        result['excluded' if entry.get('reason_code') else 'eligible'].append(entry)
    result['remaining']=sum(u.get('reason_code')=='batch_limit' for u in result['excluded'])
    result['searched_count']=len(result['eligible'])+len(result['excluded'])+result['handled_count']
    result.pop('revision',None)
    result['revision']=review._hash(result)
    return result


def mark_applied(service, job_id):
    row=_row(service,job_id)
    request=json.loads(row['request']);request['_applied']=True
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_review_jobs SET request=? WHERE id=?',
                                            (json.dumps(request,ensure_ascii=False),job_id))


def tick(service):
    conn=service.store._connection()
    with service.store.transaction():
        row=conn.execute("SELECT * FROM organization_review_jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:return False
        row=dict(row)
        conn.execute("UPDATE organization_review_jobs SET status='running',updated_at=? WHERE id=?",(_now_iso(),row['id']))
    try:
        available=_units(service)
        request=_restore_request(service,row,available)
        results=json.loads(row['judgments'])
        units=[u for key,u in available.items() if key in request['_snapshots'] and key not in results
               and _stamp(u,service)==request['_snapshots'][key]] if _signature(service,request)==request['_signature'] else []
        for offset in range(0,len(units),4):
            results.update(judge(service,units[offset:offset+4],request['condition'],request.get('exceptions',''),request))
            with service.store.transaction():
                conn.execute('UPDATE organization_review_jobs SET judgments=?,processed=?,updated_at=? WHERE id=?',
                             (json.dumps(results,ensure_ascii=False),len(results),_now_iso(),row['id']))
        with service.store.transaction():
            conn.execute("UPDATE organization_review_jobs SET status='completed',updated_at=? WHERE id=?",(_now_iso(),row['id']))
    except Exception as error:
        code=str(error) if isinstance(error,ValueError) else 'review_ai_failed'
        with service.store.transaction():
            conn.execute("UPDATE organization_review_jobs SET status='failed',error=?,updated_at=? WHERE id=?",(code[:200],_now_iso(),row['id']))
    return True


def classify_guidance(service, unit):
    """Only explicitly saved business criteria can automatically guide new data."""
    from evolvmem import organization_guidance as guidance
    before=guidance.rows(service)
    all_rules=[r for r in before if r['scope']=='future']
    rules=[r for r in all_rules if r['matching_mode']=='semantic']
    matches=[]
    excluded=[]
    from evolvmem import organization_exclusions
    exclusions=organization_exclusions.rows(service)
    for rule in rules:
        if not ks.assigned(rule):continue
        blocked=organization_exclusions.active(service,unit,rule,exclusions)
        if blocked:
            excluded.append(blocked)
            continue
        verdict=judge(service,[unit],rule['condition'],rule['exceptions'],rule)[_key(unit)]
        if verdict['decision']=='include':
            matches.append({**rule,'reason':verdict['reason'],'evidence_quote':verdict['evidence_quote'],'ownership_analysis':verdict})
        elif verdict['decision']=='uncertain':
            return {'status':'review','reason':verdict['reason']}
        else:
            excluded.append(verdict)
    if guidance.rows(service) != before:
        return {'status':'review','reason':'业务依据已变化，请重新判断。'}
    if not matches:
        return {'status':'none'}  # Exclusion from one scope does not exclude all other projects.
    if any(r['negative'] for r in matches) or len({(ks.scope(r),tuple(ks.projects(r))) for r in matches})>1:
        return {'status':'review','reason':'多条业务归类依据给出不同范围或反例，需要你核对。'}
    return {'status':'apply',**matches[0]}
