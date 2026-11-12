"""Fixed two-table hidden-rowid reader. No arbitrary SQL, path or table API.

Each page is provisional until SealedPhase closes successfully. Length/type
probes run before materializing values; no original-field normalization occurs.
"""
from contextlib import closing
import sqlite3
import time

from . import static_wal as W
from . import retained_chain as R
from . import source_mapping as S
from . import source_plan as SP
from .history_core import mapping as C

TABLES=('context_project_registry','session_archive_holds')
KEYS={TABLES[0]:('project',),TABLES[1]:('archive_id','source_context_id')}
SPECS={TABLES[0]:C.SPECS[TABLES[0]],TABLES[1]:S.SPECS[TABLES[1]]}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
check=C.check


def record(table,rowid,row):
    check(table in TABLES,'locator_table_not_allowed')
    check(type(rowid) is int and C.INT64_MIN<=rowid<=C.INT64_MAX,'locator_rowid_invalid')
    (C.validate_row if table==TABLES[0] else S.map_row)(table,row)
    key=[row[name] for name in KEYS[table]]
    check(all(v is not None for v in key),'locator_null_key_unsupported')
    # Preserve original logical key; int/text remain distinct.
    key_json=C.canonical(key).decode('utf8')
    typed=[[name,kind,type(row[name]).__name__,row[name]] for name,kind,_ in SPECS[table]]
    return dict(source_rowid=rowid,source_key_json=key_json,source_key_hash=C.digest(key),visible_row_hash=C.digest(typed))


def _authorize(table):
    names={v[0] for v in SPECS[table]}|{'ROWID','_rowid_','rowid'}
    def authorizer(action,a,b,db,trigger):
        if trigger is not None:return sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ and db=='main' and a==table and b in names:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and (b or '').lower() in ('typeof','length'):return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorizer


def _page(connection,phase,table,after):
    """Private helper; caller owns fixed sealed SQLite connection."""
    check(table in TABLES,'locator_table_not_allowed')
    check(after is None or type(after) is int and C.INT64_MIN<=after<=C.INT64_MAX,'locator_cursor_invalid')
    deadline=min(phase.deadline,time.monotonic()+10)
    def tick():
        phase.tick();check(time.monotonic()<deadline,'locator_source_page_deadline')
    def progress():
        tick();return 0
    connection.set_progress_handler(progress,200)
    encoding=connection.execute('PRAGMA encoding').fetchone()[0]
    check(encoding in ('UTF-8','UTF-16le','UTF-16be'),'locator_source_encoding')
    connection.set_authorizer(_authorize(table))
    columns=[v[0] for v in SPECS[table]]
    q=lambda name:'"'+name+'"'
    where='' if after is None else ' WHERE _rowid_>?'
    args=() if after is None else (after,)
    tail=' FROM '+q(table)+where+' ORDER BY _rowid_ LIMIT 128'
    probe=['_rowid_']
    for name in columns:probe+=['typeof('+q(name)+')','length(CAST('+q(name)+' AS BLOB))']
    tick();sizes=connection.execute('SELECT '+','.join(probe)+tail,args).fetchall();tick()
    total=0
    for item in sizes:
        check(type(item[0]) is int,'locator_rowid_storage')
        for i,(name,kind,nullable) in enumerate(SPECS[table]):
            storage,size=item[1+2*i:3+2*i]
            check(storage==('integer' if kind=='INTEGER' else 'real' if kind=='REAL' else 'text') or storage=='null' and nullable,'locator_source_storage_unsupported')
            check(size is None and storage=='null' or type(size) is int and size>=0,'locator_source_length_invalid')
            total+=(size or 0)*(2 if encoding!='UTF-8' and storage=='text' else 1)+len(name)+64
            check(total<=MAX_BYTES,'locator_source_page_byte_budget')
    tick();raw=connection.execute('SELECT _rowid_,'+','.join(q(n) for n in columns)+tail,args).fetchall();tick()
    check([r[0] for r in raw]==[r[0] for r in sizes],'locator_source_range_changed')
    values=[record(table,r[0],dict(zip(columns,r[1:]))) for r in raw]
    check(len(C.canonical(values))<=MAX_BYTES,'locator_metadata_page_byte_budget')
    check(len({v['source_key_hash'] for v in values})==len(values),'locator_duplicate_key')
    return dict(table=table,after=after,end=values[-1]['source_rowid'] if values else after,
                exhausted=len(values)<MAX_ROWS,row_count=len(values),records=values,
                page_hash=C.digest(values),input_bytes=total)


def read_page(source,core_plan,sources_plan,table,after):
    check(type(source) is W.SealedWalSource and type(core_plan) is R.Plan and type(sources_plan) is SP.Plan,'locator_sealed_source_required')
    check(core_plan.header['source']==sources_plan.header['source'],'locator_parent_source_mismatch')
    with W.SealedPhase(source,plan=core_plan,seconds=60) as phase:
        with closing(sqlite3.connect(source.path.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None,timeout=.1)) as c:
            try:
                c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_BYTES)
                c.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536)
                c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF');c.execute('BEGIN')
                page=_page(c,phase,table,after)
            except (sqlite3.Error,UnicodeError):
                phase.tick();raise C.ImportError('locator_source_decode_or_sqlite_error') from None
    check(phase.proven_complete and phase.hash_passes>=2,'locator_source_exit_incomplete')
    page.update(source_phase_complete=True,source_hash_passes=phase.hash_passes,source_seal_sha256=source.receipt_sha256)
    return page


def seal_exit(source,core_plan,sources_plan):
    """Fresh metadata-only source proof; no table scan or preserved cursor."""
    check(type(source) is W.SealedWalSource and type(core_plan) is R.Plan and type(sources_plan) is SP.Plan,'locator_sealed_source_required')
    check(core_plan.header['source']==sources_plan.header['source'],'locator_parent_source_mismatch')
    with W.SealedPhase(source,plan=core_plan,seconds=60) as phase:phase.tick()
    check(phase.proven_complete and phase.hash_passes>=2,'locator_source_exit_incomplete')
    return dict(source_phase_complete=True,source_hash_passes=phase.hash_passes,
                source_seal_sha256=source.receipt_sha256,source_file_sha256=source.sha256)
