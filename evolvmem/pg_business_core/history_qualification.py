"""Original historical verdicts inside one real authorized read snapshot.

No source kind, role, actor, or revision is invented. Raw legacy experience
is preserved but not a verified method without source event/version proof.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import threading
from evolvmem import source_qualification as verdict
from evolvmem.context_models import ContextContentType
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from .native_qualification import NativeEligibilityRead,_ids,_json,MAX_IDS,MAX_DEPENDENCIES,MAX_BYTES
from .history_qualification_sql import READ_CONTEXT,FIRST_PAGE,SOURCE_PAGE
from .history_source_pages import assemble_sources

SECTIONS=('memberships','layers','learning','resolution','registry','sources','derivations',
          'basis','conversations','evidence','rollup','knowledge')


def decoded(value,default=None):
    if isinstance(value,str):
        try:return json.loads(value)
        except (TypeError,ValueError):return default
    return value if value is not None else default


class HistoryEligibilityRead(NativeEligibilityRead):
    """Shared snapshot/budget/proof mechanics; independent historical gate."""
    def __init__(self,session,project_scope,*,clock=None):
        if not isinstance(session,LibrarySession) or session._closed or session.route.action!='read':
            raise FoundationError('read_session_required')
        if not isinstance(project_scope,ProjectScope):raise FoundationError('project_scope_required')
        project_scope.require(session)
        self.session,self.project_scope=session,project_scope
        self._thread=threading.get_ident();self._connection=session._connection
        self.library_id=session.route.library.library_id;self.project_id=project_scope.project_id
        self.rows,self.content={},{};self._remaining=MAX_BYTES
        self._clock=clock or (lambda:datetime.now(timezone.utc))
        moment=self._clock()
        self.now=(moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment).strftime('%Y-%m-%d %H:%M:%S')
        result=session.execute(READ_CONTEXT,(self.library_id,self.project_id))
        expected=(1,'history-preserved-v1',project_scope.policy_epoch,project_scope.route_epoch)
        if (len(result)!=1 or len(result[0])!=7 or tuple(result[0][:4])!=expected
                or any(type(v) is not int or v<1 for v in result[0][4:6])
                or not isinstance(result[0][6],str) or len(result[0][6])!=64):
            raise FoundationError('recall_domain_not_ready')
        self.readiness=tuple(result[0])

    def _load(self,values):
        self._live();ids=_ids(values,maximum=MAX_DEPENDENCIES)
        if len(set(ids)|self.rows.keys())>MAX_DEPENDENCIES:raise FoundationError('recall_dependency_limit')
        missing=tuple(i for i in ids if i not in self.rows)
        if not missing:return
        if len(missing)>MAX_IDS:
            for start in range(0,len(missing),MAX_IDS):self._load(missing[start:start+MAX_IDS])
            return
        rows=self.session.execute(FIRST_PAGE,(self.library_id,self.project_id,list(missing)))
        if len(rows)>len(missing):raise FoundationError('invalid_qualification_rows')
        pending={i:None for i in missing};seen=set()
        for record in rows:
            if (not isinstance(record,(tuple,list)) or len(record)!=4
                    or type(record[0]) is not int or record[0] not in pending or record[0] in seen):
                raise FoundationError('invalid_qualification_rows')
            ident,payload,source_after,source_more=record
            if payload is None:raise FoundationError('recall_dependency_limit')
            if not isinstance(payload,dict) or set(payload)!=set(SECTIONS)|{'item','item_l1_hash'}:
                raise FoundationError('invalid_qualification_shape')
            item=payload['item']
            if (not isinstance(item,dict) or item.get('id')!=ident or item.get('library_id')!=self.library_id
                    or item.get('project_id') is not None):raise FoundationError('qualification_scope_mismatch')
            for section in SECTIONS:
                entries=payload[section]
                maximum=3 if section=='layers' else 1 if section in ('learning','resolution','rollup') else 64
                if not isinstance(entries,list) or len(entries)>maximum:raise FoundationError('recall_dependency_limit')
                if any(not isinstance(e,dict) for e in entries):raise FoundationError('invalid_qualification_shape')
                for entry in entries:
                    if 'library_id' in entry and entry['library_id']!=self.library_id:
                        raise FoundationError('qualification_scope_mismatch')
                # Preserve the original source id order.  Every other section
                # keeps its established canonical proof order.
                if section!='sources':entries.sort(key=_json)
            # Charge the metadata page before any continuation; each source
            # page is charged exactly once by the callback.  The assembled
            # payload is not charged a second time.
            self._retain(payload)
            def fetch_page(after,ident=ident):
                page=self.session.execute(SOURCE_PAGE,
                    (self.library_id,self.project_id,ident,after))
                self._retain(page)
                return page
            payload=assemble_sources(payload,ident,self.library_id,
                source_after,source_more,fetch_page)
            if not self._project_current(payload):raise FoundationError('qualification_scope_mismatch')
            pending[ident]=deepcopy(payload);seen.add(ident)
        self.rows.update(pending)

    def _project_current(self,row):
        item=row['item'];key=self.project_scope.project_key
        if self.project_id is None:
            # The fixed SQL gate already proved the real session is this
            # library's owner. NULL admits only original unassigned globals;
            # original ownership/source/knowledge verdicts still run below.
            if (key is not None or item.get('scope')!='global' or item.get('project')!=''
                    or item.get('projects')!=[] or row['memberships']):return False
        else:
            memberships=[m for m in row['memberships'] if m.get('library_id')==self.library_id
                and m.get('item_id')==item['id'] and m.get('project_id')==self.project_id
                and m.get('project_key')==key]
            if not memberships or any(m.get('state')!='confirmed' for m in memberships):return False
        # Membership can be confirmed by the original manual knowledge include;
        # no secondary project is invented or rewritten into the primary column.
        resolution=self._one(row,'resolution')
        if resolution:
            if self.project_id is None and resolution.get('resolved_project'):return False
            from evolvmem.project_ownership import classify
            if resolution.get('review_state')=='withdrawn' or classify(resolution.get('resolution_state'),
                    resolution.get('review_state'),resolution.get('decision_source'),resolution.get('method')).excluded:
                return False
        for obj in row['knowledge']:
            decision=decoded(obj.get('decision_json'),{})
            if obj.get('retired') or decision.get('action')!='include':return False
            if self.project_id is None:
                from evolvmem.knowledge_scope import scope,projects
                if scope(decision)!='global' or projects(decision):return False
            elif key not in decision.get('projects',[]):return False
        return True

    def _live_sources(self,row):
        sources=[]
        for source in row['sources']:
            if source.get('exclusion'):continue
            archive=source.get('archive')
            if source.get('archive_id') is not None and (not archive or archive.get('state')!='available'):continue
            sources.append(source)
        return sources

    def currently_backed(self,item_id):
        self._load((item_id,));row=self.rows[item_id]
        if row is None or not self._project_current(row):return False
        if any(d.get('task_exists') is None or d.get('item_exists') is None for d in row['derivations']):
            raise FoundationError('historical_orphan_derivation')
        if any(b.get('stale') for b in row['basis']):raise FoundationError('basis_reconciliation_required')
        sources=self._live_sources(row)
        if row['sources'] and not sources:return False
        def members(key):
            rows=[r for r in row['conversations'] if r['source_key']==key]
            if len(rows)>1:raise FoundationError('ambiguous_conversation')
            return {int(k.split(':')[1]) for k in decoded(rows[0]['members'],[]) if k.startswith('archive:')} if rows else set()
        return verdict.currently_backed(row['derivations'],members,lambda:sources)

    def _unconfirmed(self,row):
        learning=self._one(row,'learning')
        if not learning:return False
        resolution=self._one(row,'resolution') or {}
        return verdict.unconfirmed_rule(learning['category'],learning['payload'],
            lambda:resolution.get('decision_source')=='human' and resolution.get('review_state')=='accepted',
            lambda:any(s['source_kind']=='manual' for s in self._live_sources(row)))

    def _summary_content(self,ids):
        self._load(ids);rows=[]
        for ident in sorted(ids):
            row=self.rows[ident]
            if row is None:return ''
            layers=[l for l in row['layers'] if l['layer']=='l1']
            if len(layers)!=1:return ''
            layer=layers[0];content=layer.get('content')
            if not isinstance(content,str) or hashlib.sha256(content.encode()).hexdigest()!=layer['content_hash']:
                raise FoundationError('recall_content_hash_mismatch')
            rows.append((ident,row['item']['project'],row['item']['content_type'],content))
        return verdict.rollup_content_signature(rows)

    def _summary_current(self,row):
        ids=tuple(sorted({int(s['source_ref']) for s in row['sources']
            if s['source_kind']=='context_reference' and str(s['source_ref']).isdigit()}))
        if not ids:return True
        _ids(ids,maximum=MAX_DEPENDENCIES);self._load(ids)
        rollup=self._one(row,'rollup')
        if rollup is not None and rollup.get('status','active')!='active':return False
        return verdict.summary_current(row['item'],ids,
            lambda:[self.rows[i]['item'] for i in ids if self.rows[i] is not None],now=self.now,
            backed=self.currently_backed,unconfirmed=lambda i:self._unconfirmed(self.rows[i]),
            signature=lambda:(rollup.get('source_set_hash','') if rollup else ''),
            content_signature=lambda:self._summary_content(ids))

    def current_events(self,row):
        from evolvmem.experience_ranking import task_verdicts
        sources={s['id']:s for s in self._live_sources(row)}
        latest={}
        for event in row['evidence']:
            key=event.get('event_key')
            if not key:continue
            prior=latest.get(key)
            if prior is None or (event['revision'],event['id'])>(prior['revision'],prior['id']):latest[key]=event
        events=[]
        for event in sorted(latest.values(),key=lambda e:e['id']):
            if event.get('experience_version')!=1:continue
            key=decoded(event.get('event_key'),[])
            if not isinstance(key,list) or len(key)!=3 or key[0]!=event.get('task_id') or key[2]!=1:continue
            source=sources.get(event.get('source_id'))
            proofs=source.get('event_proofs',[]) if source else []
            if not isinstance(proofs,list) or len(proofs)>64:raise FoundationError('recall_dependency_limit')
            matching=[p for p in proofs if isinstance(p,dict) and p.get('evidence_id')==event['id']]
            if len(matching)>1:raise FoundationError('invalid_qualification_shape')
            proof=matching[0] if matching else None
            valid=bool(source and isinstance(proof,dict) and proof.get('state')=='verified'
                and proof.get('library_id')==self.library_id and proof.get('source_id')==source['id'] and proof.get('item_id')==row['item']['id']
                and proof.get('source_ref')==source['source_ref'] and proof.get('event_ref')==source['source_ref']
                and proof.get('extraction_version')==source['extraction_version']
                and proof.get('task_id')==event['task_id'] and proof.get('role') in ('user','tool')
                and proof.get('evidence_id')==event['id'] and proof.get('evidence_revision')==event['revision']
                and proof.get('experience_version')==event['experience_version']
                and proof.get('evidence_note_hash')==hashlib.sha256(event['note'].encode()).hexdigest()
                and proof.get('item_revision')==row['item']['revision']
                and proof.get('item_content_hash')==row['item_l1_hash']
                and proof.get('archive_id')==source.get('archive_id')
                and isinstance(proof.get('event_hash'),str) and len(proof['event_hash'])==64
                and source['extraction_version']=='experience-v1:'+proof['event_hash'])
            if valid and source.get('archive_id') is not None:
                valid=(proof.get('archive_payload_sha256')==source['archive']['payload_sha256'])
            if valid:
                valid=(source['source_kind'] in ('tool_result','user_confirmation','historical_record')
                    and (source['source_kind']!='tool_result' or proof['role']=='tool')
                    and (source['source_kind']!='user_confirmation' or proof['role']=='user')
                    and (source['source_kind']!='user_confirmation' or event['verification_level']=='user_confirmed')
                    and (source['source_kind']!='tool_result' or event['verification_level']!='user_confirmed'))
            # Missing proof can never redeem a success; the latest failure or
            # withdrawal still suppresses old success rather than reviving it.
            if not valid and event['outcome'] in ('success','confirmed'):continue
            conditions=decoded(event.get('conditions_json'),None)
            if (not isinstance(conditions,dict) or len(conditions)>20
                    or any(not isinstance(k,str) or not isinstance(v,str) for k,v in conditions.items())):
                raise FoundationError('unsupported_experience_conditions')
            payload=decoded(row['item'].get('experience_payload'),{})
            if event['outcome'] in ('success','confirmed','failure','contradicted') and conditions!=payload.get('conditions',{}):continue
            if event['outcome']=='inapplicable' and not conditions:continue
            events.append(dict(event,conditions_json=json.dumps(conditions,ensure_ascii=False)))
        return events

    def _experience_current(self,row):
        from evolvmem.experience_ranking import task_verdicts
        values=task_verdicts(self.current_events(row)).values()
        return 'contradicted' not in values and any(v in ('success','confirmed') for v in values)

    def eligible(self,item_id):
        self._load((item_id,));row=self.rows[item_id]
        if row is None or not self.currently_backed(item_id):return False
        item=row['item'];learning=self._one(row,'learning')
        if item['content_type'] not in {t.value for t in ContextContentType}:raise FoundationError('unsupported_context_shape')
        projected=dict(item,category=learning['category'] if learning else None,
            learning_payload=learning['payload'] if learning else None)
        if (item['content_type']=='experience' or projected['category']=='experience' or item.get('experience_payload')):
            if not self._experience_current(row):return False
        return verdict.eligible_row(projected,lambda *_:self._unconfirmed(row),lambda:self._summary_current(row))

    def cases(self,item_ids):
        from evolvmem.experience_ranking import describe_case,task_verdicts
        from evolvmem.context_temporal import window_contains
        ids=_ids(item_ids);allowed=self.eligible_ids(ids);result=[]
        for ident in ids:
            if ident not in allowed:continue
            row=self.rows[ident];item=row['item'];payload=decoded(item.get('experience_payload'),None)
            if (not payload or item['status']!='active' or item['expires_at'] and item['expires_at']<=self.now
                    or not window_contains(item['effective_from'],item['effective_until'],self.now)):continue
            if not isinstance(payload,dict):raise FoundationError('unsupported_experience_payload')
            events=self.current_events(row);verdicts=task_verdicts(events)
            sources=[dict(id=s['id'],source_kind=s['source_kind'],source_ref=s['source_ref'],
                extraction_version=s['extraction_version'],digest=s['extraction_version'].partition(':')[2])
                for s in self._live_sources(row)]
            result.append(describe_case(deepcopy(payload),item_id=ident,status=item['status'],
                success_count=sum(v in ('success','confirmed') for v in verdicts.values()),
                failure_count=sum(v in ('failure','contradicted') for v in verdicts.values()),
                sources=sources,events=events,updated_at=item['updated_at']))
        return tuple(result)
