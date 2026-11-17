"""Three fixed capture tables from an explicit, already sealed 16-table source.

Private globals specialize the frozen Snapshot's exact code objects. Only its
owned-page export uses a visible-column mapper view; the second bounded query
binds source_rowid in that SAME live transaction before the final chunk hash.
Snapshot cursors never survive a transaction. A Phase range descriptor is not a
snapshot token: reopening is allowed only under the original sealed-source
entry/exit hashes and identity checks. All pages remain provisional until exit.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3
import threading
import time
from types import FunctionType,SimpleNamespace

from . import capture_mapping as M
from . import sqlite_source as S
from . import static_wal as W

from .canonical_page_prefix import canonical_prefix_rows

check=M.check
for name,pin in ((S.__file__,W.SOURCE_SHA),
                 (W.__file__,'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076')):
    check(hashlib.sha256(Path(name).read_bytes()).hexdigest()==pin,'frozen_source_dependency_changed')

# This proxy is private to chunk_arguments. Never change the frozen S module,
# the capture mapper or the public final hash's requirement for source_rowid.
_visible_mapper=SimpleNamespace(**{**vars(M),'chunk_hash':M.source_chunk_hash})

def _three(method,*,mapper=M):
    namespace=dict(method.__globals__,M=mapper,TABLES=M.TABLES,AUTO=M.AUTO)
    fn=FunctionType(method.__code__,namespace,method.__name__,method.__defaults__,method.__closure__)
    fn.__kwdefaults__=method.__kwdefaults__;return fn

_base_authorize=_three(S.Snapshot._authorize)
_base_verify=_three(S.Snapshot._verify_schema)


class Snapshot(S.Snapshot):
    _init_three=_three(S.Snapshot.__init__)
    _close=_three(S.Snapshot._close)
    _page=_three(S.Snapshot._page)
    _read_sequences=_three(S.Snapshot._read_sequences)
    chunk_arguments=_three(S.Snapshot.chunk_arguments,mapper=_visible_mapper)
    finish=_three(S.Snapshot.finish)

    def __init__(self,phase,**limits):
        check(type(phase) is Phase and phase.base._entry_verified and not phase.base._finished,'sealed_capture_phase_required')
        self.phase=phase;phase.base._check_sidecars()
        self._init_three(S.SourceRef(phase.source.namespace,phase.source.path),**limits)

    def _unchanged(self):
        super()._unchanged();self.phase.base._check_sidecars()

    def _authorize(self,action,first,second,db,origin):
        if action==sqlite3.SQLITE_PRAGMA:
            if first in ('foreign_key_list','index_list') and second in M.TABLES:return sqlite3.SQLITE_OK
            names={i['name'] for t in M.TABLES for i in M._EVIDENCE['namespaces'][self.source.namespace][t]['indexes']}
            if first=='index_info' and second in names:return sqlite3.SQLITE_OK
        return _base_authorize(self,action,first,second,db,origin)

    def _verify_schema(self):
        _base_verify(self)
        for table in M.TABLES:
            e=M._EVIDENCE['namespaces'][self.source.namespace][table]
            fk=[(r['id'],r['sequence'],r['target_table'],r['from_column'],r['to_column'],r['on_update'],r['on_delete'],r['match']) for r in e['foreign_keys']]
            S.require(self._query('PRAGMA foreign_key_list("'+table+'")')==fk,'source_fk_changed')
            indexes=[(i['sequence'],i['name'],int(i['unique']),i['origin'],int(i['partial'])) for i in e['indexes']]
            S.require(self._query('PRAGMA index_list("'+table+'")')==indexes,'source_indexes_changed')
            for i in e['indexes']:
                S.require(self._query('SELECT sql FROM sqlite_schema WHERE type=\'index\' AND name=?',(i['name'],))==[(i['ddl'],)],'source_index_ddl_changed')
                fields=[(r['sequence'],r['cid'],r['name']) for r in i['columns']]
                S.require(self._query('PRAGMA index_info("'+i['name']+'")')==fields,'source_index_columns_changed')
        self.schema_hash=M.schema_hash(self.source.namespace)
        self.report.update(source_schema_hash=self.schema_hash,scope='reviewed_three_capture_tables_only')

    def __enter__(self):
        S.require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            self._reviewed=deepcopy(M._EVIDENCE['namespaces'][self.source.namespace])
            for e in self._reviewed.values():e.update(autoincrement=False,rowid_alias=None)
            self._before=S._files(self.source.path)
            connection=sqlite3.connect(self.source.path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=.1,isolation_level=None,check_same_thread=False)
            with self._lock:
                if self._closed:connection.close();raise S.SourceIncomplete('snapshot_lifetime_exceeded')
                self._connection=connection;S.require(hasattr(connection,'setlimit'),'sqlite_length_limit_required')
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,S.MAX_PAGE_BYTES);connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536)
                connection.text_factory=S._text
                connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA busy_timeout=100');connection.execute('PRAGMA trusted_schema=OFF')
                connection.set_authorizer(self._authorize);connection.set_progress_handler(self._progress,200)
            self._query('BEGIN');self._schema_version=self._query('PRAGMA schema_version')[0][0];self._data_version=self._query('PRAGMA data_version')[0][0]
            S.require(self._query('PRAGMA query_only')==[(1,)],'readonly_connection_required')
            self._verify_schema();self._read_sequences();self._unchanged();return self
        except S.SourceIncomplete as exc:self._fail(exc.code)
        except Exception:self._fail('source_open_failed')
        except BaseException:self._close('source_interrupted');raise


class Phase:
    def __init__(self,source,*,plan=None,seconds=60):
        check(type(source) is W.SealedWalSource,'sealed_capture_source_required')
        M.schema_hash(source.namespace)  # public absence is not an empty dataset.
        self.source=source;self.plan=plan;self.base=W.SealedPhase(source,plan=plan,seconds=seconds)
    def __enter__(self):self.base.__enter__();return self
    def __exit__(self,*args):return self.base.__exit__(*args)
    @property
    def proven_complete(self):return self.base.proven_complete
    @property
    def binding(self):return self.base.binding
    @property
    def hash_passes(self):return self.base.hash_passes
    def tick(self):self.base.tick()

    def page(self,table,after,ordinal,*,page_rows=S.MAX_PAGE_ROWS):
        self.tick();check(table in M.TABLES,'unsupported_capture_table')
        check(type(ordinal) is int and 0<=ordinal<4096,'capture_page_ordinal_invalid')
        check(after is None or type(after) is int and M.INT64_MIN<=after<=M.INT64_MAX,'source_range_invalid')
        try:
            while True:
                duration=min(10.0,self.base.deadline-time.monotonic());check(duration>0,'source_phase_deadline')
                with Snapshot(self,snapshot_seconds=duration,page_rows=page_rows) as reader:
                    cursor=None
                    if after is not None:cursor=S.Cursor(reader._token,table,after);reader._cursors[table]=cursor
                    page=reader.page(table,cursor)
                    ids=[]
                    if page.rows:
                        where='' if after is None else ' WHERE _rowid_>?'
                        params=(len(page.rows),) if after is None else (after,len(page.rows))
                        ids=reader._query('SELECT _rowid_ FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT ?',params)
                        check(len(ids)==len(page.rows) and all(type(r[0]) is int for r in ids),'source_range_changed')
                        check(all(ids[i][0]<ids[i+1][0] for i in range(len(ids)-1)) and (after is None or ids[0][0]>after),'source_range_changed')
                        try:selected=canonical_prefix_rows(M,table,page.rows,ids)
                        except M.ImportError:reader._fail('frozen_mapper_value_unsupported')
                        if selected<len(page.rows):
                            if not selected:reader._fail('frozen_chunk_byte_budget')
                            # Reopen the unexported range at the same seek. The
                            # frozen reader derives hashes/cursor/lookahead.
                            page_rows=selected
                            reader._unchanged();reader._live();reader._close('sealed_capture_unexported_repage')
                            check(reader.report.get('connection_close_confirmed') is True,'source_range_close_incomplete')
                            continue
                    args=reader.chunk_arguments(page) if page.rows else None
                    if args:
                        end=ids[-1][0]
                        for row,(rowid,) in zip(args['rows'],ids):row['source_rowid']=rowid
                        _,hashed,_=M.prepare_chunk(table,args['rows'],None)
                        args['expected_input_hash']=hashed
                        # Final rowid-enriched export is independently charged and
                        # bounded. Values/order stay intact; no original row is cut.
                        reader._charge(len(M.canonical(args['rows'])))
                    else:end=after
                    seq=reader.sequence_evidence();schema=reader.schema_hash
                    spec=dict(ordinal=ordinal,table=table,chunk_key='c'+str(ordinal).zfill(6),after=after,end=end,exhausted=page.exhausted,
                        row_count=len(page.rows),native_hash=page.source_digest,input_hash=args['expected_input_hash'] if args else None,
                        input_bytes=len(M.canonical(args['rows'])) if args else 0)
                    reader._unchanged();reader._live();reader._close('sealed_capture_range_closed')
                    check(reader.report.get('connection_close_confirmed') is True and reader.report['reason']=='sealed_capture_range_closed','source_range_close_incomplete')
                self.tick()
                if args:args['chunk_key']=spec['chunk_key']
                return spec,args,seq,schema
        except S.SourceIncomplete as exc:raise M.ImportError(exc.code) from None

    def read(self,page):
        actual,args,seq,schema=self.page(page['table'],page['after'],page['ordinal'],page_rows=max(1,page['row_count']))
        check(self.plan is not None and actual==page and seq=={} and schema==self.plan.header['source_schema_hash'],'source_page_changed')
        return args
