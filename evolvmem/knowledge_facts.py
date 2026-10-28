"""Source-bound fact versions and reversible grouping. Never assigns projects.

An exact subject/property is a suggested reading group, not an entity merge.
Values and original conditions are immutable; human interpretations are a
separate ledger. Project ownership is read from the existing decision ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from evolvmem.context_store import _now_iso
from evolvmem import knowledge_identity as identity

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_fact_groups (
        id TEXT PRIMARY KEY, subject TEXT NOT NULL, property TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_versions (
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES knowledge_fact_groups(id),
        value TEXT NOT NULL, conditions_json TEXT NOT NULL, valid_from TEXT NOT NULL,
        valid_until TEXT NOT NULL, recorded_at TEXT NOT NULL, exclusive INTEGER NOT NULL DEFAULT 0)""",
    """CREATE INDEX IF NOT EXISTS idx_fact_versions_group ON knowledge_fact_versions(group_id)""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_sources (
        version_id TEXT NOT NULL REFERENCES knowledge_fact_versions(id),
        knowledge_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_id),
        source_key TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
        quote TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(version_id,knowledge_id,source_key,start,end))""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_relations (
        id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_id),
        version_id TEXT NOT NULL REFERENCES knowledge_fact_versions(id),
        type TEXT NOT NULL, target TEXT NOT NULL, quote TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'suggested')""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_issues (
        id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES knowledge_fact_groups(id),
        conditions_json TEXT NOT NULL, members_json TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_actions (
        id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, action TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_choices (
        group_id TEXT NOT NULL, conditions_json TEXT NOT NULL, version_id TEXT NOT NULL,
        PRIMARY KEY(group_id,conditions_json))""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_conditions (
        version_id TEXT PRIMARY KEY REFERENCES knowledge_fact_versions(id),
        conditions_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_fact_separations (
        base_group TEXT NOT NULL, knowledge_id TEXT NOT NULL,
        group_id TEXT NOT NULL REFERENCES knowledge_fact_groups(id),
        PRIMARY KEY(base_group,knowledge_id))""",
)

