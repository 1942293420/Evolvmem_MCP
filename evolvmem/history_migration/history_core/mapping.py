"""Reversible fixed-column mapping. Unsupported source values stop the chunk."""
import hashlib
import json
import math
from uuid import UUID

from .schema import SPECS

TABLES = ('context_project_registry', 'memories', 'context_items', 'context_layers',
          'legacy_memory_migrations', 'context_project_resolutions')
KEYS = {table: ('project' if table=='context_project_registry' else
               'legacy_memory_id' if table=='legacy_memory_migrations' else
               'item_id' if table=='context_project_resolutions' else 'id') for table in TABLES}
LINK_TABLES = ('memories','context_items')
INT64_MIN, INT64_MAX = -(2**63),2**63-1


class ImportError(Exception):
    """Fixed safe code only: never interpolate row data or connection settings."""
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def check(condition, code):
    if not condition:raise ImportError(code)


def uuid_text(value):
    try:
        parsed=UUID(value)
    except (ValueError,TypeError,AttributeError):raise ImportError('invalid_uuid') from None
    check(str(parsed)==value,'noncanonical_uuid')
    return value


def canonical(value):
    try:
        return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    except (TypeError,ValueError,UnicodeError):raise ImportError('noncanonical_source_value') from None


def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


def validate_row(table,row):
    check(table in TABLES,'unsupported_table')
    check(type(row) is dict and set(row)=={x[0] for x in SPECS[table]},'source_columns_mismatch')
    for name,kind,nullable in SPECS[table]:
        value=row[name]
        if value is None:
            check(nullable,'unexpected_source_null');continue
        if kind=='INTEGER':
            check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_source_integer')
        elif kind=='REAL':
            check(type(value) is float and math.isfinite(value),'unexpected_source_real')
        else:
            check(type(value) is str and '\x00' not in value,'unexpected_source_text')
            try:value.encode('utf8')
            except UnicodeError:raise ImportError('unexpected_source_text') from None
    return row


def _json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            check(key not in result,'duplicate_json_key');result[key]=value
        return result
    def constant(_):raise ImportError('nonfinite_json')
    def valid(value):
        if isinstance(value,str):check('\x00' not in value,'json_nul_unsupported')
        elif isinstance(value,dict):
            for key,item in value.items():valid(key);valid(item)
        elif isinstance(value,list):
            for item in value:valid(item)
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
        valid(value);canonical(value)
        return value
    except (ValueError,TypeError,RecursionError):raise ImportError('invalid_source_json') from None


def map_row(table,row,projects):
    validate_row(table,row)
    result=dict(row)
    if table in LINK_TABLES:result['revision']=1
    if table=='context_layers':
        result['legacy_layer_id']=result.pop('id')
    elif table=='context_project_registry':
        check(row['project'] in projects,'project_mapping_missing')
        result['project_uuid']=uuid_text(projects[row['project']])
    elif table=='context_items':
        for name in ('tags','projects','experience_payload'):
            raw=row[name];result['legacy_'+name+'_text']=raw
            if name=='tags' and not raw.strip():value=[]
            elif name=='tags' and not raw.lstrip().startswith(('[','{','"')):
                value=[v.strip() for v in raw.split(',') if v.strip()]
            elif name=='experience_payload' and raw=='':value=None
            else:value=_json(raw)
            if name in ('tags','projects'):
                check(type(value) is list and all(type(v) is str for v in value),'invalid_source_string_list')
            elif value is not None:
                check(type(value) is dict,'invalid_experience_payload')
            result[name]=value
        check(row['personal_general'] in (0,1),'invalid_personal_general')
        result['personal_general']=bool(row['personal_general'])
    elif table=='context_project_resolutions':
        result['legacy_evidence_json_text']=row['evidence_json']
        result['evidence_json']=_json(row['evidence_json'])
        check(type(result['evidence_json']) is list,'invalid_resolution_evidence')
    return result


def unmap_row(table,mapped):
    check(table in TABLES,'unsupported_table')
    row={name:mapped.get(name) for name,_,_ in SPECS[table]}
    if table=='context_layers':row['id']=mapped['legacy_layer_id']
    if table=='context_items':
        for name in ('tags','projects','experience_payload'):row[name]=mapped['legacy_'+name+'_text']
        check(type(mapped['personal_general']) is bool,'invalid_target_boolean')
        row['personal_general']=int(mapped['personal_general'])
    if table=='context_project_resolutions':row['evidence_json']=mapped['legacy_evidence_json_text']
    return validate_row(table,row)


def ordered_rows(table,rows):
    check(table in TABLES,'unsupported_table')
    validated=[validate_row(table,dict(row)) for row in rows]
    keys=[row[KEYS[table]] for row in validated]
    check(len(keys)==len(set(keys)),'duplicate_source_key')
    return sorted(validated,key=lambda row:row[KEYS[table]])


def chunk_hash(table,rows):
    return digest({'format':'history-six-v1','table':table,'rows':ordered_rows(table,rows)})


def target_key(table):return 'legacy_layer_id' if table=='context_layers' else KEYS[table]


def target_columns(table):
    check(table in TABLES,'unsupported_table')
    columns=[('legacy_layer_id' if table=='context_layers' and name=='id' else name) for name,_,_ in SPECS[table]]
    if table=='context_project_registry':columns.append('project_uuid')
    if table=='context_items':columns+=['legacy_'+name+'_text' for name in ('tags','projects','experience_payload')]
    if table=='context_project_resolutions':columns.append('legacy_evidence_json_text')
    if table in LINK_TABLES:columns.append('revision')
    return tuple(columns)


def is_json_column(table,column):
    return table=='context_items' and column in ('tags','projects','experience_payload') or table=='context_project_resolutions' and column=='evidence_json'
