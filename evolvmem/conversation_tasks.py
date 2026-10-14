"""Logical conversations over immutable, individually verifiable source archives.

The manifest only joins sources whose native identity was checked. Project
decisions never determine whether messages belong to the same conversation.
"""
import hashlib
import json

from evolvmem.context_store import _now_iso
from evolvmem.conversation import render

PREFIX = 'conversation:'


def is_conversation(key):
    return str(key).startswith(PREFIX)


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _identity(service,key):
    from evolvmem.organization_context import archive_info
    info=archive_info(service,key)
    if info:
        return 'codex:'+info['head'],info
    # Unrecognised/imported sources remain independent. Never guess a native
    # session from title, nearby archives, project or similar text.
    return key,None


def members(service,key):
    row=service.store._connection().execute(
        'SELECT members FROM organization_conversations WHERE source_key=?',(key,)).fetchone()
    if row is None:raise ValueError('conversation_not_found')
    return json.loads(row['members'])


def register(service,source_keys,*,expand=False):
    from evolvmem.history_organization import source_excluded,_unassigned_archives
    from evolvmem.knowledge_cleaning import record
    from evolvmem import organization_context as ctx
    visible={r['id'] for r in _unassigned_archives(service)}
    groups={}
    for key in dict.fromkeys(source_keys):
        if source_excluded(service,key):continue
        if is_conversation(key):
            row=service.store._connection().execute('SELECT identity FROM organization_conversations WHERE source_key=?',(key,)).fetchone()
            if row is None:raise ValueError('conversation_not_found')
            groups.setdefault(row['identity'],set()).update(members(service,key));continue
        if source_excluded(service,key):continue
        record(service,key,visible=visible)
        identity,info=_identity(service,key)
        group=groups.setdefault(identity,set());group.add(key)
        if expand and info:
            for sibling in service.store._connection().execute(ctx._sibling_sql(),ctx._sibling_params(info)):
                candidate=f'archive:{sibling["id"]}'
                if sibling['id'] not in visible:continue
                other=ctx.archive_info(service,candidate)
                if other and other['head']==info['head']:group.add(candidate)
    prepared=[]
    for identity,keys in groups.items():
        key=PREFIX+_hash(identity)[:32]
        exists=service.store._connection().execute('SELECT members FROM organization_conversations WHERE source_key=?',(key,)).fetchone()
        if exists:keys.update(json.loads(exists['members']))
        ordered=[]
        for member in keys:
            actual,info=_identity(service,member)
            if actual!=identity:raise ValueError('conversation_source_changed')
            record(service,member,visible=visible)
            ordered.append((info['start_line'] if info else 0,info['end_line'] if info else 0,member))
        ordered.sort()
        for previous,current in zip(ordered,ordered[1:]):
            if current[0] and current[0]<=previous[1]:raise ValueError('conversation_overlap')
        prepared.append((key,identity,[row[2] for row in ordered]))
    with service.store.transaction():
        for key,identity,keys in prepared:
            service.store._connection().execute(
                'INSERT INTO organization_conversations(source_key,identity,members,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(source_key) DO UPDATE SET members=excluded.members,updated_at=excluded.updated_at',
                (key,identity,json.dumps(keys),_now_iso()))
    return [row[0] for row in prepared]


def revision(service,key,*,visible=None,excluded=None):
    """Check current membership/revisions without assembling every message."""
    from evolvmem.history_organization import _unassigned_archives, source_excluded
    if source_excluded(service,key,excluded=excluded):raise ValueError('classification_source_changed')
    from evolvmem.knowledge_cleaning import record,review
    if visible is None:visible={r['id'] for r in _unassigned_archives(service,excluded=excluded)}
    values=[]
    for member in members(service,key):
        row=record(service,member,visible=visible,excluded=excluded)
        if member.startswith('archive:'):
            archive=service.store.get_session_archive(int(member.split(':')[1]))
            if not archive or archive['state']!='available':raise ValueError('cleaning_source_unavailable')
        values.append([member,row['expected_revision']])
    saved=review(service,key)
    if saved and saved['state']=='deleted':raise ValueError('cleaning_source_deleted')
    raw=_hash(['complete-tasks-v1',values])
    return hashlib.sha256((raw+':'+str(saved['revision'] if saved else 0)).encode()).hexdigest()


def snapshot(service,key,*,visible=None):
    from evolvmem.history_organization import _unassigned_archives
    from evolvmem.knowledge_cleaning import record,cleaned_messages
    if visible is None:visible={r['id'] for r in _unassigned_archives(service)}
    policy=service.knowledge().rules.read()
    messages=[];records=[];revisions=[];cursor=0
    for member in members(service,key):
        row=record(service,member,visible=visible)
        revisions.append([member,row['expected_revision']])
        archive=None
        if member.startswith('archive:'):
            archive=service.store.get_session_archive(int(member.split(':')[1]))
            if not archive or archive['state']!='available':raise ValueError('cleaning_source_unavailable')
        part=cleaned_messages(service,row,policy)
        text=render(part)
        if not text.strip():
            if not row.get('dialogue_available',True):continue
            raise ValueError('cleaning_source_unavailable')
        # Identity was verified when the manifest was registered. Reading the
        # view need not decrypt every native transcript again just for labels.
        info={}
        from evolvmem.session_identity import is_incremental_batch
        external=(archive or {}).get('external_session_id','')
        if (archive or {}).get('adapter')=='codex' and is_incremental_batch(external):
            start_line,end_line=external.split(':')[1].split('-')
            info={'start_line':int(start_line),'end_line':int(end_line)}
        records.append({'source_key':member,'title':row['title'],'adapter':(archive or {}).get('adapter','资料'),
                        'created_at':row['created_at'],'text':text,'start':cursor,'end':cursor+len(text),
                        'source_revision':row['expected_revision'],'start_line':(info or {}).get('start_line'),
                        'end_line':(info or {}).get('end_line')})
        messages.extend(part);cursor+=len(text)+2
    body=render(messages)
    return {'key':key,'kind':'conversation','title':records[0]['title'] if records else '无对话内容',
            'body':body,'messages':messages,'records':records,'available':True,
            'expected_revision':_hash(['complete-tasks-v1',revisions]),
            'source':'完整会话','created_at':records[0]['created_at'] if records else '',
            'dialogue_available':bool(body.strip())}


