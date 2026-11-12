"""Inspect exact B7/B8 natural keys and namespace-scoped FKs in each transaction."""
from . import operations_mapping as M
from .history_core.importer import _rows


def validate_constraints(connection):
    actual=_rows(connection,'''SELECT c.relname,k.contype,k.convalidated,k.condeferrable,
        ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY AS u(n,ord)
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=u.n ORDER BY u.ord),
        CASE WHEN k.contype='f' THEN fn.nspname||'.'||fc.relname ELSE '' END,
        ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY AS u(n,ord)
            JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=u.n ORDER BY u.ord),
        CASE WHEN k.contype='f' THEN k.confupdtype::text ELSE '' END,
        CASE WHEN k.contype='f' THEN k.confdeltype::text ELSE '' END,
        CASE WHEN k.contype='f' THEN k.confmatchtype::text ELSE '' END
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_class fc ON fc.oid=k.confrelid LEFT JOIN pg_namespace fn ON fn.oid=fc.relnamespace
        WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype IN ('p','u','f')''',(list(M.TARGET_TABLES),))
    expected=[]
    for table,spec in M.SCHEMA.items():
        expected.append((M.target_table(table),'p',True,False,['library_id','source_rowid'],'',[],'','',''))
        primary=[c['name'] for c in sorted(spec['columns'],key=lambda c:c['pk_position']) if c['pk_position']]
        unique=[primary] if primary else []
        for index in spec['indexes']:
            keys=[c['name'] for c in index['columns']]
            if index['unique'] and keys not in unique:unique.append(keys)
        for keys in unique:expected.append((M.target_table(table),'u',True,False,['library_id']+keys,'',[],'','',''))
        for fk in spec['foreign_keys']:
            expected.append((M.target_table(table),'f',True,False,['library_id',fk['from_column']],
                'em_content.'+M.TARGET_NAMES.get(fk['target_table'],fk['target_table']),['library_id',fk['to_column']],
                'a',{'NO ACTION':'a','CASCADE':'c'}[fk['on_delete']],'s'))
    M.check(sorted(actual,key=repr)==sorted(expected,key=repr),'operations_constraints_changed')
    checks=_rows(connection,'''SELECT c.relname,pg_get_constraintdef(k.oid),k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype='c' ''',(list(M.TARGET_TABLES),))
    expected_checks=[
        ('context_health_generation','CHECK ((id = 1))',True),
        ('context_health_generation','CHECK ((generation >= 0))',True),
        ('local_capture_scans','CHECK ((id = 1))',True),
        ('lan_publications',"CHECK ((author = ANY (ARRAY['jiangli'::text, 'kane'::text])))",True),
        ('lan_publications','CHECK ((active = ANY (ARRAY[(0)::bigint, (1)::bigint])))',True)]
    M.check(sorted(checks)==sorted(expected_checks),'operations_check_changed')
