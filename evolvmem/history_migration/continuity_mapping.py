"""Eight fixed tables; no JSON interpretation, status repair or identity routing."""
import hashlib
import json
import math
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical,digest,INT64_MIN,INT64_MAX

TABLES=('continuity_events', 'continuity_workstreams', 'continuity_focus', 'project_archives', 'project_events', 'project_event_sources', 'project_history_date_repairs', 'project_memory_documents')
AUTO={'continuity_events': 'id', 'project_events': 'event_id', 'project_event_sources': 'source_id'}
KEYS={t:("source_rowid",) for t in TABLES}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
_RAW=(Path(__file__).parent/'resources/continuity_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_RAW).hexdigest()=='cc91c5deb61bcc74a5e45369e0a8741df1b5d996a564d388c0f599f781c2d88e','reviewed_continuity_schema_changed')
_EVIDENCE=json.loads(_RAW)
SPECS={t:tuple((c['name'],c['type'],not c['not_null'] and c['name']!=_EVIDENCE['namespaces']['owner'][t]['rowid_alias']) for c in _EVIDENCE['namespaces']['owner'][t]['columns']) for t in TABLES}

def schema_hash(namespace):
    check(namespace in _EVIDENCE['namespaces'],'continuity_namespace_unknown')
    return digest(_EVIDENCE['namespaces'][namespace])

def applicable(namespace):
    schema_hash(namespace)
    return tuple(t for t in TABLES if t in _EVIDENCE['namespaces'][namespace])

def absent(namespace):return tuple(t for t in TABLES if t not in applicable(namespace))

def quote(name):return '"'+name+'"'

def columns(table):
    check(table in TABLES,'unsupported_continuity_table')
    return tuple(c[0] for c in SPECS[table])

def target_columns(table):return ('source_rowid',)+columns(table)

def _values(table,row):
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:check(nullable,'unexpected_continuity_null');continue
        if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_continuity_integer')
        elif kind=='REAL':
            # SQLite REAL-affinity reads yield a float. Do not cast arbitrary
            # integers/text to double and silently lose original values.
            check(type(value) is float and math.isfinite(value),'unexpected_continuity_real')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_continuity_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_continuity_text') from None

def map_row(table,row):
    check(table in TABLES,'unsupported_continuity_table')
    check(type(row) is dict and set(row)==set(target_columns(table)),'continuity_columns_mismatch')
    check(type(row['source_rowid']) is int and INT64_MIN<=row['source_rowid']<=INT64_MAX,'unexpected_continuity_rowid')
    _values(table,row)
    alias=_EVIDENCE['namespaces']['owner'][table]['rowid_alias']
    check(alias is None or row[alias]==row['source_rowid'],'continuity_rowid_alias_changed')
    return dict(row)

def unmap_row(table,row):return map_row(table,row)
def key(table,row):return tuple(row[k] for k in KEYS[table])

def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_continuity_table')
    result=[map_row(table,r) for r in rows]
    check(len({key(table,r) for r in result})==len(result),'duplicate_continuity_key')
    check(len({r['source_rowid'] for r in result})==len(result),'duplicate_continuity_rowid')
    return sorted(result,key=lambda r:key(table,r))

def source_chunk_hash(table,rows):
    """Only for the original Snapshot's owned-page export, before rowid binding."""
    check(table in TABLES,'unsupported_continuity_table')
    values=[]
    for row in rows:
        check(type(row) is dict and set(row)==set(columns(table)),'continuity_columns_mismatch')
        _values(table,row);values.append(dict(row))
    return digest({'format':'continuity-source-visible-v1','table':table,'rows':values})

def chunk_hash(table,rows):
    return digest({'format':'history-continuity-eight-v1','table':table,'rows':ordered_rows(table,rows)})

def prepare_chunk(table,rows,expected_input_hash):
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'continuity_chunk_row_budget')
    values=ordered_rows(table,rows);size=len(canonical(values))
    check(size<=MAX_BYTES,'continuity_chunk_byte_budget')
    hashed=chunk_hash(table,values)
    check(expected_input_hash is None or hashed==expected_input_hash,'continuity_input_hash_mismatch')
    return values,hashed,size

def insert_statement(table,row,library_id):
    values=map_row(table,row);names=target_columns(table)
    return ('INSERT INTO em_content.'+table+'(library_id,project_id,'+','.join(quote(n) for n in names)+') VALUES(%s::uuid,NULL,'+
            ','.join('%s' for _ in names)+')',(library_id,*(values[n] for n in names)))
