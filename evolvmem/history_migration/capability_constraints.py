"""Inspect exact B5 natural keys and namespace-scoped FKs in each transaction."""
from . import capability_mapping as M
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
        WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype IN ('p','u','f')''',(list(M.TABLES),))
    expected=[]
    for table,spec in M._EVIDENCE['namespaces']['owner'].items():
        expected.append((table,'p',True,False,['library_id','source_rowid'],'',[],'','',''))
        primary=[c['name'] for c in sorted(spec['columns'],key=lambda c:c['pk_position']) if c['pk_position']]
        unique=[primary] if primary else []
        for index in spec['indexes']:
            keys=[c['name'] for c in index['columns']]
            if index['unique'] and keys not in unique:unique.append(keys)
        for keys in unique:expected.append((table,'u',True,False,['library_id']+keys,'',[],'','',''))
        for fk in spec['foreign_keys']:
            expected.append((table,'f',True,False,['library_id',fk['from_column']],
                'em_content.'+fk['target_table'],['library_id',fk['to_column']],
                'a',{'NO ACTION':'a','CASCADE':'c'}[fk['on_delete']],'s'))
    M.check(sorted(actual,key=repr)==sorted(expected,key=repr),'capability_constraints_changed')
    checks=_rows(connection,'''SELECT c.relname,pg_get_constraintdef(k.oid),k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype='c' ''',(list(M.TABLES),))
    validate_checks(checks)


# Fixed PostgreSQL rendering of the six reviewed SQLite text checks. Only
# whitespace/parentheses/type casts are normalized, never terms or operators.
CHECKS={
 'capability_assessments': [("value",('reusable','reference','review','ignore')),('verification_status',('unverified',))],
 'capability_category_suggestions': [('status',('pending','accepted','rejected'))],
 'capability_intake': [('status',('pending','assigned','ignored'))],
 'capability_memories': [('status',('active','pending','ignored','archived'))],
 'capability_sources': [('source_kind',('context','unit','reference','manual'))],
}
def validate_checks(checks):
    import re
    def norm(s):
        # Quoted SQL literals are opaque, including doubled single quotes.
        # Only the surrounding fixed catalog rendering loses formatting.
        parts=re.split(r"('(?:''|[^'])*')",s)
        return ''.join(part if i%2 else re.sub(r'[\s()"]','',re.sub(r'::text\b','',part))
            for i,part in enumerate(parts))
    expected=[]
    for table,items in CHECKS.items():
        for field,values in items:
            expr=field+"='"+values[0]+"'" if len(values)==1 else field+"=ANYARRAY["+','.join("'"+v+"'" for v in values)+"]"
            expected.append((table,'CHECK'+expr,True))
    M.check(sorted((t,norm(expr),valid) for t,expr,valid in checks)==sorted(expected),'capability_check_changed')
