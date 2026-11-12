"""Private retained-file planning and replay. No source copying or PG connection.

An explicit offline retention attestation plus start/end whole-file SHA binds
bounded fresh SQLite transactions to one existing static file. A rowid boundary
is a seek coordinate, NEVER a persisted SQLite snapshot/cursor. Stat checks are
only early drift detection. All phase results are provisional until end SHA.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from . import sqlite_source as S
from . import autoincrement_state as A
from .history_core import mapping as M
from .history_core.importer import prepare_chunk

MAX_SOURCE_BYTES=2*1024**3
MAX_MANIFEST_BYTES=8*1024**2
MAX_PAGES=4096
MAX_ROWS=250000
MAX_INPUT_BYTES=1024**3
MAX_PHASE_SECONDS=60.0
SOURCE_SHA='3019ef0682f77de4494e636466d26da643205a98fdef8758d860fa8093186370'
HASH=re.compile('[0-9a-f]{64}\\Z')
check=M.check


@dataclass(frozen=True)
class RetainedSource:
    namespace:str
    path:Path
    sha256:str
    stability_evidence_sha256:str
    def __post_init__(self):
        check(self.namespace in S.FIXED_PATHS,'source_namespace_required')
        object.__setattr__(self,'path',Path(self.path))
        check(self.path.is_absolute(),'absolute_retained_path_required')
        check(self.path.as_posix() not in S.FIXED_PATHS.values(),'active_source_not_supported')
        check(all(type(h) is str and HASH.fullmatch(h) for h in (self.sha256,self.stability_evidence_sha256)),
              'explicit_retention_evidence_required')


def _private_write(path,value):
    payload=M.canonical(value)
    check(len(payload)<=MAX_MANIFEST_BYTES,'manifest_byte_budget')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as output:output.write(payload);output.flush();os.fsync(output.fileno())


class SourcePhase:
    """<=60 seconds, start/end SHA, <=10 seconds and 128 rows per RO transaction.

    The evidence SHA refers to a root-reviewed offline/no-writer retention
    record. This class cannot infer that record from a chmod/stat bit. Existing
    writers, unreliable storage or a missing offline guarantee are unsupported.
    Caller must also enforce an outer process deadline for stalled kernel IO.
    """
    def __init__(self,source,*,plan=None,seconds=MAX_PHASE_SECONDS):
        check(type(source) is RetainedSource,'trusted_retained_source_required')
        check(type(seconds) in (int,float) and 0<seconds<=MAX_PHASE_SECONDS,'phase_deadline_invalid')
        self.source=source;self.plan=plan;self.seconds=seconds;self.proven_complete=False;self._file=None
        self._entered=False;self.hash_passes=0

    def tick(self):
        check(self._entered and time.monotonic()<self.deadline,'source_phase_deadline')
        check(self._identity()==self.identity,'retained_source_changed')
        fd=os.fstat(self._file.fileno())
        check((fd.st_dev,fd.st_ino,fd.st_size,fd.st_mtime_ns)==self.identity,'retained_descriptor_changed')

    def _identity(self):
        p=self.source.path
        check(p.resolve()==p,'retained_path_changed')
        for suffix in ('-wal','-shm','-journal'):
            check(not Path(str(p)+suffix).exists(),'retained_sidecar_present')
        v=p.lstat()
        check(stat.S_ISREG(v.st_mode) and v.st_nlink==1,'retained_regular_single_link_required')
        check(v.st_mode & 0o222==0,'retained_source_not_readonly')
        check(0<v.st_size<=MAX_SOURCE_BYTES,'retained_source_size_budget')
        return (v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns)

    def _hash(self):
        self.tick();self._file.seek(0);h=hashlib.sha256();total=0
        while True:
            self.tick();block=self._file.read(1024*1024)
            if not block:break
            total+=len(block);check(total<=MAX_SOURCE_BYTES,'retained_source_size_budget');h.update(block)
        self.tick();check(total==self.identity[2] and h.hexdigest()==self.source.sha256,'retained_digest_changed')
        self.hash_passes+=1

    def __enter__(self):
        check(not self._entered,'source_phase_not_reopenable');self._entered=True
        self.deadline=time.monotonic()+self.seconds
        try:
            check(hashlib.sha256(Path(S.__file__).read_bytes()).hexdigest()==SOURCE_SHA,'frozen_source_changed')
            self.identity=self._identity();self._file=self.source.path.open('rb')
            header=self._file.read(100)
            check(len(header)==100 and header[:16]==b'SQLite format 3\x00','retained_sqlite_header_required')
            # mode=ro may still create WAL/SHM. Reject BEFORE SQLite is opened;
            # never change journal mode, silently set immutable, or delete files.
            check(header[18:20]==b'\x01\x01','retained_wal_header_unsupported')
            self._hash()
            self.binding=dict(namespace=self.source.namespace,file_sha256=self.source.sha256,
                stability_evidence_sha256=self.source.stability_evidence_sha256,
                path_sha256=hashlib.sha256(os.fsencode(str(self.source.path))).hexdigest(),identity=list(self.identity))
            self._plan_document=self.plan.header if self.plan is not None else None
            if self.plan is not None:check(self.binding==self._plan_document['source'],'retained_binding_changed')
            return self
        except BaseException:
            if self._file:self._file.close()
            raise

    def __exit__(self,kind,value,traceback):
        try:
            if kind is None:self._hash();self.proven_complete=True
        finally:
            if self._file:self._file.close()
        return False

    def page(self,table,after,ordinal,projects):
        """Explicit fresh range entry; private cursor belongs only to this reader.

        It is safe only under the retained-file phase, never callable for an
        active DB. Frozen Snapshot budgets, authorizer, schema, length limits,
        timer, storage-type witness and actual close remain in force.
        """
        self.tick();check(table in M.TABLES,'unsupported_source_table')
        check(after is None or type(after) is int and M.INT64_MIN<=after<=M.INT64_MAX,'range_boundary_invalid')
        duration=min(10.0,self.deadline-time.monotonic());check(duration>0,'source_phase_deadline')
        try:
            with S.Snapshot(S.SourceRef(self.source.namespace,self.source.path),snapshot_seconds=duration) as reader:
                cursor=None
                if after is not None:
                    # New object capability, not reconstructed/serialized old cursor.
                    cursor=S.Cursor(reader._token,table,after);reader._cursors[table]=cursor
                page=reader.page(table,cursor)
                args=reader.chunk_arguments(page) if page.rows else None
                if args:
                    prepare_chunk(table,args['rows'],projects,args['expected_input_hash'])
                    where='' if after is None else ' WHERE _rowid_>?'
                    params=(len(page.rows),) if after is None else (after,len(page.rows))
                    positions=reader._query('SELECT _rowid_ FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT ?',params)
                    check(len(positions)==len(page.rows),'source_range_changed');end=positions[-1][0]
                else:end=after
                sequence=reader.sequence_evidence();schema_hash=reader.schema_hash
                spec=dict(ordinal=ordinal,table=table,chunk_key='p'+str(ordinal).zfill(6),after=after,end=end,
                    exhausted=page.exhausted,row_count=len(page.rows),native_hash=page.source_digest,
                    input_hash=args['expected_input_hash'] if args else None,
                    input_bytes=len(M.canonical(args['rows'])) if args else 0)
                reader._unchanged();reader._live();reader._close('retained_range_closed')
                check(reader.report.get('connection_close_confirmed') is True and
                    reader.report['reason']=='retained_range_closed','source_range_close_incomplete')
            self.tick()
            if args:args['chunk_key']=spec['chunk_key']
            return spec,args,sequence,schema_hash
        except S.SourceIncomplete as exc:raise M.ImportError(exc.code) from None

    def read(self,spec):
        check(self.plan is not None,'source_plan_required')
        doc=self._plan_document
        actual,args,sequence,schema=self.page(spec['table'],spec['after'],spec['ordinal'],dict(doc['project_mapping']))
        check(actual==spec and schema==doc['source_schema_hash'] and sequence==doc['sequence'],'source_page_changed')
        return args


class Plan:
    def __init__(self,document):
        self._raw=M.canonical(document);check(len(self._raw)<=MAX_MANIFEST_BYTES,'manifest_byte_budget')
        self.sha256=hashlib.sha256(self._raw).hexdigest();self._validate()
        # Only bounded metadata is retained. Runtime page access parses one
        # small descriptor, not the entire manifest on every transaction.
        d=self.document;self._page_data=tuple(M.canonical(p) for p in d.pop('pages'));self._header=M.canonical(d)
    @property
    def document(self):return json.loads(self._raw)
    @property
    def header(self):return json.loads(self._header)
    @property
    def pages(self):return tuple(json.loads(p) for p in self._page_data)
    @property
    def page_count(self):return len(self._page_data)
    def page(self,index):
        check(type(index) is int and 0<=index<self.page_count,'plan_page_required')
        return json.loads(self._page_data[index])
    @property
    def counts(self):return self.header['counts']
    @property
    def highwaters(self):
        d=self.header
        maxima=dict(self._allocation_maxima)
        return {t:A.resolved_highwater(e,d['counts'][t],maxima[t]) for t,e in d['sequence'].items()}
    @property
    def run_id(self):return self.header['run_id']
    @property
    def project_mapping(self):return tuple(tuple(p) for p in self.header['project_mapping'])
    @property
    def manifest(self):
        d=self.header
        return dict(format=d['format'],manifest_sha256=self.sha256,run_id=d['run_id'],source=d['source'],
            source_schema_hash=d['source_schema_hash'],expected_counts=d['counts'],source_sequence=d['sequence'],
            project_mapping=d['project_mapping'],input_bytes=d['input_bytes'],pages=self.page_count)

    def _validate(self):
        d=self.document;M.uuid_text(d['run_id'])
        check(d['format']=='history-retained-six-v1' and d['sqlite_snapshot_token_resumable'] is False,'manifest_format_invalid')
        check(d['complete'] is True and set(d['counts'])==set(M.TABLES),'manifest_incomplete')
        pages=d['pages'];check(0<len(pages)<=MAX_PAGES,'manifest_page_budget')
        counts={t:0 for t in M.TABLES};previous={};ended=set();last_table=-1;total=0
        for index,p in enumerate(pages):
            table=p['table'];check(table in M.TABLES and p['ordinal']==index and p['chunk_key']=='p'+str(index).zfill(6),'manifest_page_order')
            ti=M.TABLES.index(table);check(ti>=last_table and table not in ended,'manifest_table_order');last_table=ti
            check(p['after']==previous.get(table) and type(p['row_count']) is int and 0<=p['row_count']<=128,'manifest_range_invalid')
            check(type(p['exhausted']) is bool and HASH.fullmatch(p['native_hash']),'manifest_range_invalid')
            if p['row_count']:
                check(type(p['end']) is int and (p['after'] is None or p['end']>p['after']) and
                    HASH.fullmatch(p['input_hash']) and 0<p['input_bytes']<=4*1024**2,'manifest_range_invalid')
            else:check(p['exhausted'] and p['end']==p['after'] and p['input_hash'] is None and p['input_bytes']==0,'manifest_empty_invalid')
            previous[table]=p['end'];counts[table]+=p['row_count'];total+=p['input_bytes']
            if p['exhausted']:ended.add(table)
        check(ended==set(M.TABLES) and counts==d['counts'] and total==d['input_bytes'],'manifest_totals_invalid')
        check(sum(counts.values())<=MAX_ROWS and total<=MAX_INPUT_BYTES,'manifest_total_budget')
        check(set(d['sequence'])==set(S.AUTO),'manifest_sequence_invalid')
        projects=d['project_mapping'];check(type(projects) is list and len(dict(projects))==len(projects),'project_mapping_invalid')
        for name,value in projects:check(type(name) is str and name and '\x00' not in name,'project_mapping_invalid');M.uuid_text(value)
        check(len(set(dict(projects).values()))==len(projects),'duplicate_project_uuid')
        maxima={t:max(0,previous[t] or 0) for t in S.AUTO}
        self._allocation_maxima=tuple(maxima.items())
        expected=all(A.ready(e,counts[t],maxima[t]) for t,e in d['sequence'].items())
        check(type(d['highwaters_ready']) is bool and (d['highwaters_ready']==expected or
            d['highwaters_ready'] is False and not A.legacy_ready(d['sequence'],maxima)),
            'manifest_sequence_readiness_invalid')

    def require_importable(self):
        check(self.header['highwaters_ready'],'source_highwaters_unresolved')


def load_plan(path,expected_sha256):
    p=Path(path);check(p.stat().st_size<=MAX_MANIFEST_BYTES,'manifest_byte_budget');raw=p.read_bytes()
    check(type(expected_sha256) is str and hashlib.sha256(raw).hexdigest()==expected_sha256,'manifest_digest_changed')
    try:plan=Plan(json.loads(raw))
    except (KeyError,TypeError,ValueError):raise M.ImportError('manifest_structure_invalid') from None
    check(plan.sha256==expected_sha256,'manifest_not_canonical');return plan


def plan_source(source,path,*,run_id,project_mapping=(),max_rows=MAX_ROWS,max_pages=MAX_PAGES,max_input_bytes=MAX_INPUT_BYTES,seconds=60):
    """Once-only metadata plan, no business-row spool or automatic source copy."""
    path=Path(path);M.uuid_text(run_id)
    for value,maximum in ((max_rows,MAX_ROWS),(max_pages,MAX_PAGES),(max_input_bytes,MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=maximum,'plan_budget_invalid')
    _private_write(str(path)+'.started',dict(phase='planning',complete=False,source_sha256=source.sha256))
    try:
        pages=[];counts={t:0 for t in M.TABLES};total=0;sequence=None;schema=None;metadata_bytes=0;maxima={t:0 for t in S.AUTO}
        with SourcePhase(source,seconds=seconds) as phase:
            for table in M.TABLES:
                after=None
                while True:
                    page,args,seq,sh=phase.page(table,after,len(pages),dict(project_mapping))
                    if sequence is None:sequence=seq;schema=sh
                    check(sequence==seq and schema==sh,'source_metadata_changed')
                    pages.append(page);counts[table]+=page['row_count'];total+=page['input_bytes']
                    if table in maxima:maxima[table]=max(maxima[table],page['end'] or 0)
                    check(sum(counts.values())<=max_rows,'plan_row_budget');check(len(pages)<=max_pages,'plan_page_budget')
                    check(total<=max_input_bytes,'plan_input_byte_budget')
                    metadata_bytes+=len(M.canonical(page))+1
                    check(metadata_bytes<=MAX_MANIFEST_BYTES-32768,'manifest_byte_budget')
                    if page['exhausted']:break
                    after=page['end']
        ready=all(A.ready(e,counts[t],maxima[t]) for t,e in sequence.items())
        plan=Plan(dict(format='history-retained-six-v1',run_id=run_id,source=phase.binding,
            source_schema_hash=schema,sequence=sequence,highwaters_ready=ready,project_mapping=list(project_mapping),
            counts=counts,input_bytes=total,pages=pages,complete=True,sqlite_snapshot_token_resumable=False))
        _private_write(path,plan.document);return plan
    except BaseException as exc:
        code=exc.code if isinstance(exc,M.ImportError) else 'source_plan_failed'
        _private_write(str(path)+'.failed',dict(complete=False,code=code));raise
