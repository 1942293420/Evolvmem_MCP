"""Key/hash-only delta plans and bounded dependency components."""
from dataclasses import dataclass
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import time
import uuid
from . import catchup_catalog as C
from .catchup_source import Snapshot

def change(t,before,after):
    row=after if after is not None else before
    op='insert' if before is None else 'delete' if after is None else 'update'
    if op=='update' and t.rowid:
        e=t.evidence('owner');cols=e.get('columns',e.get('columns_xinfo',()))
        logical=[c['name'] for c in sorted(cols,key=lambda c:c['pk_position']) if c['pk_position']]
        if not logical or any(before[k]!=after[k] for k in logical):op='delete_insert'
    return dict(table=t.name,key=list(t.key(row)),op=op,
        before_hash=C.row_hash(t.name,before) if before is not None else None,
        after_hash=C.row_hash(t.name,after) if after is not None else None,
        input_bytes=len(C.value_bytes(t,[before,after])))

def merge_rows(t,old,new,*,old_summary=None,new_summary=None):
    """Ordered original source keys; updates/deletes cannot hide below max ID."""
    def stream(rows,summary):
        h=hashlib.sha256();k=hashlib.sha256();count=0;size=0;previous=None
        for row in rows:
            key=t.key(row);C.check(previous is None or previous<key,'catchup_source_key_order');previous=key
            line=C.typed_line(t,row);h.update(line);k.update(C.canonical(key)+b'\n');count+=1;size+=len(line)
            yield row
        if summary is not None:summary.update(count=count,typed_sha256=h.hexdigest(),keys_sha256=k.hexdigest(),input_bytes=size)
    a=iter(stream(old,old_summary));b=iter(stream(new,new_summary));x=next(a,None);y=next(b,None)
    while x is not None or y is not None:
        if y is None or x is not None and t.key(x)<t.key(y):
            yield change(t,x,None);x=next(a,None)
        elif x is None or t.key(y)<t.key(x):
            yield change(t,None,y);y=next(b,None)
        else:
            if C.row_hash(t.name,x)!=C.row_hash(t.name,y):yield change(t,x,y)
            x=next(a,None);y=next(b,None)

def component_pages(ops,edges):
    """Iterative SCC, then DAG order. A component is never split or applied big."""
    n=len(ops);edges={i:set(edges.get(i,())) for i in range(n)}
    C.check(all(0<=j<n for values in edges.values() for j in values),'catchup_dependency_index')
    seen=set();order=[]
    for start in range(n):
        if start in seen:continue
        seen.add(start);stack=[(start,iter(sorted(edges[start])))]
        while stack:
            node,it=stack[-1];child=next(it,None)
            if child is None:order.append(node);stack.pop()
            elif child not in seen:seen.add(child);stack.append((child,iter(sorted(edges[child]))))
    reverse={i:set() for i in range(n)}
    for i,values in edges.items():
        for j in values:reverse[j].add(i)
    groups=[];index={}
    for start in reversed(order):
        if start in index:continue
        group=[];stack=[start];index[start]=len(groups)
        while stack:
            node=stack.pop();group.append(node)
            for child in reverse[node]:
                if child not in index:index[child]=len(groups);stack.append(child)
        C.check(len(group)<=C.MAX_ROWS,'catchup_component_row_budget')
        C.check(sum(ops[i]['input_bytes'] for i in group)<=C.changes_budget([ops[i] for i in group]),'catchup_component_byte_budget')
        groups.append(sorted(group))
    outgoing={i:set() for i in range(len(groups))};degree=dict.fromkeys(outgoing,0)
    for i,values in edges.items():
        for j in values:
            a,b=index[i],index[j]
            if a!=b and b not in outgoing[a]:outgoing[a].add(b);degree[b]+=1
    ready=sorted(i for i in outgoing if degree[i]==0);result=[];page=[];size=0
    while ready:
        g=ready.pop(0);values=[ops[i] for i in groups[g]];amount=sum(x['input_bytes'] for x in values)
        if page and (len(page)+len(values)>C.MAX_ROWS or size+amount>C.changes_budget(page+values)):result.append(page);page=[];size=0
        # component ID retains the atomic group's boundary inside a packed page.
        for value in values:value=dict(value,component=g);page.append(value)
        size+=amount
        for child in sorted(outgoing[g]):
            degree[child]-=1
            if degree[child]==0:ready.append(child);ready.sort()
    if page:result.append(page)
    C.check(sum(map(len,result))==n,'catchup_dependency_cycle_unresolved');return result

