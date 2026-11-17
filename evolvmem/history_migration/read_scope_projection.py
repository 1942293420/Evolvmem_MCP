"""Owner-maintained scope projection over preserved history; never edits raw rows."""
from __future__ import annotations
import hashlib
import json
from uuid import UUID
from evolvmem.library_foundation import FoundationError
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.project_ownership import classify

MAX_PAGE=128
MAX_BYTES=8*1024*1024

def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _array(value):
    if isinstance(value,str):value=json.loads(value)
    if not isinstance(value,list) or len(value)>64 or any(not isinstance(x,str) for x in value):
        raise FoundationError('invalid_history_projects')
    return list(dict.fromkeys(value))

def project_memberships(library_id,item,resolution,registry,knowledge,*,mapping_revision):
    """Deterministic original decision bindings, including explicitly held claims.

    Canonical human acceptance is final, including retained machine states.
    Only the original explicit item project(s) supply membership candidates. Old
    registry UUIDs and current ACL UUIDs are distinct fields. No content copied.
    """
    UUID(library_id)
    if type(mapping_revision) is not int or mapping_revision<1:raise FoundationError('invalid_history_mapping_revision')
    if type(item.get('id')) is not int or item['id']<1:raise FoundationError('invalid_history_item')
    registered={r['project']:r for r in registry}
    keys=_array(item.get('projects') or []) or ([item['project']] if item.get('project') else [])
    primary=item.get('project')
    human_accepted=bool(resolution and resolution.get('decision_source')=='human' and resolution.get('review_state')=='accepted')
    objects=sorted(knowledge,key=lambda r:r['knowledge_id'])
    decisions=[]
    for obj in objects:
        decision=json.loads(obj['decision_json'])
        if not isinstance(decision,dict):raise FoundationError('invalid_history_knowledge_decision')
        decisions.append((obj,decision))
    rows=[]
    for key in sorted(set(keys)):
        if not key or key.startswith('__'):continue
        acl_uuid=project_uuid(key)
        reg=registered.get(key)
        reason=''
        if reg is None or reg.get('status')!='active':reason='registry_not_active'
        elif not resolution:reason='resolution_missing'
        elif resolution.get('review_state') not in ('not_required','accepted'):reason='review_'+str(resolution.get('review_state','missing'))
        elif not classify(resolution.get('resolution_state'),resolution.get('review_state'),
                resolution.get('decision_source'),resolution.get('method')).confirmed:reason='resolution_'+str(resolution.get('resolution_state','missing'))
        elif not primary or primary not in keys:reason='item_project_conflict'
        elif resolution.get('resolved_project') and resolution['resolved_project']!=primary:reason='resolution_project_conflict'
        elif not human_accepted and not resolution.get('resolved_project'):reason='resolution_project_conflict'
        elif item.get('scope')!='project':reason='explicit_global_policy_required'
        for obj,decision in decisions:
            if obj.get('retired') or decision.get('action')!='include':reason=reason or 'knowledge_excluded_or_retired'
            elif key not in _array(decision.get('projects',[])):reason=reason or 'knowledge_project_conflict'
        source={'item':{k:item.get(k) for k in ('id','revision','project','projects','scope','status')},
                'resolution':resolution,'registry':reg,'knowledge':objects}
        rows.append(dict(library_id=library_id,item_id=item['id'],project_id=acl_uuid,project_key=key,
            registry_project_uuid=str(reg['project_uuid']) if reg else None,
            decision_ref='context_project_resolutions:'+str(item['id']) if resolution else 'unresolved:item:'+str(item['id']),
            decision_revision=resolution.get('revision',0) if resolution else 0,
            decision_hash=_hash(source),state='held' if reason else 'confirmed',reason=reason,
            mapping_revision=mapping_revision))
    return rows


