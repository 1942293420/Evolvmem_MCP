"""Human-selected project batches over old intake records; no model decisions."""
import sqlite3
from collections import OrderedDict

from evolvmem import knowledge_scope as ks
from evolvmem.project_ownership import load_ownership, UNREVIEWED_FACT

LABELS = {'history':'会话历史', 'knowledge':'项目知识资料',
          'verification':'经验等待验证', 'managed':'任务与系统记录',
          'conflict':'归属需要单独判断', 'answer':'问答需要核对答案'}


def _kind(row):
    if row.get('experience_payload'):
        return 'verification'
    if row['content_type'] in ('workstream_checkpoint','project_summary'):
        return 'managed'
    if (row['ownership']['reason'] in ('project_ownership_conflict','project_ownership_rejected')
            or len(row['projects'])>1 or row['scope']=='global'):
        return 'conflict'
    if row['learning'].get('answer_support',{}).get('verdict') in ('review','reject'):
        return 'answer'
    return 'history' if row['content_type']=='session_summary' else 'knowledge'


def overview(service, body):
    project=str(body.get('project') or '')
    kb=service.knowledge();kb._project(project)
    if not project:raise ValueError('project_required')
    rows=kb.conn.execute("SELECT id,project,projects,status FROM context_items i WHERE "+ks.match_sql('i')+
                         " AND status IN ('active','candidate') ORDER BY id",(project,)).fetchall()
    facts=load_ownership(service.store,[r['id'] for r in rows])
    groups=OrderedDict()
    for entry in rows:
        if entry['status']!='candidate' and not facts.get(entry['id'],UNREVIEWED_FACT).excluded:continue
        row=kb.detail(entry['id'],include_legacy_ids=False)
        kind=_kind(row)
        group=groups.setdefault(kind,{'id':kind,'title':LABELS[kind],'items':[]})
        group['items'].append({'id':row['id'],'title':row['title'],
            'preview':row['body'][:280],'expected_revision':row['revision'],
            'confirmed':row['ownership']['decision_source']=='human' and row['ownership']['state']=='confirmed',
            'reason':row['ingestion_reason'] or row['ownership']['reason']})
    for g in groups.values():g['count']=len(g['items'])
    return {'project':project,'total':sum(g['count'] for g in groups.values()),
            'groups':[g for k,g in groups.items() if k in ('history','knowledge')],
            'exceptions':[g for k,g in groups.items() if k not in ('history','knowledge')]}


def apply(service, body):
    kb=service.knowledge()
    source=str(body.get('source_project') or '')
    target=str(body.get('project') or '')
    action=body.get('action')
    items=body.get('items')
    kb._project(source)
    if not source or action not in ('archive','defer'):raise ValueError('invalid_request')
    if action=='archive':
        kb._project(target)
        if not target:raise ValueError('project_required')
    if not isinstance(items,list) or not 1<=len(items)<=20:raise ValueError('invalid_items')
    if any(not isinstance(e,dict) or type(e.get('id')) is not int or not isinstance(e.get('expected_revision'),str) for e in items):
        raise ValueError('invalid_items')
    if len({e['id'] for e in items})!=len(items):raise ValueError('invalid_items')
    results=[];changed=[]
    from evolvmem.project_store import ProjectStoreError
    for entry in items:
        try:
            # Each material is atomic: a failed publish cannot leave an
            # ownership change behind. No whole-project or future-row sweep.
            with service._cutover_lock.shared(),service.store.transaction():
                row=kb._check(entry['id'],entry)
                if source not in row['projects'] or row['status'] not in ('active','candidate'):
                    raise ValueError('revision_conflict')
                if _kind(row) not in ('history','knowledge'):
                    raise ValueError('individual_review_required')
                if action=='archive':
                    human=row['ownership']['decision_source']=='human' and row['ownership']['state']=='confirmed'
                    if human and row['projects']!=[target]:raise ValueError('confirmed_project_unchanged')
                    if not human:
                        row=kb.assign(row['id'],{'project':target,'expected_revision':row['revision']},sync=False)
                    kb.transition(row['id'],{'action':'publish','expected_revision':row['revision']},sync=False)
                else:
                    kb.transition(row['id'],{'action':'reject','expected_revision':row['revision']},sync=False)
            changed.append(entry['id']);results.append({'id':entry['id'],'ok':True})
        except (ValueError,ProjectStoreError,sqlite3.IntegrityError) as error:
            results.append({'id':entry['id'],'ok':False,'error':str(error) if not isinstance(error,sqlite3.IntegrityError) else 'identity_conflict'})
    # Refresh derived views once per bounded batch, not once per operation.
    if changed:kb._sync(changed)
    return {'items':results,'succeeded':len(changed),'failed':len(results)-len(changed)}
