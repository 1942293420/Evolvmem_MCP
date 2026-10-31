"""Background AI review of business criteria, with source-bound proposals."""
import json
import time
import uuid
from datetime import datetime, timezone

from evolvmem import knowledge_scope as ks
from evolvmem.context_store import _now_iso
from evolvmem import organization_review_retrieval as retrieval

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
                   'knowledge_scope', 'condition', 'exceptions', 'scope', 'guidance', 'continue_job_id', 'model_choice')


def model_options():
    from evolvmem.kimi_hooks import _load_llm_config
    config = _load_llm_config(log_errors=False)
    items = [{'id':'auto', 'label':'自动选择（失败时切换备用模型）'}]
    for key, candidate in [('primary', config), ('fallback', getattr(config, 'fallback', None))]:
        if candidate is not None and getattr(candidate, 'provider', None):
            name = {'deepseek':'DeepSeek', 'mimo':'MiMo', 'kimi':'Kimi'}.get(candidate.provider, candidate.provider)
            items.append({'id':key, 'provider':candidate.provider, 'model':candidate.model,
                          'label':f'{name} · {candidate.model}'})
    return {'items':items}


def _units(service):
    from evolvmem import organization_review as review
    return {_key(u):u for g in review._groups(service) for u in g['members']}


def _stamp(unit, service, *, source_checks=None):
    from evolvmem.organization_review import _hash
    # Extraction progress and updated_at do not change the reviewed content.
    from evolvmem.ownership_reasoning import stamp, context_for
    context = context_for(service, unit, source_checks=source_checks)
    return _hash([{k:unit[k] for k in ('revision','text','title','source_revision','rule_revision')}, stamp(service,unit,context)])


def _signature(service, request):
    from evolvmem.organization_review import _hash
    target=ks.normalize(service,request,required=True)
    names={p['project']:p['display_name'] or p['project'] for p in service.knowledge().registry()}
    from evolvmem.ownership_reasoning import VERSION
    from evolvmem.task_context import VERSION as CONTEXT_VERSION
    signature = [VERSION, CONTEXT_VERSION, retrieval.VERSION, target, [names.get(p,p) for p in target['projects']],
                  str(request.get('condition','')).strip(), str(request.get('exceptions','')).strip(),
                  service.config.context_vector_min_similarity,service.config.context_reranker_min_score,
                  str(service.config.model_path),service.config.embedding_dim,
                  service.knowledge().rules.read()['revision']]
    if request.get('model_choice', 'auto') != 'auto':
        signature.append(request['model_choice'])
    return _hash(signature)


