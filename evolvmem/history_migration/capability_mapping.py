"""Seven fixed tables; no JSON interpretation, status repair or identity routing."""
import hashlib
import json
import math
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical,digest,INT64_MIN,INT64_MAX

TABLES=('capability_categories', 'capability_memories', 'capability_sources', 'capability_assessments', 'capability_category_suggestions', 'capability_intake', 'existing_capability_assessment_runs')
AUTO=('capability_memories', 'capability_sources', 'capability_assessments', 'capability_category_suggestions', 'capability_intake')
KEYS={t:("source_rowid",) for t in TABLES}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
_RAW=(Path(__file__).parent/'resources/capability_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_RAW).hexdigest()=='1d1aad797bd719af2721770c97cd67f666fcacbb86cf7caac9079533d0e7050b','reviewed_capability_schema_changed')
_EVIDENCE=json.loads(_RAW)
SPECS={t:tuple((c['name'],c['type'],not c['not_null'] and c['name']!=_EVIDENCE['namespaces']['owner'][t]['rowid_alias']) for c in _EVIDENCE['namespaces']['owner'][t]['columns']) for t in TABLES}

def schema_hash(namespace):
    check(namespace in _EVIDENCE['namespaces'],'capability_namespace_unknown')
    return digest(_EVIDENCE['namespaces'][namespace])

def applicable(namespace):
    schema_hash(namespace)
    return tuple(t for t in TABLES if t in _EVIDENCE['namespaces'][namespace])

def absent(namespace):return tuple(t for t in TABLES if t not in applicable(namespace))

def quote(name):return '"'+name+'"'

def columns(table):
    check(table in TABLES,'unsupported_capability_table')
    return tuple(c[0] for c in SPECS[table])

def target_columns(table):return ('source_rowid',)+columns(table)

def _values(table,row):
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:check(nullable,'unexpected_capability_null');continue
        if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_capability_integer')
        elif kind=='REAL':
            # SQLite REAL-affinity reads yield a float. Do not cast arbitrary
            # integers/text to double and silently lose original values.
            check(type(value) is float and math.isfinite(value),'unexpected_capability_real')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_capability_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_capability_text') from None

def map_row(table,row):
    check(table in TABLES,'unsupported_capability_table')
    check(type(row) is dict and set(row)==set(target_columns(table)),'capability_columns_mismatch')
    check(type(row['source_rowid']) is int and INT64_MIN<=row['source_rowid']<=INT64_MAX,'unexpected_capability_rowid')
    _values(table,row)
    alias=_EVIDENCE['namespaces']['owner'][table]['rowid_alias']
    check(alias is None or row[alias]==row['source_rowid'],'capability_rowid_alias_changed')
    return dict(row)

def unmap_row(table,row):return map_row(table,row)
def key(table,row):return tuple(row[k] for k in KEYS[table])

def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_capability_table')
    result=[map_row(table,r) for r in rows]
    check(len({key(table,r) for r in result})==len(result),'duplicate_capability_key')
    check(len({r['source_rowid'] for r in result})==len(result),'duplicate_capability_rowid')
    return sorted(result,key=lambda r:key(table,r))

def source_chunk_hash(table,rows):
    """Only for the original Snapshot's owned-page export, before rowid binding."""
    check(table in TABLES,'unsupported_capability_table')
    values=[]
    for row in rows:
        check(type(row) is dict and set(row)==set(columns(table)),'capability_columns_mismatch')
        _values(table,row);values.append(dict(row))
    return digest({'format':'capability-source-visible-v1','table':table,'rows':values})

def chunk_hash(table,rows):
    return digest({'format':'history-capability-seven-v1','table':table,'rows':ordered_rows(table,rows)})

def prepare_chunk(table,rows,expected_input_hash):
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'capability_chunk_row_budget')
    values=ordered_rows(table,rows);size=len(canonical(values))
    check(size<=MAX_BYTES,'capability_chunk_byte_budget')
    hashed=chunk_hash(table,values)
    check(expected_input_hash is None or hashed==expected_input_hash,'capability_input_hash_mismatch')
    return values,hashed,size

def insert_statement(table,row,library_id):
    values=map_row(table,row);names=target_columns(table)
    return ('INSERT INTO em_content.'+table+'(library_id,project_id,'+','.join(quote(n) for n in names)+') VALUES(%s::uuid,NULL,'+
            ','.join('%s' for _ in names)+')',(library_id,*(values[n] for n in names)))
