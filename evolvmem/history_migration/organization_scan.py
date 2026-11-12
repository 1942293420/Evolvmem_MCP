"""Bounded query-only organization pages; no target completion authority."""
import hashlib
import json
import sqlite3
from . import organization_mapping as M

TABLES=tuple(M.EVIDENCE)

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')

def validate_schema(c):
    for table,e in M.EVIDENCE.items():
        ddl=c.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?',('table',table)).fetchall()
        if ddl!=[(e['ddl'],)]:raise ValueError('source_exact_ddl_changed')
        actual=c.execute('PRAGMA table_xinfo("'+table+'")').fetchall()
        expected=[(x['cid'],x['name'],x['type'],int(x['not_null']),x['default_sql'],x['pk_position'],0) for x in e['columns']]
        if actual!=expected:raise ValueError('source_exact_columns_changed')

def page(c,table,after):
    if table not in TABLES:raise ValueError('source_table')
    if after is not None and (type(after) is not int or not -(2**63)<=after<2**63):raise ValueError('source_cursor')
    names=tuple(M.SPECS[table]);cols=','.join('"'+x+'"' for x in names)
    where='' if after is None else ' WHERE _rowid_>?'
    params=() if after is None else (after,)
    cursor=c.execute('SELECT _rowid_,'+cols+' FROM "'+table+'"'+where+' ORDER BY _rowid_ LIMIT 128',params)
    result=[];more=False
    try:
        while len(result)<128:
            values=cursor.fetchone()
            if values is None:break
            row=dict(zip(('source_rowid',)+names,values));M.map_row(table,row)
            if len(canonical([row]))>M.MAX_BYTES:raise ValueError('source_row_byte_budget')
            if len(canonical(result+[row]))>M.MAX_BYTES:more=True;break
            result.append(row)
    finally:cursor.close()
    if len(result)==128:
        more=c.execute('SELECT 1 FROM "'+table+'" WHERE _rowid_>? LIMIT 1',(result[-1]['source_rowid'],)).fetchone() is not None
    if result:M.chunk(table,result)
    raw=canonical(result)
    return dict(table=table,after=after,end=result[-1]['source_rowid'] if result else after,
        exhausted=not more,rows=result,input_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())

def task_sequence(c):
    """Keep the raw sequence evidence; absence is allowed only for an empty table.

    Exact organization schema validation and the source seal remain caller gates.
    A bounded existence query proves empty, without inventing a sequence record
    or obtaining allocation history from max(id).
    """
    seq=c.execute("SELECT seq,typeof(seq) FROM sqlite_sequence WHERE name='organization_tasks'").fetchall()
    if len(seq)==1 and seq[0][1]=='integer' and type(seq[0][0]) is int:
        return dict(present=True,storage_type='integer',value=seq[0][0])
    if not seq and c.execute('SELECT 1 FROM organization_tasks LIMIT 1').fetchone() is None:
        return dict(present=False,storage_type=None,value=None)
    raise ValueError('source_task_highwater_unresolved')


def task_highwater(c):
    """The missing, proven-empty case has allocation floor zero, not row seq=0."""
    evidence=task_sequence(c)
    return evidence['value'] if evidence['present'] else 0


def sealed_pages(phase):
    """Caller must close SealedPhase successfully before trusting these pages."""
    from . import static_wal as W
    if type(phase) is not W.SealedPhase or not phase._entry_verified or phase._finished:
        raise ValueError('sealed_phase_required')
    phase.tick();phase._check_sidecars()
    c=sqlite3.connect(phase.source.path.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None,timeout=.1)
    try:
        c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,M.MAX_BYTES)
        c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
        c.set_progress_handler(lambda: (phase.tick(),0)[1],200)
        c.execute('BEGIN');validate_schema(c)
        for table in TABLES:
            after=None
            while True:
                phase.tick();phase._check_sidecars();value=page(c,table,after)
                yield value
                if value['exhausted']:break
                after=value['end']
        task_highwater(c)
        phase.tick();phase._check_sidecars()
    finally:c.close()
