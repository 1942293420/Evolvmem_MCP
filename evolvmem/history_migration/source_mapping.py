"""Fixed seven-table lossless value mapping; paths are inert original data.

No SQL connection, archive open, JSON interpretation, default substitution or
timestamp normalization occurs here. Unsupported SQLite storage types stop.
"""
import json
import hashlib
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical,digest,INT64_MIN,INT64_MAX

TABLES=('session_archives','session_archive_storage','conversation_history',
        'archive_source_exclusions','session_archive_holds','context_sources','context_evidence')
AUTO=('session_archives','context_sources','context_evidence')
KEYS={t:('id',) if t in AUTO else ('archive_id','source_context_id') if t=='session_archive_holds' else ('archive_id',) for t in TABLES}
MAX_ROWS=128
MAX_BYTES=4*1024*1024
_EVIDENCE_RAW=(Path(__file__).parent/'resources/sources_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_EVIDENCE_RAW).hexdigest()=='4202389596363497111f38baa72ba8997904623085c3b67a09af76d881b9a048','reviewed_schema_artifact_changed')
_EVIDENCE=json.loads(_EVIDENCE_RAW)
SPECS={t:tuple((c['name'],c['type'],not c['not_null'] and not c['pk_position']) for c in _EVIDENCE['namespaces']['owner'][t]['columns']) for t in TABLES}

def schema_hash(namespace):
    check(namespace in ('owner','kane','public'),'source_namespace_required')
    return digest(_EVIDENCE['namespaces'][namespace])

def validate_schema(table,metadata):
    check(table in TABLES,'unsupported_source_table')
    # Compare DDL, xinfo, defaults, constraints and indexes, not affinity alone.
    check(type(metadata) is dict and any(metadata==_EVIDENCE['namespaces'][ns][table] for ns in ('owner','kane','public')),'source_schema_changed')

def columns(table):
    check(table in TABLES,'unsupported_source_table')
    return tuple(c[0] for c in SPECS[table])

def map_row(table,row):
    check(type(row) is dict and set(row)==set(columns(table)),'source_columns_mismatch')
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:check(nullable,'unexpected_source_null');continue
        if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_source_integer')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_source_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_source_text') from None
    return dict(row)

def unmap_row(table,row):return map_row(table,row)
def key(table,row):return tuple(row[k] for k in KEYS[table])

def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_source_table')
    values=[map_row(table,row) for row in rows]
    keys=[key(table,row) for row in values]
    check(len(keys)==len(set(keys)),'duplicate_source_key')
    return sorted(values,key=lambda r:key(table,r))

def chunk_hash(table,rows):return digest({'format':'history-sources-seven-v1','table':table,'rows':ordered_rows(table,rows)})

def prepare_chunk(table,rows,expected_input_hash):
    check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'chunk_row_budget')
    rows=ordered_rows(table,rows);size=len(canonical(rows))
    check(size<=MAX_BYTES,'chunk_byte_budget')
    hashed=chunk_hash(table,rows)
    check(expected_input_hash is None or expected_input_hash==hashed,'input_hash_mismatch')
    return rows,hashed,size

def insert_statement(table,row,library_id):
    values=map_row(table,row);names=columns(table)
    override=' OVERRIDING SYSTEM VALUE' if table in AUTO else ''
    return ('INSERT INTO em_content.'+table+'(library_id,project_id,'+','.join(names)+')'+override+
            ' VALUES(%s::uuid,NULL,'+','.join(['%s']*len(names))+')',(library_id,*(values[n] for n in names)))