def _prepare(service, request, base, units):
    checks = {}
    stamps = {_key(u):_stamp(units[_key(u)],service,source_checks=checks) for u in base['excluded']
              if u.get('reason_code') in ('ai_pending','batch_limit') and _key(u) in units}
    return {**request, '_preview':base, '_signature':_signature(service,request),
            '_candidate_snapshots':stamps,
            '_snapshots':{_key(u):stamps[_key(u)] for u in base['excluded']
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


def _same_continuation(current, previous):
    # A complete task leaves the review list after saving. Another task may
    # anchor the next batch, but the business criteria and conversation stay bound.
    if not _same_request({**current, 'group_id': previous.get('group_id')}, previous):
        return False
    if current.get('selection') == 'all':
        return True
    if current.get('selection') != 'group':
        return False
    sources = {u['source_key'] for u in previous['eligible'] + previous['excluded']}
    current_sources = {u['source_key'] for u in current['eligible'] + current['excluded']}
    return bool(current_sources) and current_sources <= sources


def start(service, body, *, _cached_only=False):
    from evolvmem import organization_review as review
    submitted_at = _now_iso()
    request={k:body[k] for k in _REQUEST_FIELDS if k in body}
    choice = request.get('model_choice', 'auto')
    if choice not in ('auto','primary','fallback'):
        raise ValueError('invalid_review_model')
    if choice != 'auto':
        from evolvmem.kimi_hooks import _load_llm_config, select_llm_config
        config = _load_llm_config()
        if config is None:
            raise ValueError('review_model_unavailable')
        select_llm_config(config, choice)
    request.update(matching_mode='semantic',job_id=uuid.uuid4().hex)
    conn=service.store._connection()
    with service.store.transaction():
        base=review.preview(service,request,_judgments={})
        units=_units(service)
        rows=[dict(r) for r in conn.execute('SELECT rowid AS job_order,* FROM organization_review_jobs ORDER BY created_at DESC,rowid DESC LIMIT 100').fetchall()]
        if request.get('continue_job_id'):
            parent=_row(service,request['continue_job_id'])
            previous=_restore_request(service,parent,units)
            if parent['status']!='completed' or not _same_continuation(base,previous['_preview']) or _signature(service,request)!=previous['_signature']:
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
            # Continue only the original recalled pool, in its original order.
            parent_request=json.loads(parent['request'])
            if parent_request.get('_ranking') is not None:
                request['_ranking']=[r for r in parent_request['_ranking'] if r['key'] not in skipped]
                request['_retrieval']=parent_request.get('_retrieval',{})
        request=_prepare(service,request,base,units)
        for old in rows:
            previous=json.loads(old['request'])
            if previous.get('_signature')!=request['_signature']:continue
            if (all(previous.get('_candidate_snapshots',{}).get(k)==v for k,v in request['_candidate_snapshots'].items())
                    and _same_request(base,previous['_preview']) and not request.get('continue_job_id')):
                if old['status'] in ('pending','running','completed') and not previous.get('_applied') and not _cached_only:
                    return get(service,old['id'])
                if previous.get('_ranking') is not None:
                    request['_ranking']=previous['_ranking']
                    request['_retrieval']=previous.get('_retrieval',{})
                    break
        if '_ranking' in request:
            base=retrieval.select(base,request['_ranking'])
            request['_preview']={**base,**request.get('_retrieval',{})}
            request['_snapshots']={_key(u):_stamp(units[_key(u)],service) for u in base['excluded']
                                  if u.get('reason_code')=='ai_pending' and _key(u) in units}
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
        if _cached_only and (len(cached)!=total or '_ranking' not in request):
            return None
        status='completed' if len(cached)==total and '_ranking' in request else 'pending'
        conn.execute('INSERT INTO organization_review_jobs(id,status,request,base_revision,judgments,processed,total,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (request['job_id'],status,json.dumps(request,ensure_ascii=False),base['revision'],
                      json.dumps(cached,ensure_ascii=False),len(cached),total,submitted_at,_now_iso()))
    return get(service,request['job_id'])


def _row(service, job_id):
    row=service.store._connection().execute('SELECT * FROM organization_review_jobs WHERE id=?',(str(job_id),)).fetchone()
    if row is None:
        raise ValueError('review_job_not_found')
    return dict(row)


def get(service, job_id, *, _current_units=None):
    row=_row(service,job_id)
    result={k:row[k] for k in ('id','status','error','processed','total')}
    end = (datetime.fromisoformat(row['updated_at']).replace(tzinfo=timezone.utc)
           if row['status'] in ('completed','failed','stopped') else datetime.now(timezone.utc))
    result['elapsed_seconds'] = max(0, int((end -
        datetime.fromisoformat(row['created_at']).replace(tzinfo=timezone.utc)).total_seconds()))
    result['waiting_for'] = ''
    if row['status'] == 'pending':
        active = service.store._connection().execute("SELECT 1 FROM organization_tasks WHERE status='running' LIMIT 1").fetchone()
        result['waiting_for'] = 'organization' if active else 'queue'
    request=json.loads(row['request'])
    result.update(request.get('_retrieval',{}))
    result['phase']=request.get('_phase','retrieval')
    result['model_status']=request.get('_model_status',{})
    result['model_calls']=request.get('_model_calls',[])
    result['reused_count']=request.get('_reused',0)
    result['request']={k:request[k] for k in _REQUEST_FIELDS if k in request}
    if request.get('_signature') != _signature(service,request):
        result['request'].pop('continue_job_id',None)
    if row['status'] in ('completed','stopped'):
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
            p=job.get('proposal')
            if p and p.get('ai_judged_count') and not p['eligible'] and not any(
                    u.get('reason_code') in ('ai_uncertain','ai_excluded','ai_pending','review_changed','value_review','batch_limit') for u in p['excluded']):
                # Older releases did not persist _applied for mixed decisions.
                # Reading history must not invite the user to repeat them.
                continue
            return {'job':job | {'resume_group_id':group['id']}}
        except ValueError:
            continue
    return {'job':None}


def proposal(service, body, *, _current_units=None):
    from evolvmem import organization_review as review
    row=_row(service,body.get('job_id'))
    if row['status'] not in ('completed','stopped'):
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
    source_checks = {}
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
                    reason,code='这条资料尚未由 AI 判断，你可直接选择归入项目或暂不归档。','value_review'
                else:
                    reason,code='本条尚未进行归类判断，可继续判断下一批。','batch_limit'
                result['excluded'].append(review._brief(unit)|{'reason':reason,'reason_code':code})
            continue
        unit=units.get(key)
        verdict=judgments.get(key)
        valid=(unit is not None and not result['needs_recheck']
               and _stamp(unit,service,source_checks=source_checks)==request['_snapshots'].get(key))
        entry={k:v for k,v in entry.items() if k not in ('reason','reason_code')}
        if not valid:
            handled=service.store._connection().execute("SELECT 1 FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id WHERE u.task_id=? AND u.digest=? AND (u.decision IN ('manual','auto') OR u.disposition='set_aside') AND t.status!='superseded'",(entry['task_id'],entry['digest'])).fetchone()
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


def stop(service, job_id):
    row=_row(service,job_id)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_review_jobs SET status='stopped',updated_at=? WHERE id=? AND status IN ('pending','running')",
            (_now_iso(),row['id']))
    return get(service,job_id)


