"""Rebuildable project reading projection shared by the UI and Agent retrieval.

No model calls or writes. Ownership, source eligibility and fact decisions keep
living in their original ledgers; directory headings are only navigation.
"""
from __future__ import annotations
from datetime import date
import json
import re
from types import SimpleNamespace

from evolvmem import knowledge_facts as facts, knowledge_scope as ks
from evolvmem.context_store import _now_iso
from evolvmem.context_temporal import window_contains
from evolvmem.project_ownership import load_ownership, UNREVIEWED_FACT
from evolvmem.unit_derivations import current_item_ids

SECTIONS = ('业务规则', '运行与部署', '问题处理', '决策变化', '开发经验', '参考知识')
CATEGORIES = {'habit':'业务规则', 'project_convention':'业务规则', 'task_requirement':'业务规则',
              'environment':'运行与部署', 'decision':'决策变化', 'experience':'开发经验'}


def _admitted(store, project):
    conn = store._connection()
    rows = conn.execute("""SELECT i.*,m.title,l.category,json_extract(l.payload,'$.trigger') AS trigger,l.payload AS learning_payload
        FROM context_items i LEFT JOIN knowledge_metadata m ON m.item_id=i.id
        LEFT JOIN learning_memories l ON l.item_id=i.id
        WHERE i.status='active' AND i.content_type NOT IN ('workstream_checkpoint','project_summary')
        AND """ + ks.match_sql('i'), (project,)).fetchall()
    ownership = load_ownership(store, [r['id'] for r in rows])
    backed = set(current_item_ids(store, [r['id'] for r in rows]))
    now = _now_iso()
    return {r['id']:dict(r) for r in rows if r['id'] in backed
            and not ownership.get(r['id'],UNREVIEWED_FACT).excluded
            and (not r['expires_at'] or r['expires_at']>now)
            and window_contains(r['effective_from'],r['effective_until'],now)}


def _version(v, sources, archived=True):
    return {k:v[k] for k in ('id','value','conditions','valid_from','valid_until','recorded_at','state','verification_count','verification')} | {
        'sources':[{k:s[k] for k in ('knowledge_id','source_key','start','end','quote','role','adapter','source_created_at','projects')} for s in sources],
        'archive_state':'archived' if archived else 'not_in_project'}


def _current(versions, at):
    by_condition = {}
    for v in versions:
        if (not v['valid_from'] or v['valid_from']<=at) and (not v['valid_until'] or at<v['valid_until']):
            by_condition.setdefault(facts.encoded(v['conditions']), []).append(v)
    current = []
    for candidates in by_condition.values():
        chosen = [v for v in candidates if v['state']=='confirmed']
        if chosen:
            current.extend(chosen)
        elif len(candidates)==1:
            current.extend(candidates)
        else:
            # Multiple unchosen values remain explicit alternatives; arrival
            # order never turns one into the authoritative conclusion.
            current.extend(candidates)
    return current