def _source_col(t,target_col):
    if t.domain=='core' and t.name=='context_layers' and target_col=='legacy_layer_id':return 'id'
    return target_col if target_col in t.source_columns else None

def release_only_update(t,before,after,catalog,projects):
    """Only exit the exact reviewed active-identity index; never acquire a key."""
    if t.domain!='core' or t.name!='context_items' or before is None or after is None:return False
    a=t.mapped(before,projects);b=t.mapped(after,projects)
    if a.get('status')!='active' or b.get('status')=='active':return False
    if any(a.get(k)!=b.get(k) for k in ('id','identity_key','project','scope')):return False
    expected=dict(table='context_items',index='context_one_active_identity',
        columns=['identity_key','project','scope'],predicate="(status = 'active'::text)",predicate_columns=['status'],
        definition="CREATE UNIQUE INDEX context_one_active_identity ON em_content.context_items USING btree (library_id, identity_key, project, scope) WHERE (status = 'active'::text)")
    changed=[u for u in catalog['unique'] if u['table']==t.name
        and any(a.get(k)!=b.get(k) for k in set(u['columns'])|set(u.get('predicate_columns',())))]
    if changed!=[expected]:return False
    for f in catalog['foreign']+catalog.get('external_foreign',[]):
        if f['parent']==t.name and any(a.get(k)!=b.get(k) for k in f['parent_columns']):return False
        if f.get('table')==t.name and any(a.get(k)!=b.get(k) for k in f['columns'] if k not in f.get('nullable',())):return False
    return True

def dependency_pages(old,new,ops,catalog,projects,*,deadline,max_changes):
    """Use actual target FK/unique definitions; conservative partial-index groups.

    For replacement only, retain all old dependent rows as explicit restore
    changes. No CASCADE may erase an unjournaled row. Large closure fails before
    begin. Other FK relations form a DAG, avoiding giant parent/child batches.
    """
    lookup={(x['table'],tuple(x['key'])):i for i,x in enumerate(ops)};edges={}
    def tick():C.check(time.monotonic()<deadline,'catchup_plan_deadline')
    def pair(i):
        # bounded cache: the authoritative before/after images remain in seals.
        tick();o=ops[i];t=C.table(o['table'])
        return old.row(t,o['key']),new.row(t,o['key'])
    def add(t,before,after):
        key=(t.name,t.key(after if after is not None else before))
        if key in lookup:return lookup[key]
        C.check(len(ops)<max_changes,'catchup_change_budget')
        o=change(t,before,after)
        if o['before_hash']==o['after_hash']:o['op']='restore'
        o['mode']='replace' if before is not None and after is not None else o['op']
        lookup[key]=len(ops);ops.append(o);return len(ops)-1
    def edge(a,b):
        if a!=b:edges.setdefault(a,set()).add(b)
    unique=catalog['unique'];foreign=catalog['foreign']
    # Determine which updates need release/reinsert rather than ordinary UPDATE.
    for i,o in enumerate(ops):
        before,after=pair(i);t=C.table(o['table']);mode='replace' if o['op']=='delete_insert' else o['op']
        if mode=='update':
            a=t.mapped(before,projects);b=t.mapped(after,projects)
            for u in unique:
                if u['table']!=t.name:continue
                watched=set(u['columns'])|set(u.get('predicate_columns',()))
                if any(a.get(k)!=b.get(k) for k in watched):
                    mode='release_update' if release_only_update(t,before,after,catalog,projects) else 'replace';break
        o['mode']=mode
    # Replacements preserve dependent rows, even if their source text is equal.
    i=0
    while i<len(ops):
        o=ops[i];before,after=pair(i);t=C.table(o['table'])
        if o['mode'] in ('delete','replace') and before is not None:
            for f in foreign:
                if f['parent']!=t.name:continue
                child=C.table(f['table']);pc=[_source_col(t,c) for c in f['parent_columns']];cc=[_source_col(child,c) for c in f['columns']]
                C.check(all(pc) and all(cc),'catchup_unmapped_fk')
                values=[before[k] for k in pc]
                if any(v is None for v in values):continue
                matches=old.find(child,cc,values)
                C.check(len(matches)<=128,'catchup_component_row_budget')
                for row in matches:
                    j=add(child,row,new.row(child,child.key(row)))
                    if ops[j]['op'] not in ('delete',):ops[j]['mode']='replace'
                    # Both live in one transaction to preserve immediate FKs.
                    edge(i,j);edge(j,i)
        i+=1
    for i,o in enumerate(ops):
        t=C.table(o['table']);before,after=pair(i)
        for f in foreign:
            if f['table']!=t.name:continue
            parent=C.table(f['parent']);cols=[_source_col(t,c) for c in f['columns']];pcols=[_source_col(parent,c) for c in f['parent_columns']]
            C.check(all(cols) and all(pcols),'catchup_unmapped_fk')
            if after is not None:
                vals=[after[k] for k in cols]
                if not any(v is None for v in vals):
                    parents=new.find(parent,pcols,vals,limit=2);C.check(len(parents)==1,'catchup_source_fk_missing')
                    j=lookup.get((parent.name,parent.key(parents[0])))
                    if j is not None:edge(j,i)
            if before is not None:
                vals=[before[k] for k in cols]
                if not any(v is None for v in vals):
                    for row in old.find(parent,pcols,vals,limit=2):
                        j=lookup.get((parent.name,parent.key(row)))
                        if j is not None and ops[j]['mode'] in ('delete','replace'):edge(i,j)
        if after is not None:
            for u in unique:
                if u['table']!=t.name:continue
                cols=[_source_col(t,c) for c in u['columns']]
                if not all(cols):continue # project_uuid is a fixed trusted mapping
                vals=[after[c] for c in cols]
                if any(v is None for v in vals):continue
                for row in old.find(t,cols,vals):
                    if t.key(row)==t.key(after):continue
                    j=lookup.get((t.name,t.key(row)))
                    if j is not None:edge(j,i)
                    # Partial indexes conservatively retain harmless collisions;
                    # actual PostgreSQL uniqueness remains authoritative.
    return component_pages(ops,edges)

