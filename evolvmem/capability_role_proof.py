"""Bounded, read-only roles from existing structured originals; no decryption.

Metadata budgets precede all prose reads. A source's compact spans are reusable
only for the same connection/content/policy version, never across transactions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evolvmem.conversation import clean_messages, render
from evolvmem.extraction_policy import redact_messages
from evolvmem.knowledge_rules import KnowledgeRules
from evolvmem.topic_segmentation import message_spans

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_MESSAGES = 8192
MAX_SPANS = 8192
MAX_CACHED_SPANS = 16384
MAX_UNIT_CHARS = 24000


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class UnitRoleRead:
    def __init__(self, store):
        self.store, self.conn = store, store._connection()
        self.cache = {}
        self.epoch = None
        self._scope_active = False

    def token(self):
        return (id(self.conn), self.conn.execute('PRAGMA data_version').fetchone()[0], self.conn.total_changes,
                self.conn.execute('PRAGMA schema_version').fetchone()[0])

    def _policy(self):
        config = getattr(self.store, 'config', None)
        if config is not None:
            directory = config.data_dir
        else:
            filename = next((r[2] for r in self.conn.execute('PRAGMA database_list') if r[1]=='main'), '')
            if not filename:
                raise ValueError('policy_location_unproven')
            directory = Path(filename).parent
        return KnowledgeRules(directory).read()

    @staticmethod
    def failed(reason, task=None):
        return {'ready':False,'reason':'unit_source_roles_unproven:'+reason,'task':task,'spans':[]}

    def preflight(self, task_id, digest=None):
        """Only lengths/hashes/identity, before provenance can read a body."""
        row=self.conn.execute('SELECT id,source_key,source_revision,rule_revision,status,superseded_by,'
            'length(CAST(source_snapshot AS BLOB)) AS snapshot_bytes,length(source_snapshot) AS snapshot_chars '
            'FROM organization_tasks WHERE id=?',(task_id,)).fetchone()
        if row is None:return self.failed('task_missing')
        task=dict(row)
        if digest is not None:
            sizes=self.conn.execute('SELECT max(length(text)),max(length(CAST(text AS BLOB))) '
                'FROM organization_units WHERE task_id=? AND digest=?',(task_id,digest)).fetchone()
            if (sizes[0] or 0)>MAX_UNIT_CHARS or (sizes[1] or 0)>MAX_SOURCE_BYTES:
                return self.failed('unit_over_budget',task)
        if not task['snapshot_bytes']:return self.failed('snapshot_missing',task)
        total=task['snapshot_bytes']
        if total>MAX_SOURCE_BYTES:return self.failed('over_budget',task)
        key=task['source_key']
        if key.startswith('conversation:'):
            size=self.conn.execute('SELECT length(CAST(members AS BLOB)) FROM organization_conversations WHERE source_key=?',(key,)).fetchone()
            if not size or not size[0]:return self.failed('manifest_unproven',task)
            if size[0]>MAX_MEMBERS*32:return self.failed('manifest_over_budget',task)
            try:members=json.loads(self.conn.execute('SELECT members FROM organization_conversations WHERE source_key=?',(key,)).fetchone()[0])
            except (TypeError,ValueError):return self.failed('manifest_unproven',task)
        else:members=[key]
        if isinstance(members,list) and len(members)>MAX_MEMBERS:return self.failed('member_count_over_budget',task)
        if (not isinstance(members,list) or not members
                or any(not isinstance(k,str) for k in members) or len(set(members))!=len(members)):
            return self.failed('manifest_unproven',task)
        metadata=[]
        for member in members:
            kind,sep,identity=member.partition(':')
            if not sep or not identity.isdigit() or int(identity)<1 or kind not in ('archive','item'):
                return self.failed('source_kind_unproven',task)
            identity=int(identity)
            if kind=='archive':
                row=self.conn.execute('SELECT a.id,a.payload_sha256,a.state,h.content_hash,'
                    'length(CAST(h.messages AS BLOB)) AS messages_bytes,length(CAST(h.body AS BLOB)) AS body_bytes '
                    'FROM session_archives a LEFT JOIN conversation_history h ON h.archive_id=a.id WHERE a.id=?',(identity,)).fetchone()
                if row is None or row['state']!='available' or not row['messages_bytes'] or not row['body_bytes']:
                    return self.failed('structured_history_missing',task)
                info=dict(row);total+=info['messages_bytes']+info['body_bytes']
            else:
                item=self.conn.execute('SELECT id,content_type,CASE WHEN experience_payload IS NULL OR experience_payload=\'\' THEN 0 ELSE 1 END AS managed_payload '
                    'FROM context_items WHERE id=?',(identity,)).fetchone()
                layers=[dict(r) for r in self.conn.execute('SELECT layer,content_hash,length(CAST(content AS BLOB)) AS bytes FROM context_layers WHERE item_id=? ORDER BY layer',(identity,))]
                if item is None or not layers:return self.failed('item_missing',task)
                info={'item':dict(item),'layers':layers};total+=sum(r['bytes'] or 0 for r in layers)
            metadata.append({'key':member,'kind':kind,'id':identity,'identity':info})
            if total>MAX_SOURCE_BYTES:return self.failed('over_budget',task)
        return {'ready':True,'reason':'','task':task,'members':metadata,'budget_bytes':total}

    def read(self, task_id):
        gate=self.preflight(task_id)
        if not gate['ready']:return gate
        try:policy=self._policy()
        except (ValueError,OSError):return self.failed('policy_unproven',gate['task'])
        token=self.token()
        cache_ok = not self.conn.in_transaction or self._scope_active
        if not cache_ok or token!=self.epoch:
            self.cache.clear();self.epoch=token
        key=_hash([gate,policy['revision'],token])
        if cache_ok and key in self.cache:return self.cache[key]
        spans=[];cursor=0;digest=hashlib.sha256();message_count=0
        for member in gate['members']:
            if member['kind']=='archive':
                row=self.conn.execute('SELECT messages,body,content_hash FROM conversation_history WHERE archive_id=? '
                    'AND content_hash=? AND length(CAST(messages AS BLOB))=? AND length(CAST(body AS BLOB))=?',
                    (member['id'],member['identity']['content_hash'],member['identity']['messages_bytes'],member['identity']['body_bytes'])).fetchone()
                if row is None:return self.failed('source_changed_during_read',gate['task'])
                try:
                    raw=row['messages'];messages=json.loads(raw)
                    if isinstance(messages,list):
                        message_count+=len(messages)
                        if message_count>MAX_MESSAGES:return self.failed('message_count_over_budget',gate['task'])
                    if (not isinstance(messages,list) or any(not isinstance(m,dict) or not isinstance(m.get('role'),str)
                            or not isinstance(m.get('content'),str) for m in messages)
                            or hashlib.sha256(raw.encode()).hexdigest()!=row['content_hash']
                            or row['content_hash']!=member['identity']['content_hash']
                            or render(messages)!=row['body']):
                        return self.failed('structured_history_mismatch',gate['task'])
                except (TypeError,ValueError,KeyError):return self.failed('structured_history_mismatch',gate['task'])
            else:
                layers={}
                for layer in member['identity']['layers']:
                    row=self.conn.execute('SELECT content,content_hash FROM context_layers WHERE item_id=? AND layer=? '
                        'AND content_hash=? AND length(CAST(content AS BLOB))=?',
                        (member['id'],layer['layer'],layer['content_hash'],layer['bytes'])).fetchone()
                    if not row or row['content_hash']!=layer['content_hash'] or hashlib.sha256(row['content'].encode()).hexdigest()!=row['content_hash']:
                        return self.failed('item_content_mismatch',gate['task'])
                    layers[layer['layer']]=row['content']
                item=member['identity']['item'];managed=item['content_type'] in ('workstream_checkpoint','project_summary') or item['managed_payload']
                text=(layers.get('l1') or layers.get('l0','')) if managed else (layers.get('l2') or layers.get('l1') or layers.get('l0',''))
                messages=[{'role':'unknown','content':text}]
                message_count+=1
                if message_count>MAX_MESSAGES:return self.failed('message_count_over_budget',gate['task'])
            safe,_=redact_messages(clean_messages(messages,policy=policy))
            if len(spans)+len(safe)>MAX_SPANS:return self.failed('span_count_over_budget',gate['task'])
            text=render(safe)
            if not text:return self.failed('empty_cleaned_source',gate['task'])
            if cursor:
                separator=self.conn.execute('SELECT substr(source_snapshot,?,2) FROM organization_tasks WHERE id=?',(cursor+1,task_id)).fetchone()[0]
                if separator!='\n\n':return self.failed('snapshot_mismatch',gate['task'])
                cursor+=2;digest.update(b'\n\n')
            if not self.conn.execute('SELECT substr(source_snapshot,?,?)=? FROM organization_tasks WHERE id=?',(cursor+1,len(text),text,task_id)).fetchone()[0]:
                return self.failed('snapshot_mismatch',gate['task'])
            spans.extend({'start':cursor+s['content_start'],'end':cursor+s['content_end'],'role':s['role']} for s in message_spans(safe))
            cursor+=len(text);digest.update(text.encode())
        if cursor!=gate['task']['snapshot_chars']:return self.failed('snapshot_mismatch',gate['task'])
        if token!=self.token() or policy['revision']!=self._policy()['revision'] or gate!=self.preflight(task_id):
            return self.failed('source_changed_during_read',gate['task'])
        result={'ready':True,'reason':'','task':gate['task'],'spans':spans,'metadata':{
            'version':'stored-structural-roles:v1',
            'source_identity_hash':_hash([gate['task']['id'],gate['task']['source_key'],
                gate['task']['source_revision'],gate['members']]),'snapshot_sha256':digest.hexdigest()}}
        if cache_ok and len(spans)<=MAX_CACHED_SPANS:
            if len(self.cache)>=8 or sum(len(x['spans']) for x in self.cache.values())+len(spans)>MAX_CACHED_SPANS:self.cache.clear()
            self.cache[key]=result
        return result


def project_roles(ranges, spans):
    roles=[];offset=0
    for a,b in ranges:
        for span in spans:
            start,end=max(a,span['start']),min(b,span['end'])
            if end>start:roles.append({'start':offset+start-a,'end':offset+end-a,'role':span['role']})
        offset+=b-a+2
    return roles
