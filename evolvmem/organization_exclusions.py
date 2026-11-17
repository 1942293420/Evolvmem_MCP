"""Human decisions exclude one current fragment from specific target scopes."""
from evolvmem import knowledge_scope as ks
from evolvmem.context_store import _now_iso


def _keys(target):
    return ['global'] if ks.scope(target)=='global' else ['project:'+p for p in ks.projects(target)]


def rows(service):
    return {(r['source_key'],r['digest'],r['scope_key']):dict(r)
            for r in service.store._connection().execute('SELECT * FROM organization_scope_exclusions')}


def _unit(service,unit):
    unit=dict(unit)
    if 'source_revision' not in unit:
        task=service.store._connection().execute('SELECT source_key,source_revision,rule_revision FROM organization_tasks WHERE id=?',(unit['task_id'],)).fetchone()
        if task is None:raise ValueError('unit_not_found')
        unit.update(dict(task))
    return unit


def _stamp(unit):
    from evolvmem.organization_review import _hash
    return _hash({k:unit[k] for k in ('digest','text','title','source_revision','rule_revision')})


def active(service,unit,target,known=None):
    keys=_keys(target)
    known=rows(service) if known is None else known
    if not keys or not any(k[1]==unit['digest'] for k in known):return None
    unit=_unit(service,unit)
    if unit['rule_revision']!=service.knowledge().rules.read()['revision']:return None
    stamp=_stamp(unit)
    matches=[known[(unit['source_key'],unit['digest'],key)] for key in keys
             if (unit['source_key'],unit['digest'],key) in known and known[(unit['source_key'],unit['digest'],key)]['content_stamp']==stamp]
    if not matches:return None
    from evolvmem.organization_review import _hash
    projects=[r['scope_key'].split(':',1)[1] for r in matches if r['scope_key'].startswith('project:')]
    names={p['project']:p['display_name'] or p['project'] for p in service.knowledge().registry()}
    label='个人通用' if not projects else '、'.join(names.get(p,p) for p in projects)
    return {'reason_code':'manual_excluded','reason':f'你已确认这条不归入「{label}」。资料仍可归入其他范围。',
            'excluded_projects':projects,'exclusion_revision':_hash([(r['scope_key'],r['revision'],r['content_stamp']) for r in matches])}


def decide(service,body):
    from evolvmem import organization_review as review,auto_organization as org
    saved=body.get('proposal')
    if not isinstance(saved,dict) or not saved.get('revision'):raise ValueError('review_preview_required')
    action=body.get('action')
    if action not in ('exclude','undo'):raise ValueError('invalid_items')
    if type(body.get('task_id')) is not int or not isinstance(body.get('digest'),str):raise ValueError('invalid_items')
    with service.store.transaction():
        current=review.preview(service,saved)
        if current!=saved:raise ValueError('review_preview_changed')
        entry=next((u for u in current['eligible']+current['excluded'] if u['task_id']==body['task_id'] and u['digest']==body['digest']),None)
        units={f"{u['task_id']}:{u['digest']}":u for g in review._groups(service) for u in g['members']}
        unit=units.get(f"{body['task_id']}:{body['digest']}")
        if entry is None or unit is None or entry['revision']!=unit['revision']:raise ValueError('review_preview_changed')
        org._assert_current(service,org.task_view(service,unit['task_id']))
        if action=='undo' and entry.get('reason_code')!='manual_excluded':raise ValueError('review_preview_changed')
        write_decision(service,unit,current,action)
        if action=='undo' and saved.get('job_id'):
            # Rebuild a current preview only when every judgment is already
            # available. Undo never starts model work or drops cached evidence.
            import json
            from evolvmem import organization_semantic_review as semantic
            request=json.loads(semantic._row(service,saved['job_id'])['request'])
            restored=semantic.start(service,request,_cached_only=True)
            if restored is not None:
                return {'ok':True,'action':action,'proposal':restored['proposal']}
    return {'ok':True,'action':action,'proposal':review.preview(service,saved)}


def write_decision(service, unit, target, action):
    """Caller validates source and preview and owns the transaction."""
    conn=service.store._connection()
    for key in _keys(target):
        if action=='exclude':
            conn.execute('INSERT INTO organization_scope_exclusions(source_key,digest,scope_key,content_stamp,revision,updated_at) VALUES(?,?,?,?,1,?) ON CONFLICT(source_key,digest,scope_key) DO UPDATE SET content_stamp=excluded.content_stamp,revision=organization_scope_exclusions.revision+1,updated_at=excluded.updated_at WHERE organization_scope_exclusions.content_stamp!=excluded.content_stamp',
                         (unit['source_key'],unit['digest'],key,_stamp(unit),_now_iso()))
        else:
            conn.execute('DELETE FROM organization_scope_exclusions WHERE source_key=? AND digest=? AND scope_key=? AND content_stamp=?',
                         (unit['source_key'],unit['digest'],key,_stamp(unit)))