@dataclass(frozen=True)
class Plan:
    _raw:bytes
    sha256:str
    def __post_init__(self):
        C.check(len(self._raw)<=64*1024**2 and hashlib.sha256(self._raw).hexdigest()==self.sha256,'catchup_plan_hash')
        d=self.document;C.check(d['format']=='history-catchup-v1' and d['complete'] is True,'catchup_plan_incomplete')
        C.core.uuid_text(d['epoch_id'])
        C.check(len(d['pages'])<=100000,'catchup_page_count_budget')
        for i,p in enumerate(d['pages']):
            C.check(p['ordinal']==i and len(p['changes'])<=128 and sum(o['input_bytes'] for o in p['changes'])<=C.changes_budget(p['changes']),'catchup_page_budget')
            C.check(p['sha256']==C.digest(p['changes']),'catchup_page_hash')
    @property
    def document(self):return json.loads(self._raw)
    @classmethod
    def from_document(cls,d):
        raw=C.canonical(d);return cls(raw,hashlib.sha256(raw).hexdigest())

def build_plan(old_source,new_source,*,base_binding,previous_proof,target,catalog,
        projects,caught_up_to,epoch_id=None,seconds=60,max_changes=100000,max_input_bytes=4*1024**3):
    C.check(old_source.namespace==new_source.namespace,'catchup_namespace_changed')
    C.check(type(max_changes) is int and 1<=max_changes<=100000,'catchup_change_budget')
    deadline=time.monotonic()+min(seconds,60);ops=[];old_summary={};new_summary={}
    with ExitStack() as stack:
        old=stack.enter_context(Snapshot(old_source,seconds=seconds,max_input_bytes=max_input_bytes))
        new=stack.enter_context(Snapshot(new_source,seconds=seconds,max_input_bytes=max_input_bytes))
        for t in C.tables(old_source.namespace):
            old_summary[t.name]={};new_summary[t.name]={}
            for o in merge_rows(t,old.rows(t),new.rows(t),old_summary=old_summary[t.name],new_summary=new_summary[t.name]):
                ops.append(o);C.check(len(ops)<=max_changes,'catchup_change_budget')
        pages=dependency_pages(old,new,ops,catalog,dict(projects),deadline=deadline,max_changes=max_changes)
        old_sequence=old.sequences();new_sequence=new.sequences()
    C.check(old.phase.proven_complete and new.phase.proven_complete,'catchup_source_phase_incomplete')
    return Plan.from_document(dict(format='history-catchup-v1',epoch_id=epoch_id or str(uuid.uuid4()),
        namespace=old_source.namespace,base_binding=base_binding,previous_proof=previous_proof,target=target,
        old_seal=old_source.receipt_sha256,new_seal=new_source.receipt_sha256,
        old_summary=old_summary,new_summary=new_summary,old_sequence=old_sequence,new_sequence=new_sequence,
        catalog=catalog,project_mapping=list(projects),caught_up_to=caught_up_to,
        pages=[dict(ordinal=i,sha256=C.digest(p),changes=p) for i,p in enumerate(pages)],complete=True))
