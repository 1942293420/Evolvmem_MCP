"""Explicit, reversible expression links. Similarity is only a reading aid."""
import json

from evolvmem import knowledge_identity as identity, knowledge_scope as ks
from evolvmem.context_store import _now_iso


def _score(query, text):
    # Ranking is a bounded local hint; full originals remain available for the
    # human comparison. Long logs must not cause quadratic matching work.
    def grams(value):
        value=value.casefold()[:4000]
        return {value[i:i+2] for i in range(max(0,len(value)-1))}
    a,b=grams(query),grams(text)
    return len(a & b)/max(1,len(a | b))


def _scope(obj):
    d = json.loads(obj['decision_json'])
    return (d.get('action'), ks.scope(d), tuple(sorted(ks.projects(d))))


def _entry(service, kid, backed=None):
    if isinstance(kid,str) and kid.startswith('legacy:'):
        from evolvmem import auto_organization as org
        _,task_id,digest = kid.split(':',2)
        task = org._task_row(service,int(task_id))
        org._assert_current(service,task)
        unit = org.find_unit(service,int(task_id),digest)
        if unit['knowledge_id'] or unit['decision']!='manual' or unit['disposition']!='keep' or not ks.assigned(unit):
            raise ValueError('equivalence_target_unavailable')
        from evolvmem.unit_derivations import currently_backed
        item = service.store._connection().execute('SELECT status FROM context_items WHERE id=?',(unit['item_id'],)).fetchone()
        if not item or item['status']!='active' or not currently_backed(service.store,unit['item_id']):
            raise ValueError('equivalence_target_unavailable')
        if not identity._anchors(task,unit,identity._parts(service,task)):
            raise ValueError('equivalence_target_unavailable')
        return {**unit, **ks.fields(unit), 'knowledge_id':kid, 'revision':org._unit_revision(unit),
                'decision_json':identity._json(identity._decision(unit))}
    obj = identity._object(service, kid)
    if not obj or _scope(obj)[0] != 'include' or not obj['item_id']:
        raise ValueError('equivalence_target_unavailable')
    from evolvmem.unit_derivations import currently_backed
    if (obj['item_id'] not in backed if backed is not None else not currently_backed(service.store,obj['item_id'])):
        raise ValueError('equivalence_target_unavailable')
    conn = service.store._connection()
    item = conn.execute('SELECT status FROM context_items WHERE id=?', (obj['item_id'],)).fetchone()
    if not item or item['status'] != 'active':
        raise ValueError('equivalence_target_unavailable')
    decision = conn.execute('SELECT coverage_json FROM knowledge_decisions WHERE knowledge_id=? AND revision=?',
                            (kid, obj['revision'])).fetchone()
    if not decision:
        raise ValueError('equivalence_target_unavailable')
    coverage = json.loads(decision['coverage_json'])
    unit = conn.execute('SELECT title FROM organization_units WHERE knowledge_id=? ORDER BY task_id DESC LIMIT 1', (kid,)).fetchone()
    return {**obj, 'text': '\n\n'.join(a['quote'] for a in coverage['anchors']),
            'title': unit['title'] if unit else '已归档知识', **json.loads(obj['decision_json'])}


def candidates(service, body):
    from evolvmem import auto_organization as org
    unit = org.find_unit(service, body.get('task_id'), body.get('digest'))
    query = str(body.get('q') or unit['text'])[:4000]
    from evolvmem.unit_derivations import current_item_ids
    objects = service.store._connection().execute("SELECT knowledge_id,item_id FROM knowledge_objects WHERE retired=0 AND decision_json!='{}'").fetchall()
    backed = set(current_item_ids(service.store,[r['item_id'] for r in objects if r['item_id']]))
    items = []
    for row in objects:
        if row[0] == unit['knowledge_id']:
            continue
        try:
            entry = _entry(service, row[0], backed)
        except ValueError:
            continue
        entry['score'] = _score(query,entry['text'])
        if body.get('q') and str(body['q']).casefold() not in (entry['title']+' '+entry['text']).casefold():
            continue
        items.append({k: entry[k] for k in ('knowledge_id','revision','title','text','projects','knowledge_scope','score')})
    for row in service.store._connection().execute("SELECT u.task_id,u.digest FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
            "WHERE u.knowledge_id='' AND u.decision='manual' AND u.disposition='keep' AND t.status!='superseded'"):
        try:
            entry = _entry(service,f"legacy:{row['task_id']}:{row['digest']}")
        except (ValueError,LookupError):
            continue
        if body.get('q') and str(body['q']).casefold() not in (entry['title']+' '+entry['text']).casefold():
            continue
        entry['score'] = _score(query,entry['text'])
        items.append({k:entry[k] for k in ('knowledge_id','revision','title','text','projects','knowledge_scope','score')})
    return {'items': sorted(items, key=lambda i: -i['score'])[:20]}


