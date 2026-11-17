"""Current native source qualification, owned by one authenticated RR snapshot.

This is neither a history promotion nor an authority lease. The caller retains
the existing lease, independently repeats this read in its final snapshot, and
compares dependency proofs and selected IDs before publishing any content.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading

from evolvmem import source_qualification as verdict
from evolvmem.context_models import ContextContentType
from evolvmem.context_retrieval_inputs import _weight
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope
from .native_qualification_sql import READ_CONTEXT, BATCH, SUMMARY_CONTENT

MAX_IDS = 240
MAX_DEPENDENCIES = 512
MAX_BYTES = 8 * 1024 * 1024
_SECTIONS = ('learning','decision','resolution','registry','sources','derivations','evidence','rollup')
_SINGLE = ('learning','decision','resolution','registry','rollup')


def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',', ':'),allow_nan=False)


def _ids(values, *, maximum=MAX_IDS):
    if (not isinstance(values,(list,tuple,set,frozenset)) or len(values)>maximum
            or any(type(i) is not int or i<1 for i in values)):
        raise FoundationError('invalid_qualification_ids')
    return tuple(sorted(set(values)))


class NativeEligibilityRead:
    def __init__(self, session, project_scope, *, clock=None):
        if not isinstance(session,LibrarySession) or session._closed or session.route.action!='read':
            raise FoundationError('read_session_required')
        if not isinstance(project_scope,ProjectScope):
            raise FoundationError('project_scope_required')
        project_scope.require(session)
        self.session,self.project_scope=session,project_scope
        self._thread=threading.get_ident()
        self._connection=session._connection
        self.library_id=session.route.library.library_id
        self.project_id=project_scope.project_id
        self.rows,self.content={},{}
        self._remaining=MAX_BYTES
        self._clock=clock or (lambda:datetime.now(timezone.utc))
        moment=self._clock()
        self.now=(moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment).strftime('%Y-%m-%d %H:%M:%S')
        result=session.execute(READ_CONTEXT,(self.library_id,self.project_id))
        expected=(1,'native-v1',project_scope.policy_epoch,project_scope.route_epoch)
        if (len(result)!=1 or len(result[0])!=5 or tuple(result[0][:4])!=expected
                or type(result[0][4]) is not int or result[0][4]<1):
            raise FoundationError('recall_domain_not_ready')
        self.readiness=tuple(result[0])

    def _live(self):
        self.project_scope.require(self.session)
        if threading.get_ident()!=self._thread or self.session._connection is not self._connection:
            raise FoundationError('qualification_snapshot_changed')

    def _retain(self,value):
        size=_weight(value,self._remaining)
        if size is None:raise FoundationError('recall_dependency_limit')
        self._remaining-=size

    def _load(self, values):
        self._live()
        ids=_ids(values,maximum=MAX_DEPENDENCIES)
        if len(set(ids)|self.rows.keys())>MAX_DEPENDENCIES:
            raise FoundationError('recall_dependency_limit')
        missing=tuple(i for i in ids if i not in self.rows)
        if not missing:return
        if len(missing)>MAX_IDS:
            for offset in range(0,len(missing),MAX_IDS):
                self._load(missing[offset:offset+MAX_IDS])
            return
        rows=self.session.execute(BATCH,(self.library_id,self.project_id,list(missing)))
        if len(rows)>len(missing):raise FoundationError('invalid_qualification_rows')
        pending={i:None for i in missing}
        seen=set()
        for record in rows:
            if (not isinstance(record,(tuple,list)) or len(record)!=2 or type(record[0]) is not int
                    or record[0] not in pending or record[0] in seen):
                raise FoundationError('invalid_qualification_rows')
            ident,payload=record
            if payload is None:raise FoundationError('recall_dependency_limit')
            if not isinstance(payload,dict) or set(payload)!=set(_SECTIONS)|{'item','item_l1_hash'}:
                raise FoundationError('invalid_qualification_shape')
            item=payload['item']
            if (not isinstance(item,dict) or item.get('id')!=ident or item.get('library_id')!=self.library_id
                    or item.get('project_id')!=self.project_id):
                raise FoundationError('qualification_scope_mismatch')
            for name in _SECTIONS:
                entries=payload[name]
                if not isinstance(entries,list) or any(not isinstance(e,dict) for e in entries):
                    raise FoundationError('invalid_qualification_shape')
                if len(entries)>(1 if name in _SINGLE else 64):
                    raise FoundationError('recall_dependency_limit')
                for entry in entries:
                    if entry.get('library_id')!=self.library_id or entry.get('project_id')!=self.project_id:
                        raise FoundationError('qualification_scope_mismatch')
                # Stable proof ordering, independent of aggregate/driver order.
                entries.sort(key=_json)
            self._retain(payload)
            pending[ident]=deepcopy(payload)
            seen.add(ident)
        self.rows.update(pending)  # includes negative results, only this snapshot

    @staticmethod
    def _one(row,name):
        values=row[name]
        return values[0] if values else None

    def _source_live(self,source):
        state=source['qualification']
        if state is None:
            # Ordinary current writer origin. This is not a success/experience
            # verification and cannot independently validate a human decision.
            return (source['source_kind']=='migration' and source['archive_id'] is None
                    and source['extraction_version']=='legacy-write.v1' and bool(source['source_ref']))
        if (state.get('library_id')!=self.library_id or state.get('project_id')!=self.project_id
                or state.get('source_id')!=source['id'] or state.get('item_id')!=source['item_id']):
            raise FoundationError('qualification_scope_mismatch')
        return (state['state']=='available' and state['verification']=='verified'
                and state['revision']>0 and state['source_revision']>0
                and all(state[key]==source[key] for key in ('source_kind','source_ref','extraction_version','archive_id')))

    def _live_sources(self,row):
        return [s for s in row['sources'] if self._source_live(s)]

    def _project_current(self,row):
        item=row['item']
        # Text applicability is never a security grant. The registered UUID is
        # fixed by resolve_project_scope and each query also has its predicate.
        if self.project_id is None:
            return not item['project'] and not item['projects']
        registry=self._one(row,'registry')
        if (item['project']!=self.project_scope.project_key or registry is None
                or registry['project_uuid']!=self.project_id or registry['status']!='active'):
            return False
        resolution=self._one(row,'resolution')
        if resolution is not None:
            if resolution['review_state'] in ('rejected','withdrawn','pending'):
                return False
            if resolution['resolved_project'] and resolution['resolved_project']!=item['project']:
                return False
        return True

    def _decision(self,row):
        decision=self._one(row,'decision')
        if decision and decision['decision']=='accepted' and (
                decision['item_revision']!=row['item']['revision'] or row['item_l1_hash'] is None
                or decision['item_content_hash']!=row['item_l1_hash']):
            return None
        return decision['decision'] if decision else None

    def _unconfirmed(self,row):
        learning=self._one(row,'learning')
        if learning is None:return False
        return verdict.unconfirmed_rule(learning['category'],learning['payload'],
            lambda:self._decision(row)=='accepted',
            lambda:any(s['source_kind']=='manual' for s in self._live_sources(row)))

    def currently_backed(self,item_id):
        self._load((item_id,))
        row=self.rows[item_id]
        if row is None or not self._project_current(row) or self._decision(row) in ('rejected','withdrawn'):
            return False
        sources=self._live_sources(row)
        if row['item']['source_count']!=len(row['sources']):
            raise FoundationError('qualification_source_count_mismatch')
        if row['sources'] and not sources:return False
        usable={s['id']:s for s in sources}
        derivations=[]
        archives={}
        for stored in row['derivations']:
            d=dict(stored)
            if d['basis_source_id'] is not None:
                b=d['basis_source']
                descriptor=d['basis_descriptor']
                if (b is None or b['state']!='available' or b['verification']!='verified'
                        or descriptor is None or not self._source_live(dict(descriptor,qualification=b))
                        or b['source_revision']!=d['basis_source_revision'] or b['content_hash']!=d['basis_content_hash']):
                    raise FoundationError('basis_reconciliation_required')
            source=usable.get(d['source_id'])
            source_state=source['qualification'] if source else None
            if (source is None or source_state is None
                    or source_state['source_revision']!=d['source_revision']):
                d['status']='superseded'
            d['item_scope']=row['item']['scope']
            d['item_project']=row['item']['project']
            d['item_projects']=json.dumps(row['item']['projects'],ensure_ascii=False)
            d['projects']=json.dumps(d['projects'],ensure_ascii=False)
            archives[d['source_key']]=set(d['conversation_archives'])
            derivations.append(d)
        # Native IDs make the dependency exact even when a source has no archive
        # address. A withdrawn derivation cannot redeem itself via that same
        # physical source; genuinely independent current sources still can.
        derived_source_ids={d['source_id'] for d in derivations}
        return verdict.currently_backed(derivations,lambda key:archives.get(key,set()),
            lambda:[s for s in sources if s['id'] not in derived_source_ids])

    def _experience_current(self,row):
        from .native_experience import experience_current
        return experience_current(row,self._live_sources(row))

    def cases(self,item_ids):
        """Current qualified payloads and evidence; never a cached old verdict."""
        from .native_experience import case_from_row
        from evolvmem.context_temporal import window_contains
        ids=_ids(item_ids)
        allowed=self.eligible_ids(ids)
        result=[]
        for ident in ids:
            if ident not in allowed:continue
            row=self.rows[ident]
            if row['item']['content_type'] not in ('experience','playbook'):continue
            item=row['item']
            if (item['status']!='active' or item['expires_at'] and item['expires_at']<=self.now
                    or not window_contains(item['effective_from'],item['effective_until'],self.now)):
                continue
            if not row['item']['experience_payload'] or not self._experience_current(row):continue
            result.append(case_from_row(row,self._live_sources(row)))
        return tuple(result)

    def _summary_content(self,ids):
        missing=tuple(i for i in ids if i not in self.content)
        if missing:
            rows=self.session.execute(SUMMARY_CONTENT,(self.library_id,self.project_id,list(missing)))
            pending={i:None for i in missing}
            seen=set()
            for r in rows:
                if (len(r)!=5 or r[0] not in pending or r[0] in seen or not isinstance(r[3],str)
                        or hashlib.sha256(r[3].encode('utf-8')).hexdigest()!=r[4]):
                    raise FoundationError('recall_content_hash_mismatch')
                self._retain(r)
                pending[r[0]]=tuple(r)
                seen.add(r[0])
            self.content.update(pending)
        if any(self.content[i] is None for i in ids):return ''
        return verdict.rollup_content_signature(tuple(self.content[i][:4] for i in ids))

    def _summary_current(self,row):
        ids=tuple(sorted({int(s['source_ref']) for s in row['sources']
            if s['source_kind']=='context_reference' and s['source_ref'].isdigit()}))
        if not ids:return True  # canonical hand-authored no-derived-link rule
        _ids(ids)
        self._load(ids)
        source_rows=[self.rows[i]['item'] for i in ids if self.rows[i] is not None]
        rollup=self._one(row,'rollup')
        if rollup is not None and rollup['status']!='active':return False
        return verdict.summary_current(row['item'],ids,lambda:source_rows,now=self.now,
            backed=self.currently_backed,
            unconfirmed=lambda i:self._unconfirmed(self.rows[i]),
            signature=lambda:(rollup['source_set_hash'] if rollup else ''),
            content_signature=lambda:self._summary_content(ids))

    def eligible(self,item_id):
        self._load((item_id,))
        row=self.rows[item_id]
        if row is None or not self.currently_backed(item_id):return False
        item=row['item']
        if item['content_type'] not in {kind.value for kind in ContextContentType}:
            raise FoundationError('unsupported_context_shape')
        learning=self._one(row,'learning')
        projected=dict(item,category=learning['category'] if learning else None,
            learning_payload=learning['payload'] if learning else None)
        if (item['content_type']=='experience' or projected['category']=='experience' or item['experience_payload']) and not self._experience_current(row):
            return False
        return verdict.eligible_row(projected,lambda *_:self._unconfirmed(row),lambda:self._summary_current(row))

    def eligible_ids(self,item_ids):
        ids=_ids(item_ids)
        self._load(ids)
        # Batch direct summary dependencies too; recursively linked summaries
        # are only source-current checks, matching canonical rollup behavior.
        linked=set()
        for ident in ids:
            row=self.rows[ident]
            if row and row['item']['content_type']=='project_summary':
                linked.update(int(s['source_ref']) for s in row['sources']
                    if s['source_kind']=='context_reference' and s['source_ref'].isdigit())
        self._load(tuple(sorted(linked)))
        return frozenset(ident for ident in ids if self.eligible(ident))

    def proof(self):
        self._live()
        dependencies=(self.library_id,self.project_id,self.readiness,self.rows,self.content)
        return hashlib.sha256(_json(dependencies).encode('utf-8')).hexdigest()
