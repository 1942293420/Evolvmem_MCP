"""Bounded selected-result dependency proof; never reranks or encodes."""
from datetime import datetime,timezone
import hashlib
import json

from evolvmem.context_temporal import TIMESTAMP_FORMAT,window_contains
from evolvmem.lan_experience_snapshot import ExperienceReadError

MAX_NODES=32
MAX_DEPTH=8
MAX_FAMILY=64
MAX_ROWS=4096
MAX_BYTES=8*1024*1024
CHUNK_BYTES=65536


class SelectedExperienceProof:
    """Same request/snapshot only; retain hashes rather than case/layer bodies.

    describe() is the canonical case/evidence projection consumed by recall's
    project/conditions/verification rules. Equality of that complete projection
    and fixed arguments preserves those decisions without duplicating the rules.
    Family metadata is conservative: a related family change rejects, without
    imposing the semantic channel's extra gates on lexical-only candidates.
    """
    def __init__(self,service,ids,*,public,request_args,at=None):
        self.service=service;self.ids=tuple(ids);self.public=public
        if (len(self.ids)>23 or len(set(self.ids))!=len(self.ids)
                or any(type(i) is not int or i<=0 for i in self.ids)):
            raise ExperienceReadError('experience_read_unavailable')
        self.request=json.dumps(request_args,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        self.expected,self.windows=self._capture()
        moment=at or datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
        self.time_state=self._time_state(self.windows,moment)

    @staticmethod
    def _time_state(windows,moment):
        return tuple((expiry is None or expiry>moment,window_contains(start,end,moment))
                     for expiry,start,end in windows)

    def check_time(self,moment):
        if self._time_state(self.windows,moment)!=self.time_state:
            raise ExperienceReadError('experience_read_changed')

    def check(self):
        current,_=self._capture()
        if current!=self.expected:
            raise ExperienceReadError('experience_read_changed')

    def _capture(self):
        try:return self._capture_current()
        except (ValueError,TypeError,KeyError,AttributeError,UnicodeError):
            raise ExperienceReadError('experience_read_unavailable') from None

    def _capture_current(self):
        from evolvmem.memory_eligibility import eligible
        from evolvmem.unit_derivations import BackingRead
        store=self.service.store;conn=store._connection()
        digest=hashlib.sha256();used=0;row_count=0

        def feed(label,value):
            nonlocal used,row_count
            data=json.dumps([label,value],sort_keys=True,separators=(',',':'),
                            ensure_ascii=False,allow_nan=False).encode('utf-8')
            used+=len(data);row_count+=1
            if used>MAX_BYTES or row_count>MAX_ROWS:
                raise ExperienceReadError('experience_read_proof_limit')
            digest.update(len(data).to_bytes(8,'big'));digest.update(data)

        def row_list(sql,params):
            rows=[]
            for row in conn.execute(sql,params):
                value=dict(row);feed('dependency_row:'+sql,value);rows.append(value)
            return rows

        def layers(item_id):
            nonlocal used
            rows=conn.execute('SELECT id,layer,content_hash,generator,created_at,updated_at,'
                              'typeof(content) AS storage,length(CAST(content AS BLOB)) AS bytes '
                              'FROM context_layers WHERE item_id=? ORDER BY layer',(item_id,))
            for row in rows:
                size=row['bytes']
                if type(size) is not int or size<0 or used+size>MAX_BYTES:
                    raise ExperienceReadError('experience_read_proof_limit')
                feed('layer',dict(row));content=hashlib.sha256()
                for offset in range(0,size,CHUNK_BYTES):
                    block=conn.execute('SELECT substr(CAST(content AS BLOB),?,?) FROM context_layers WHERE id=?',
                                       (offset+1,CHUNK_BYTES,row['id'])).fetchone()[0]
                    if not isinstance(block,bytes) or len(block)!=min(CHUNK_BYTES,size-offset):
                        raise ExperienceReadError('experience_read_unavailable')
                    content.update(block);used+=len(block)
                feed('actual_layer_bytes',content.hexdigest())

        feed('fixed_request',[self.request,self.public,self.ids])
        nodes={};sources={};edges={};queue=[(i,()) for i in self.ids]
        while queue:
            item_id,ancestors=queue.pop(0)
            if item_id in ancestors or len(ancestors)>MAX_DEPTH:
                raise ExperienceReadError('experience_read_proof_limit')
            if item_id in nodes:continue
            if len(nodes)>=MAX_NODES:
                raise ExperienceReadError('experience_read_proof_limit')
            row=conn.execute('SELECT * FROM context_items WHERE id=?',(item_id,)).fetchone()
            if row is None:raise ExperienceReadError('experience_read_changed')
            nodes[item_id]=dict(row)
            sources[item_id]=row_list('SELECT * FROM context_sources WHERE item_id=? ORDER BY id',(item_id,))
            edges[item_id]=[]
            for source in sources[item_id]:
                if source['source_kind']=='experience':
                    ref=source['source_ref']
                    if not isinstance(ref,str) or not ref.isdecimal() or int(ref)<=0:
                        raise ExperienceReadError('experience_read_unavailable')
                    edges[item_id].append(int(ref))
                    queue.append((int(ref),ancestors+(item_id,)))

        depths={}
        def depth(item_id,trail):
            if item_id in trail:raise ExperienceReadError('experience_read_proof_limit')
            if item_id not in depths:
                depths[item_id]=max((1+depth(child,trail+(item_id,)) for child in edges[item_id]),default=0)
            if depths[item_id]>MAX_DEPTH:raise ExperienceReadError('experience_read_proof_limit')
            return depths[item_id]
        for item_id in self.ids:depth(item_id,())

        family={}
        for item in nodes.values():
            for row in conn.execute('SELECT * FROM context_items WHERE identity_key=? AND project=? AND scope=? '
                                    'ORDER BY id LIMIT ?',
                                    (item['identity_key'],item['project'],item['scope'],MAX_FAMILY+1)):
                family[row['id']]=dict(row)
                if len(family)>MAX_FAMILY:
                    raise ExperienceReadError('experience_read_proof_limit')
        all_ids=tuple(sorted(set(nodes)|set(family)))
        if all_ids:
            marks=','.join('?' for _ in all_ids)
            for table in ('context_sources','unit_derivations'):
                if conn.execute(f'SELECT count(*) FROM {table} WHERE item_id IN ({marks})',all_ids).fetchone()[0]>MAX_ROWS:
                    raise ExperienceReadError('experience_read_proof_limit')
        backing=BackingRead(store,all_ids)
        for item_id in all_ids:
            current=eligible(store,item_id,read=backing)
            if item_id in nodes and not current:
                raise ExperienceReadError('experience_read_changed')
            feed('item',[item_id,nodes.get(item_id,family.get(item_id)),current])
            feed('backing',[item_id,sorted((dict(r) for r in backing.rows.get(item_id,())),
                                         key=lambda r:json.dumps(r,sort_keys=True))])
            feed('backing_sources',[item_id,sorted((dict(r) for r in backing.sources.get(item_id,())),
                                                 key=lambda r:json.dumps(r,sort_keys=True))])
            feed('learning',row_list('SELECT * FROM learning_memories WHERE item_id=? ORDER BY item_id',(item_id,)))
            feed('resolutions',row_list('SELECT * FROM context_project_resolutions WHERE item_id=? ORDER BY item_id',(item_id,)))
        # These members are exactly the lazily read conversation dependencies of
        # the canonical currently_backed rule, including its empty-set outcome.
        feed('conversation_archives',sorted((k,sorted(v)) for k,v in backing.archives.items()))
        for item_id in sorted(nodes):
            feed('sources',[item_id,sources[item_id]])
            events=row_list('SELECT * FROM context_evidence WHERE item_id=? ORDER BY id',(item_id,))
            feed('evidence',[item_id,events])
            if not self.public or item_id not in self.ids:
                # No require_serving()/cache/model path: describe is canonical
                # read-only projection, including non-preview proof fields.
                feed('case',self.service.experiences().describe(item_id))
            layers(item_id)
        if self.public:
            for item_id in self.ids:
                row=conn.execute('SELECT * FROM lan_publications WHERE context_id=? AND active=1',(item_id,)).fetchone()
                if row is None:raise ExperienceReadError('experience_read_changed')
                feed('publication',dict(row))
        windows=tuple((family.get(i,nodes.get(i))['expires_at'],
                       family.get(i,nodes.get(i))['effective_from'],
                       family.get(i,nodes.get(i))['effective_until']) for i in all_ids)
        return digest.digest(),windows
