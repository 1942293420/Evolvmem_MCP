"""Seven-table preservation boundary, not a scanner/importer/completion receipt."""
import json
from pathlib import Path

EVIDENCE=json.loads((Path(__file__).parent/'resources/organization_reviewed_schema.json').read_text(encoding='utf-8'))
SPECS={table:{c['name']:c for c in spec['columns']} for table,spec in EVIDENCE.items()}
MAX_ROWS=128
MAX_BYTES=4*1024*1024


def map_row(table,row):
    if table not in SPECS or type(row) is not dict or set(row)!=set(SPECS[table])|{'source_rowid'}:
        raise ValueError('source_shape')
    ident=row['source_rowid']
    if type(ident) is not int or not -(2**63)<=ident<2**63:raise ValueError('source_rowid')
    for name,spec in SPECS[table].items():
        value=row[name]
        nullable=not spec['not_null'] and not (table in ('organization_tasks','learning_memories') and spec['pk_position'])
        if value is None and nullable:continue
        if spec['type']=='INTEGER':
            if type(value) is not int or not -(2**63)<=value<2**63:raise ValueError('source_storage_type')
        elif type(value) is not str or '\x00' in value:
            raise ValueError('source_storage_type')
        elif len(value.encode('utf8'))>MAX_BYTES:raise ValueError('source_size_limit')
    alias={'organization_tasks':'id','learning_memories':'item_id'}.get(table)
    if alias and row[alias]!=ident:raise ValueError('source_rowid_alias_changed')
    # No JSON parsing, timestamp normalization, ID substitution or default fill.
    return dict(row)


def chunk(table,rows):
    if not 1<=len(rows)<=MAX_ROWS:raise ValueError('source_row_limit')
    result=[map_row(table,row) for row in rows]
    ids=[r['source_rowid'] for r in result]
    if len(set(ids))!=len(ids):raise ValueError('duplicate_source_rowid')
    if len(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8'))>MAX_BYTES:
        raise ValueError('source_size_limit')
    return sorted(result,key=lambda row:row['source_rowid'])