def confirm(service, body):
    from evolvmem import auto_organization as org
    from evolvmem.organization_review_decisions import archive_one
    with service.store.transaction():
        target = _entry(service, body.get('target_knowledge_id'))
        if target['revision'] != body.get('target_revision'):
            raise ValueError('revision_conflict')
        if target['knowledge_id'].startswith('legacy:'):
            # Adopt this explicitly selected human record, inside the same
            # transaction as confirmation. Reading candidates never writes.
            old_task = org._task_row(service,target['task_id'])
            identity.capture_existing(service,old_task)
            old = org.find_unit(service,target['task_id'],target['digest'])
            target = _entry(service,old['knowledge_id'])
        task = org.task_view(service, body.get('task_id'))
        org._assert_current(service, task)
        unit = org.find_unit(service, task['id'], body.get('digest'))
        if org._unit_revision(unit) != body.get('expected_revision'):
            raise ValueError('revision_conflict')
        kid = unit['knowledge_id']
        if not kid or kid == target['knowledge_id']:
            raise ValueError('equivalence_same_knowledge')
        source = identity._object(service, kid)
        if not source:
            raise ValueError('equivalence_source_unavailable')
        if unit['decision'] == 'manual' and _scope(source) != _scope(target):
            raise ValueError('equivalence_scope_conflict')
        conn = service.store._connection()
        # Keep one-hop groups: no cycles or implicit re-parenting of other
        # expressions when the user confirms only these two texts.
        if conn.execute('SELECT 1 FROM knowledge_equivalences WHERE active=1 AND (source_knowledge_id=? OR target_knowledge_id=?)',
                        (target['knowledge_id'], kid)).fetchone():
            raise ValueError('equivalence_choose_original')
        existing = conn.execute('SELECT * FROM knowledge_equivalences WHERE source_knowledge_id=?', (kid,)).fetchone()
        if existing and existing['active']:
            raise ValueError('equivalence_already_confirmed')
        if unit['decision'] != 'manual':
            archive_one(service, {**body, **ks.fields(target), 'action':'include'})
        now = _now_iso()
        conn.execute('INSERT INTO knowledge_equivalences(source_knowledge_id,target_knowledge_id,created_at,updated_at) VALUES(?,?,?,?) '
                     'ON CONFLICT(source_knowledge_id) DO UPDATE SET target_knowledge_id=excluded.target_knowledge_id,active=1,revision=revision+1,updated_at=excluded.updated_at',
                     (kid, target['knowledge_id'], now, now))
        record = dict(conn.execute('SELECT * FROM knowledge_equivalences WHERE source_knowledge_id=?', (kid,)).fetchone())
        return {'unit': org.find_unit(service, task['id'], unit['digest']), 'equivalence':record}


def records(service):
    items = []
    for row in service.store._connection().execute('SELECT * FROM knowledge_equivalences WHERE active=1 ORDER BY updated_at DESC'):
        item = dict(row)
        for side in ('source','target'):
            try:
                entry = _entry(service, row[side+'_knowledge_id'])
                item[side] = {k:entry[k] for k in ('title','text','projects','knowledge_scope')}
            except ValueError:
                item[side] = {'title':'原资料已变更','text':'','projects':[]}
        items.append(item)
    return {'items':items}


def dispatch(service, method, route, body):
    if method == 'GET' and route == 'candidates':
        return candidates(service, body)
    if method == 'GET' and not route:
        return records(service)
    if method == 'POST' and route == 'confirm':
        return confirm(service, body)
    if method == 'POST' and route == 'revoke':
        with service.store.transaction():
            changed = service.store._connection().execute('UPDATE knowledge_equivalences SET active=0,revision=revision+1,updated_at=? '
                'WHERE source_knowledge_id=? AND revision=? AND active=1',
                (_now_iso(), body.get('knowledge_id'), body.get('expected_revision'))).rowcount
            if not changed:
                raise ValueError('revision_conflict')
        return {'ok':True}
    raise LookupError('route_not_found')


def coalesce(store, items, objects):
    """Reading projection only; all original sources and decisions survive."""
    links = {}
    for r in store._connection().execute('SELECT * FROM knowledge_equivalences WHERE active=1'):
        a, b = objects.get(r['source_knowledge_id']), objects.get(r['target_knowledge_id'])
        if a and b and _scope(a) == _scope(b) and _scope(a)[0] == 'include':
            links[r['source_knowledge_id']] = r['target_knowledge_id']
    by_kid = {k:i for i in items for k in i['knowledge_ids']}
    removed = set()
    for a,b in links.items():
        source, target = by_kid.get(a), by_kid.get(b)
        if source is None or target is None or source is target or source['id'] in removed:
            continue
        # A directory fact can include multiple independent knowledge objects.
        # Do not absorb those other objects via a link to just one member.
        if set(source['knowledge_ids']) != {a}:
            continue
        target.setdefault('expressions', []).append({'title':source['title'], 'text':source['body'] or '\n'.join(v['value'] for v in source['versions']),
                                                    'knowledge_id':a, 'sources':source['sources']})
        for field in ('item_ids','knowledge_ids','sources','versions','current','alternatives','warnings','relations'):
            for value in source[field]:
                if value not in target[field]:
                    target[field].append(value)
        if source['kind'] == 'fact':
            target['kind'] = 'fact'
        removed.add(source['id'])
    return [i for i in items if i['id'] not in removed]
