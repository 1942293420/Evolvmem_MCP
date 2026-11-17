"""Project-scoped native retrieval inputs; transaction-local batching only."""
from dataclasses import fields
import hashlib
import json
import re
from datetime import datetime,timezone

from evolvmem.context_models import (ContextItem, ContextContentType, ContextScope,
    ContextStatus, ContextTier, ContextLayer, ContextRetrievalRecord, ContextSearchHit,
    ContextReadRequest, ContextReadResult)
from evolvmem.context_retrieval_inputs import _weight
from evolvmem.context_temporal import temporal_rank,window_contains
from evolvmem.library_foundation import FoundationError
from .eligibility_read import PgEligibilityRead

MAX_ROWS=512
MAX_BYTES=8*1024*1024


def _require(ok,code):
    if not ok:raise FoundationError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def lexical_terms(query):
    """Same word/bigram population as the immutable SQL index function."""
    _require(isinstance(query,str) and 0<len(query.strip())<=4000,'invalid_recall_query')
    values=set()
    for word in re.findall(r'[\w一-鿿]+',query.lower()):
        values.add(word)
        if re.search('[一-鿿]',word):
            values.update(word[i:i+2] for i in range(len(word)-1))
    result=tuple(sorted(values,key=lambda x:(-len(x),x))[:40])
    _require(bool(result),'invalid_recall_query')
    return result


def experience_candidate_filter(experience):
    """One fixed pre-budget predicate shared by lexical and ANN context lanes."""
    if experience is None:return '',()
    _require(isinstance(experience,dict) and isinstance(experience.get('project'),str)
        and isinstance(experience.get('constraints'),dict) and len(experience['constraints'])<=20
        and all(isinstance(k,str) and isinstance(v,str) for k,v in experience['constraints'].items()),
        'invalid_experience_candidates')
    constraints=json.dumps(experience['constraints'],ensure_ascii=False,separators=(',',':'),sort_keys=True)
    # Type-aware JSON comparison matches canonical string conditions. Guard the
    # set-returning function itself: optimizer predicate order is not a gate.
    predicate=''' AND c.status='active'
        AND jsonb_typeof(c.experience_payload)='object' AND c.experience_payload<>'{}'::jsonb
        AND jsonb_typeof(c.experience_payload->'conditions')='object'
        AND (c.project=%s OR c.experience_payload->'transferable'='true'::jsonb)
        AND NOT EXISTS (SELECT 1 FROM jsonb_each(CASE
          WHEN jsonb_typeof(c.experience_payload->'conditions')='object'
          THEN c.experience_payload->'conditions' ELSE '{}'::jsonb END) AS evidence_condition(key,value)
          WHERE %s::jsonb ? evidence_condition.key
           AND (%s::jsonb->evidence_condition.key) IS DISTINCT FROM evidence_condition.value)'''
    return predicate,(experience['project'],constraints,constraints)


