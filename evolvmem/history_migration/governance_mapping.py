"""Fixed 26-table B3 preservation boundary. No classification or JSON normalization."""
import hashlib,json,math
from pathlib import Path
from .history_core.mapping import ImportError,check,canonical,digest,INT64_MIN,INT64_MAX
TABLES=('capability_provenance_repairs', 'classification_results', 'context_project_aliases', 'context_project_rollups', 'context_project_workspace_bindings', 'corrections', 'extraction_receipts', 'governance_migration_audit', 'governance_module_backfill_runs', 'governance_module_seed_runs', 'governance_source_enrollment_audit', 'governance_source_enrollments', 'organization_guidance', 'organization_review_feedback', 'organization_review_jobs', 'organization_scope_exclusions', 'organization_settings', 'organization_task_upgrades', 'private_governance_empty_terminals', 'private_governance_empty_terminals_v2', 'private_governance_source_upgrades', 'project_module_audit', 'project_modules', 'project_module_bindings', 'project_module_names', 'typed_ownership_repairs')
AUTO={'corrections': 'id', 'organization_guidance': 'id', 'project_module_audit': 'audit_id', 'project_module_bindings': 'binding_id'}
ALIASES={'corrections': 'id', 'governance_source_enrollments': 'archive_id', 'organization_guidance': 'id', 'organization_settings': 'id', 'organization_task_upgrades': 'task_id', 'project_module_audit': 'audit_id', 'project_module_bindings': 'binding_id'}
MAX_ROWS=128
from .governance_large_row import PAGE_BYTES
MAX_BYTES=PAGE_BYTES
_RAW=(Path(__file__).parent/'resources/governance_reviewed_schema.json').read_bytes()
check(hashlib.sha256(_RAW).hexdigest()=='32b80295edff64039c708f2b7b10b684fbbe546a64d262892888253f6ccbbde6','reviewed_governance_schema_changed')
_EVIDENCE=json.loads(_RAW)
SPECS={t:tuple((c['name'],c['type'],not c['not_null'] and not(t in ALIASES and c['name']==ALIASES[t])) for c in _EVIDENCE['namespaces']['owner'][t]['columns']) for t in TABLES}
KEYS={t:('source_rowid',) for t in TABLES}

# Logical source codes stay unchanged in schemas, hashes and receipts.
TARGET_NAMES={t:t for t in TABLES}
TARGET_NAMES.update({'context_project_aliases': 'history_context_project_aliases', 'context_project_workspace_bindings': 'history_context_project_workspace_bindings'})
TARGET_TABLES=tuple(TARGET_NAMES[t] for t in TABLES)
def target_table(table):
 check(table in TABLES,'unsupported_governance_target_table');return TARGET_NAMES[table]

def schema_hash(namespace):
 check(namespace in _EVIDENCE['namespaces'],'governance_namespace_invalid')
 return digest(_EVIDENCE['namespaces'][namespace])
def applicable(namespace):return tuple(t for t in TABLES if t in _EVIDENCE['namespaces'][namespace])
def columns(table):
 check(table in TABLES,'unsupported_governance_table');return tuple(c[0] for c in SPECS[table])
def target_columns(table):return ('source_rowid',)+columns(table)
def _values(table,row):
 for name,kind,nullable in SPECS[table]:
  value=row[name]
  if value is None:check(nullable,'unexpected_governance_null');continue
  if kind=='INTEGER':check(type(value) is int and INT64_MIN<=value<=INT64_MAX,'unexpected_governance_integer')
  elif kind=='REAL':check(type(value) is float and math.isfinite(value),'unexpected_governance_real')
  else:
   check(type(value) is str and '\x00' not in value,'unexpected_governance_text')
   try:value.encode('utf8')
   except UnicodeError:raise ImportError('unexpected_governance_text') from None
def map_row(table,row):
 check(table in TABLES and type(row) is dict and set(row)==set(target_columns(table)),'governance_columns_mismatch')
 check(type(row['source_rowid']) is int and INT64_MIN<=row['source_rowid']<=INT64_MAX,'unexpected_governance_rowid')
 _values(table,row)
 if table in ALIASES:check(row[ALIASES[table]]==row['source_rowid'],'governance_rowid_alias_changed')
 return dict(row)
def unmap_row(table,row):return map_row(table,row)
def key(table,row):return (row['source_rowid'],)
def ordered_rows(table,rows):
 result=[map_row(table,r) for r in rows]
 check(len({r['source_rowid'] for r in result})==len(result),'duplicate_governance_rowid')
 return sorted(result,key=lambda r:r['source_rowid'])
def source_chunk_hash(table,rows):
 values=[]
 for row in rows:
  check(type(row) is dict and set(row)==set(columns(table)),'governance_columns_mismatch');_values(table,row);values.append(dict(row))
 return digest({'format':'governance-source-visible-v1','table':table,'rows':values})
def chunk_hash(table,rows):return digest({'format':'history-governance-v1','table':table,'rows':ordered_rows(table,rows)})
def prepare_chunk(table,rows,expected_input_hash):
 check(type(rows) in (list,tuple) and 1<=len(rows)<=MAX_ROWS,'governance_chunk_row_budget')
 values=ordered_rows(table,rows);size=len(canonical(values));check(size<=MAX_BYTES,'governance_chunk_byte_budget')
 hashed=chunk_hash(table,values);check(expected_input_hash is None or hashed==expected_input_hash,'governance_input_hash_mismatch')
 return values,hashed,size
def insert_statement(table,row,library_id):
 values=map_row(table,row);names=target_columns(table)
 return ('INSERT INTO em_content.'+target_table(table)+'(library_id,project_id,'+','.join('"'+n+'"' for n in names)+') VALUES(%s::uuid,NULL,'+','.join('%s' for _ in names)+')',(library_id,*(values[n] for n in names)))
