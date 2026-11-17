"""Three fixed tables; no JSON interpretation, status repair or identity routing."""
import hashlib
import json
import math
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical,digest,INT64_MIN,INT64_MAX

TABLES=('lan_session_uploads','lan_session_heads','lan_session_backfills')
AUTO=()
KEYS={t:('device_id','session_id','sha256') if t==TABLES[0] else ('device_id','session_id') for t in TABLES}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
_RAW=(Path(__file__).parent/'resources/capture_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_RAW).hexdigest()=='f120f586f289c4c5238a7550eb50e062c628235e4e34162eba2aab141d797016','reviewed_capture_schema_changed')
_EVIDENCE=json.loads(_RAW)
SPECS={t:tuple((c['name'],c['type'],not c['not_null']) for c in _EVIDENCE['namespaces']['owner'][t]['columns']) for t in TABLES}

def schema_hash(namespace):
    check(namespace in ('owner','kane'),'capture_namespace_not_applicable')
    return digest(_EVIDENCE['namespaces'][namespace])

def columns(table):
    check(table in TABLES,'unsupported_capture_table')
    return tuple(c[0] for c in SPECS[table])

def target_columns(table):return ('source_rowid',)+columns(table)

def _values(table,row):
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:check(nullable,'unexpected_capture_null');continue
        if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_capture_integer')
        elif kind=='REAL':
            # SQLite REAL-affinity reads yield a float. Do not cast arbitrary
            # integers/text to double and silently lose original values.
            check(type(value) is float and math.isfinite(value),'unexpected_capture_real')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_capture_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_capture_text') from None

def map_row(table,row):
    check(table in TABLES,'unsupported_capture_table')
    check(type(row) is dict and set(row)==set(target_columns(table)),'capture_columns_mismatch')
    check(type(row['source_rowid']) is int and INT64_MIN<=row['source_rowid']<=INT64_MAX,'unexpected_capture_rowid')
    _values(table,row);return dict(row)

def unmap_row(table,row):return map_row(table,row)
def key(table,row):return tuple(row[k] for k in KEYS[table])

def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_capture_table')
    result=[map_row(table,r) for r in rows]
    check(len({key(table,r) for r in result})==len(result),'duplicate_capture_key')
    check(len({r['source_rowid'] for r in result})==len(result),'duplicate_capture_rowid')
    return sorted(result,key=lambda r:key(table,r))

def source_chunk_hash(table,rows):
    """Only for the original Snapshot's owned-page export, before rowid binding."""
    check(table in TABLES,'unsupported_capture_table')
    values=[]
    for row in rows:
        check(type(row) is dict and set(row)==set(columns(table)),'capture_columns_mismatch')
        _values(table,row);values.append(dict(row))
    check(len({key(table,r) for r in values})==len(values),'duplicate_capture_key')
    return digest({'format':'capture-source-visible-v1','table':table,'rows':sorted(values,key=lambda r:key(table,r))})

def chunk_hash(table,rows):
    return digest({'format':'history-capture-three-v1','table':table,'rows':ordered_rows(table,rows)})

def prepare_chunk(table,rows,expected_input_hash):
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'capture_chunk_row_budget')
    values=ordered_rows(table,rows);size=len(canonical(values))
    check(size<=MAX_BYTES,'capture_chunk_byte_budget')
    hashed=chunk_hash(table,values)
    check(expected_input_hash is None or hashed==expected_input_hash,'capture_input_hash_mismatch')
    return values,hashed,size

def insert_statement(table,row,library_id):
    values=map_row(table,row);names=target_columns(table)
    return ('INSERT INTO em_content.'+table+'(library_id,project_id,'+','.join(names)+') VALUES(%s::uuid,NULL,'+
            ','.join('%s' for _ in names)+')',(library_id,*(values[n] for n in names)))
