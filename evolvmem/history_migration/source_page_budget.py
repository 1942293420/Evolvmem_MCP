"""Fixed-domain source page preflight, without altering the frozen reader.

Only scalar storage types and byte lengths cross into Python before a bounded
body SELECT. The probe and body read share the caller's live RO transaction.
The allowlist resolves reviewed mappings; callers cannot provide SQL or columns.
"""
from importlib import import_module
import re

from . import sqlite_source as S

_DOMAINS={
    'governance':'governance_mapping',
    'knowledge':'knowledge_mapping',
    'capability':'capability_mapping',
    'continuity':'continuity_mapping',
    'operations':'operations_mapping',
}
_STORAGE={'INTEGER':'integer','REAL':'real','TEXT':'text','BLOB':'blob'}
# Invalid UTF-8 TEXT is returned by the frozen reader as bytes. Reserve the
# longer Python type header even though the mapping subsequently rejects it.
_PYTHON={'null':'NoneType','integer':'int','real':'float','text':'bytes','blob':'bytes'}


def _quote(name):
    S.require(type(name) is str and re.fullmatch(r'[a-z][a-z0-9_]*',name) is not None,
              'source_probe_identifier_invalid')
    return '"'+name+'"'


def bounded_page(reader,domain,table,cursor,read_page):
    """Call a bound frozen page reader only after deciding its safe row limit.

    Snapshot._authorize must permit built-in length and read-only PRAGMA encoding
    (no argument) in addition to its fixed policy. No transaction is opened here.
    The frozen reader still owns cursor validation, hashes, accounting and
    completion. This function never truncates a row or discards the remainder.
    """
    S.require(type(domain) is str and domain in _DOMAINS,'source_probe_domain_invalid')
    mapping=import_module('.'+_DOMAINS[domain],__package__)
    reader._unchanged()
    S.require(table in mapping.applicable(reader.source.namespace),'unsupported_source_table')
    S.require(table not in reader._done,'table_already_exhausted')
    if cursor is not None:
        S.require(type(cursor) is S.Cursor and cursor._owner is reader._token and cursor._table==table,
                  'cursor_snapshot_mismatch')
    S.require(cursor is reader._cursors.get(table),'cursor_replayed')
    S.require(cursor is not None or table not in reader._cursors,'cursor_replayed')
    S.require(0<reader.page_rows<=S.MAX_PAGE_ROWS,'invalid_source_budget')
    encoding=reader._query('PRAGMA encoding')
    S.require(encoding in ([('UTF-8',)],[('UTF-16le',)],[('UTF-16be',)]),
              'source_probe_encoding_unsupported')
    utf16=encoding!=[('UTF-8',)]
    after=cursor._after if cursor else None
    specs=mapping.SPECS[table]
    expressions=[]
    for name,kind,nullable in specs:
        S.require(kind in _STORAGE,'source_probe_declared_type_invalid')
        column=_quote(name)
        expressions.extend(('typeof('+column+')',
            "CASE typeof("+column+") WHEN 'null' THEN 0 WHEN 'real' THEN 8 "
            'ELSE length(CAST('+column+' AS BLOB)) END'))
    where='' if after is None else ' WHERE _rowid_>?'
    params=(reader.page_rows,) if after is None else (after,reader.page_rows)
    metadata=reader._query('SELECT _rowid_,'+','.join(expressions)+' FROM '+_quote(table)+where+
                           ' ORDER BY _rowid_ LIMIT ?',params)
    S.require(len(metadata)<=reader.page_rows,'source_probe_row_budget')
    selected=0;used=0;last=after
    for item in metadata:
        S.require(len(item)==1+2*len(specs),'source_probe_shape_changed')
        rowid=item[0]
        S.require(type(rowid) is int and (last is None or rowid>last),'rowid_order_changed')
        size=0
        for index,(name,kind,nullable) in enumerate(specs):
            storage,length=item[1+2*index:3+2*index]
            S.require(storage==_STORAGE[kind] or (nullable and storage=='null'),
                      'source_probe_storage_changed')
            S.require(type(length) is int and length>=0,'source_probe_length_invalid')
            if utf16 and storage=='text':
                # CAST(TEXT AS BLOB) has database-encoding bytes, but sqlite3's
                # text_factory and the frozen hash receive UTF-8. Each UTF-16
                # code unit needs at most three UTF-8 bytes; paired surrogates
                # use fewer. Round upward also for malformed odd-byte text.
                length=3*((length+1)//2)
            size+=len(name)+len(storage)+len(_PYTHON[storage])+3+8+length
        if size>reader.page_bytes or used+size>reader.page_bytes:
            if not selected:reader._fail('source_row_over_budget')
            break
        selected+=1;used+=size;last=rowid
    S.require(sum(reader._counts.values())+selected<=reader.total_rows,'source_total_row_budget')
    S.require(reader._bytes+used<=reader.total_bytes,'source_total_byte_budget')
    # LIMIT is set before execute/fetch. Frozen _page checks for the next row by
    # rowid only, keeping a rejected body's bytes out of the Python process.
    original_rows=reader.page_rows
    reader.page_rows=max(1,selected)
    try:return read_page(table,cursor)
    finally:reader.page_rows=original_rows
