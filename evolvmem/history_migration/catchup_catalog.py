"""Adapters to the existing nine lossless mappings, never a new data model."""
from dataclasses import dataclass
from pathlib import Path
import json
import hashlib
from .history_core import mapping as core
from . import source_mapping, organization_mapping, capture_mapping, governance_mapping
from . import knowledge_mapping, capability_mapping, continuity_mapping, operations_mapping
from . import governance_large_row as L

check, ImportError, canonical, digest = core.check, core.ImportError, core.canonical, core.digest
MAX_ROWS=128
MAX_BYTES=4*1024*1024
MODULES=dict(core=core,sources=source_mapping,organization=organization_mapping,
    capture=capture_mapping,governance=governance_mapping,knowledge=knowledge_mapping,
    capability=capability_mapping,continuity=continuity_mapping,operations=operations_mapping)

def quote(name):
    check(type(name) is str and name.replace('_','').isalnum(),'catchup_identifier')
    return '"'+name+'"'

@dataclass(frozen=True)
class Table:
    domain:str
    name:str
    @property
    def byte_budget(self):return L.PAGE_BYTES if self.domain=="governance" and self.name==L.TABLE else MAX_BYTES
    @property
    def sqlite_budget(self):return L.SQLITE_BYTES if self.domain=="governance" and self.name==L.TABLE else MAX_BYTES
    @property
    def module(self):return MODULES[self.domain]
    @property
    def keys(self):
        if self.domain=='core':return (core.KEYS[self.name],)
        if self.domain=='organization':return ('source_rowid',)
        return self.module.KEYS[self.name]
    @property
    def rowid(self):return self.domain not in ('core','sources')
    @property
    def columns(self):
        specs=self.module.SPECS[self.name]
        return tuple(specs) if self.domain=='organization' else tuple(c[0] for c in specs)
    @property
    def source_columns(self):return (('source_rowid',) if self.rowid else ())+self.columns
    @property
    def target_name(self):
        return self.module.target_table(self.name) if self.domain in ('governance','operations') else self.name
    @property
    def target_columns(self):
        return core.target_columns(self.name) if self.domain=='core' else self.source_columns
    @property
    def target_keys(self):
        return (core.target_key(self.name),) if self.domain=='core' else self.keys
    @property
    def target_key_sql(self):
        specs=self.module.SPECS[self.name]
        kinds={n:v['type'] for n,v in specs.items()} if self.domain=='organization' else {s[0]:s[1] for s in specs}
        return tuple(quote(target)+(' COLLATE "C"' if kinds.get(source)=='TEXT' else '')
            for source,target in zip(self.keys,self.target_keys))
    @property
    def immutable_columns(self):
        # PostgreSQL ALWAYS identities cannot be assigned even their current
        # value. All original identity aliases are original primary keys; the
        # rowid-backed domains also retain those logical keys separately.
        names=set(self.target_keys)
        if self.rowid:
            names.update(c['name'] for c in self.evidence('owner')['columns'] if c['pk_position'])
        if self.name=='context_project_registry':names.add('project_uuid')
        return frozenset(names)
    def validate(self,row):
        try:
            return core.validate_row(self.name,row) if self.domain=='core' else self.module.map_row(self.name,row)
        except (ValueError,TypeError,KeyError):raise ImportError('catchup_original_mapping_rejected') from None
    def key(self,row):return tuple(row[k] for k in self.keys)
    def mapped(self,row,projects):
        self.validate(row)
        return core.map_row(self.name,row,projects) if self.domain=='core' else dict(row)
    def unmapped(self,row):
        return core.unmap_row(self.name,row) if self.domain=='core' else self.validate(row)
    def insert(self,row,projects,library_id):
        if self.domain=='core':
            # Existing mapper, with its original raw JSON preservation. Catch-up
            # supplies cyclic links explicitly in a later bounded UPDATE.
            from .history_core.importer import insert_statement
            return insert_statement(self.name,row,projects,library_id)
        if self.domain=='organization':
            values=self.validate(row);cols=self.target_columns
            override=' OVERRIDING SYSTEM VALUE' if self.name=='organization_tasks' else ''
            return ('INSERT INTO em_content.'+quote(self.target_name)+'(library_id,project_id,'+
                ','.join(map(quote,cols))+')'+override+' VALUES(%s::uuid,NULL,'+','.join('%s' for _ in cols)+')',
                (library_id,*(values[n] for n in cols)))
        return self.module.insert_statement(self.name,row,library_id)
    def update(self,row,projects,library_id,*,null_columns=()):
        mapped=self.mapped(row,projects)
        check(not self.immutable_columns.intersection(null_columns),'catchup_immutable_link_clear_unsupported')
        cols=tuple(name for name in self.target_columns if name not in self.immutable_columns)
        mapped.update({n:None for n in null_columns})
        values=[];assign=[]
        for name in cols:
            value=mapped[name]
            is_json=self.domain=='core' and core.is_json_column(self.name,name)
            if is_json and value is not None:value=canonical(value).decode()
            assign.append(quote(name)+'=%s'+('::jsonb' if is_json else ''));values.append(value)
        return ('UPDATE em_content.'+quote(self.target_name)+' SET '+','.join(assign)+
            ' WHERE library_id=%s::uuid AND '+' AND '.join(quote(k)+' IS NOT DISTINCT FROM %s' for k in self.target_keys),
            (*values,library_id,*self.key(row)))
    def evidence(self,namespace):
        if self.domain=='organization':return self.module.EVIDENCE[self.name]
        if self.domain=='core':
            return json.loads((Path(__file__).parent/'resources/core_reviewed_schema.json').read_bytes())['namespaces'][namespace][self.name]
        return self.module._EVIDENCE['namespaces'][namespace][self.name]

