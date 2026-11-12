"""Bounded six-table SQLite reader; no PG, models, copies, or route activation.

Cursors are capabilities of one live object/transaction, not serialized SQLite
snapshots. A completed transcript is evidence about returned rows, never a way
to reopen that SQLite snapshot. All pages remain provisional until finish().
"""
from copy import deepcopy
from dataclasses import dataclass,field
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import struct
import threading
import time
import weakref

from .history_core import mapping as M
from .history_core import schema as CORE_SCHEMA

TABLES=M.TABLES
AUTO=('memories','context_items','context_layers')
MAX_PAGE_ROWS=128
MAX_PAGE_BYTES=4*1024*1024
MAX_TOTAL_ROWS=3000
MAX_TOTAL_BYTES=32*1024*1024
MAX_SNAPSHOT_SECONDS=10.0
MAX_QUERY_SECONDS=2.0
SCHEMA_SHA='81f328ebc314aa74bce5af19b9dbb2e3a92a3577bf2cc659c513344ecd53065b'
CORE_MAPPING_SHA='8b1de58545ef7812493a3a7c3c514e53738c6a619888dd8700f3e2b73043f92b'
CORE_SCHEMA_SHA='bb31be08a6a6be003f964891120c40c41febad445ec0948718183b2a1c3f7a96'
FIXED_PATHS={
    'owner':'/home/jiangli/.claude/evolvmem/memory.db',
    'kane':'/home/jiangli/.local/share/evolvmem-lan/users/kane/memory.db',
    'public':'/home/jiangli/.local/share/evolvmem-lan/public/memory.db'}


class SourceIncomplete(Exception):
    """Fixed code only; SQL, row text and paths never enter error messages."""
    def __init__(self,code):self.code=code;super().__init__(code)


def require(ok,code):
    if not ok:raise SourceIncomplete(code)


@dataclass(frozen=True)
class SourceRef:
    namespace:str
    path:Path
    def __post_init__(self):
        require(self.namespace in FIXED_PATHS,'source_namespace_required')
        object.__setattr__(self,'path',Path(self.path))
        require(self.path.is_absolute(),'absolute_trusted_source_required')


def registered_source(namespace):
    """Privileged orchestrator convenience; no input path or connection URL."""
    require(namespace in FIXED_PATHS,'source_namespace_required')
    return SourceRef(namespace,Path(FIXED_PATHS[namespace]))


@dataclass(frozen=True,eq=False)
class Cursor:
    _owner:object=field(repr=False)
    _table:str=field(repr=False)
    _after:int=field(repr=False)
    def __reduce__(self):raise TypeError('live_snapshot_cursor_not_serializable')


@dataclass(frozen=True,eq=False)
class Page:
    table:str
    chunk_key:str
    rows:tuple=field(repr=False)
    storage_types:tuple=field(repr=False)
    source_digest:str
    byte_count:int
    exhausted:bool
    next_cursor:object=field(repr=False)
    def __reduce__(self):raise TypeError('provisional_page_requires_live_snapshot')


def _text(raw):
    try:return raw.decode('utf8')
    except UnicodeDecodeError:return raw  # retain invalid TEXT bytes + typeof witness


def _typed_hash(rows,types):
    h=hashlib.sha256();size=0
    for row,kinds in zip(rows,types):
        for name,value in row.items():
            kind=kinds[name]
            if value is None:raw=b''
            elif type(value) is bytes:raw=value
            elif type(value) is str:raw=value.encode('utf8')
            elif type(value) is int:raw=str(value).encode('ascii')
            elif type(value) is float:raw=struct.pack('>d',value)
            else:raise SourceIncomplete('unsupported_sqlite_value')
            header=(name+'\x00'+kind+'\x00'+type(value).__name__+'\x00').encode('ascii')
            h.update(header);h.update(len(raw).to_bytes(8,'big'));h.update(raw)
            size+=len(header)+8+len(raw)
    return h.hexdigest(),size


def _files(path):
    values={}
    for name,p in (('db',path),('wal',Path(str(path)+'-wal'))):
        try:
            info=p.lstat()
            require(stat.S_ISREG(info.st_mode) and p.resolve()==p,'source_path_identity_changed')
            values[name]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
        except FileNotFoundError:
            require(name=='wal','source_missing');values[name]=None
    return values


