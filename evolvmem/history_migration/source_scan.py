"""Seven fixed tables under the original sealed phase and Snapshot budgets.

Table-bound Snapshot methods use the EXACT original code objects in a private
globals dictionary with only M/TABLES/AUTO specialized. No frozen module/global
is changed, no source executes as code, and no caller supplies a table set.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import FunctionType
from . import source_mapping as M
from . import sqlite_source as S
from . import static_wal as W

check=M.check
SOURCE_PACKAGE_SHA='d1679760b8690aea950a1c2c8386a3b2edb2bca1dd0500a1c01502ba5c155faa'
for name,pin in ((M.__file__,'d14736226981739ed5d6f1b9ca5daca727f5957b6b142f96e3fba649edc6081a'),
                 (S.__file__,W.SOURCE_SHA),(W.__file__,'f1893673189ae3814b13e0cdc7da39b6810c79e44472345d8ab37f75b967e076')):
    check(hashlib.sha256(Path(name).read_bytes()).hexdigest()==pin,'frozen_source_dependency_changed')

def _seven(method):
    namespace=dict(method.__globals__,M=M,TABLES=M.TABLES,AUTO=M.AUTO)
    fn=FunctionType(method.__code__,namespace,method.__name__,method.__defaults__,method.__closure__)
    fn.__kwdefaults__=method.__kwdefaults__;return fn

_base_authorize=_seven(S.Snapshot._authorize)
_base_verify=_seven(S.Snapshot._verify_schema)

class Snapshot(S.Snapshot):
    _init_seven=_seven(S.Snapshot.__init__)
    _close=_seven(S.Snapshot._close)
    _page=_seven(S.Snapshot._page)
    _read_sequences=_seven(S.Snapshot._read_sequences)
    chunk_arguments=_seven(S.Snapshot.chunk_arguments)
    finish=_seven(S.Snapshot.finish)

    def __init__(self,phase,**limits):
        check(type(phase) is Phase and phase.base._entry_verified and not phase.base._finished,'sealed_seven_phase_required')
        self.phase=phase;phase.base._check_sidecars()
        self._init_seven(S.SourceRef(phase.source.namespace,phase.source.path),**limits)
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
        self.report.update(source_schema_hash=self.schema_hash,scope='reviewed_seven_source_tables_only')
    def __enter__(self):
        S.require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            self._reviewed=deepcopy(M._EVIDENCE['namespaces'][self.source.namespace])
            for table,e in self._reviewed.items():
                e['autoincrement']=table in M.AUTO
                e['rowid_alias']='id' if table in M.AUTO else None if table=='session_archive_holds' else 'archive_id'
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


def _canonical_prefix_rows(table,rows):
    # Native typed bytes and escaped canonical JSON have separate frozen caps.
    # Count an exact prefix only; never shorten a value or replace its storage type.
    used=2;selected=0
    for row in rows:
        body=M.canonical(M.map_row(table,dict(row)))
        cost=len(body)+int(selected>0)
        if used+cost>S.MAX_PAGE_BYTES:break
        used+=cost;selected+=1
    return selected


class Phase:
    def __init__(self,source,*,plan=None,seconds=60):
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
    def page(self,table,after,ordinal):
        self.tick();check(table in M.TABLES,'unsupported_source_table')
        check(after is None or type(after) is int and M.INT64_MIN<=after<=M.INT64_MAX,'source_range_invalid')
        page_rows=S.MAX_PAGE_ROWS
        try:
            while True:
                duration=min(10.0,self.base.deadline-time.monotonic());check(duration>0,'source_phase_deadline')
                with Snapshot(self,snapshot_seconds=duration,page_rows=page_rows) as reader:
                    cursor=None
                    if after is not None:cursor=S.Cursor(reader._token,table,after);reader._cursors[table]=cursor
                    page=reader.page(table,cursor)
                    try:selected=_canonical_prefix_rows(table,page.rows)
                    except M.ImportError:reader._fail('frozen_mapper_value_unsupported')
                    if selected<len(page.rows):
                        if not selected:reader._fail('frozen_chunk_byte_budget')
                        # The native page is provisional and unexported. Reopen
                        # at the same seek with a smaller LIMIT so frozen code
                        # derives the exact cursor, typed hash and exhaustion.
                        page_rows=selected
                        reader._unchanged();reader._live();reader._close('sealed_seven_unexported_repage')
                        check(reader.report.get('connection_close_confirmed') is True,'source_range_close_incomplete')
                        continue
                    args=reader.chunk_arguments(page) if page.rows else None
                    if args:
                        M.prepare_chunk(table,args['rows'],args['expected_input_hash'])
                        where='' if after is None else ' WHERE _rowid_>?';params=(len(page.rows),) if after is None else (after,len(page.rows))
                        ids=reader._query('SELECT _rowid_ FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT ?',params)
                        check(len(ids)==len(page.rows),'source_range_changed');end=ids[-1][0]
                    else:end=after
                    seq=reader.sequence_evidence();schema=reader.schema_hash
                    spec=dict(ordinal=ordinal,table=table,chunk_key='s'+str(ordinal).zfill(6),after=after,end=end,exhausted=page.exhausted,
                        row_count=len(page.rows),native_hash=page.source_digest,input_hash=args['expected_input_hash'] if args else None,
                        input_bytes=len(M.canonical(args['rows'])) if args else 0)
                    reader._unchanged();reader._live();reader._close('sealed_seven_range_closed')
                    check(reader.report.get('connection_close_confirmed') is True and reader.report['reason']=='sealed_seven_range_closed','source_range_close_incomplete')
                self.tick()
                if args:args['chunk_key']=spec['chunk_key']
                return spec,args,seq,schema
        except S.SourceIncomplete as exc:raise M.ImportError(exc.code) from None
    def read(self,page):
        actual,args,seq,schema=self.page(page['table'],page['after'],page['ordinal'])
        check(self.plan is not None and actual==page and seq==self.plan.header['sequence'] and schema==self.plan.header['source_schema_hash'],'source_page_changed')
        return args
