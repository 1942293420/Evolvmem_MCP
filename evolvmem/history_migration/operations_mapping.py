"""Nine fixed tables; no JSON interpretation, status repair or identity routing."""
import hashlib
import json
import math
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical as _json_canonical,INT64_MIN,INT64_MAX

TABLES=('context_health_generation', 'lan_memory_revision', 'lan_publications', 'lan_workspace_bindings', 'lan_write_requests', 'local_capture_files', 'local_capture_scans', 'organization_review_vectors', 'round_metrics')
AUTO=("local_capture_files",)
KEYS={t:("source_rowid",) for t in TABLES}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
_RAW=(Path(__file__).parent/'resources/operations_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_RAW).hexdigest()=='5af2d607ad5c60c9fa491227b2d63a63c97e1c24fc3efe200aa3efaf85a0363d','reviewed_operations_schema_changed')
_EVIDENCE=json.loads(_RAW)
SCHEMA={t:next(ns[t] for ns in _EVIDENCE['namespaces'].values() if t in ns) for t in TABLES}
SPECS={t:tuple((c['name'],c['type'],not c['not_null'] and c['name']!=SCHEMA[t]['rowid_alias']) for c in SCHEMA[t]['columns']) for t in TABLES}

def _has_blob(value):
    if type(value) is bytes:return True
    if type(value) in (list,tuple):return any(_has_blob(v) for v in value)
    if type(value) is dict:return any(_has_blob(v) for v in value.values())
    return False

def _binary_wire(value):
    # All nodes are typed. No user TEXT/JSON or dictionary can impersonate bytes.
    if type(value) is bytes:return ['blob-hex',value.hex()]
    if type(value) is dict:return ['map',[[k,_binary_wire(v)] for k,v in sorted(value.items())]]
    if type(value) in (list,tuple):return ['sequence',[_binary_wire(v) for v in value]]
    return [type(value).__name__,value]

def canonical(value):
    # Plain metadata stays JSON-loadable; binary chunks use a disjoint prefix.
    # Hex expansion is included in the fixed 4 MiB export budget.
    if _has_blob(value):return b'OPERATIONS-BINARY-v1:' + _json_canonical(_binary_wire(value))
    return _json_canonical(value)

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


# Logical source codes stay unchanged in schemas, hashes and receipts.
TARGET_NAMES={t:t for t in TABLES}
TARGET_NAMES.update({'lan_write_requests': 'history_lan_write_requests'})
TARGET_TABLES=tuple(TARGET_NAMES[t] for t in TABLES)
def target_table(table):
 check(table in TABLES,'unsupported_operations_target_table');return TARGET_NAMES[table]

def schema_hash(namespace):
    check(namespace in _EVIDENCE['namespaces'],'operations_namespace_unknown')
    return digest(_EVIDENCE['namespaces'][namespace])

def applicable(namespace):
    schema_hash(namespace)
    return tuple(t for t in TABLES if t in _EVIDENCE['namespaces'][namespace])

def absent(namespace):return tuple(t for t in TABLES if t not in applicable(namespace))

def quote(name):return '"'+name+'"'

def columns(table):
    check(table in TABLES,'unsupported_operations_table')
    return tuple(c[0] for c in SPECS[table])

def target_columns(table):return ('source_rowid',)+columns(table)

def _values(table,row):
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:check(nullable,'unexpected_operations_null');continue
        if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_operations_integer')
        elif kind=='BLOB':check(type(value) is bytes,'unexpected_operations_blob')
        elif kind=='REAL':
            # SQLite REAL-affinity reads yield a float. Do not cast arbitrary
            # integers/text to double and silently lose original values.
            check(type(value) is float and math.isfinite(value),'unexpected_operations_real')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_operations_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_operations_text') from None

def map_row(table,row):
    check(table in TABLES,'unsupported_operations_table')
    check(type(row) is dict and set(row)==set(target_columns(table)),'operations_columns_mismatch')
    check(type(row['source_rowid']) is int and INT64_MIN<=row['source_rowid']<=INT64_MAX,'unexpected_operations_rowid')
    _values(table,row)
    alias=SCHEMA[table]['rowid_alias']
    check(alias is None or row[alias]==row['source_rowid'],'operations_rowid_alias_changed')
    if table in ('context_health_generation','local_capture_scans'):check(row['id']==1,'operations_singleton_changed')
    if table=='context_health_generation':check(row['generation']>=0,'operations_generation_invalid')
    if table=='lan_publications':
        check(row['author'] in ('jiangli','kane') and row['active'] in (0,1),'operations_publication_constraint')
    return dict(row)

def unmap_row(table,row):return map_row(table,row)
def key(table,row):return tuple(row[k] for k in KEYS[table])

def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_operations_table')
    result=[map_row(table,r) for r in rows]
    check(len({key(table,r) for r in result})==len(result),'duplicate_operations_key')
    check(len({r['source_rowid'] for r in result})==len(result),'duplicate_operations_rowid')
    return sorted(result,key=lambda r:key(table,r))

def source_chunk_hash(table,rows):
    """Only for the original Snapshot's owned-page export, before rowid binding."""
    check(table in TABLES,'unsupported_operations_table')
    values=[]
    for row in rows:
        check(type(row) is dict and set(row)==set(columns(table)),'operations_columns_mismatch')
        _values(table,row);values.append(dict(row))
    return digest({'format':'operations-source-visible-v1','table':table,'rows':values})

def chunk_hash(table,rows):
    return digest({'format':'history-operations-nine-v1','table':table,'rows':ordered_rows(table,rows)})

def prepare_chunk(table,rows,expected_input_hash):
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'operations_chunk_row_budget')
    values=ordered_rows(table,rows);size=len(canonical(values))
    check(size<=MAX_BYTES,'operations_chunk_byte_budget')
    hashed=chunk_hash(table,values)
    check(expected_input_hash is None or hashed==expected_input_hash,'operations_input_hash_mismatch')
    return values,hashed,size

def insert_statement(table,row,library_id):
    values=map_row(table,row);names=target_columns(table)
    return ('INSERT INTO em_content.'+target_table(table)+'(library_id,project_id,'+','.join(quote(n) for n in names)+') VALUES(%s::uuid,NULL,'+
            ','.join('%s' for _ in names)+')',(library_id,*(values[n] for n in names)))
