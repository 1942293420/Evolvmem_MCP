"""Fixed governance-domain manifest, bound to a verified seven-table parent plan.

Construction validates metadata only. Actual parent completion and generation
are checked again by the importer in every target transaction.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from . import retained_chain as R
from . import resume_planning as P
from . import source_plan as S
from .history_core.importer import Target
from .history_core.mapping import uuid_text
from . import governance_mapping as M
from .organization_complete import Plan as OrganizationPlan

check=M.check

class Plan:
    def __init__(self,document,parent_plan,target,organization_plan):
        check(type(parent_plan) is S.Plan and type(target) is Target,'trusted_governance_parent_target_required')
        parent_plan.require_importable()
        check(type(organization_plan) is OrganizationPlan and organization_plan.parent.sha256==parent_plan.sha256,'governance_organization_parent_required')
        self.organization_plan=organization_plan
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
        check(type(index) is int and 0<=index<self.page_count,'governance_page_required')
        return json.loads(self._pages[index])
    def require_importable(self):
        self._parent.require_importable();self._validate()
    def _validate(self):
        d=self.document
        check(set(d)=={'format','complete','sqlite_snapshot_token_resumable','source','parent_manifest_sha256',
            'parent_run_id','target','run_id','source_schema_hash','counts','input_bytes','pages','sequence_evidence','organization_manifest_sha256','organization_run_id'},'governance_manifest_shape')
        check(d['format']=='history-governance-twenty-six-manifest-v1' and d['complete'] is True
            and d['sqlite_snapshot_token_resumable'] is False,'governance_manifest_invalid')
        uuid_text(d['run_id'])
        check(d['run_id'] not in (self._parent.run_id,self._parent.run.core_run_id),'governance_run_identity_required')
        check(d['parent_manifest_sha256']==self._parent.sha256 and d['parent_run_id']==self._parent.run_id
            and d['target']==asdict(self._target) and d['source']==self._parent.header['source'],'governance_parent_binding_changed')
        check(d['source_schema_hash']==M.schema_hash(d['source']['namespace']),'reviewed_governance_schema_required')
        check(d['organization_manifest_sha256']==self.organization_plan.sha256 and d['organization_run_id']==self.organization_plan.run_id,'governance_organization_binding_changed')
        seq=d['sequence_evidence'];check(type(seq) is dict and set(seq)==set(M.AUTO),'governance_sequences_required')
        for t,v in seq.items():
            check(type(v) is dict and set(v)=={'present','value'} and type(v['present']) is bool,'governance_sequence_invalid')
            check((v['present'] and type(v['value']) is int and 0<=v['value']<=M.INT64_MAX) or (not v['present'] and v['value'] is None),'governance_sequence_invalid')
            check(t in M.applicable(d['source']['namespace']) or not v['present'],'governance_absent_sequence')
        pages=d['pages'];check(type(pages) is list and 0<len(pages)<=R.MAX_PAGES,'governance_manifest_page_budget')
        previous={};ended=set();counts={t:0 for t in M.TABLES};size=0;last=-1
        fields={'ordinal','table','chunk_key','after','end','exhausted','row_count','native_hash','input_hash','input_bytes'}
        for i,p in enumerate(pages):
            check(type(p) is dict and set(p)==fields,'governance_page_shape')
            t=p['table'];check(t in M.TABLES and type(p['ordinal']) is int and p['ordinal']==i
                and p['chunk_key']=='c'+str(i).zfill(6),'governance_manifest_page_order')
            ti=M.TABLES.index(t);check(ti>=last and t not in ended,'governance_manifest_table_order');last=ti
            check(p['after']==previous.get(t) and type(p['row_count']) is int and 0<=p['row_count']<=M.MAX_ROWS
                and type(p['exhausted']) is bool and type(p['native_hash']) is str and R.HASH.fullmatch(p['native_hash'])
                and type(p['input_bytes']) is int,'governance_manifest_range_invalid')
            if p['after'] is not None:check(type(p['after']) is int and M.INT64_MIN<=p['after']<=M.INT64_MAX,'governance_cursor_invalid')
            if p['row_count']:
                check(type(p['end']) is int and M.INT64_MIN<=p['end']<=M.INT64_MAX and (p['after'] is None or p['end']>p['after'])
                    and type(p['input_hash']) is str and R.HASH.fullmatch(p['input_hash'])
                    and 0<p['input_bytes']<=M.MAX_BYTES,'governance_manifest_range_invalid')
            else:check(p['end']==p['after'] and p['exhausted'] and p['input_hash'] is None and p['input_bytes']==0,'governance_manifest_empty_invalid')
            if t not in M.applicable(d['source']['namespace']):check(p['row_count']==0 and p['after'] is None,'governance_absent_table_rows')
            if t in M.AUTO and p['row_count']:check(seq[t]['present'] and seq[t]['value']>=p['end'],'governance_sequence_below_rows')
            previous[t]=p['end'];counts[t]+=p['row_count'];size+=p['input_bytes']
            if p['exhausted']:ended.add(t)
        check(type(d['counts']) is dict and set(d['counts'])==set(M.TABLES)
            and all(type(v) is int for v in d['counts'].values()) and type(d['input_bytes']) is int
            and ended==set(M.TABLES) and counts==d['counts'] and size==d['input_bytes']
            and size<=R.MAX_INPUT_BYTES and sum(counts.values())<=R.MAX_ROWS,'governance_manifest_totals_invalid')

def load_plan(path,expected_sha256,parent_plan,target,organization_plan):
    p=Path(path);check(p.stat().st_size<=R.MAX_MANIFEST_BYTES,'manifest_byte_budget')
    raw=p.read_bytes();check(hashlib.sha256(raw).hexdigest()==expected_sha256,'manifest_digest_changed')
    plan=Plan(json.loads(raw),parent_plan,target,organization_plan);check(plan.sha256==expected_sha256,'noncanonical_manifest');return plan

def plan_source(source,parent_plan,target,path,*,run_id,organization_plan,seconds=60,max_rows=R.MAX_ROWS,max_pages=R.MAX_PAGES,max_input_bytes=R.MAX_INPUT_BYTES):
    from .governance_scan import Phase
    check(type(parent_plan) is S.Plan and type(target) is Target,'trusted_governance_parent_target_required')
    parent_plan.require_importable();M.schema_hash(source.namespace)
    for value,ceiling in ((max_rows,R.MAX_ROWS),(max_pages,R.MAX_PAGES),(max_input_bytes,R.MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=ceiling,'governance_plan_budget_invalid')
    path=Path(path)
    if not Path(str(path)+'.started').exists():R._private_write(str(path)+'.started',dict(complete=False,source_sha256=source.sha256))
    try:
        def read(phase,table,after,ordinal):
            page,args,seq,schema=phase.page(table,after,ordinal)
            return page,seq,schema
        result=P.scan(source,path,run_id=run_id,domain='governance',tables=M.TABLES,count_tables=M.TABLES,
            phase_factory=lambda **kw:Phase(source,**kw),read_page=read,binding=dict(parents=[parent_plan.sha256,organization_plan.sha256],parent_source=parent_plan.header['source'],target=asdict(target)),
            seconds=seconds,max_rows=max_rows,max_pages=max_pages,max_input_bytes=max_input_bytes)
        if isinstance(result,P.PlanningProgress):return result
        counts,pages,size,schema=result.counts,result.pages,result.input_bytes,result.schema
        sequence_evidence=result.sequence
        plan=Plan(dict(format='history-governance-twenty-six-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
            source=result.source,parent_manifest_sha256=parent_plan.sha256,parent_run_id=parent_plan.run_id,
            target=asdict(target),run_id=run_id,source_schema_hash=schema,counts=counts,input_bytes=size,pages=pages,
            sequence_evidence=sequence_evidence,organization_manifest_sha256=organization_plan.sha256,organization_run_id=organization_plan.run_id),parent_plan,target,organization_plan)
        R._private_write(path,plan.document);return plan
    except BaseException as exc:
        if not Path(str(path)+'.failed').exists():R._private_write(str(path)+'.failed',dict(complete=False,code=exc.code if isinstance(exc,M.ImportError) else 'governance_plan_failed'))
        raise