def directory(store, project, *, q='', at='', conditions=None, item_ids=None):
    if not project or project.startswith('__'):
        raise ValueError('project_required')
    if not isinstance(q,str) or len(q)>4000:
        raise ValueError('invalid_query')
    when = facts.business_date(at) or date.today().isoformat()
    wanted = facts.conditions(conditions or {})
    conn = store._connection()
    admitted = _admitted(store,project)
    objects = {r['knowledge_id']:dict(r) for r in conn.execute('SELECT * FROM knowledge_objects WHERE retired=0')}
    owners = {}
    for kid,obj in objects.items():
        decision = json.loads(obj['decision_json'])
        if obj['item_id'] in admitted and decision.get('action')=='include' and project in decision.get('projects',[]):
            owners[kid] = obj['item_id']
    items = []
    covered = {owners[r[0]] for r in conn.execute('SELECT DISTINCT knowledge_id FROM knowledge_fact_sources WHERE active=1') if r[0] in owners}
    # A fact may have both saved and proposed sources. Only saved sources form
    # project knowledge. Conflicting proposals stay in a separate warning lane.
    groups = facts.browse(SimpleNamespace(store=store), {'conditions':wanted})['items'] if owners else []
    for group in groups:
        versions, alternatives = [], []
        for v in group['versions']:
            sources = [s for s in v['sources'] if s['knowledge_id'] in owners and project in s['projects']]
            if sources:
                proof = facts._verification(SimpleNamespace(store=store),v,sources)
                versions.append(_version(dict(v,verification=proof,verification_count=len(proof)),sources))
            else:
                alternatives.append(_version(v,v['sources'],False))
        if not versions:
            continue
        kids = {s['knowledge_id'] for v in versions for s in v['sources']}
        ids = sorted({owners[k] for k in kids})
        covered.update(ids)
        issues = [r for r in group['issue_history'] if r['state'] in ('pending','deferred')
                  and {v['id'] for v in versions}.intersection(json.loads(r['members_json']))]
        member_ids = {i for r in issues for i in json.loads(r['members_json'])}
        chosen_elsewhere = [v for v in alternatives if v['state']=='confirmed' and any(x['conditions']==v['conditions'] for x in versions)]
        alternatives = [v for v in alternatives if v['id'] in member_ids or v in chosen_elsewhere]
        warnings = [{'kind':r['kind'],'state':r['state'],'conditions':json.loads(r['conditions_json']),
                     'message':'存在分歧，尚未决定采用哪条结论' if r['kind']=='conflict' else '有新的变化，尚未确认替换原结论'} for r in issues]
        if chosen_elsewhere:
            warnings.append({'kind':'unarchived_choice','state':'pending','conditions':{},'message':'已确认另一条结论，但其资料未归入本项目'})
        current = _current(versions,when)
        # An explicit historical query uses business dates, within each condition.
        if at:
            current = [v for v in current if not v['valid_from'] or v['valid_from']==max(
                (x['valid_from'] for x in current if x['conditions']==v['conditions']),default='')]
        section = CATEGORIES.get(objects[sorted(kids)[0]]['category'],'参考知识')
        item = dict(id='fact:'+group['id'],kind='fact',title=group['subject']+' · '+group['property'],
                    subject=group['subject'],property=group['property'],section=section,item_ids=ids,
                    knowledge_ids=sorted(kids),versions=versions,current=current,alternatives=alternatives,
                    warnings=warnings,relations=[r for r in group['relations'] if r['knowledge_id'] in kids],
                    sources=[s for v in versions for s in v['sources']],body='',trigger='')
        items.append(item)
    fallback_ids = [i for i in admitted if not wanted and i not in covered]
    layers_by_id, sources_by_id, titles = {}, {}, {}
    categories_by_id = {o['item_id']:o['category'] for o in objects.values()}
    for offset in range(0,len(fallback_ids),400):
        batch=fallback_ids[offset:offset+400]
        marks=','.join('?' for _ in batch)
        for r in conn.execute(f"SELECT item_id,layer,content FROM context_layers WHERE item_id IN ({marks}) AND layer IN ('l0','l1')",batch):
            layers_by_id.setdefault(r['item_id'],{})[r['layer']]=r['content']
        for r in conn.execute(f'SELECT item_id,source_kind,source_ref,archive_id,created_at FROM context_sources WHERE item_id IN ({marks}) ORDER BY id',batch):
            sources_by_id.setdefault(r['item_id'],[]).append({k:r[k] for k in ('source_kind','source_ref','archive_id','created_at')})
        for r in conn.execute(f'SELECT o.item_id,u.title FROM organization_units u JOIN knowledge_objects o ON o.knowledge_id=u.knowledge_id WHERE o.item_id IN ({marks}) ORDER BY u.task_id DESC',batch):
            titles.setdefault(r['item_id'],r['title'])
    for item_id in fallback_ids:
        row=admitted[item_id]
        if item_id in covered:
            continue
        # No facts (old data or unavailable model): keep the user's saved
        # material readable, without pretending its prose is a verified method.
        layers = layers_by_id.get(item_id,{})
        text = layers.get('l1') or layers.get('l0','')
        sources = sources_by_id.get(item_id,[])
        category = categories_by_id.get(item_id,row['category'])
        section = CATEGORIES.get(category,'参考知识')
        if row['content_type']=='playbook':
            section='问题处理'
        title = titles.get(item_id) or re.sub(r'^[a-z_]+:\s*','',row['title'] or layers.get('l0','')[:120])
        items.append(dict(id='item:'+str(item_id),kind='material',title=title,
                          section=section,item_ids=[item_id],knowledge_ids=[k for k,o in objects.items() if o['item_id']==item_id],body=text,trigger=row['trigger'] or '',
                          sources=sources,versions=[],current=[],relations=[],warnings=[],alternatives=[]))
    from evolvmem.knowledge_equivalence import coalesce
    items = coalesce(store, items, objects)
    if item_ids is not None:
        allowed=set(item_ids)
        items=[i for i in items if allowed.intersection(i['item_ids'])]
    if q.strip():
        from evolvmem.memory_recall import relevance
        items=[i for i in items if relevance(q, search_text(i))>0]
        items.sort(key=lambda i:-relevance(q,search_text(i)))
    else:
        items.sort(key=lambda i:(SECTIONS.index(i['section']),i['title'],i['id']))
    return {'project':project,'items':items,'total':len(items), 'at':at,
            'sections':[{'name':s,'count':sum(i['section']==s for i in items)} for s in SECTIONS if any(i['section']==s for i in items)]}