def tick(service):
    conn=service.store._connection()
    with service.store.transaction():
        row=conn.execute("SELECT * FROM organization_review_jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:return False
        row=dict(row)
        conn.execute("UPDATE organization_review_jobs SET status='running',updated_at=? WHERE id=?",(_now_iso(),row['id']))
    request={}
    try:
        available=_units(service)
        request=_restore_request(service,row,available)
        results=json.loads(row['judgments'])
        if '_ranking' not in request:
            candidates=[available[key] for key,stamp in request['_candidate_snapshots'].items()
                        if key in available and _stamp(available[key],service)==stamp]
            from evolvmem import organization_exclusions
            exclusions=organization_exclusions.rows(service)
            candidates=[u for u in candidates if not organization_exclusions.active(service,u,request,exclusions)]
            def progress(**values):
                if _row(service,row['id'])['status']=='stopped':raise ValueError('review_stopped')
                request['_phase']=values.pop('phase',request.get('_phase','retrieval'))
                request.setdefault('_retrieval',{}).update(values)
                with service.store.transaction():
                    conn.execute('UPDATE organization_review_jobs SET request=?,updated_at=? WHERE id=?',
                                 (json.dumps(request,ensure_ascii=False),_now_iso(),row['id']))
            request['_ranking']=retrieval.rank(service,candidates,request,progress)
            request['_preview']={**retrieval.select(request['_preview'],request['_ranking']),**request.get('_retrieval',{})}
            request['_snapshots']={_key(u):_stamp(available[_key(u)],service) for u in request['_preview']['excluded']
                                  if u.get('reason_code')=='ai_pending'}
            results={key:value for key,value in results.items() if key in request['_snapshots']}
            progress(phase='judging')
            with service.store.transaction():
                conn.execute('UPDATE organization_review_jobs SET total=?,processed=? WHERE id=?',
                             (len(request['_snapshots']),len(results),row['id']))
        units=[available[key] for key in request['_snapshots'] if key in available and key not in results
               and _stamp(available[key],service)==request['_snapshots'][key]] if _signature(service,request)==request['_signature'] else []
        def model_progress(status):
            if _row(service,row['id'])['status']=='stopped':raise ValueError('review_stopped')
            request['_phase']='judging'
            request['_model_status']=status
            if status.get('call_id'):
                calls = request.setdefault('_model_calls', [])
                index = next((i for i,c in enumerate(calls) if c.get('call_id')==status['call_id']), None)
                if index is None:
                    calls.append(status)
                else:
                    calls[index]=status
                request['_model_calls']=calls[-40:]
            with service.store.transaction():
                conn.execute('UPDATE organization_review_jobs SET request=?,updated_at=? WHERE id=?',
                             (json.dumps(request,ensure_ascii=False),_now_iso(),row['id']))
        for offset in range(0,len(units),4):
            if _row(service,row['id'])['status']=='stopped':return True
            from evolvmem import organization_exclusions
            exclusions=organization_exclusions.rows(service)
            current=_units(service)
            batch=[current[_key(u)] for u in units[offset:offset+4] if _key(u) in current
                   and _stamp(current[_key(u)],service)==request['_snapshots'][_key(u)]
                   and not organization_exclusions.active(service,current[_key(u)],request,exclusions)]
            if batch:
                from evolvmem.kimi_hooks import llm_provider_status
                with llm_provider_status(model_progress):
                    results.update(judge(service,batch,request['condition'],request.get('exceptions',''),request))
            with service.store.transaction():
                conn.execute('UPDATE organization_review_jobs SET judgments=?,processed=?,updated_at=? WHERE id=?',
                             (json.dumps(results,ensure_ascii=False),len(results),_now_iso(),row['id']))
        with service.store.transaction():
            conn.execute("UPDATE organization_review_jobs SET status='completed',updated_at=? WHERE id=? AND status='running'",(_now_iso(),row['id']))
    except Exception as error:
        code=str(error) if isinstance(error,ValueError) else 'review_ai_failed'
        if request.get('_phase','retrieval') in ('retrieval','reranking') and code!='review_stopped':
            code='review_retrieval_unavailable'
        with service.store.transaction():
            conn.execute("UPDATE organization_review_jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status!='stopped'",(code[:200],_now_iso(),row['id']))
    return True


def classify_guidance(service, unit, direct_analysis=None):
    """Only explicitly saved business criteria can automatically guide new data."""
    from evolvmem import organization_guidance as guidance
    before=guidance.rows(service)
    all_rules=[r for r in before if r['scope']=='future']
    rules=[r for r in all_rules if r['matching_mode']=='semantic']
    matches=[]
    excluded=[]
    from evolvmem import ownership_reasoning as reasoning
    direct = reasoning.current_direct_analysis(service, unit, direct_analysis)
    ignored_uncertain = []
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
            # Uncertainty about another positive project criterion is not a
            # contradiction of current, source-proved ownership. Global and
            # negative rules, and every overlapping scope, remain protective.
            if (direct and not rule['negative'] and ks.scope(rule) == 'project'
                    and ks.projects(rule) and set(ks.projects(rule)).isdisjoint(direct['projects'])):
                ignored_uncertain.append(rule['id'])
                continue
            return {'status':'review','reason':verdict['reason']}
        else:
            excluded.append(verdict)
    if guidance.rows(service) != before:
        return {'status':'review','reason':'业务依据已变化，请重新判断。'}
    if ignored_uncertain and reasoning.current_direct_analysis(service, unit, direct_analysis) != direct:
        return {'status':'review','reason':'原文或关联需求已变化，请重新核对。'}
    if not matches:
        return {'status':'none', 'ignored_uncertain_guidance_ids': ignored_uncertain}
    if any(r['negative'] for r in matches) or len({(ks.scope(r),tuple(ks.projects(r))) for r in matches})>1:
        return {'status':'review','reason':'多条业务归类依据给出不同范围或反例，需要你核对。'}
    return {'status':'apply',**matches[0]}
