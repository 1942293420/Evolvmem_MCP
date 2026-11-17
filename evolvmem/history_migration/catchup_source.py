"""Fixed-table bounded reader inside the existing operational seal proof."""
import hashlib
import sqlite3
import time
from . import catchup_catalog as C
from .static_wal import SealedPhase, SealedWalSource

class Snapshot:
    def __init__(self,source,*,seconds=60,max_input_bytes=4*1024**3):
        C.check(type(source) is SealedWalSource,'catchup_sealed_source_required')
        C.check(type(max_input_bytes) is int and 0<max_input_bytes<=4*1024**3,'catchup_input_budget')
        self.source=source;self.phase=SealedPhase(source,seconds=min(seconds,60))
        self.input_bytes=0;self.max_input_bytes=max_input_bytes;self.connection=None
    def __enter__(self):
        self.phase.__enter__()
        try:
            self.connection=sqlite3.connect(self.source.path.as_uri()+'?mode=ro&immutable=1',
                uri=True,timeout=.1,isolation_level=None)
            c=self.connection
            c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,C.MAX_BYTES)
            c.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536)
            c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
            c.set_progress_handler(lambda:(self.phase.tick(),0)[1],1000)
            c.execute('BEGIN')
            for t in C.tables(self.source.namespace):self.validate_schema(t)
            return self
        except BaseException as exc:
            if self.connection:self.connection.close()
            self.phase.__exit__(type(exc),exc,exc.__traceback__);raise
    def __exit__(self,kind,value,tb):
        try:
            if self.connection:self.connection.close()
        except BaseException as exc:
            self.phase.__exit__(type(exc),exc,exc.__traceback__);raise
        return self.phase.__exit__(kind,value,tb)
    def validate_schema(self,t):
        self.phase.tick();e=t.evidence(self.source.namespace);c=self.connection
        C.check(c.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?',('table',t.name)).fetchall()==[(e['ddl'],)],'catchup_schema_ddl_changed')
        cols=e.get('columns_xinfo',e.get('columns'))
        expected=[(x['cid'],x['name'],x['type'],int(x['not_null']),x['default_sql'],x['pk_position'],x.get('hidden',0)) for x in cols]
        C.check(c.execute('PRAGMA table_xinfo('+C.quote(t.name)+')').fetchall()==expected,'catchup_schema_columns_changed')
        if 'foreign_keys' in e:
            expected=[(x['id'],x['sequence'],x['target_table'],x['from_column'],x['to_column'],x['on_update'],x['on_delete'],x['match']) for x in e['foreign_keys']]
            C.check(c.execute('PRAGMA foreign_key_list('+C.quote(t.name)+')').fetchall()==expected,'catchup_schema_links_changed')
        # The exact table DDL fixes constraints. Explicit indexes are also fixed
        # where the original reviewed evidence includes them.
        if 'indexes' in e:
            for index in e['indexes']:
                C.check(c.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",(index['name'],)).fetchall()==[(index['ddl'],)],'catchup_schema_index_changed')
    def _select(self,t):
        return ('_rowid_ AS source_rowid,' if t.rowid else '')+','.join(map(C.quote,t.columns))
    def _row(self,t,values):
        row=dict(zip(t.source_columns,values));t.validate(row)
        size=len(C.value_bytes(t,row));C.check(size<=t.byte_budget,'catchup_row_byte_budget')
        self.input_bytes+=size;C.check(self.input_bytes<=self.max_input_bytes,'catchup_source_byte_budget')
        return row
    def rows(self,t):
        """Keyset pages avoid offset scans and hold at most 128/4MiB rows."""
        after=None
        while True:
            rows=self.page(t,after)
            if not rows:return
            for row in rows:yield row
            after=t.key(rows[-1])
    def page(self,t,after=None):
        self.phase.tick()
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,t.sqlite_budget)
        cols=tuple('_rowid_' if k=='source_rowid' else C.quote(k) for k in t.keys)
        order=','.join(cols);where=''
        if after is not None:where=' WHERE ('+order+')>('+','.join('?' for _ in cols)+')'
        cursor=self.connection.execute('SELECT '+self._select(t)+' FROM '+C.quote(t.name)+where+' ORDER BY '+order+' LIMIT 128',() if after is None else after)
        rows=[];size=2
        try:
            for values in cursor:
                row=self._row(t,values);amount=len(C.value_bytes(t,row))+1
                if rows and size+amount>t.byte_budget:break
                C.check(size+amount<=t.byte_budget,'catchup_page_byte_budget')
                rows.append(row);size+=amount
        finally:cursor.close()
        return rows
    def find(self,t,columns,values,*,limit=129):
        C.check(len(columns)==len(values) and set(columns)<=set(t.source_columns),'catchup_lookup_columns')
        C.check(type(limit) is int and 1<=limit<=129,'catchup_lookup_budget')
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,t.sqlite_budget)
        names=['_rowid_' if k=='source_rowid' else C.quote(k) for k in columns]
        cursor=self.connection.execute('SELECT '+self._select(t)+' FROM '+C.quote(t.name)+
            ' WHERE '+' AND '.join(n+' IS ?' for n in names)+' LIMIT '+str(limit),tuple(values))
        try:
            result=[];size=2
            for values in cursor:
                row=self._row(t,values);size+=len(C.value_bytes(t,row))+1
                C.check(size<=t.byte_budget,'catchup_lookup_byte_budget');result.append(row)
            return result
        finally:cursor.close()
    def row(self,t,key):
        rows=self.find(t,t.keys,key,limit=2)
        C.check(len(rows)<=1,'catchup_duplicate_key');return rows[0] if rows else None
    def sequences(self):
        names={t.name for t in C.tables(self.source.namespace)}
        exists=self.connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone()
        rows=self.connection.execute('SELECT name,seq,typeof(seq) FROM sqlite_sequence ORDER BY name').fetchall() if exists else []
        result={}
        for name,value,kind in rows:
            if name in names:
                C.check(type(value) is int and kind=='integer' and 0<=value<2**63,'catchup_sequence_storage')
                result[name]=dict(present=True,value=value,storage_type=kind)
        for t in C.tables(self.source.namespace):
            if 'AUTOINCREMENT' in t.evidence(self.source.namespace)['ddl'].upper():
                result.setdefault(t.name,dict(present=False,value=None,storage_type=None))
        return result

def summarize(t,rows):
    digest=hashlib.sha256();keys=hashlib.sha256();count=0;size=0
    for row in rows:
        line=C.typed_line(t,row);digest.update(line)
        keys.update(C.canonical(t.key(row))+b'\n');count+=1;size+=len(line)
    return dict(count=count,typed_sha256=digest.hexdigest(),keys_sha256=keys.hexdigest(),input_bytes=size)