def search_text(item):
    return '\n'.join([item['title'], item['body'], item['trigger'],
                      *[v['value']+' '+facts.encoded(v['conditions']) for v in item['versions']],
                      *[r['target'] for r in item['relations']],
                      *[e['text'] for e in item.get('expressions',[])]])


def describe(item):
    """One indivisible recall block: never truncate away a conflict warning."""
    lines = [f"〔项目知识 · {item['title']} · {item['section']}〕"]
    lines.extend('注意：'+w['message'] for w in item['warnings'])
    if item['kind']=='material':
        lines.extend([item['body'], '来源：'+', '.join('#'+str(i) for i in item['item_ids'])])
        if item['trigger']:
            lines.append('适用：'+item['trigger'])
    else:
        if item['body']:
            lines.append(item['body'])
        for v in item['current']:
            lines.append(('已确认结论：' if v['state']=='confirmed' else '已归档资料中的说法：')+v['value']+
                         '；适用：'+('、'.join(k+'='+x for k,x in v['conditions'].items()) or '未注明')+
                         (f"；生效：{v['valid_from']}" if v['valid_from'] else ''))
            for s in v['sources']:
                lines.append(f"来源 {s['source_key']}:{s['start']}-{s['end']}：{s['quote']}")
        if not item['current']:
            lines.append('所查日期没有适用结论；其他版本仅作历史参考。')
        for v in item['alternatives']:
            lines.append('未归入本项目的分歧资料（不能作为本项目结论）：'+v['value'])
            for s in v['sources']:
                lines.append(f"来源 {s['source_key']}:{s['start']}-{s['end']}：{s['quote']}")
        old=[v for v in item['versions'] if v not in item['current']]
        if old:
            lines.append('其他版本：'+'；'.join(v['value']+'（'+(v['valid_from'] or '日期未注明')+'）' for v in old))
        for r in item['relations']:
            if r['type']!='属于项目':
                lines.append(f"关联建议：{r['type']} {r['target']}；依据：{r['quote']}")
    lines.extend('已确认的同义表达：'+e['text'] for e in item.get('expressions',[]))
    return '\n'.join(lines)+'\n'