_TABLES={}
for _domain,_module in MODULES.items():
    for _name in (tuple(_module.SPECS) if _domain=='organization' else _module.TABLES):
        check(_name not in _TABLES,'catchup_duplicate_domain_table')
        _TABLES[_name]=Table(_domain,_name)

def table(name):
    check(name in _TABLES,'catchup_unreviewed_table');return _TABLES[name]

def tables(namespace):
    check(namespace in ('owner','kane','public'),'catchup_namespace')
    result=[]
    for t in _TABLES.values():
        if t.domain=='capture' and namespace=='public':continue
        m=t.module
        if hasattr(m,'applicable') and t.name not in m.applicable(namespace):continue
        if hasattr(m,'_EVIDENCE') and t.name not in m._EVIDENCE['namespaces'][namespace]:continue
        result.append(t)
    return tuple(result)

def binary_table(t):
    return t.domain=='operations' and t.name=='organization_review_vectors'

def value_bytes(t,value):
    # Reuse the original all-node typed codec only for the reviewed BLOB table.
    return t.module.canonical(value) if binary_table(t) else canonical(value)

def journal_json(t,row):
    # The existing binary wire is JSON-loadable; native bytes stay in SQL args.
    if binary_table(t) and t.module._has_blob(row):
        return canonical(t.module._binary_wire(row)).decode()
    return canonical(row).decode()

def row_hash(name,row):
    t=table(name);t.validate(row)
    return hashlib.sha256(value_bytes(t,dict(format='history-catchup-typed-row-v1',table=name,row=row))).hexdigest()

def typed_line(t,row):
    t.validate(row)
    return value_bytes(t,[t.key(row),row])+b'\n'


def changes_budget(changes):
    """Only the fixed audit table opts into the measured before/after profile."""
    limit=MAX_BYTES
    for change in changes:
        t=table(change['table'])
        check(type(change['input_bytes']) is int and 0<=change['input_bytes']<=t.byte_budget,
              'catchup_change_byte_budget')
        limit=max(limit,t.byte_budget)
    return limit