RELATIONS = {'涉及', '使用', '修改', '适用于', '支持', '验证', '补充', '替代', '存在分歧'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def key(*parts):
    return hashlib.sha256(encoded(parts).encode()).hexdigest()[:32]


def conditions(value):
    if isinstance(value, str):
        value = json.loads(value or '{}')
    if not isinstance(value, dict) or len(value) > 12 or any(
            not isinstance(k, str) or not k.strip() or len(k) > 80 or
            not isinstance(v, str) or not v.strip() or len(v) > 200 for k, v in value.items()):
        raise ValueError('invalid_fact_conditions')
    return {k.strip(): v.strip() for k, v in value.items()}


def business_date(value):
    if not value:
        return ''
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('invalid_fact_date')
    return date.fromisoformat(value).isoformat()


def _sources(service, task, unit, quote, *, context=None):
    """Locate exact quotes on original native sources, never model offsets/roles."""
    from evolvmem import auto_organization as org, topic_segmentation
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        return []
    spans, parts = context or (topic_segmentation.message_spans(org.source_messages(service, task['source_key'])), identity._parts(service, task))
    anchors = identity._anchors(task, unit, parts)
    result = []
    for a in anchors:
        if quote not in a['quote']:
            continue
        # Ambiguous repeated excerpts need a larger quote; don't invent a location.
        if a['quote'].count(quote) != 1:
            continue
        start = a['start'] + a['quote'].index(quote)
        part = next(p for p in parts if p['source_key'] == a['key'])
        absolute = part['start'] + start
        role = next((s['role'] for s in spans if s['start'] <= absolute and absolute+len(quote) <= s['end']), 'unknown')
        result.append((a['key'], start, start+len(quote), quote, role))
    return result


def ingest_task(service, task):
    """Runs inside organization's source-guarded transaction; no model call here."""
    from evolvmem.task_narrative import read
    service.store._require_transaction('ingest_fact_task')
    conn = service.store._connection()
    touched = set()
    source_context = None
    names = [name for p in service.knowledge().registry() for name in [p['project'],p.get('display_name',''),*p.get('aliases',[]) ] if name]
    for row in conn.execute('SELECT * FROM organization_units WHERE task_id=?', (task['id'],)).fetchall():
        unit = dict(row)
        kid = unit['knowledge_id']
        if not kid:
            continue
        record = read(unit['task_record'])
        facts = record.get('facts', [])
        if not isinstance(facts, list):
            continue
        # A relation is itself a source-backed claim. Keep it queryable even
        # when the model emitted no other attribute for its subject.
        facts = list(facts)
        for relation in record.get('relations', []) if isinstance(record.get('relations', []), list) else []:
            if (isinstance(relation, dict) and relation.get('type') in RELATIONS
                and isinstance(relation.get('subject'), str) and relation['subject'].strip()
                and not any(f.get('subject')==relation['subject'] for f in facts if isinstance(f,dict))):
                facts.append({'subject':relation['subject'], 'property':relation['type']+'对象',
                              'value':relation.get('target'), 'conditions':relation.get('conditions',{}),
                              'evidence':relation.get('evidence') or [{'quote':relation.get('quote')}], 'exclusive':False})
        if facts and source_context is None:
            from evolvmem import auto_organization as org, topic_segmentation
            source_context = (topic_segmentation.message_spans(org.source_messages(service, task['source_key'])), identity._parts(service, task))
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            try:
                subject, prop, value = (fact.get(k, '').strip() for k in ('subject', 'property', 'value'))
                if not subject or not prop or not value or len(subject)>200 or len(prop)>120 or len(value)>2000:
                    continue
                cond = conditions(fact.get('conditions', {}))
                start, end = business_date(fact.get('valid_from')), business_date(fact.get('valid_until'))
                if start and end and start >= end:
                    continue
                refs = fact.get('evidence', [])
                if not isinstance(refs, list):
                    continue
                sources = [s for r in refs if isinstance(r, dict) for s in _sources(service, task, unit, r.get('quote'), context=source_context)]
                if not sources or (start and not any(start in ref[3] for ref in sources)):
                    continue
                if end and not any(end in ref[3] for ref in sources):
                    end = ''  # An inferred cutoff must not erase an evidenced old fact.
                # Unqualified names remain source-local. An explicit subject in
                # the quote can be suggested together; this grants no ownership.
                qualified = any(subject in s[3] for s in sources) and any(re.search(r'(?<![a-z0-9])'+re.escape(n.casefold())+r'(?![a-z0-9])',subject.casefold()) for n in names)
                base = key(subject, prop, '' if qualified else kid)
                split = conn.execute('SELECT group_id FROM knowledge_fact_separations WHERE base_group=? AND knowledge_id=?', (base, kid)).fetchone()
                gid = split['group_id'] if split else base
                conn.execute('INSERT OR IGNORE INTO knowledge_fact_groups(id,subject,property,created_at) VALUES(?,?,?,?)', (gid,subject,prop,_now_iso()))
                exclusive = fact.get('exclusive') is True
                vid = key(gid, value, cond, start, end)
                inserted = conn.execute('INSERT OR IGNORE INTO knowledge_fact_versions VALUES(?,?,?,?,?,?,?,?)',
                                       (vid,gid,value,encoded(cond),start,end,_now_iso(),int(exclusive))).rowcount
                for source in sources:
                    conn.execute('INSERT INTO knowledge_fact_sources(version_id,knowledge_id,source_key,start,end,quote,role) VALUES(?,?,?,?,?,?,?) '
                                 'ON CONFLICT(version_id,knowledge_id,source_key,start,end) DO UPDATE SET active=1', (vid,kid,*source))
                relations = record.get('relations', [])
                for rel in relations if isinstance(relations, list) else []:
                    if not isinstance(rel, dict) or rel.get('type') not in RELATIONS:
                        continue
                    if rel.get('subject') and rel['subject'] != subject:
                        continue
                    target = rel.get('target')
                    quote = rel.get('quote') or next((r.get('quote') for r in rel.get('evidence',[]) if isinstance(r,dict)), '')
                    if not isinstance(target, str) or not target.strip() or len(target)>200 or not _sources(service,task,unit,quote,context=source_context):
                        continue
                    if not rel.get('subject') and not any(quote in ref[3] or ref[3] in quote for ref in sources):
                        continue
                    conn.execute('INSERT OR IGNORE INTO knowledge_fact_relations(id,knowledge_id,version_id,type,target,quote) VALUES(?,?,?,?,?,?)',
                                 (key(kid,vid,rel['type'],target,quote),kid,vid,rel['type'],target,quote))
                if inserted:
                    conn.execute('UPDATE knowledge_fact_groups SET revision=revision+1 WHERE id=?', (gid,))
                touched.add(gid)
            except (ValueError, TypeError, AttributeError):
                # Invalid optional enrichment never blocks direct archive/save.
                continue
    for row in conn.execute('SELECT * FROM organization_units WHERE task_id=?', (task['id'],)).fetchall():
        sync_ownership_relations(service,task,dict(row),context=source_context)
    for gid in touched:
        _refresh_issues(service, gid)


def sync_ownership_relations(service, task, unit, *, context=None):
    from evolvmem.ownership_reasoning import validated
    analysis = validated(unit)
    if not analysis:
        return
    conn = service.store._connection()
    versions = [dict(r) for r in conn.execute('SELECT DISTINCT version_id,quote FROM knowledge_fact_sources WHERE knowledge_id=? AND active=1', (unit['knowledge_id'],))]
    if not versions:
        return
    chain = analysis.get('chain', {})
    for target in analysis.get('targets', []):
        quote = target.get('quote','')
        if not _sources(service,task,unit,quote,context=context):
            continue
        relation = ('修改' if target.get('role')=='owner' and quote in chain.get('change','') else
                    '验证' if target.get('role')=='related' and quote in chain.get('result','') else
                    '适用于' if target.get('role')=='applicable' else '涉及')
        for version in versions:
            if quote not in version['quote'] and version['quote'] not in quote:
                continue
            vid = version['version_id']
            conn.execute('INSERT OR IGNORE INTO knowledge_fact_relations(id,knowledge_id,version_id,type,target,quote) VALUES(?,?,?,?,?,?)',
                         (key(unit['knowledge_id'],vid,relation,target['project'],quote),unit['knowledge_id'],vid,relation,target['project'],quote))


def _versions(conn, gid):
    rows = conn.execute('SELECT v.*, c.conditions_json AS interpreted_conditions FROM knowledge_fact_versions v '
                        'LEFT JOIN knowledge_fact_conditions c ON c.version_id=v.id WHERE v.group_id=? '
                        'AND EXISTS(SELECT 1 FROM knowledge_fact_sources s WHERE s.version_id=v.id AND s.active=1) '
                        'ORDER BY v.valid_from,v.recorded_at,v.id', (gid,)).fetchall()
    return [dict(r, conditions=json.loads(r['interpreted_conditions'] or r['conditions_json'])) for r in rows]


def _refresh_issues(service, gid):
    conn = service.store._connection()
    versions = _versions(conn, gid)
    by_condition = {}
    for v in versions:
        by_condition.setdefault(encoded(v['conditions']), []).append(v)
    active = set()
    for cond, values in by_condition.items():
        if len({v['value'] for v in values}) < 2 or not all(v['exclusive'] for v in values):
            continue
        dated = all(v['valid_from'] for v in values) and len({v['valid_from'] for v in values}) == len(values)
        dated = dated and not any(a['valid_until'] and b['valid_from'] and a['valid_from'] < b['valid_from'] < a['valid_until'] for a in values for b in values)
        choices = _choices(conn,gid)
        confirmed = next((v for v in values if v['id'] in choices), None)
        # An older dated record adds history, not a request to reconfirm the present.
        if dated and confirmed and max(v['valid_from'] for v in values) <= confirmed['valid_from']:
            continue
        iid = key(gid, cond)
        members = sorted(v['id'] for v in values)
        old = conn.execute('SELECT * FROM knowledge_fact_issues WHERE id=?', (iid,)).fetchone()
        active.add(iid)
        if old and json.loads(old['members_json']) == members:
            continue
        conn.execute("INSERT INTO knowledge_fact_issues(id,group_id,conditions_json,members_json,kind,updated_at) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET members_json=excluded.members_json,kind=excluded.kind,state='pending',revision=revision+1,updated_at=excluded.updated_at",
                     (iid,gid,cond,encoded(members),'change' if dated else 'conflict',_now_iso()))
    for issue in conn.execute("SELECT id FROM knowledge_fact_issues WHERE group_id=? AND state='pending'", (gid,)).fetchall():
        if issue['id'] not in active:
            conn.execute("UPDATE knowledge_fact_issues SET state='resolved',revision=revision+1,updated_at=? WHERE id=?", (_now_iso(),issue['id']))


def _group(conn, gid, revision):
    row = conn.execute('SELECT * FROM knowledge_fact_groups WHERE id=?', (gid,)).fetchone()
    if row is None:
        raise ValueError('fact_not_found')
    if type(revision) is not int or row['revision'] != revision:
        raise ValueError('revision_conflict')
    return row


def _choices(conn, gid):
    return {r['version_id'] for r in conn.execute('SELECT version_id FROM knowledge_fact_choices WHERE group_id=?', (gid,))}


def _choose(conn, gid, vid):
    version = next(v for v in _versions(conn,gid) if v['id']==vid)
    conn.execute('INSERT INTO knowledge_fact_choices VALUES(?,?,?) ON CONFLICT(group_id,conditions_json) DO UPDATE SET version_id=excluded.version_id', (gid,encoded(version['conditions']),vid))


def _log(conn, gid, action, body):
    conn.execute('INSERT INTO knowledge_fact_actions(group_id,action,payload_json,created_at) VALUES(?,?,?,?)', (gid,action,encoded(body),_now_iso()))
    conn.execute('UPDATE knowledge_fact_groups SET revision=revision+1 WHERE id=?', (gid,))


def decide(service, action, body):
    conn = service.store._connection()
    with service.store.transaction():
        if action == 'resolve':
            issue = conn.execute('SELECT * FROM knowledge_fact_issues WHERE id=?', (body.get('id'),)).fetchone()
            if issue is None or issue['revision'] != body.get('expected_revision') or type(body.get('expected_revision')) is not int:
                raise ValueError('revision_conflict')
            gid = issue['group_id']
            choice = body.get('action')
            if choice == 'choose':
                vid = body.get('version_id')
                if vid not in json.loads(issue['members_json']) or vid not in {v['id'] for v in _versions(conn,gid)}:
                    raise ValueError('invalid_fact_selection')
                _choose(conn,gid,vid)
            elif choice == 'conditions':
                mapping = body.get('conditions')
                if not isinstance(mapping, dict) or set(mapping) != set(json.loads(issue['members_json'])):
                    raise ValueError('invalid_fact_conditions')
                parsed = {vid: conditions(c) for vid,c in mapping.items()}
                if any(not c for c in parsed.values()) or len({encoded(c) for c in parsed.values()}) != len(parsed):
                    raise ValueError('invalid_fact_conditions')
                confirmed = _choices(conn,gid)
                for vid,c in parsed.items():
                    conn.execute('INSERT INTO knowledge_fact_conditions VALUES(?,?) ON CONFLICT(version_id) DO UPDATE SET conditions_json=excluded.conditions_json', (vid,encoded(c)))
                    if vid in confirmed:
                        conn.execute('DELETE FROM knowledge_fact_choices WHERE group_id=? AND version_id=?',(gid,vid))
                        _choose(conn,gid,vid)
            elif choice != 'defer':
                raise ValueError('invalid_fact_action')
            conn.execute('UPDATE knowledge_fact_issues SET state=?,revision=revision+1,updated_at=? WHERE id=?', ('deferred' if choice=='defer' else 'resolved',_now_iso(),issue['id']))
            _log(conn,gid,choice,body)
            if choice=='conditions':
                _refresh_issues(service,gid)
        elif action == 'confirm':
            gid = body.get('id')
            _group(conn,gid,body.get('expected_revision', -1))
            if body.get('version_id') not in {v['id'] for v in _versions(conn,gid)}:
                raise ValueError('invalid_fact_selection')
            _choose(conn,gid,body['version_id'])
            _log(conn,gid,action,body)
            _refresh_issues(service,gid)
        elif action == 'split':
            gid = body.get('id')
            group = _group(conn,gid,body.get('expected_revision', -1))
            kid = body.get('knowledge_id')
            sources = conn.execute('SELECT s.* FROM knowledge_fact_sources s JOIN knowledge_fact_versions v ON v.id=s.version_id WHERE v.group_id=? AND s.active=1', (gid,)).fetchall()
            if kid not in {s['knowledge_id'] for s in sources} or len({s['knowledge_id'] for s in sources}) < 2:
                raise ValueError('invalid_fact_split')
            new = key(gid,kid,'separate')
            conn.execute('INSERT INTO knowledge_fact_groups(id,subject,property,created_at) VALUES(?,?,?,?)', (new,group['subject'],group['property'],_now_iso()))
            conn.execute('INSERT INTO knowledge_fact_separations VALUES(?,?,?)', (gid,kid,new))
            moved = {}
            for v in _versions(conn,gid):
                own = [s for s in sources if s['knowledge_id']==kid and s['version_id']==v['id']]
                if not own:
                    continue
                vid = key(new,v['value'],json.loads(v['conditions_json']),v['valid_from'],v['valid_until'])
                moved[v['id']] = vid
                conn.execute('INSERT INTO knowledge_fact_versions VALUES(?,?,?,?,?,?,?,?)', (vid,new,v['value'],v['conditions_json'],v['valid_from'],v['valid_until'],v['recorded_at'],v['exclusive']))
                for s in own:
                    conn.execute('INSERT INTO knowledge_fact_sources VALUES(?,?,?,?,?,?,?,1)', (vid,kid,s['source_key'],s['start'],s['end'],s['quote'],s['role']))
                conn.execute('UPDATE knowledge_fact_sources SET active=0 WHERE version_id=? AND knowledge_id=?', (v['id'],kid))
                conn.execute('UPDATE knowledge_fact_relations SET version_id=? WHERE version_id=? AND knowledge_id=?', (vid,v['id'],kid))
                if v['interpreted_conditions']:
                    conn.execute('INSERT INTO knowledge_fact_conditions VALUES(?,?)', (vid,v['interpreted_conditions']))
                if v['id'] in _choices(conn,gid):
                    _choose(conn,new,vid)
                    _log(conn,new,'confirm',{'version_id':vid,'inherited_from':gid})
            _log(conn,gid,action,{**body,'new_group':new,'versions':moved})
            _refresh_issues(service,gid)
            _refresh_issues(service,new)
        else:
            raise ValueError('invalid_fact_action')
    return browse(service, {})



def _verification(service, version, sources):
    """Read only resolved experience evidence, not model claims or archive clicks.

    A derivation binds the case to this knowledge. Exact supporting quote and
    conditions bind its real evidence to this fact. Retellings of the same
    canonical source event remain one verification even with different labels.
    """
    conn = service.store._connection()
    kids = list({s['knowledge_id'] for s in sources})
    marks = ','.join('?' for _ in kids)
    rows = conn.execute(f"""SELECT DISTINCT e.*,s.source_kind,s.source_ref,s.extraction_version
        FROM context_evidence e JOIN context_sources s ON s.id=e.source_id
        WHERE e.event_key!='' AND e.item_id IN (
            SELECT item_id FROM knowledge_objects WHERE knowledge_id IN ({marks})
            UNION SELECT d.item_id FROM unit_derivations d JOIN organization_units u
            ON u.task_id=d.unit_task_id AND u.digest=d.unit_digest
            WHERE u.knowledge_id IN ({marks}))
        ORDER BY e.id""", (*kids,*kids)).fetchall()
    latest = {}
    for row in rows:
        latest[(row['item_id'],row['event_key'])] = dict(row)
    events = {}
    for r in latest.values():
        if r['source_kind'] not in ('tool_result','user_confirmation','historical_record'):
            continue
        cond = json.loads(r['conditions_json'] or '{}')
        if any(cond.get(k)!=v for k,v in version['conditions'].items()):
            continue
        if not any(s['quote'] in r['note'] for s in sources):
            continue
        # The resolver's source ref identifies the original event; changing a
        # caller-supplied event label or repeating its narrative cannot add one.
        events[(r['source_kind'],r['source_ref'])] = r
    return [dict(level=r['verification_level'], source_ref=r['source_ref'],
                 source_kind=r['source_kind'], observed_at=r['observed_at'])
            for r in events.values() if r['outcome'] in ('success','confirmed')]


def browse(service, options):
    conn = service.store._connection()
    wanted = conditions(options.get('conditions', {}))
    at = business_date(options.get('at'))
    project, query = options.get('project', ''), str(options.get('q', '')).strip().casefold()
    items, issues = [], []
    for row in conn.execute('SELECT * FROM knowledge_fact_groups ORDER BY created_at DESC,id').fetchall():
        group = dict(row)
        if query and query not in (group['subject']+' '+group['property']).casefold():
            continue
        versions, relations = [], []
        confirmed = _choices(conn,group['id'])
        prior_confirmations = {json.loads(r['payload_json']).get('version_id') for r in conn.execute(
            "SELECT payload_json FROM knowledge_fact_actions WHERE group_id=? AND action IN ('choose','confirm')", (group['id'],))}
        for v in _versions(conn,group['id']):
            if any(v['conditions'].get(k)!=value for k,value in wanted.items()):
                continue
            sources = []
            for s in conn.execute('SELECT s.*,o.decision_json,o.retired FROM knowledge_fact_sources s JOIN knowledge_objects o USING(knowledge_id) WHERE version_id=? AND s.active=1', (v['id'],)):
                decision = json.loads(s['decision_json'])
                projects = decision.get('projects', []) if decision.get('action')=='include' and not s['retired'] else []
                if project and project not in projects:
                    continue
                archive = conn.execute('SELECT adapter,created_at FROM session_archives WHERE id=?', (int(s['source_key'].split(':')[1]),)).fetchone() if re.fullmatch(r'archive:[0-9]+',s['source_key']) else None
                sources.append(dict(s, projects=projects, adapter=archive['adapter'] if archive else '资料', source_created_at=archive['created_at'] if archive else ''))
            if not sources:
                continue
            verification = _verification(service,v,sources)
            v.update(sources=sources, original_conditions=json.loads(v['conditions_json']), verification_count=len(verification),
                     verification=verification,
                     state='confirmed' if v['id'] in confirmed else 'previously_confirmed' if v['id'] in prior_confirmations else 'suggested')
            versions.append(v)
            kids = {s['knowledge_id'] for s in sources}
            relations.extend(dict(r) for r in conn.execute('SELECT * FROM knowledge_fact_relations WHERE version_id=?', (v['id'],)) if r['knowledge_id'] in kids)
            for s in sources:
                for p in s['projects']:
                    relations.append(dict(type='属于项目',target=p,state='confirmed',knowledge_id=s['knowledge_id'],version_id=v['id'],quote=s['quote']))
        if not versions:
            continue
        ids = {v['id'] for v in versions}
        pending = [dict(r) for r in conn.execute("SELECT * FROM knowledge_fact_issues WHERE group_id=? AND state='pending'", (group['id'],)) if len(ids.intersection(json.loads(r['members_json'])))>1]
        issues.extend(pending)
        applicable = [v for v in versions if (not v['valid_from'] or v['valid_from']<=(at or date.today().isoformat())) and (not v['valid_until'] or (at or date.today().isoformat())<v['valid_until'])]
        chosen = next((v for v in applicable if v['id'] in confirmed), None)
        if chosen is None and applicable:
            # Business time, never arrival order, selects a dated historical view.
            if at and all(v['valid_from'] for v in applicable):
                newest = max(v['valid_from'] for v in applicable)
                latest = [v for v in applicable if v['valid_from']==newest]
                chosen = latest[0] if len(latest)==1 else None
            elif len(applicable)==1:
                chosen = applicable[0]
        if len({encoded(v['conditions']) for v in applicable}) > 1:
            chosen = None  # Distinct environments have no single universal conclusion.
        all_issues = [dict(r) for r in conn.execute('SELECT * FROM knowledge_fact_issues WHERE group_id=?', (group['id'],))]
        group.update(issue_history=all_issues,versions=versions, current=chosen, relations=list({encoded(r):r for r in relations}.values()), issues=pending)
        items.append(group)
    return {'items':items, 'issues':issues, 'total':len(items)}


def dispatch(service, method, route, body):
    if method == 'GET' and not route:
        return browse(service,body)
    if method == 'POST' and route in ('resolve','confirm','split'):
        return decide(service,route,body)
    raise LookupError('route_not_found')