def enqueue(service,source_keys,*,expand=False):
    from evolvmem import auto_organization as org
    sync=[]
    with service.store.transaction():
        keys=register(service,source_keys,expand=expand)
        result = org.enqueue(service,keys,deferred_sync=sync)
    if sync:service.knowledge()._sync(sorted(set(sync)))
    # Retire old fragment jobs only after the new durable job exists. Original
    # units and human decisions remain stored and are carried into task review.
    for task in result['items']:
        for member in members(service,task['source_key']):
            with service.store.transaction():
                service.store._connection().execute(
                    "UPDATE organization_tasks SET status='superseded',superseded_by=?,updated_at=? "
                    "WHERE source_key=? AND status!='superseded'",
                    (task['id'],_now_iso(),member))
                service.store._connection().execute(
                    "INSERT OR REPLACE INTO organization_task_upgrades(task_id,replacement_id,updated_at) SELECT id,?,? FROM organization_tasks WHERE source_key=? AND superseded_by=?",
                    (task['id'],_now_iso(),member,task['id']))
            org._mark_outputs_stale(service,member,current_task=task['id'])
    return result


def protect_prior_decisions(service,task):
    """Changed task boundaries cannot silently expand a previous human decision."""
    from evolvmem import topic_segmentation, task_narrative
    conn=service.store._connection()
    source=snapshot(service,task['source_key'])
    offsets={r['source_key']:r for r in source['records']}
    keys=[task['source_key'],*offsets]
    marks=','.join('?' for _ in keys)
    previous=[dict(r) for r in conn.execute(
        'SELECT u.*,t.source_key,t.source_snapshot FROM organization_units u '
        'JOIN organization_tasks t ON t.id=u.task_id '
        f'WHERE t.id<? AND t.source_key IN ({marks}) AND (u.decision=\'manual\' OR EXISTS('
        'SELECT 1 FROM organization_scope_exclusions e WHERE e.source_key=t.source_key AND e.digest=u.digest)) '
        'ORDER BY t.id DESC', [task['id'],*keys])]
    if not previous:return
    with service.store.transaction():
        for row in conn.execute('SELECT * FROM organization_units WHERE task_id=?',(task['id'],)).fetchall():
            unit=dict(row)
            if unit['decision']=='manual':continue
            ranges=topic_segmentation.unit_ranges(unit)
            decisions=[];seen=set()
            for old in previous:
                token=(old['source_key'],old['digest'])
                if token in seen:continue
                seen.add(token)
                part=offsets.get(old['source_key'])
                base=part['start'] if part else 0
                old_ranges=topic_segmentation.unit_ranges(old) or [[old['source_start'],old['source_end']]]
                if part and old['source_snapshot']!=part['text']:
                    old_ranges=[[0,len(part['text'])]]
                if not any(a<base+y and b>base+x for a,b in ranges for x,y in old_ranges):continue
                exclusions=[r['scope_key'] for r in conn.execute(
                    'SELECT scope_key FROM organization_scope_exclusions WHERE source_key=? AND digest=?',token)]
                decisions.append({'title':old['title'],'project':old['project'],
                                  'projects':json.loads(old['projects'] or '[]'),
                                  'knowledge_scope':old['knowledge_scope'], 'decision':old['decision'],
                                  'excluded_scopes':exclusions,'text':old['text']})
                if old['text']==unit['text'] and old['decision']=='manual':
                    conn.execute("UPDATE organization_units SET decision='manual',project=?,projects=?,knowledge_scope=?,reason=?,disposition=? WHERE task_id=? AND digest=?",
                                 (old['project'],old['projects'],old['knowledge_scope'],old['reason'],old['disposition'],task['id'],unit['digest']))
            if decisions:
                record=task_narrative.read(unit['task_record']);record['prior_decisions']=decisions
                conn.execute('UPDATE organization_units SET task_record=? WHERE task_id=? AND digest=?',
                             (json.dumps(record,ensure_ascii=False),task['id'],unit['digest']))
                from evolvmem.organization_exclusions import _stamp
                full={**unit,'source_revision':task['source_revision'],'rule_revision':task['rule_revision']}
                for scope in {s for d in decisions for s in d['excluded_scopes']}:
                    conn.execute('INSERT OR IGNORE INTO organization_scope_exclusions(source_key,digest,scope_key,content_stamp,revision,updated_at) VALUES(?,?,?,?,1,?)',
                                 (task['source_key'],unit['digest'],scope,_stamp(full),_now_iso()))


def handled_members(service,*,finished=False):
    statuses="('completed','review')" if finished else "('pending','running','completed','review')"
    rows=service.store._connection().execute(
        'SELECT c.members FROM organization_conversations c WHERE EXISTS('
        f'SELECT 1 FROM organization_tasks t WHERE t.source_key=c.source_key AND t.status IN {statuses})')
    return {key for row in rows for key in json.loads(row['members'])}
