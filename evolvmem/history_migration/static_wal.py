"""Explicit controlled-retained-copy reader; never an OS immutability claim.

Only a root-reviewed offline/no-writer receipt permits the immutable SQLite
URI. Live paths/inodes, nonempty WAL, identity/content drift and ordinary
SourceRef values are rejected. No chmod/checkpoint/header/sidecar mutation.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from . import retained_chain as R
from . import resume_planning as P
from . import autoincrement_state as A
from . import sqlite_source as S
from .history_core import mapping as M
from .history_core.importer import prepare_chunk

STAT_FIELDS=('dev','ino','size','mtime_ns','ctime_ns','mode','nlink')
CHAIN_SHA='fd6b901642ff867443398a18c215b05818dfb4db895b34f56299e5971ec7d9bf'
SOURCE_SHA=R.SOURCE_SHA
MAX_SOURCE_BYTES=R.MAX_SOURCE_BYTES
MAX_MANIFEST_BYTES=R.MAX_MANIFEST_BYTES
MAX_ROWS=R.MAX_ROWS
MAX_PAGES=R.MAX_PAGES
MAX_INPUT_BYTES=R.MAX_INPUT_BYTES
Plan=R.Plan
load_plan=R.load_plan
_private_write=R._private_write
check=M.check
# Frozen Snapshot entry uses these exact original helpers/constants.
require=S.require
SourceIncomplete=S.SourceIncomplete
SCHEMA_SHA=S.SCHEMA_SHA
CORE_MAPPING_SHA=S.CORE_MAPPING_SHA
CORE_SCHEMA_SHA=S.CORE_SCHEMA_SHA
CORE_SCHEMA=S.CORE_SCHEMA
MAX_PAGE_BYTES=S.MAX_PAGE_BYTES
_text=S._text
_files=S._files


def _stat_info(info):return {name:getattr(info,'st_'+name) for name in STAT_FIELDS}


def _active_identities():
    """Fixed production paths: stat ONLY, never open SQLite/read their contents."""
    result=set()
    for path in S.FIXED_PATHS.values():
        try:v=Path(path).stat()
        except OSError:raise M.ImportError('active_identity_unavailable') from None
        check(stat.S_ISREG(v.st_mode),'active_identity_unavailable');result.add((v.st_dev,v.st_ino))
    return result


@dataclass(frozen=True)
class SealedWalSource:
    _raw:bytes
    receipt_sha256:str
    def __post_init__(self):
        check(type(self._raw) is bytes and len(self._raw)<=65536,'seal_receipt_budget')
        check(type(self.receipt_sha256) is str and hashlib.sha256(self._raw).hexdigest()==self.receipt_sha256,'seal_receipt_digest_changed')
        def pairs(entries):
            out={}
            for key,value in entries:
                check(key not in out,'seal_duplicate_key');out[key]=value
            return out
        try:d=json.loads(self._raw,object_pairs_hook=pairs)
        except (TypeError,ValueError):raise M.ImportError('seal_receipt_invalid') from None
        check(type(d) is dict and set(d)=={'format','namespace','path','basis','os_immutable_proven','retention_evidence_sha256','files'},'seal_receipt_invalid')
        offline=d['format']=='controlled-retained-db-only-v1'
        check(d['format'] in ('controlled-retained-empty-wal-v1','controlled-retained-db-only-v1')
            and d['basis']=='controlled_retained_copy_no_writers'
            and d['os_immutable_proven'] is False,'operational_seal_required')
        check(d['namespace'] in S.FIXED_PATHS and type(d['path']) is str and Path(d['path']).is_absolute(),'seal_path_required')
        check(Path(d['path']).as_posix() not in S.FIXED_PATHS.values(),'active_source_path')
        check(type(d['retention_evidence_sha256']) is str and R.HASH.fullmatch(d['retention_evidence_sha256']),'retention_evidence_required')
        check(type(d['files']) is dict and set(d['files'])==({'db'} if offline else {'db','wal','shm'}),
              'sealed_offline_file_required' if offline else 'sealed_three_files_required')
        for item in d['files'].values():
            check(type(item) is dict and set(item)=={'stat','sha256'} and type(item['sha256']) is str and R.HASH.fullmatch(item['sha256']),'sealed_file_evidence_invalid')
            s=item['stat'];check(type(s) is dict and set(s)==set(STAT_FIELDS) and all(type(v) is int and v>=0 for v in s.values()),'sealed_stat_invalid')
            check(stat.S_ISREG(s['mode']) and s['nlink']==1,'sealed_regular_single_link_required')
        check(100<=d['files']['db']['stat']['size']<=MAX_SOURCE_BYTES,'sealed_source_size_budget')
        check(d['files']['db']['stat']['mode'] & 0o222==0,'sealed_source_not_readonly')
        if not offline:
            check(d['files']['wal']['stat']['size']==0 and d['files']['wal']['sha256']==hashlib.sha256(b'').hexdigest(),'sealed_empty_wal_required')
            check(d['files']['shm']['stat']['size']==32768,'sealed_shm_size_unsupported')
    @classmethod
    def from_receipt(cls,raw,expected_sha256):return cls(raw,expected_sha256)
    @property
    def document(self):return json.loads(self._raw)
    @property
    def offline(self):return self.document['format']=='controlled-retained-db-only-v1'
    @property
    def namespace(self):return self.document['namespace']
    @property
    def path(self):return Path(self.document['path'])
    @property
    def sha256(self):return self.document['files']['db']['sha256']
    @property
    def stability_evidence_sha256(self):return self.document['retention_evidence_sha256']


class SealedPhase(R.SourcePhase):
    def __init__(self,source,*,plan=None,seconds=60):
        check(type(source) is SealedWalSource,'explicit_sealed_wal_source_required')
        check(type(seconds) in (int,float) and 0<seconds<=60,'phase_deadline_invalid')
        self.source=source;self.plan=plan;self.seconds=seconds;self.proven_complete=False;self._file=None
        self._entered=False;self._finished=False;self.hash_passes=0;self._entry_verified=False
        self._expected=source.document['files'];self._owner=(os.getpid(),threading.get_ident())

    def _identities(self):
        p=self.source.path
        check(p.resolve()==p,'sealed_path_changed');check(not Path(str(p)+'-journal').exists(),'sealed_journal_present')
        active=_active_identities();observed={}
        if self.source.offline:
            check(all(not Path(str(p)+suffix).exists() for suffix in ('-wal','-shm','-journal')),
                  'offline_sidecar_present')
        files=(('db',p),) if self.source.offline else (('db',p),('wal',Path(str(p)+'-wal')),('shm',Path(str(p)+'-shm')))
        for name,path in files:
            try:info=path.lstat()
            except OSError:raise M.ImportError('sealed_file_missing') from None
            check(path.resolve()==path and stat.S_ISREG(info.st_mode) and info.st_nlink==1,'sealed_regular_single_link_required')
            check((info.st_dev,info.st_ino) not in active,'active_source_inode')
            observed[name]=_stat_info(info)
            check(observed[name]==self._expected[name]['stat'],'sealed_identity_changed')
        return observed

    def tick(self):
        check(self._entered and not self._finished,'sealed_phase_closed')
        check(self._owner==(os.getpid(),threading.get_ident()),'sealed_phase_owner_changed')
        check(time.monotonic()<self.deadline,'source_phase_deadline')
        self._identities()
        if self._file is not None:check(_stat_info(os.fstat(self._file.fileno()))==self._expected['db']['stat'],'sealed_descriptor_changed')

    def _check_sidecars(self):
        self.tick()
        if self.source.offline:return
        for name in ('wal','shm'):
            path=Path(str(self.source.path)+'-'+name)
            with path.open('rb') as f:
                check(_stat_info(os.fstat(f.fileno()))==self._expected[name]['stat'],'sealed_descriptor_changed')
                data=f.read(32769)
                check(len(data)==self._expected[name]['stat']['size'] and hashlib.sha256(data).hexdigest()==self._expected[name]['sha256'],
                    'sealed_sidecar_digest_changed')
        self.tick()

    def __enter__(self):
        check(not self._entered,'source_phase_not_reopenable');self._entered=True;self.deadline=time.monotonic()+self.seconds
        try:
            check(hashlib.sha256(Path(R.__file__).read_bytes()).hexdigest()==CHAIN_SHA,'frozen_chain_changed')
            check(hashlib.sha256(Path(S.__file__).read_bytes()).hexdigest()==SOURCE_SHA,'frozen_source_changed')
            self._identities();self.identity=tuple(self._expected['db']['stat'][k] for k in STAT_FIELDS)
            self._file=self.source.path.open('rb');self.tick();header=self._file.read(100)
            valid_header=header[18:20] in (b'\x01\x01',b'\x02\x02') if self.source.offline else header[18:20]==b'\x02\x02'
            check(len(header)==100 and header[:16]==b'SQLite format 3\x00' and valid_header,'sealed_wal_header_required')
            self._check_sidecars();self._hash();self._check_sidecars();self._entry_verified=True
            self.binding=dict(namespace=self.source.namespace,file_sha256=self.source.sha256,
                stability_evidence_sha256=self.source.stability_evidence_sha256,
                path_sha256=hashlib.sha256(os.fsencode(str(self.source.path))).hexdigest(),
                source_kind='controlled_retained_db_only' if self.source.offline else 'controlled_retained_empty_wal',
                seal_receipt_sha256=self.source.receipt_sha256,
                files=self._expected,os_immutable_proven=False)
            self._plan_document=self.plan.header if self.plan is not None else None
            if self.plan is not None:check(self.binding==self._plan_document['source'],'retained_binding_changed')
            return self
        except BaseException:
            if self._file:self._file.close()
            self._finished=True;raise

    def __exit__(self,kind,value,traceback):
        complete=False
        try:
            if kind is None:self._check_sidecars();self._hash();self._check_sidecars();complete=True
        finally:
            self._finished=True
            if self._file:self._file.close()
        self.proven_complete=complete
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
            with ImmutableSnapshot(self,snapshot_seconds=duration) as reader:
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

class ImmutableSnapshot(S.Snapshot):
    """Dedicated sealed-ref entry. Ordinary frozen SourceRef cannot opt in."""
    def __init__(self,phase,**limits):
        check(type(phase) is SealedPhase and phase._entry_verified,'sealed_phase_proof_required')
        phase._check_sidecars();self.phase=phase
        super().__init__(S.SourceRef(phase.source.namespace,phase.source.path),**limits)
    def _unchanged(self):
        super()._unchanged();self.phase._check_sidecars()

    def __enter__(self):
        require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            catalog=Path(S.__file__).parent/'resources/core_reviewed_schema.json'
            require(hashlib.sha256(catalog.read_bytes()).hexdigest()==SCHEMA_SHA,'reviewed_schema_changed')
            require(hashlib.sha256(Path(M.__file__).read_bytes()).hexdigest()==CORE_MAPPING_SHA,'frozen_mapping_changed')
            require(Path(CORE_SCHEMA.__file__).parent==Path(M.__file__).parent
                and hashlib.sha256(Path(CORE_SCHEMA.__file__).read_bytes()).hexdigest()==CORE_SCHEMA_SHA
                and CORE_SCHEMA.SPECS is M.SPECS,'frozen_schema_changed')
            self._reviewed=json.loads(catalog.read_text(encoding='utf8'))['namespaces'][self.source.namespace]
            require(self.source.path.resolve()==self.source.path,'source_path_identity_changed')
            self._before=_files(self.source.path)
            connection=sqlite3.connect(self.source.path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=.1,
                isolation_level=None,check_same_thread=False)
            with self._lock:
                if self._closed:connection.close();raise SourceIncomplete('snapshot_lifetime_exceeded')
                self._connection=connection
                require(hasattr(connection,'setlimit'),'sqlite_length_limit_required')
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_PAGE_BYTES)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536)
                connection.text_factory=_text
                connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA busy_timeout=100')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.set_authorizer(self._authorize);connection.set_progress_handler(self._progress,200)
            self._query('BEGIN')
            self._schema_version=self._query('PRAGMA schema_version')[0][0]
            self._data_version=self._query('PRAGMA data_version')[0][0]
            require(self._query('PRAGMA query_only')==[(1,)],'readonly_connection_required')
            self._verify_schema()
            self._read_sequences()
            self._unchanged();return self
        except SourceIncomplete as exc:self._fail(exc.code)
        except Exception:self._fail('source_open_failed')
        except BaseException:
            self._close('source_interrupted');raise


def plan_source(source,path,*,run_id,project_mapping=(),max_rows=MAX_ROWS,max_pages=MAX_PAGES,max_input_bytes=MAX_INPUT_BYTES,seconds=60,planning_binding=None):
    """Bounded resumable metadata plan; no business-row spool or source copy."""
    path=Path(path);M.uuid_text(run_id)
    for value,maximum in ((max_rows,MAX_ROWS),(max_pages,MAX_PAGES),(max_input_bytes,MAX_INPUT_BYTES)):
        check(type(value) is int and 0<value<=maximum,'plan_budget_invalid')
    if not Path(str(path)+'.started').exists():_private_write(str(path)+'.started',dict(phase='planning',complete=False,source_sha256=source.sha256))
    try:
        def read(phase,table,after,ordinal):
            page,args,seq,schema=phase.page(table,after,ordinal,dict(project_mapping))
            return page,seq,schema
        result=P.scan(source,path,run_id=run_id,domain='core',tables=M.TABLES,count_tables=M.TABLES,
            phase_factory=lambda **kw:SealedPhase(source,**kw),read_page=read,
            binding=dict(project_mapping=list(project_mapping),operator_binding=planning_binding),seconds=seconds,
            max_rows=max_rows,max_pages=max_pages,max_input_bytes=max_input_bytes)
        if isinstance(result,P.PlanningProgress):return result
        pages,counts,total=result.pages,result.counts,result.input_bytes
        sequence,schema,maxima=result.sequence,result.schema,result.maxima
        ready=all(A.ready(e,counts[t],maxima[t]) for t,e in sequence.items())
        plan=Plan(dict(format='history-retained-six-v1',run_id=run_id,source=result.source,
            source_schema_hash=schema,sequence=sequence,highwaters_ready=ready,project_mapping=list(project_mapping),
            counts=counts,input_bytes=total,pages=pages,complete=True,sqlite_snapshot_token_resumable=False))
        _private_write(path,plan.document);return plan
    except BaseException as exc:
        code=exc.code if isinstance(exc,M.ImportError) else 'source_plan_failed'
        if not Path(str(path)+'.failed').exists():_private_write(str(path)+'.failed',dict(complete=False,code=code))
        raise
