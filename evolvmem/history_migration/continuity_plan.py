"""Fixed continuity-domain manifest, bound to a verified six-table core plan.

Construction validates metadata only. Actual parent completion and generation
are checked again by the importer in every target transaction.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from . import retained_chain as R
from . import resume_planning as P
from . import retained_chain as S
from .history_core.importer import Target
from .history_core.mapping import uuid_text
from . import continuity_mapping as M

check=M.check

class Plan:
    def __init__(self,document,parent_plan,target):
        check(type(parent_plan) is S.Plan and type(target) is Target,'trusted_continuity_parent_target_required')
        parent_plan.require_importable()
        self._parent=parent_plan;self._target=target
        self._raw=M.canonical(document);check(len(self._raw)<=R.MAX_MANIFEST_BYTES,'manifest_byte_budget')
        self.sha256=hashlib.sha256(self._raw).hexdigest();self._validate()
        d=self.document;self._pages=tuple(M.canonical(p) for p in d.pop('pages'));self._header=M.canonical(d)
    @property
    def document(self):return json.loads(self._raw)
    @property
    def header(self):return json.loads(self._header)
    @property
    def parent_plan(self):return self._parent
    @property
    def target(self):return self._target
    @property
    def page_count(self):return len(self._pages)
    @property
    def run_id(self):return self.header['run_id']
    @property
    def counts(self):return self.header['counts']
    def page(self,index):
        check(type(index) is int and 0<=index<self.page_count,'continuity_page_required')
        return json.loads(self._pages[index])
    def require_importable(self):
        self._parent.require_importable();self._validate()
    def _validate(self):
        d=self.document
        check(set(d)=={'format','complete','sqlite_snapshot_token_resumable','source','parent_manifest_sha256',
            'parent_run_id','target','run_id','source_schema_hash','counts','input_bytes','pages','sequence','not_applicable'},'continuity_manifest_shape')
        check(d['format']=='history-continuity-eight-manifest-v1' and d['complete'] is True
            and d['sqlite_snapshot_token_resumable'] is False,'continuity_manifest_invalid')
        uuid_text(d['run_id'])
        check(d['run_id'] not in (self._parent.run_id,),'continuity_run_identity_required')
        check(d['parent_manifest_sha256']==self._parent.sha256 and d['parent_run_id']==self._parent.run_id
            and d['target']==asdict(self._target) and d['source']==self._parent.header['source'],'continuity_parent_binding_changed')
        check(d['source_schema_hash']==M.schema_hash(d['source']['namespace']),'reviewed_continuity_schema_required')
        applicable=M.applicable(d['source']['namespace'])
        check(d['not_applicable']==list(M.absent(d['source']['namespace'])),'continuity_absence_changed')
        pages=d['pages'];check(type(pages) is list and 0<len(pages)<=R.MAX_PAGES,'continuity_manifest_page_budget')
        previous={};ended=set();counts={t:0 for t in M.TABLES};size=0;last=-1
        fields={'ordinal','table','chunk_key','after','end','exhausted','row_count','native_hash','input_hash','input_bytes'}
        for i,p in enumerate(pages):
            check(type(p) is dict and set(p)==fields,'continuity_page_shape')
            t=p['table'];check(t in applicable and type(p['ordinal']) is int and p['ordinal']==i
                and p['chunk_key']=='c'+str(i).zfill(6),'continuity_manifest_page_order')
            ti=M.TABLES.index(t);check(ti>=last and t not in ended,'continuity_manifest_table_order');last=ti
            check(p['after']==previous.get(t) and type(p['row_count']) is int and 0<=p['row_count']<=M.MAX_ROWS
                and type(p['exhausted']) is bool and type(p['native_hash']) is str and R.HASH.fullmatch(p['native_hash'])
                and type(p['input_bytes']) is int,'continuity_manifest_range_invalid')
            if p['after'] is not None:check(type(p['after']) is int and M.INT64_MIN<=p['after']<=M.INT64_MAX,'continuity_cursor_invalid')
            if p['row_count']:
                check(type(p['end']) is int and M.INT64_MIN<=p['end']<=M.INT64_MAX and (p['after'] is None or p['end']>p['after'])
                    and type(p['input_hash']) is str and R.HASH.fullmatch(p['input_hash'])
                    and 0<p['input_bytes']<=M.MAX_BYTES,'continuity_manifest_range_invalid')
            else:check(p['end']==p['after'] and p['exhausted'] and p['input_hash'] is None and p['input_bytes']==0,'continuity_manifest_empty_invalid')
            previous[t]=p['end'];counts[t]+=p['row_count'];size+=p['input_bytes']
            if p['exhausted']:ended.add(t)
        check(type(d['counts']) is dict and set(d['counts'])==set(M.TABLES)
            and all(type(v) is int for v in d['counts'].values()) and type(d['input_bytes']) is int
            and ended==set(applicable) and counts==d['counts'] and size==d['input_bytes']
            and size<=R.MAX_INPUT_BYTES and sum(counts.values())<=R.MAX_ROWS,'continuity_manifest_totals_invalid')
        sequence=d['sequence'];auto=tuple(t for t in M.AUTO if t in applicable)
        check(type(sequence) is dict and set(sequence)==set(auto),'continuity_sequence_set_changed')
        for t,e in sequence.items():
            from .continuity_sequences import allocation
            allocation(e,previous.get(t) or 0)
    @property
    def highwaters(self):return {t:e['value'] for t,e in self.header['sequence'].items()}


def load_plan(path,expected_sha256,parent_plan,target):
    p=Path(path);check(p.stat().st_size<=R.MAX_MANIFEST_BYTES,'manifest_byte_budget')
    raw=p.read_bytes();check(hashlib.sha256(raw).hexdigest()==expected_sha256,'manifest_digest_changed')
    plan=Plan(json.loads(raw),parent_plan,target);check(plan.sha256==expected_sha256,'noncanonical_manifest');return plan

def plan_source(source,parent_plan,target,path,*,run_id,seconds=60,max_rows=R.MAX_ROWS,max_pages=R.MAX_PAGES,max_input_bytes=R.MAX_INPUT_BYTES):
    from .continuity_scan import Phase
    check(type(parent_plan) is S.Plan and type(target) is Target,'trusted_continuity_parent_target_required')
    parent_plan.require_importable();M.schema_hash(source.namespace)
    for value,ceiling in ((max_rows,R.MAX_ROWS),(max_pages,R.MAX_PAGES),(max_input_bytes,R.MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=ceiling,'continuity_plan_budget_invalid')
    path=Path(path)
    if not Path(str(path)+'.started').exists():R._private_write(str(path)+'.started',dict(complete=False,source_sha256=source.sha256))
    try:
        def read(phase,table,after,ordinal):
            page,args,seq,schema=phase.page(table,after,ordinal)
            return page,seq,schema
        result=P.scan(source,path,run_id=run_id,domain='continuity',tables=M.applicable(source.namespace),count_tables=M.TABLES,
            phase_factory=lambda **kw:Phase(source,**kw),read_page=read,binding=dict(parents=[parent_plan.sha256],parent_source=parent_plan.header['source'],target=asdict(target)),
            seconds=seconds,max_rows=max_rows,max_pages=max_pages,max_input_bytes=max_input_bytes)
        if isinstance(result,P.PlanningProgress):return result
        counts,pages,size,schema=result.counts,result.pages,result.input_bytes,result.schema
        sequence=result.sequence
        plan=Plan(dict(format='history-continuity-eight-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
            source=result.source,parent_manifest_sha256=parent_plan.sha256,parent_run_id=parent_plan.run_id,
            target=asdict(target),run_id=run_id,source_schema_hash=schema,counts=counts,input_bytes=size,pages=pages,sequence=sequence,not_applicable=list(M.absent(source.namespace))),parent_plan,target)
        R._private_write(path,plan.document);return plan
    except BaseException as exc:
        if not Path(str(path)+'.failed').exists():R._private_write(str(path)+'.failed',dict(complete=False,code=exc.code if isinstance(exc,M.ImportError) else 'continuity_plan_failed'))
        raise
