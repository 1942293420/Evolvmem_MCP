"""Eight fixed continuity tables from an explicit, already sealed source containing this namespace's reviewed B6 tables.

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

from . import continuity_mapping as M
from . import sqlite_source as S
from . import static_wal as W
from .source_page_budget import bounded_page

from .canonical_page_prefix import canonical_prefix_rows

check=M.check
for name,pin in ((S.__file__,W.SOURCE_SHA),
                 (W.__file__,'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076')):
    check(hashlib.sha256(Path(name).read_bytes()).hexdigest()==pin,'frozen_source_dependency_changed')

# This proxy is private to chunk_arguments. Never change the frozen S module,
# the continuity mapper or the public final hash's requirement for source_rowid.
_visible_mapper=SimpleNamespace(**{**vars(M),'chunk_hash':M.source_chunk_hash})

def _eight(method,*,mapper=M,auto=M.AUTO):
    namespace=dict(method.__globals__,M=mapper,TABLES=M.TABLES,AUTO=auto)
    fn=FunctionType(method.__code__,namespace,method.__name__,method.__defaults__,method.__closure__)
    fn.__kwdefaults__=method.__kwdefaults__;return fn

_base_authorize=_eight(S.Snapshot._authorize)


class Snapshot(S.Snapshot):
    _init_eight=_eight(S.Snapshot.__init__)
    _close=_eight(S.Snapshot._close)
    _base_page=_eight(S.Snapshot._page,auto=())
    chunk_arguments=_eight(S.Snapshot.chunk_arguments,mapper=_visible_mapper)
    finish=_eight(S.Snapshot.finish)

    def __init__(self,phase,**limits):
        check(type(phase) is Phase and phase.base._entry_verified and not phase.base._finished,'sealed_continuity_phase_required')
        self.phase=phase;phase.base._check_sidecars()
        self._init_eight(S.SourceRef(phase.source.namespace,phase.source.path),**limits)

    def _unchanged(self):
        super()._unchanged();self.phase.base._check_sidecars()

    def _page(self,table,cursor=None):
        page=bounded_page(self,'continuity',table,cursor,self._base_page)
        if table in M.AUTO:
            for row in page.rows:self._max_ids[table]=max(self._max_ids[table],row[M.AUTO[table]])
        return page

    def _authorize(self,action,first,second,db,origin):
        if action==sqlite3.SQLITE_FUNCTION and second=='length':return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA and first=='encoding' and second is None:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA:
            if first in ('foreign_key_list','index_list') and second in M.TABLES:return sqlite3.SQLITE_OK
            names={i['name'] for t in M.applicable(self.source.namespace) for i in M._EVIDENCE['namespaces'][self.source.namespace][t]['indexes']}
            if first=='index_info' and second in names:return sqlite3.SQLITE_OK
        return _base_authorize(self,action,first,second,db,origin)

    def _verify_schema(self):
        validate_schema(self,self.source.namespace)
        self.schema_hash=M.schema_hash(self.source.namespace)
        self.report.update(source_schema_hash=self.schema_hash,scope='reviewed_continuity_tables_only')

    def _read_sequences(self):
        names=M.applicable(self.source.namespace)
        found=self._query('SELECT name,seq,typeof(seq) FROM sqlite_sequence WHERE name IN ('+','.join('?' for _ in names)+') LIMIT 9',names)
        evidence={t:dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,
            allocation_history_verified=False) for t in M.AUTO if t in names}
        seen=set()
        for name,value,kind in found:
            S.require(name in evidence and name not in seen,'continuity_sequence_shape_changed');seen.add(name)
            S.require(type(value) is int and kind=='integer' and M.INT64_MIN<=value<=M.INT64_MAX,'continuity_sequence_invalid')
            evidence[name].update(present=True,value=value,storage_type=kind)
        self._sequences=evidence

    def __enter__(self):
        S.require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            self._reviewed=deepcopy(M._EVIDENCE['namespaces'][self.source.namespace])
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
        check(type(source) is W.SealedWalSource,'sealed_continuity_source_required')
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
        self.tick();check(table in M.applicable(self.source.namespace),'unsupported_continuity_table')
        check(type(ordinal) is int and 0<=ordinal<4096,'continuity_page_ordinal_invalid')
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
                            reader._unchanged();reader._live();reader._close('sealed_continuity_unexported_repage')
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
                    reader._unchanged();reader._live();reader._close('sealed_continuity_range_closed')
                    check(reader.report.get('connection_close_confirmed') is True and reader.report['reason']=='sealed_continuity_range_closed','source_range_close_incomplete')
                self.tick()
                if args:args['chunk_key']=spec['chunk_key']
                return spec,args,seq,schema
        except S.SourceIncomplete as exc:raise M.ImportError(exc.code) from None

    def read(self,page):
        actual,args,seq,schema=self.page(page['table'],page['after'],page['ordinal'],page_rows=max(1,page['row_count']))
        check(self.plan is not None and actual==page and seq==self.plan.header['sequence'] and schema==self.plan.header['source_schema_hash'],'source_page_changed')
        return args


def validate_schema(connection,namespace):
    """Exact columns, DDL, FK and indexes, including reviewed ABSENT tables."""
    expected=M._EVIDENCE['namespaces'][namespace]
    query=connection._query if isinstance(connection,Snapshot) else lambda sql,args=():connection.execute(sql,args).fetchall()
    for table in M.TABLES:
        found=query('SELECT type,sql FROM sqlite_schema WHERE name=?',(table,))
        if table not in expected:
            check(found==[],'continuity_reviewed_absence_changed');continue
        e=expected[table]
        check(found==[('table',e['ddl'])],'continuity_exact_ddl_changed')
        xinfo=[(c['cid'],c['name'],c['type'],int(c['not_null']),c['default_sql'],c['pk_position'],c['hidden']) for c in e['columns_xinfo']]
        check(query('PRAGMA table_xinfo("'+table+'")')==xinfo,'continuity_exact_columns_changed')
        fk=[(r['id'],r['sequence'],r['target_table'],r['from_column'],r['to_column'],r['on_update'],r['on_delete'],r['match']) for r in e['foreign_keys']]
        check(query('PRAGMA foreign_key_list("'+table+'")')==fk,'continuity_fk_changed')
        indexes=[(i['sequence'],i['name'],int(i['unique']),i['origin'],int(i['partial'])) for i in e['indexes']]
        check(query('PRAGMA index_list("'+table+'")')==indexes,'continuity_indexes_changed')
        for i in e['indexes']:
            check(query("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",(i['name'],))==[(i['ddl'],)],'continuity_index_ddl_changed')
            check(query('PRAGMA index_info("'+i['name']+'")')==[(r['sequence'],r['cid'],r['name']) for r in i['columns']],'continuity_index_columns_changed')
