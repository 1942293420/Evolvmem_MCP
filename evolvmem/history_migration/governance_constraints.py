"""Validate the fixed B3 key/FK/index and identity sequence catalog."""
import hashlib,json
from pathlib import Path
from .history_core.importer import _rows as rows
from .governance_mapping import check,TABLES,AUTO,ALIASES,TARGET_NAMES,TARGET_TABLES,target_table
raw=(Path(__file__).parent/'resources/governance_target_constraints.json').read_bytes()
check(hashlib.sha256(raw).hexdigest()=='e2acafaa9f70458c669ed599ee00ab0e1196ac21523da8cf827938ab82eac875','governance_constraint_contract_changed')
CONTRACT=json.loads(raw)
def check_sequences(c):
 for table,column in AUTO.items():
  name='em_content.'+table+'_'+column+'_seq'
  check(rows(c,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+table,column))==[(name,)],'governance_sequence_identity')
  actual=rows(c,"SELECT s.seqtypid='int8'::regtype,s.seqincrement,s.seqmin,s.seqmax,s.seqcache,s.seqcycle,p.relowner=current_user::regrole FROM pg_sequence s JOIN pg_class p ON p.oid=s.seqrelid WHERE s.seqrelid=%s::regclass",(name,))
  check(actual==[(True,1,1,2**63-1,1,False,True)],'governance_sequence_configuration')
def check_constraints(c):
 actual=rows(c,"""SELECT c.relname,k.conname,k.contype,k.convalidated,k.condeferrable,
  ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(n,o) JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=x.n ORDER BY x.o),
  r.relname,ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY x(n,o) JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=x.n ORDER BY x.o),
  CASE WHEN k.contype='f' THEN k.confupdtype END,CASE WHEN k.contype='f' THEN k.confdeltype END,CASE WHEN k.contype='f' THEN k.confmatchtype END,rn.nspname
  FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  LEFT JOIN pg_class r ON r.oid=k.confrelid LEFT JOIN pg_namespace rn ON rn.oid=r.relnamespace
  WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype IN ('p','u','f') ORDER BY c.relname,k.conname""",(list(TARGET_TABLES),))
 expected=[]
 for x in CONTRACT['keys_and_fks']:
  ref=x['ref'];expected.append((target_table(x['table']),x['name'],x['type'],True,False,x['columns'],TARGET_NAMES.get(ref[0],ref[0]) if ref else None,ref[1] if ref else [],'a' if ref else None,'a' if ref else None,'s' if ref else None,'em_content' if ref else None))
 check(actual==sorted(expected),'governance_keys_foreign_keys_changed')
 actual=rows(c,"""SELECT c.relname,i.relname,x.indisunique,x.indisvalid,x.indisready,
 ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(n,o) JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.n ORDER BY k.o),pg_get_expr(x.indpred,x.indrelid)
 FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND NOT EXISTS(SELECT 1 FROM pg_constraint k WHERE k.conindid=i.oid) ORDER BY c.relname,i.relname""",(list(TARGET_TABLES),))
 check(actual==sorted((target_table(t),name,u,True,True,cols,pred) for t,name,u,cols,pred in CONTRACT['indexes']),'governance_indexes_changed')
 expected=[]
 for ti,t in enumerate(TABLES):
  if t in ALIASES:expected.append((target_table(t),'hg_alias_'+str(ti),'CHECK (('+ALIASES[t]+' = source_rowid))'))
  if t=='organization_settings':expected.append((target_table(t),'hg_settings_singleton','CHECK ((id = 1))'))
  for name,values in CONTRACT['checks'].get(t,{}).items():
   expected.append((target_table(t),'hg_check_'+str(ti)+'_'+name,'CHECK (('+name+" = ANY (ARRAY["+', '.join("'"+v+"'::text" for v in values)+'])))'))
 actual=rows(c,"""SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content' AND c.relname=ANY(%s::text[]) AND k.contype='c' AND k.convalidated AND NOT k.condeferrable ORDER BY c.relname,k.conname""",(list(TARGET_TABLES),))
 check(actual==sorted(expected),'governance_checks_changed')
 check_sequences(c)
