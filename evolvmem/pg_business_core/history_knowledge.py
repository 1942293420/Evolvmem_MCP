"""Bounded project knowledge projection over original B4 ledgers.

Manual choices/conditions/conflicts and same-scope equivalences are retained;
no SQLite connection or full-library scan is used by this runtime adapter.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from evolvmem.context_temporal import window_contains
from evolvmem.library_foundation import FoundationError
from evolvmem.project_knowledge import CATEGORIES,_current,_version,describe
from evolvmem.knowledge_scope import scope as knowledge_scope,projects as knowledge_projects
from .history_qualification import decoded
from .history_qualification_sql import KNOWLEDGE

CATALOG=('knowledge_objects','knowledge_fact_groups','knowledge_fact_versions','knowledge_decisions',
    'knowledge_equivalences','knowledge_fact_actions','knowledge_fact_choices','knowledge_fact_conditions',
    'knowledge_fact_issues','knowledge_fact_relations','knowledge_fact_separations','knowledge_fact_sources',
    'knowledge_metadata','knowledge_source_bindings','knowledge_evidence_items')


def fact_verification(store,version,sources,owners,evidence_items):
    kids={s['knowledge_id'] for s in sources}
    ids={owners[k] for k in kids}
    ids.update(e['item_id'] for e in evidence_items if e['knowledge_id'] in kids)
    events={}
    for ident in sorted(ids):
        row=store.eligibility.rows.get(ident)
        if row is None or not store.eligible(ident):continue
        physical={s['id']:s for s in store.eligibility._live_sources(row)}
        for event in store.eligibility.current_events(row):
            source=physical.get(event.get('source_id'))
            if source is None or source['source_kind'] not in ('tool_result','user_confirmation','historical_record'):continue
            conditions=decoded(event['conditions_json'],{})
            if any(conditions.get(k)!=v for k,v in version['conditions'].items()):continue
            if not any(s['quote'] in event['note'] for s in sources):continue
            key=(source['source_kind'],source['source_ref'])
            prior=events.get(key)
            if prior is None or event['id']>prior[0]['id']:events[key]=(event,source)
    return [dict(level=e['verification_level'],source_ref=s['source_ref'],source_kind=s['source_kind'],observed_at=e['observed_at'])
        for e,s in events.values() if e['outcome'] in ('success','confirmed')]


def project_catalog(store,catalog,selected,*,at=''):
    """Pure projection after the SQL security boundary; content stays scoped."""
    project=store.scope.project_key;objects={o['knowledge_id']:o for o in catalog['knowledge_objects']}
    owners={}
    for kid,obj in objects.items():
        ident=obj['item_id'];row=store.rows.get(ident);decision=decoded(obj['decision_json'],{})
        applicable=(knowledge_scope(decision)=='global' and not knowledge_projects(decision)
            if store.scope.project_id is None else project in decision.get('projects',[]))
        if (row and store.eligible(ident) and row['status']=='active' and not obj['retired']
                and decision.get('action')=='include' and applicable
                and (not row.get('expires_at') or row['expires_at']>store.eligibility.now)
                and window_contains(row['effective_from'],row['effective_until'],store.eligibility.now)):
            owners[kid]=ident
    managed={o['item_id'] for o in objects.values() if o['item_id'] is not None}
    versions={v['id']:v for v in catalog['knowledge_fact_versions']}
    conditions={c['version_id']:decoded(c['conditions_json'],{}) for c in catalog['knowledge_fact_conditions']}
    chosen={c['version_id'] for c in catalog['knowledge_fact_choices']}
    previous={decoded(a['payload_json'],{}).get('version_id') for a in catalog['knowledge_fact_actions']
        if a['action'] in ('choose','confirm')}
    covered=set();items=[]
    when=at or date.today().isoformat()
    for group in catalog['knowledge_fact_groups']:
        admitted=[]
        for original in sorted(versions.values(),key=lambda v:(v['valid_from'],v['recorded_at'],v['id'])):
            if original['group_id']!=group['id']:continue
            sources=[]
            for s in catalog['knowledge_fact_sources']:
                if s['version_id']!=original['id'] or not s['active'] or s['knowledge_id'] not in owners:continue
                obj=objects[s['knowledge_id']];decision=decoded(obj['decision_json'],{})
                sources.append(dict(s,projects=decision.get('projects',[]),adapter=s.get('adapter','资料'),
                    source_created_at=s.get('source_created_at','')))
            if not sources:continue
            value=dict(original,conditions=conditions.get(original['id'],decoded(original['conditions_json'],{})),
                state='confirmed' if original['id'] in chosen else 'previously_confirmed' if original['id'] in previous else 'suggested',
                verification_count=0,verification=[])
            proof=fact_verification(store,value,sources,owners,catalog.get('knowledge_evidence_items',[]))
            value.update(verification=proof,verification_count=len(proof))
            admitted.append(_version(value,sources))
        if not admitted:continue
        kids={s['knowledge_id'] for v in admitted for s in v['sources']};ids=sorted({owners[k] for k in kids});covered.update(ids)
        visible_versions={v['id'] for v in admitted}
        issues=[i for i in catalog['knowledge_fact_issues'] if i['group_id']==group['id']
            and i['state'] in ('pending','deferred') and visible_versions.intersection(decoded(i['members_json'],[]))]
        warnings=[dict(kind=i['kind'],state=i['state'],conditions=decoded(i['conditions_json'],{}),
            message='存在分歧，尚未决定采用哪条结论' if i['kind']=='conflict' else '有新的变化，尚未确认替换原结论') for i in issues]
        if catalog['restricted_conflicts']:
            warnings.append(dict(kind='restricted_conflict',state='pending',conditions={},message='另有未授权资料或决定，不能作为本项目结论'))
        current=_current(admitted,when)
        if at:
            current=[v for v in current if not v['valid_from'] or v['valid_from']==max(
                (x['valid_from'] for x in current if x['conditions']==v['conditions']),default='')]
        items.append(dict(id='fact:'+group['id'],kind='fact',title=group['subject']+' · '+group['property'],
            subject=group['subject'],property=group['property'],section=CATEGORIES.get(objects[sorted(kids)[0]]['category'],'参考知识'),
            item_ids=ids,knowledge_ids=sorted(kids),versions=admitted,current=current,alternatives=[],warnings=warnings,
            relations=[r for r in catalog['knowledge_fact_relations'] if r['knowledge_id'] in kids
                and r['version_id'] in visible_versions],sources=[s for v in admitted for s in v['sources']],body='',trigger=''))
    metadata={m['item_id']:m for m in catalog['knowledge_metadata']}
    for ident in sorted(set(owners.values())-covered):
        row=store.rows[ident];layers=store.layers.get(ident,());kidrows=[o for k,o in objects.items() if owners.get(k)==ident]
        meta=metadata.get(ident,{})
        text=next((l[1] for l in layers if l[0]=='l1'),'') or next((l[1] for l in layers if l[0]=='l0'),'')
        title=meta.get('title') or next((l[1][:120] for l in layers if l[0]=='l0'),'')
        section='问题处理' if row['content_type']=='playbook' else CATEGORIES.get(kidrows[0]['category'],'参考知识')
        items.append(dict(id='item:'+str(ident),kind='material',title=title,section=section,item_ids=[ident],
            knowledge_ids=[o['knowledge_id'] for o in kidrows],body=text,trigger='',sources=[],versions=[],current=[],
            relations=[],warnings=[],alternatives=[]))
    # Canonical one-hop, explicitly confirmed equivalence; never pull objects
    # omitted by the SQL scope boundary back in through a relation.
    by_kid={kid:i for i in items for kid in i['knowledge_ids']};removed=set()
    for link in catalog['knowledge_equivalences']:
        a,b=link['source_knowledge_id'],link['target_knowledge_id']
        source,target=by_kid.get(a),by_kid.get(b)
        if not link['active'] or a not in owners or b not in owners or source is None or target is None or source is target:continue
        da,db=(decoded(objects[k]['decision_json'],{}) for k in (a,b))
        scope=lambda d:(d.get('action'),d.get('knowledge_scope',d.get('scope','project')),tuple(sorted(d.get('projects',[]))))
        if scope(da)!=scope(db) or set(source['knowledge_ids'])!={a} or source['id'] in removed:continue
        target.setdefault('expressions',[]).append(dict(title=source['title'],text=source['body'] or '\n'.join(v['value'] for v in source['versions']),
            knowledge_id=a,sources=source['sources']))
        for field in ('item_ids','knowledge_ids','sources','versions','current','alternatives','warnings','relations'):
            for value in source[field]:
                if value not in target[field]:target[field].append(value)
        if source['kind']=='fact':target['kind']='fact'
        removed.add(source['id'])
    wanted=set(selected)
    return managed,[i for i in items if i['id'] not in removed and wanted.intersection(i['item_ids'])]


class PgHistoryKnowledgeDirectory:
    def __init__(self,store):self.store=store

    def load(self,ids,*,at=''):
        ids=tuple(dict.fromkeys(ids))
        if len(ids)>240:raise FoundationError('recall_candidate_limit')
        if not ids:return set(),[]
        store=self.store
        rows=store.session.execute(KNOWLEDGE,(store.lib,store.project,list(ids),at))
        if len(rows)!=1 or len(rows[0])!=1:raise FoundationError('invalid_knowledge_shape')
        catalog=rows[0][0]
        if not isinstance(catalog,dict) or set(catalog)!=set(CATALOG)|{'restricted_conflicts'}:
            raise FoundationError('invalid_knowledge_shape')
        for key in CATALOG:
            if not isinstance(catalog[key],list) or len(catalog[key])>512 or any(not isinstance(v,dict) for v in catalog[key]):
                raise FoundationError('recall_dependency_limit')
        if type(catalog['restricted_conflicts']) is not bool:raise FoundationError('invalid_knowledge_shape')
        for key in CATALOG:
            catalog[key].sort(key=lambda row:json.dumps(row,sort_keys=True,ensure_ascii=False))
        store._retain(catalog);store.knowledge_proof.append(deepcopy(catalog))
        extra=tuple(dict.fromkeys([o['item_id'] for o in catalog['knowledge_objects'] if o['item_id'] is not None]
            +[e['item_id'] for e in catalog['knowledge_evidence_items']]))
        store._fetch_items(extra)
        for start in range(0,len(extra),240):store.allowed.update(store.eligibility.eligible_ids(extra[start:start+240]))
        return project_catalog(store,catalog,ids,at=at)

    def visible(self,ident):
        managed,items=self.load((ident,))
        return ident not in managed or any(ident in i['item_ids'] for i in items)

    def project_results(self,results,request):
        if not results or not request.project or request.cross_project:return tuple(results)
        ids=tuple(dict.fromkeys(i for r in results for i in (r.id,*r.duplicate_ids)))
        managed,items=self.load(ids,at=(request.as_of or '')[:10])
        visible={i for row in items for i in row['item_ids']};output=[]
        for result in results:
            if result.id in managed and result.id not in visible:continue
            linked=tuple(i for i in items if set(i['item_ids']).intersection((result.id,*result.duplicate_ids)))
            facts=[i for i in linked if i['kind']=='fact']
            output.append(replace(result,knowledge=linked,l0='\n'.join(describe(i) for i in facts) if facts else result.l0))
        return tuple(output)