def projection_page(connection,library_id,*,after_id=0,limit=MAX_PAGE,mapping_revision):
    """Trusted owner fixed bounded reads; no identifiers supplied by callers."""
    if type(limit) is not int or not 1<=limit<=MAX_PAGE or type(after_id) is not int or after_id<0:
        raise FoundationError('invalid_history_projection_page')
    with connection.cursor() as c:
        c.execute('SELECT to_jsonb(i) FROM em_content.context_items i WHERE library_id=%s AND id>%s ORDER BY id LIMIT %s',(library_id,after_id,limit))
        items=[r[0] for r in c.fetchall()]
        ids=[r['id'] for r in items]
        if not ids:return (),after_id
        c.execute('SELECT to_jsonb(r) FROM em_content.context_project_resolutions r WHERE library_id=%s AND item_id=ANY(%s) ORDER BY item_id',(library_id,ids))
        resolutions={r[0]['item_id']:r[0] for r in c.fetchall()}
        c.execute('SELECT to_jsonb(k) FROM em_content.knowledge_objects k WHERE library_id=%s AND item_id=ANY(%s) ORDER BY item_id,knowledge_id LIMIT 513',(library_id,ids))
        knowledge=[r[0] for r in c.fetchall()]
        if len(knowledge)>512 or len(json.dumps((items,knowledge),ensure_ascii=False).encode())>MAX_BYTES:raise FoundationError('history_projection_dependency_limit')
        keys=set()
        for item in items:keys.update(_array(item['projects']) or [item['project']])
        if len(keys)>512 or len(knowledge)>512:raise FoundationError('history_projection_dependency_limit')
        c.execute('SELECT to_jsonb(r) FROM em_content.context_project_registry r WHERE library_id=%s AND project=ANY(%s) ORDER BY project',(library_id,sorted(keys)))
        registry=[r[0] for r in c.fetchall()]
    rows=tuple(row for item in items for row in project_memberships(library_id,item,resolutions.get(item['id']),registry,
        [k for k in knowledge if k['item_id']==item['id']],mapping_revision=mapping_revision))
    if len(json.dumps(rows,ensure_ascii=False).encode())>MAX_BYTES:raise FoundationError('history_projection_dependency_limit')
    return rows,items[-1]['id']


