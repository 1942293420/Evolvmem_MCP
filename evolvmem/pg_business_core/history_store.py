"""Historical store contracts backed only by the fixed scope-aware functions."""
from datetime import datetime,timezone
import hashlib
import json
from evolvmem.context_models import (ContextSearchHit,ContextContentType,ContextStatus,ContextLayer,
    ContextReadRequest,ContextReadResult,ContextSearchRequest)
from evolvmem.context_temporal import window_contains
from evolvmem.library_foundation import FoundationError
from .history_qualification import HistoryEligibilityRead
from .history_qualification_sql import FAMILY,LEXICAL,LEGACY,LAYER
from .native_store import PgNativeRecallStore,_require,_digest,lexical_terms,experience_candidate_filter


class PgHistoryRecallStore(PgNativeRecallStore):
    retrieval_support='lexical-only'

    def __init__(self,session,project_scope):
        project_scope.require(session)
        _require(session.route.action=='read','read_session_required')
        self.session,self.scope=session,project_scope
        self.lib,self.project=project_scope.library_id,project_scope.project_id
        self.eligibility=HistoryEligibilityRead(session,project_scope)
        self.rows={};self.layers={};self.families={};self.allowed=set()
        self.blocked_families=set();self.family_proof=[]
        self.origins={};self.lexical_proof=[];self.knowledge_proof=[];self.layer_proof=[]
        self._remaining=8*1024*1024

    def lexical_candidates(self,query,limit=240,*,content_types=(),moment=None,min_confidence=0,include_reference=True,experience=None):
        _require(type(limit) is int and 1<=limit<=240,'invalid_recall_budget')
        lexical_terms(query)  # canonical validation; SQL owns token population.
        moment=moment or self.eligibility.now
        # Share native validation and canonical JSON values. The fixed definer
        # applies applicability before LIMIT; a post-budget Python filter can
        # never recover a suitable experience displaced by incompatible rows.
        _,experience_params=experience_candidate_filter(experience)
        experience_project=experience['project'] if experience is not None else None
        experience_constraints=experience_params[1] if experience is not None else None
        rows=self.session.execute(LEXICAL,(self.lib,self.project,query,list(content_types),moment,
            min_confidence,include_reference,limit,experience_project,experience_constraints))
        _require(len(rows)<=limit,'recall_candidate_limit')
        self._retain(rows);self.lexical_proof=rows
        return tuple(ContextSearchHit(int(i),float(score),(ContextLayer(layer),),
            ContextContentType(kind),project,ContextStatus(status)) for i,layer,score,kind,project,status in rows)

    def _fetch_items(self,ids):
        ids=tuple(dict.fromkeys(ids));missing=tuple(i for i in ids if i not in self.rows)
        _require(len(set(ids)|self.rows.keys())<=512,'recall_dependency_limit')
        self.eligibility._load(missing)
        for ident in missing:
            envelope=self.eligibility.rows[ident]
            if envelope is None:continue
            row=envelope['item'];layers=sorted(envelope['layers'],key=lambda l:l['layer'])
            _require(tuple(l['layer'] for l in layers)==('l0','l1','l2'),'invalid_recall_layers')
            for layer in layers:
                content=layer.get('content')
                _require(isinstance(content,str) and hashlib.sha256(content.encode()).hexdigest()==layer['content_hash'],
                    'recall_content_hash_mismatch')
            self._retain((row,layers));self.rows[ident]=row
            self.layers[ident]=tuple((l['layer'],l['content'],l['content_hash'],l['generator'],
                len(l['content'].encode())) for l in layers)

    def prepare(self,ids):
        ids=tuple(dict.fromkeys(ids));_require(len(ids)<=240,'recall_candidate_limit');self._fetch_items(ids)
        keys=tuple(dict.fromkeys((self.rows[i]['identity_key'],self.rows[i]['project'],self.rows[i]['scope'])
            for i in ids if i in self.rows))
        missing=tuple(k for k in keys if k not in self.families)
        if missing:
            rows=self.session.execute(FAMILY,(self.lib,self.project,json.dumps(missing,ensure_ascii=False)))
            _require(len(rows)<=512,'recall_dependency_limit');self._retain(rows)
            grouped={key:[] for key in missing}
            for record in rows:
                _require(isinstance(record,(tuple,list)) and len(record)==5,'invalid_recall_family')
                key=tuple(record[:3]);ident,qualification=record[3:]
                _require(key in grouped,'recall_scope_changed')
                _require((qualification=='denied' and ident is None) or
                    (qualification=='admitted' and type(ident) is int and ident>0),'invalid_recall_family')
                grouped[key].append((ident,qualification))
            admitted=[]
            for key,records in grouped.items():
                _require(bool(records),'recall_family_incomplete')
                denied=[r for r in records if r[1]=='denied']
                if denied:
                    _require(records==[(None,'denied')],'invalid_recall_family')
                    self.blocked_families.add(key);self.families[key]=()
                else:
                    members=tuple(r[0] for r in records)
                    _require(len(members)==len(set(members)),'invalid_recall_family')
                    self.families[key]=members;admitted.extend(members)
            self.family_proof.extend(tuple(r) for r in rows)
            self.allowed.difference_update(i for i in self.rows if self.family_blocked(i))
            self._fetch_items(admitted)
            _require(all(i in self.rows for i in admitted),'recall_family_incomplete')
        # Denied families never cause a foreign successor lookup. Other broken
        # dependencies retain the original fail-closed error behavior.
        successors=tuple(r['superseded_by'] for i,r in self.rows.items()
            if not self.family_blocked(i) and r['superseded_by'] is not None)
        self._fetch_items(successors)
        _require(all(i in self.rows for i in successors),'recall_family_incomplete')
        pending=tuple(i for i in self.rows if i not in self.allowed and not self.family_blocked(i))
        for start in range(0,len(pending),240):self.allowed.update(self.eligibility.eligible_ids(pending[start:start+240]))

    def family_blocked(self,item_id):
        row=self.rows.get(item_id)
        return row is not None and (row['identity_key'],row['project'],row['scope']) in self.blocked_families

    def eligible(self,item_id):
        return not self.family_blocked(item_id) and item_id in self.allowed

    def legacy_projection(self,ids):
        ids=tuple(dict.fromkeys(ids));_require(len(ids)<=240,'recall_candidate_limit')
        if not ids:return {}
        rows=self.session.execute(LEGACY,(self.lib,self.project,list(ids)));self._retain(rows)
        for ident,row in rows:
            _require(ident in ids and isinstance(row,dict) and row.get('library_id')==self.lib
                and row.get('project_id') is None,'recall_scope_changed')
            _require(ident not in self.origins,'ambiguous_legacy_mapping');self.origins[ident]=row
        return dict(self.origins)

    def read(self,request):
        if not isinstance(request,ContextReadRequest):raise TypeError('ContextReadRequest required')
        self.prepare((request.id,));row=self.rows.get(request.id)
        code='not_found' if row is None else 'not_readable' if (row['status']!='active' or not self.eligible(request.id)) else (
            'expired' if row['expires_at'] and row['expires_at']<=self.eligibility.now else None)
        if row and not window_contains(row['effective_from'],row['effective_until'],self.eligibility.now):code='not_readable'
        if code:return ContextReadResult(request.id,request.layer,'',code)
        # Managed visibility is identical to search; retain its full dependencies
        # before disclosing any full original layer.
        from .history_knowledge import PgHistoryKnowledgeDirectory
        directory=PgHistoryKnowledgeDirectory(self)
        if not directory.visible(request.id):return ContextReadResult(request.id,request.layer,'','not_readable')
        rows=self.session.execute(LAYER,(self.lib,self.project,request.id,request.layer.value))
        _require(len(rows)<=1,'invalid_recall_layers');self._retain(rows);self.layer_proof=rows
        if not rows:return ContextReadResult(request.id,request.layer,'','invalid_layer')
        payload=rows[0][0]
        _require(isinstance(payload,dict) and payload.get('item',{}).get('id')==request.id,'recall_scope_changed')
        layer=payload.get('layer')
        _require(isinstance(layer,dict) and layer.get('layer')==request.layer.value,'invalid_recall_layers')
        content=layer.get('content')
        _require(isinstance(content,str) and hashlib.sha256(content.encode()).hexdigest()==layer.get('content_hash'),
            'recall_content_hash_mismatch')
        original=next(l for l in self.layers[request.id] if l[0]==request.layer.value)
        _require(layer['content_hash']==original[2],'recall_content_hash_mismatch')
        return ContextReadResult(request.id,request.layer,content)

    def proof(self):
        return _digest((super().proof(),self.knowledge_proof,self.layer_proof,
            sorted(self.blocked_families),self.family_proof))
