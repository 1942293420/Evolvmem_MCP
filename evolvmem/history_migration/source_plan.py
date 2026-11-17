"""Fixed seven-table metadata manifest on the SAME sealed thirteen-table file."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from . import retained_chain as R
from . import resume_planning as P
from . import source_mapping as M
from . import autoincrement_state as A
from .source_importer import SourceRun,Target
from .source_scan import Phase

check=M.check

class Plan:
    def __init__(self,document):
        self._raw=M.canonical(document);check(len(self._raw)<=R.MAX_MANIFEST_BYTES,'manifest_byte_budget')
        self.sha256=hashlib.sha256(self._raw).hexdigest();self._validate()
        d=self.document;self._pages=tuple(M.canonical(p) for p in d.pop('pages'));self._header=M.canonical(d)
    @property
    def document(self):return json.loads(self._raw)
    @property
    def header(self):return json.loads(self._header)
    @property
    def page_count(self):return len(self._pages)
    def page(self,index):
        check(type(index) is int and 0<=index<self.page_count,'source_page_required');return json.loads(self._pages[index])
    @property
    def run(self):return _run(self.header['run'])
    @property
    def run_id(self):return self.run.run_id
    @property
    def counts(self):return dict(self.run.expected_counts)
    @property
    def highwaters(self):
        d=self.header
        maxima=dict(self._allocation_maxima)
        return {t:A.resolved_highwater(e,self.counts[t],maxima[t]) for t,e in d['sequence'].items()}
    def require_importable(self):check(self.header['highwaters_ready'],'source_highwaters_unresolved')
    def _validate(self):
        d=self.document;run=_run(d['run'])
        check(d['format']=='history-sources-seven-manifest-v1' and d['complete'] is True
            and d['sqlite_snapshot_token_resumable'] is False,'source_manifest_invalid')
        check(d['source']['namespace']==run.source_namespace and d['source']['file_sha256']==run.source_file_sha256
            and d['source_schema_hash']==run.source_schema_hash,'source_manifest_binding_changed')
        pages=d['pages'];check(type(pages) is list and 0<len(pages)<=R.MAX_PAGES,'source_manifest_page_budget')
        previous={};ended=set();counts={t:0 for t in M.TABLES};size=0;last=-1
        for i,p in enumerate(pages):
            t=p['table'];check(t in M.TABLES and p['ordinal']==i and p['chunk_key']=='s'+str(i).zfill(6),'source_manifest_page_order')
            ti=M.TABLES.index(t);check(ti>=last and t not in ended,'source_manifest_table_order');last=ti
            check(p['after']==previous.get(t) and type(p['row_count']) is int and 0<=p['row_count']<=128
                and type(p['exhausted']) is bool and type(p['native_hash']) is str and R.HASH.fullmatch(p['native_hash']),'source_manifest_range_invalid')
            if p['row_count']:
                check(type(p['end']) is int and M.INT64_MIN<=p['end']<=M.INT64_MAX and (p['after'] is None or p['end']>p['after'])
                    and type(p['input_hash']) is str and R.HASH.fullmatch(p['input_hash'])
                    and type(p['input_bytes']) is int and 0<p['input_bytes']<=M.MAX_BYTES,'source_manifest_range_invalid')
            else:check(p['end']==p['after'] and p['exhausted'] and p['input_hash'] is None and p['input_bytes']==0,'source_manifest_empty_invalid')
            previous[t]=p['end'];counts[t]+=p['row_count'];size+=p['input_bytes']
            if p['exhausted']:ended.add(t)
        check(ended==set(M.TABLES) and counts==dict(run.expected_counts) and size==d['input_bytes']
            and size<=R.MAX_INPUT_BYTES and sum(counts.values())<=R.MAX_ROWS,'source_manifest_totals_invalid')
        seq=d['sequence'];check(set(seq)==set(M.AUTO),'source_sequence_invalid')
        check(dict(run.source_highwaters)=={t:e['value'] for t,e in seq.items()},'source_sequence_binding_changed')
        maxima={t:max(0,previous[t] or 0) for t in M.AUTO}
        self._allocation_maxima=tuple(maxima.items())
        ready=all(A.ready(e,counts[t],maxima[t]) for t,e in seq.items())
        check(type(d['highwaters_ready']) is bool and (d['highwaters_ready'] is ready or
            d['highwaters_ready'] is False and not A.legacy_ready(seq,maxima)),'source_sequence_readiness_changed')

def _run(d):
    check(type(d) is dict,'source_run_manifest_required')
    values=dict(d)
    for k in ('expected_counts','source_highwaters'):values[k]=tuple(tuple(p) for p in values[k])
    return SourceRun(**values)

def load_plan(path,expected_sha256):
    p=Path(path);check(p.stat().st_size<=R.MAX_MANIFEST_BYTES,'manifest_byte_budget')
    raw=p.read_bytes();check(hashlib.sha256(raw).hexdigest()==expected_sha256,'manifest_digest_changed')
    plan=Plan(json.loads(raw));check(plan.sha256==expected_sha256,'noncanonical_manifest');return plan

def plan_source(source,core_plan,target,path,*,run_id,seconds=60,max_rows=R.MAX_ROWS,max_pages=R.MAX_PAGES,max_input_bytes=R.MAX_INPUT_BYTES):
    check(type(core_plan) is R.Plan and type(target) is Target,'trusted_core_plan_target_required');core_plan.require_importable()
    for value,ceiling in ((max_rows,R.MAX_ROWS),(max_pages,R.MAX_PAGES),(max_input_bytes,R.MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=ceiling,'source_plan_budget_invalid')
    path=Path(path)
    if not Path(str(path)+'.started').exists():R._private_write(str(path)+'.started',dict(complete=False,source_sha256=source.sha256))
    try:
        def read(phase,table,after,ordinal):
            page,args,seq,schema=phase.page(table,after,ordinal)
            return page,seq,schema
        result=P.scan(source,path,run_id=run_id,domain='sources',tables=M.TABLES,count_tables=M.TABLES,
            phase_factory=lambda **kw:Phase(source,**kw),read_page=read,binding=dict(parents=[core_plan.sha256],parent_source=core_plan.header['source'],target=asdict(target)),
            seconds=seconds,max_rows=max_rows,max_pages=max_pages,max_input_bytes=max_input_bytes)
        if isinstance(result,P.PlanningProgress):return result
        counts,pages,size,schema=result.counts,result.pages,result.input_bytes,result.schema
        seq,maxima={t:result.sequence[t] for t in M.AUTO},result.maxima
        ready=all(A.ready(e,counts[t],maxima[t]) for t,e in seq.items())
        core_binding=M.digest({'source':core_plan.manifest,'target':asdict(target)})
        run=SourceRun(run_id,core_plan.run_id,core_binding,source.namespace,source.sha256,schema,tuple(counts.items()),tuple((t,e['value']) for t,e in seq.items()))
        plan=Plan(dict(format='history-sources-seven-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
            source=result.source,run=run.manifest,source_schema_hash=schema,sequence=seq,highwaters_ready=ready,input_bytes=size,pages=pages))
        R._private_write(path,plan.document);return plan
    except BaseException as exc:
        if not Path(str(path)+'.failed').exists():R._private_write(str(path)+'.failed',dict(complete=False,code=exc.code if isinstance(exc,M.ImportError) else 'source_plan_failed'))
        raise