def apply_projection_page(connection,library_id,item_ids,rows):
    """Caller owns transaction and fixed completed-epoch fence. Never enables reads."""
    if len(item_ids)>MAX_PAGE or any(type(i) is not int or i<1 for i in item_ids):raise FoundationError('invalid_history_projection_page')
    if any(r['library_id']!=library_id or r['item_id'] not in item_ids for r in rows):raise FoundationError('history_projection_scope_mismatch')
    with connection.cursor() as c:
        c.execute('DELETE FROM em_content.history_read_memberships WHERE library_id=%s AND item_id=ANY(%s)',(library_id,list(item_ids)))
        for r in rows:
            c.execute('''INSERT INTO em_content.history_read_memberships(library_id,item_id,project_id,project_key,registry_project_uuid,
                decision_ref,decision_revision,decision_hash,state,reason,mapping_revision)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',tuple(r[k] for k in ('library_id','item_id','project_id','project_key',
                    'registry_project_uuid','decision_ref','decision_revision','decision_hash','state','reason','mapping_revision')))

def build_projection(connection,library_id,*,epoch_id,completion_hash,mapping_revision):
    """Owner full projection in one RR write transaction; publishes mapping only.

    Source tables are SHARE locked so a catch-up or manual write cannot race the
    scan. The latest verified epoch is locked and checked. Readiness stays off.
    Caller commits/rolls back; neither route nor library ACL state is changed.
    """
    UUID(library_id);UUID(epoch_id)
    if not isinstance(completion_hash,str) or len(completion_hash)!=64:raise FoundationError('invalid_history_completion_hash')
    if type(mapping_revision) is not int or mapping_revision<1:raise FoundationError('invalid_history_mapping_revision')
    with connection.cursor() as c:
        c.execute("SELECT current_setting('transaction_isolation'),current_setting('transaction_read_only'),r.rolcanlogin,r.rolsuper,r.rolbypassrls FROM pg_roles r WHERE r.rolname=current_user")
        if c.fetchone()!=('repeatable read','off',False,False,False):raise FoundationError('history_projection_owner_transaction_required')
        c.execute("SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.history.scope_projection',0))")
        c.execute('''LOCK TABLE em_content.context_items,em_content.context_project_resolutions,
          em_content.context_project_registry,em_content.knowledge_objects IN SHARE MODE''')
        c.execute('''SELECT l.epoch_id::text,l.completion_hash FROM em_content.history_catchup_latest l
          JOIN em_content.history_catchup_epochs e ON e.library_id=l.library_id AND e.epoch_id=l.epoch_id
          WHERE l.library_id=%s AND e.state='complete' AND l.completion_hash=e.completion_hash
          AND NOT EXISTS(SELECT 1 FROM em_content.history_catchup_epochs pending WHERE pending.library_id=l.library_id AND pending.state<>'complete')
          FOR UPDATE OF l,e''',(library_id,))
        if c.fetchone()!=(epoch_id,completion_hash):raise FoundationError('history_projection_epoch_changed')
        c.execute('SELECT mapping_revision FROM em_security.history_membership_state WHERE library_id=%s FOR UPDATE',(library_id,))
        found=c.fetchone()
        if found is None or mapping_revision<=found[0]:raise FoundationError('history_projection_revision_conflict')
        c.execute('DELETE FROM em_content.history_read_memberships WHERE library_id=%s',(library_id,))
    after=0;count=0;digest=hashlib.sha256()
    while True:
        rows,next_id=projection_page(connection,library_id,after_id=after,mapping_revision=mapping_revision)
        if next_id==after:break
        with connection.cursor() as c:
            c.execute('SELECT id FROM em_content.context_items WHERE library_id=%s AND id>%s AND id<=%s ORDER BY id',(library_id,after,next_id))
            ids=tuple(r[0] for r in c.fetchall())
        apply_projection_page(connection,library_id,ids,rows)
        for row in rows:
            digest.update(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n')
            count+=1
        after=next_id
    membership_hash=digest.hexdigest()
    with connection.cursor() as c:
        c.execute('''UPDATE em_security.history_membership_state SET mapping_revision=%s,membership_hash=%s,
          completion_epoch=%s,completion_hash=%s,dirty=false WHERE library_id=%s''',
          (mapping_revision,membership_hash,epoch_id,completion_hash,library_id))
        c.execute('UPDATE em_security.history_readiness SET enabled=false,revision=revision+1 WHERE library_id=%s',(library_id,))
    return dict(library_id=library_id,mapping_revision=mapping_revision,membership_hash=membership_hash,
                completion_epoch=epoch_id,completion_hash=completion_hash,membership_count=count,last_item_id=after)

def save_event_proofs(connection,records):
    """Persist only canonical owner-verified native event bindings; never enable."""
    columns=('library_id','source_id','item_id','evidence_id','evidence_revision','experience_version',
        'evidence_note_hash','item_revision','item_content_hash','source_ref','extraction_version','archive_id',
        'archive_payload_sha256','task_id','role','event_hash','event_ref','state','proof_hash')
    if len(records)>64 or any(set(r)!=set(columns) for r in records):raise FoundationError('invalid_history_event_proofs')
    with connection.cursor() as c:
        for record in records:
            c.execute('''INSERT INTO em_content.history_source_event_proofs(library_id,source_id,item_id,evidence_id,
              evidence_revision,experience_version,evidence_note_hash,item_revision,item_content_hash,source_ref,
              extraction_version,archive_id,archive_payload_sha256,task_id,role,event_hash,event_ref,state,proof_hash)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(library_id,source_id,evidence_id) DO UPDATE SET
              item_id=EXCLUDED.item_id,evidence_revision=EXCLUDED.evidence_revision,experience_version=EXCLUDED.experience_version,
              evidence_note_hash=EXCLUDED.evidence_note_hash,item_revision=EXCLUDED.item_revision,
              item_content_hash=EXCLUDED.item_content_hash,source_ref=EXCLUDED.source_ref,
              extraction_version=EXCLUDED.extraction_version,archive_id=EXCLUDED.archive_id,
              archive_payload_sha256=EXCLUDED.archive_payload_sha256,task_id=EXCLUDED.task_id,role=EXCLUDED.role,
              event_hash=EXCLUDED.event_hash,event_ref=EXCLUDED.event_ref,state=EXCLUDED.state,proof_hash=EXCLUDED.proof_hash''',
              tuple(record[k] for k in columns))
