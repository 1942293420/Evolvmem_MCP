"""Three fixed governance tables from an explicit, already sealed 16-table source.

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

from . import governance_mapping as M
from . import sqlite_source as S
from . import static_wal as W
from .source_page_budget import bounded_page
from . import governance_large_row as L

check=M.check
for name,pin in ((S.__file__,W.SOURCE_SHA),
                 (W.__file__,'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076')):
    check(hashlib.sha256(Path(name).read_bytes()).hexdigest()==pin,'frozen_source_dependency_changed')

# This proxy is private to chunk_arguments. Never change the frozen S module,
# the governance mapper or the public final hash's requirement for source_rowid.
_visible_mapper=SimpleNamespace(**{**vars(M),'chunk_hash':M.source_chunk_hash})

def _three(method,*,mapper=M):
    namespace=dict(method.__globals__,M=mapper,TABLES=M.TABLES,AUTO=(),MAX_PAGE_BYTES=L.PAGE_BYTES)
    fn=FunctionType(method.__code__,namespace,method.__name__,method.__defaults__,method.__closure__)
    fn.__kwdefaults__=method.__kwdefaults__;return fn

_base_authorize=_three(S.Snapshot._authorize)
_base_verify=_three(S.Snapshot._verify_schema)


class Snapshot(S.Snapshot):
    _init_three=_three(S.Snapshot.__init__)
    _close=_three(S.Snapshot._close)
    _base_page=_three(S.Snapshot._page)
    def _read_sequences(self):
        found=self._query('SELECT name,seq,typeof(seq) FROM sqlite_sequence WHERE name IN ('+','.join('?' for _ in M.AUTO)+') LIMIT 5',tuple(M.AUTO))
        evidence={t:dict(present=False,value=None) for t in M.AUTO}
        for name,value,kind in found:
            S.require(name in M.applicable(self.source.namespace) and not evidence[name]['present'] and type(value) is int and 0<=value<=M.INT64_MAX and kind=='integer','governance_sequence_invalid')
            evidence[name]=dict(present=True,value=value)
        self._sequences=evidence
    chunk_arguments=_three(S.Snapshot.chunk_arguments,mapper=_visible_mapper)
    finish=_three(S.Snapshot.finish)

    def __init__(self,phase,**limits):
        check(type(phase) is Phase and phase.base._entry_verified and not phase.base._finished,'sealed_governance_phase_required')
        self.phase=phase;phase.base._check_sidecars()
        limits.setdefault("page_bytes",L.PAGE_BYTES)
        self._init_three(S.SourceRef(phase.source.namespace,phase.source.path),**limits)

    def _unchanged(self):
        super()._unchanged();self.phase.base._check_sidecars()

    def _page(self,table,cursor):
        return bounded_page(self,'governance',table,cursor,self._base_page)

    def _authorize(self,action,first,second,db,origin):
        if action==sqlite3.SQLITE_FUNCTION and second=='length':return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA and first=='encoding' and second is None:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA:
            if first in ('foreign_key_list','index_list') and second in M.TABLES:return sqlite3.SQLITE_OK
            names={i['name'] for t in M.applicable(self.source.namespace) for i in M._EVIDENCE['namespaces'][self.source.namespace][t]['indexes']}
            if first=='index_info' and second in names:return sqlite3.SQLITE_OK
        return _base_authorize(self,action,first,second,db,origin)

    def _verify_schema(self):
        present=M.applicable(self.source.namespace)
        for table in M.TABLES:
            if table not in present:S.require(self._query('SELECT 1 FROM sqlite_schema WHERE name=?',(table,))==[],'governance_absent_table_appeared')
        fn=FunctionType(S.Snapshot._verify_schema.__code__,dict(S.Snapshot._verify_schema.__globals__,M=M,TABLES=present),S.Snapshot._verify_schema.__name__)
        fn(self)
        for table in M.applicable(self.source.namespace):
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
        self.report.update(source_schema_hash=self.schema_hash,scope='reviewed_three_governance_tables_only')

    def __enter__(self):
        S.require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            self._reviewed=deepcopy(M._EVIDENCE['namespaces'][self.source.namespace])
            for t,e in self._reviewed.items():e.update(autoincrement=t in M.AUTO,rowid_alias=M.ALIASES.get(t))
            self._before=S._files(self.source.path)
            connection=sqlite3.connect(self.source.path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=.1,isolation_level=None,check_same_thread=False)
            with self._lock:
                if self._closed:connection.close();raise S.SourceIncomplete('snapshot_lifetime_exceeded')
                self._connection=connection;S.require(hasattr(connection,'setlimit'),'sqlite_length_limit_required')
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,L.SQLITE_BYTES);connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536)
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
        check(type(source) is W.SealedWalSource,'sealed_governance_source_required')
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

    def page(self,table,after,ordinal,*,_page_rows=S.MAX_PAGE_ROWS):
        self.tick();check(table in M.TABLES,'unsupported_governance_table')
        check(type(ordinal) is int and 0<=ordinal<4096,'governance_page_ordinal_invalid')
        check(after is None or type(after) is int and M.INT64_MIN<=after<=M.INT64_MAX,'source_range_invalid')
        duration=min(10.0,self.base.deadline-time.monotonic());check(duration>0,'source_phase_deadline')
        try:
            with Snapshot(self,snapshot_seconds=duration,page_rows=_page_rows) as reader:
                cursor=None
                if after is not None:cursor=S.Cursor(reader._token,table,after);reader._cursors[table]=cursor
                absent=table not in M.applicable(self.source.namespace)
                check(not absent or after is None,'governance_absent_table_cursor')
                page=S.Page(table,'absent',(),(),hashlib.sha256(b'').hexdigest(),0,True,None) if absent else reader.page(table,cursor)
                args=reader.chunk_arguments(page) if page.rows else None
                if args:
                    where='' if after is None else ' WHERE _rowid_>?'
                    params=(len(page.rows),) if after is None else (after,len(page.rows))
                    ids=reader._query('SELECT _rowid_ FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT ?',params)
                    check(len(ids)==len(page.rows) and all(type(r[0]) is int for r in ids),'source_range_changed')
                    check(all(ids[i][0]<ids[i+1][0] for i in range(len(ids)-1)) and (after is None or ids[0][0]>after),'source_range_changed')
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
                reader._unchanged();reader._live();reader._close('sealed_governance_range_closed')
                check(reader.report.get('connection_close_confirmed') is True and reader.report['reason']=='sealed_governance_range_closed','source_range_close_incomplete')
            self.tick()
            if args:args['chunk_key']=spec['chunk_key']
            return spec,args,seq,schema
        except S.SourceIncomplete as exc:raise M.ImportError(exc.code) from None

    def read(self,page):
        actual,args,seq,schema=self.page(page['table'],page['after'],page['ordinal'],_page_rows=max(1,page['row_count']))
        check(self.plan is not None and actual==page and seq==self.plan.header['sequence_evidence'] and schema==self.plan.header['source_schema_hash'],'source_page_changed')
        return args