class PgNativeRecallStore:
    def __init__(self,session,project_scope):
        project_scope.require(session)
        _require(session.route.action=='read','read_session_required')
        self.session,self.scope=session,project_scope
        self.lib,self.project=project_scope.library_id,project_scope.project_id
        self.eligibility=PgEligibilityRead(session,project_scope)
        self.rows={};self.layers={};self.families={};self.allowed=set()
        self.origins={};self.lexical_proof=[];self._remaining=MAX_BYTES
        self.layer_proof=[]

    def _retain(self,value):
        size=_weight(value,self._remaining)
        _require(size is not None,'recall_dependency_limit')
        self._remaining-=size

    def lexical_candidates(self,query,limit=240,*,content_types=(),moment=None,min_confidence=0,include_reference=True,experience=None):
        _require(type(limit) is int and 1<=limit<=240,'invalid_recall_budget')
        moment=moment or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        terms=lexical_terms(query)
        # Only generated alphanumeric terms enter tsquery syntax; the complete
        # expression remains a bound SQL parameter, never a SQL fragment.
        expression=' | '.join("'"+t.replace("'","''")+"'" for t in terms)
        experience_sql,experience_params=experience_candidate_filter(experience)
        rows=self.session.execute('''WITH q AS (SELECT to_tsquery('simple',%s) AS query)
          SELECT l.item_id,l.layer,ts_rank_cd(
           to_tsvector('simple',em_content.recall_lexical_text(l.content)),q.query),
           c.content_type,c.project,c.status
          FROM em_content.context_layers l JOIN em_content.context_items c
           ON c.library_id=l.library_id AND c.id=l.item_id AND c.project_id IS NOT DISTINCT FROM l.project_id
          CROSS JOIN q WHERE l.library_id=%s AND l.project_id IS NOT DISTINCT FROM %s
           AND l.layer IN ('l0','l1') AND c.status IN ('active','superseded')
           AND (%s::text[]='{}'::text[] OR c.content_type=ANY(%s::text[]))
           AND c.confidence >= %s
           AND (%s OR c.tier <> 'reference')
           AND (c.expires_at IS NULL OR c.expires_at > %s)
           AND (c.effective_from IS NULL OR c.effective_from <= %s)
           AND (c.effective_until IS NULL OR c.effective_until > %s)
           AND (c.status='active' OR (c.content_type='decision'
                AND (c.effective_from IS NOT NULL OR c.occurred_at IS NOT NULL)))
           AND to_tsvector('simple',em_content.recall_lexical_text(l.content)) @@ q.query'''+experience_sql+'''
          ORDER BY 3 DESC,l.item_id,l.layer LIMIT %s''',
          (expression,self.lib,self.project,list(content_types),list(content_types),
           min_confidence,include_reference,moment,moment,moment,*experience_params,limit))
        self._retain(rows);self.lexical_proof=rows
        return tuple(ContextSearchHit(int(i),float(score),(ContextLayer(layer),),
            ContextContentType(kind),project,ContextStatus(status))
            for i,layer,score,kind,project,status in rows)

    def _fetch_items(self,ids):
        missing=tuple(i for i in dict.fromkeys(ids) if i not in self.rows)
        _require(all(type(i) is int and i>0 for i in missing)
                 and len(set(missing)|self.rows.keys())<=MAX_ROWS,'recall_dependency_limit')
        if not missing:return
        marks=','.join('%s' for _ in missing)
        rows=self.session.execute('SELECT to_jsonb(c) FROM em_content.context_items c '
            f'WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id IN ({marks}) ORDER BY id',
            (self.lib,self.project,*missing))
        for (row,) in rows:
            _require(row['library_id']==self.lib and row['project_id']==self.project,'recall_scope_changed')
            self._retain(row);self.rows[row['id']]=row

    def prepare(self,ids):
        ids=tuple(dict.fromkeys(ids))
        _require(len(ids)<=240,'recall_candidate_limit')
        self._fetch_items(ids)
        keys=tuple(dict.fromkeys((self.rows[i]['identity_key'],self.rows[i]['project'],self.rows[i]['scope'])
             for i in ids if i in self.rows))
        new_keys=tuple(key for key in keys if key not in self.families)
        if new_keys:
            values=','.join('(%s,%s,%s)' for _ in new_keys)
            rows=self.session.execute('WITH wanted(identity_key,project,scope) AS (VALUES '+values+') '
                'SELECT c.identity_key,c.project,c.scope,c.id FROM em_content.context_items c '
                'JOIN wanted w USING(identity_key,project,scope) '
                'WHERE c.library_id=%s AND c.project_id IS NOT DISTINCT FROM %s ORDER BY c.id LIMIT 513',
                (*[v for key in new_keys for v in key],self.lib,self.project))
            _require(len(rows)<=MAX_ROWS,'recall_dependency_limit')
            self._retain(rows)
            for key in new_keys:self.families[key]=tuple(r[3] for r in rows if tuple(r[:3])==key)
            self._fetch_items(r[3] for r in rows)
        successors=tuple(r['superseded_by'] for r in self.rows.values() if r['superseded_by'] is not None)
        self._fetch_items(successors)
        missing=tuple(i for i in self.rows if i not in self.layers)
        if missing:
            marks=','.join('%s' for _ in missing)
            layers=self.session.execute('SELECT item_id,layer,CASE WHEN layer=\'l0\' THEN content ELSE NULL END,'
                'content_hash,generator,octet_length(content) FROM em_content.context_layers '
                f'WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id IN ({marks}) '
                'ORDER BY item_id,layer',(self.lib,self.project,*missing))
            self._retain(layers)
            for i in missing:
                found=tuple(tuple(r[1:]) for r in layers if r[0]==i)
                _require(tuple(r[0] for r in found)==('l0','l1','l2'),'invalid_recall_layers')
                _require(isinstance(found[0][1],str) and hashlib.sha256(found[0][1].encode()).hexdigest()==found[0][2],
                         'recall_content_hash_mismatch')
                self.layers[i]=found
        # Qualification is one batched set per snapshot; family resolution and
        # canonical dedup reuse it instead of rescanning every source per item.
        for start in range(0,len(missing),240):
            self.allowed.update(self.eligibility.eligible_ids(missing[start:start+240]))

    def _item(self,row):
        excluded={'layers','content_type','scope','status','tier','tags','projects'}
        args={f.name:row[f.name] for f in fields(ContextItem) if f.name not in excluded}
        return ContextItem(**args,content_type=ContextContentType(row['content_type']),
            scope=ContextScope(row['scope']),status=ContextStatus(row['status']),tier=ContextTier(row['tier']),
            tags=tuple(row['tags']),projects=tuple(row['projects']),layers=None)

    def get_retrieval_records(self,ids):
        _require(all(i in self.layers for i in ids if i in self.rows),'recall_inputs_not_prepared')
        return tuple(ContextRetrievalRecord(self._item(self.rows[i]),self.layers[i][0][1],
            tuple(ContextLayer(r[0]) for r in self.layers[i])) for i in dict.fromkeys(ids) if i in self.rows)

    def get_by_identity(self,key,*,project,scope,include_layers=False):
        family=(key,project,scope.value)
        _require(family in self.families,'recall_family_not_prepared')
        return tuple(self._item(self.rows[i]) for i in self.families[family])

    def successor_ranks(self,ids):
        return {i:temporal_rank(self.rows[n]['effective_from'],self.rows[n]['occurred_at'])
            for i in ids if i in self.rows and (n:=self.rows[i]['superseded_by']) in self.rows}

    def retrieval_fingerprints(self,ids):
        return {i:(tuple((json.dumps(self.rows[i]['experience_payload'] or {},sort_keys=True,ensure_ascii=False),
              *(self.rows[i][k] for k in ('effective_from','effective_until','occurred_at','expires_at')),r[0],r[2])
              for r in self.layers[i]),None) for i in ids if i in self.layers}

    def eligible(self,item_id):
        return item_id in self.allowed

    def read(self,request):
        if not isinstance(request,ContextReadRequest):raise TypeError('ContextReadRequest required')
        self.prepare((request.id,));row=self.rows.get(request.id)
        code='not_found' if row is None else 'not_readable' if (row['status']!='active' or not self.eligible(request.id)) else (
            'expired' if row['expires_at'] and row['expires_at']<=self.eligibility.now else None)
        if row and not window_contains(row['effective_from'],row['effective_until'],self.eligibility.now):code='not_readable'
        if code:return ContextReadResult(request.id,request.layer,'',code)
        # Retrieval retains only L0 text. A point read verifies every original
        # layer against the prepared hashes before disclosing the requested one.
        layers=self.session.execute('SELECT layer,content,content_hash,generator,octet_length(content) '
            'FROM em_content.context_layers WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s '
            'AND item_id=%s ORDER BY layer',(self.lib,self.project,request.id))
        self._retain(layers);self.layer_proof=layers
        _require(tuple(r[0] for r in layers)==('l0','l1','l2'),'invalid_recall_layers')
        for layer,original in zip(layers,self.layers[request.id]):
            name,content,content_hash,generator,size=layer
            _require(isinstance(content,str) and hashlib.sha256(content.encode()).hexdigest()==content_hash,
                'recall_content_hash_mismatch')
            _require((name,content_hash,generator,size)==(original[0],original[2],original[3],original[4]),
                'recall_content_hash_mismatch')
        content=next(r[1] for r in layers if r[0]==request.layer.value)
        return ContextReadResult(request.id,request.layer,content)

    def legacy_projection(self,ids):
        ids=tuple(dict.fromkeys(ids))
        if not ids:return {}
        marks=','.join('%s' for _ in ids)
        rows=self.session.execute('SELECT m.context_item_id,to_jsonb(l) FROM em_content.legacy_memory_migrations m '
            'JOIN em_content.memories l ON l.library_id=m.library_id AND l.id=m.legacy_memory_id '
            'AND l.project_id IS NOT DISTINCT FROM m.project_id '
            f'WHERE m.library_id=%s AND m.project_id IS NOT DISTINCT FROM %s AND m.context_item_id IN ({marks}) '
            "AND l.status='active' ORDER BY m.context_item_id,l.id",(self.lib,self.project,*ids))
        self._retain(rows)
        for item,row in rows:
            _require(item not in self.origins,'ambiguous_legacy_mapping')
            self.origins[item]=row
        return dict(self.origins)

    def proof(self):
        return _digest((self.rows,self.layers,sorted(self.families.items()),self.origins,
                        sorted(self.allowed),self.eligibility.proof(),self.layer_proof))