class Snapshot:
    """One private reader and one bounded lifetime; context-manager required.

    SourceRef is trusted maintenance configuration, not HTTP input. Limits may
    be reduced for tests/operations, never increased past this reviewed scope.
    The timer interrupts SQL and closes an idle connection independently of
    caller cooperation. OS-stalled IO still requires an outer process deadline.
    """
    def __init__(self,source,*,page_rows=128,page_bytes=MAX_PAGE_BYTES,total_rows=MAX_TOTAL_ROWS,
                 total_bytes=MAX_TOTAL_BYTES,snapshot_seconds=10.0,query_seconds=2.0):
        require(type(source) is SourceRef,'trusted_source_ref_required')
        for value,maximum in ((page_rows,128),(page_bytes,MAX_PAGE_BYTES),(total_rows,MAX_TOTAL_ROWS),(total_bytes,MAX_TOTAL_BYTES)):
            require(type(value) is int and 0<value<=maximum,'invalid_source_budget')
        for value,maximum in ((snapshot_seconds,MAX_SNAPSHOT_SECONDS),(query_seconds,MAX_QUERY_SECONDS)):
            require(type(value) in (int,float) and math.isfinite(value) and 0<value<=maximum,'invalid_source_deadline')
        self.source=source;self.page_rows=page_rows;self.page_bytes=page_bytes
        self.total_rows=total_rows;self.total_bytes=total_bytes
        self.seconds=snapshot_seconds;self.query_seconds=query_seconds
        self._connection=None;self._lock=threading.RLock();self._closed_event=threading.Event()
        self._token=object();self._label=secrets.token_hex(12);self._opened=False;self._closed=False
        self._owner=(os.getpid(),threading.get_ident());self._timer=None;self._query_deadline=None
        self._cursors={};self._done=set();self._counts={t:0 for t in TABLES};self._bytes=0
        self._max_ids={t:0 for t in AUTO};self._pages=weakref.WeakKeyDictionary();self._page_number=0
        self._exported=weakref.WeakSet()
        self._transcript=hashlib.sha256();self._sequences=None
        self.report=dict(read_complete=False,history_complete=False,reason='not_opened',snapshot_token_resumable=False,
            importer_validation='not_proven_by_source_reader')

    def __enter__(self):
        require(not self._opened and not self._closed,'snapshot_not_reopenable')
        self._opened=True;self._started=time.monotonic();self._deadline=self._started+self.seconds
        self._timer=threading.Timer(self.seconds,self._expire);self._timer.daemon=True;self._timer.start()
        try:
            catalog=Path(__file__).parent/'resources/core_reviewed_schema.json'
            require(hashlib.sha256(catalog.read_bytes()).hexdigest()==SCHEMA_SHA,'reviewed_schema_changed')
            require(hashlib.sha256(Path(M.__file__).read_bytes()).hexdigest()==CORE_MAPPING_SHA,'frozen_mapping_changed')
            require(Path(CORE_SCHEMA.__file__).parent==Path(M.__file__).parent
                and hashlib.sha256(Path(CORE_SCHEMA.__file__).read_bytes()).hexdigest()==CORE_SCHEMA_SHA
                and CORE_SCHEMA.SPECS is M.SPECS,'frozen_schema_changed')
            self._reviewed=json.loads(catalog.read_text(encoding='utf8'))['namespaces'][self.source.namespace]
            require(self.source.path.resolve()==self.source.path,'source_path_identity_changed')
            self._before=_files(self.source.path)
            connection=sqlite3.connect(self.source.path.as_uri()+'?mode=ro',uri=True,timeout=.1,
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

    def __exit__(self,kind,value,traceback):
        if not self._closed:self._close('snapshot_closed_incomplete')
        if self._timer and self._timer is not threading.current_thread():self._timer.join(.3)
        return False

    def _authorize(self,action,first,second,db,origin):
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ:
            # SQLite emits an extra table-presence read with no database/column
            # while compiling rowid-only SELECTs. It grants no additional table.
            if first in TABLES and second=='' and db in (None,'main') and origin is None:return sqlite3.SQLITE_OK
            if db!='main' or origin is not None:return sqlite3.SQLITE_DENY
            if first in TABLES and (second in {x[0] for x in M.SPECS[first]} or second.upper()=='ROWID'):
                return sqlite3.SQLITE_OK
            if first in ('sqlite_master','sqlite_schema') and second in ('type','name','tbl_name','rootpage','sql'):
                return sqlite3.SQLITE_OK
            if first=='sqlite_sequence' and second in ('name','seq'):return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA and first in ('table_xinfo','table_list','schema_version','data_version','query_only'):
            if first in ('table_xinfo','table_list') and second not in TABLES:return sqlite3.SQLITE_DENY
            if first in ('schema_version','data_version','query_only') and second is not None:return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and second=='typeof':return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_TRANSACTION and first in ('BEGIN','ROLLBACK'):return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _progress(self):
        now=time.monotonic()
        return int(self._closed or now>=self._deadline or self._query_deadline is not None and now>=self._query_deadline)

    def _expire(self):
        connection=self._connection
        if connection is not None:
            try:connection.interrupt()
            except sqlite3.Error:pass
        self._close('snapshot_lifetime_exceeded')

    def _close(self,reason,*,complete=False):
        with self._lock:
            if self._closed:return
            self._closed=True
            if self._timer:self._timer.cancel()
            close_confirmed=True
            if self._connection is not None:
                try:self._connection.close()  # ends read transaction; no SQL commit
                except sqlite3.Error:
                    close_confirmed=False;complete=False;reason='source_close_unconfirmed'
            ended=time.monotonic()
            if complete and ended>=self._deadline:
                complete=False;reason='snapshot_lifetime_exceeded'
            self.report=dict(self.report,read_complete=complete,reason=reason,
                counts=tuple((t,self._counts[t]) for t in TABLES),output_bytes=self._bytes,
                elapsed_seconds=round(ended-getattr(self,'_started',ended),6),
                snapshot_token_resumable=False,history_complete=False,connection_close_confirmed=close_confirmed)
            self._closed_event.set()

    def _fail(self,code):
        self._close(code);raise SourceIncomplete(code) from None

    def _live(self):
        require((os.getpid(),threading.get_ident())==self._owner,'snapshot_owner_mismatch')
        if self._closed:raise SourceIncomplete('snapshot_closed')
        require(self._opened,'snapshot_not_opened')
        if time.monotonic()>=self._deadline:self._fail('snapshot_lifetime_exceeded')

    def _unchanged(self):
        self._live()
        try:current=_files(self.source.path)
        except SourceIncomplete:self._fail('source_changed')
        except OSError:self._fail('source_changed')
        if current!=self._before:self._fail('source_changed')

    def _sql_error(self,exc):
        if time.monotonic()>=self._deadline:self._fail('snapshot_lifetime_exceeded')
        if self._query_deadline is not None and time.monotonic()>=self._query_deadline:self._fail('query_deadline_exceeded')
        code=getattr(exc,'sqlite_errorcode',0)&255
        self._fail('sqlite_value_over_budget' if code==sqlite3.SQLITE_TOOBIG else
                   'source_sql_denied' if code==sqlite3.SQLITE_AUTH else 'source_sql_failed')

    def _query(self,sql,params=(),*,all_rows=True):
        with self._lock:
            self._live();self._query_deadline=min(self._deadline,time.monotonic()+self.query_seconds)
            cursor=None
            try:
                cursor=self._connection.execute(sql,params)
                result=[]
                if cursor.description:
                    for _ in range(513):
                        self._live()
                        if time.monotonic()>=self._query_deadline:self._fail('query_deadline_exceeded')
                        row=cursor.fetchone()
                        if row is None:break
                        result.append(row)
                    else:self._fail('metadata_row_budget')
                self._live()
                if time.monotonic()>=self._query_deadline:self._fail('query_deadline_exceeded')
                return result
            except sqlite3.Error as exc:self._sql_error(exc)
            finally:
                if cursor is not None:
                    try:cursor.close()
                    except sqlite3.Error:pass
                self._query_deadline=None

    def _verify_schema(self):
        proof={}
        for table in TABLES:
            expected=self._reviewed[table]
            rows=self._query('SELECT type,sql FROM sqlite_schema WHERE name=?',(table,))
            require(len(rows)==1 and rows[0][0]=='table' and type(rows[0][1]) is str
                and hashlib.sha256(rows[0][1].encode()).hexdigest()==expected['ddl_sha256'],'source_schema_mismatch')
            xinfo=self._query('PRAGMA table_xinfo("'+table+'")')
            expected_x=[(c['cid'],c['name'],c['type'],int(c['not_null']),c['default_sql'],c['pk_position'],c['hidden']) for c in expected['columns_xinfo']]
            require(xinfo==expected_x and all(c[-1]==0 and c[1].lower() not in ('rowid','oid','_rowid_') for c in xinfo),'source_schema_mismatch')
            require({c[1] for c in xinfo}=={c[0] for c in M.SPECS[table]},'frozen_source_columns_mismatch')
            listing=self._query('PRAGMA table_list("'+table+'")')
            require(len(listing)==1 and listing[0][0:3]==('main',table,'table') and listing[0][4]==0,'reviewed_rowid_table_required')
            self._query('SELECT _rowid_,rowid,oid FROM "'+table+'" LIMIT 0')
            proof[table]=dict(ddl_sha256=expected['ddl_sha256'],columns_xinfo=expected['columns_xinfo'],
                autoincrement=expected['autoincrement'],rowid_alias=expected['rowid_alias'])
        self.schema_hash=hashlib.sha256(M.canonical(proof)).hexdigest()
        self.report.update(source_namespace=self.source.namespace,source_schema_hash=self.schema_hash,
            schema_version=self._schema_version,scope='reviewed_six_tables_only')

    def _read_sequences(self):
        names=','.join('?' for _ in TABLES)
        rows=self._query('SELECT name,seq,typeof(seq) FROM sqlite_sequence WHERE name IN ('+names+') LIMIT 7',TABLES)
        evidence={t:dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,
            allocation_history_verified=False) for t in AUTO}
        seen=set()
        for name,value,kind in rows:
            require(name in AUTO,'sequence_for_non_autoincrement')
            require(name not in seen,'duplicate_sequence_entry');seen.add(name)
            require(type(value) not in (bytes,str) or len(value)<=1024,'sequence_value_over_budget')
            evidence[name].update(present=True,value=value,storage_type=kind)
        self._sequences=evidence

    def sequence_evidence(self):
        self._unchanged();self._charge(16384)
        result=deepcopy(self._sequences);self._live();return result

    def _charge(self,size):
        with self._lock:
            self._live()
            if self._bytes+size>self.total_bytes:self._fail('source_total_byte_budget')
            self._bytes+=size

    def page(self,table,cursor=None):
        try:return self._page(table,cursor)
        except SourceIncomplete as exc:
            if not self._closed:self._close(exc.code)
            raise
        except sqlite3.Error as exc:self._sql_error(exc)
        except Exception:self._fail('source_read_failed')
        except BaseException:
            self._close('source_interrupted');raise

    def _page(self,table,cursor):
        self._unchanged();require(table in TABLES,'unsupported_source_table')
        require(table not in self._done,'table_already_exhausted')
        if cursor is not None:
            require(type(cursor) is Cursor and cursor._owner is self._token and cursor._table==table,'cursor_snapshot_mismatch')
        require(cursor is self._cursors.get(table),'cursor_replayed')
        require(cursor is not None or table not in self._cursors,'cursor_replayed')
        after=cursor._after if cursor else None
        columns=tuple(c[0] for c in M.SPECS[table]);quoted=','.join('"'+n+'"' for n in columns)
        kinds=','.join('typeof("'+n+'")' for n in columns)
        where='' if after is None else ' WHERE _rowid_>?'
        sql='SELECT _rowid_,'+quoted+','+kinds+' FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT ?'
        params=(self.page_rows,) if after is None else (after,self.page_rows)
        rows=[];types=[];used=0;last=after;more=False
        with self._lock:
            self._live();self._query_deadline=min(self._deadline,time.monotonic()+self.query_seconds)
            raw=None
            try:
                raw=self._connection.execute(sql,params)
                for _ in range(self.page_rows):
                    self._live()
                    if time.monotonic()>=self._query_deadline:self._fail('query_deadline_exceeded')
                    values=raw.fetchone()
                    if values is None:break
                    rowid=values[0]
                    require(type(rowid) is int and (last is None or rowid>last),'rowid_order_changed')
                    row=dict(zip(columns,values[1:len(columns)+1]));storage=dict(zip(columns,values[len(columns)+1:]))
                    alias=self._reviewed[table]['rowid_alias']
                    require(alias is None or row[alias]==rowid,'rowid_alias_changed')
                    _,size=_typed_hash((row,),(storage,))
                    if size>self.page_bytes:self._fail('source_row_over_budget')
                    if used+size>self.page_bytes:more=True;break
                    rows.append(row);types.append(storage);used+=size;last=rowid
                if time.monotonic()>=self._query_deadline:self._fail('query_deadline_exceeded')
            except sqlite3.Error as exc:self._sql_error(exc)
            finally:
                if raw is not None:
                    try:raw.close()
                    except sqlite3.Error:pass
                self._query_deadline=None
        if rows and not more:
            more=bool(self._query('SELECT _rowid_ FROM "'+table+'" WHERE _rowid_>? ORDER BY _rowid_ LIMIT 1',(last,)))
        require(sum(self._counts.values())+len(rows)<=self.total_rows,'source_total_row_budget')
        require(self._bytes+used<=self.total_bytes,'source_total_byte_budget')
        self._unchanged()
        hashed,_=_typed_hash(rows,types);self._page_number+=1
        next_cursor=Cursor(self._token,table,last) if more else None
        page=Page(table,'s'+self._label+'.'+str(TABLES.index(table))+'.'+str(self._page_number),
                  tuple(rows),tuple(types),hashed,used,not more,next_cursor)
        self._pages[page]=hashed;self._counts[table]+=len(rows);self._bytes+=used
        self._transcript.update(M.canonical((table,self._page_number,hashed,len(rows),used,not more)))
        if table in AUTO:
            for row in rows:self._max_ids[table]=max(self._max_ids[table],row['id'])
        if more:self._cursors[table]=next_cursor
        else:self._done.add(table);self._cursors[table]=None
        self._live();return page

    def chunk_arguments(self,page):
        self._unchanged()
        try:
            require(type(page) is Page and page in self._pages,'page_snapshot_mismatch')
            require(page not in self._exported,'chunk_already_exported')
            require(_typed_hash(page.rows,page.storage_types)[0]==self._pages[page],'source_page_mutated')
            require(bool(page.rows),'empty_page_has_no_import_chunk')
            rows=[dict(row) for row in page.rows]
            hashed=M.chunk_hash(page.table,rows)
            payload_bytes=len(M.canonical(rows))
            require(payload_bytes<=MAX_PAGE_BYTES,'frozen_chunk_byte_budget')
            self._charge(payload_bytes);self._exported.add(page)
            self._live()
            return dict(table=page.table,chunk_key=page.chunk_key,rows=rows,expected_input_hash=hashed)
        except M.ImportError:self._fail('frozen_mapper_value_unsupported')
        except SourceIncomplete as exc:self._fail(exc.code)
        except Exception:self._fail('source_page_mutated')

    def finish(self):
        with self._lock:
            self._unchanged()
            self._charge(32768)  # bounded final metadata envelope, including sequence copies
            if self._done!=set(TABLES):self._fail('six_tables_not_exhausted')
            if self._query('PRAGMA schema_version')!=[(self._schema_version,)] or self._query('PRAGMA data_version')!=[(self._data_version,)]:
                self._fail('source_changed')
            self._unchanged()
            high_ready=all(e['present'] and e['storage_type']=='integer' and type(e['value']) is int
                and self._max_ids[t]<=e['value']<M.INT64_MAX for t,e in self._sequences.items())
            sequence_hashes=[(t,e['present'],e['storage_type'],
                _typed_hash(({'seq':e['value']},),({'seq':e['storage_type'] or 'missing'},))[0])
                for t,e in self._sequences.items()]
            transcript=dict(namespace=self.source.namespace,schema_hash=self.schema_hash,
                file_identity=self._before,pages_digest=self._transcript.hexdigest(),sequence_hashes=sequence_hashes)
            self.report.update(source_snapshot_hash=hashlib.sha256(M.canonical(transcript)).hexdigest(),
                snapshot_hash_kind='bounded_six_table_read_transcript_not_reopenable_snapshot',
                sequence_evidence=deepcopy(self._sequences),run_highwaters_ready=high_ready,
                total_changes=self._connection.total_changes)
            self._live()  # final lifetime check after transcript work, before completion
            self._close('complete',complete=True)
            if not self.report['read_complete']:raise SourceIncomplete(self.report['reason'])
            try:unchanged=_files(self.source.path)==self._before
            except (OSError,SourceIncomplete):unchanged=False
            if not unchanged:
                self.report.update(read_complete=False,reason='source_changed')
                raise SourceIncomplete('source_changed')
            return deepcopy(self.report)
