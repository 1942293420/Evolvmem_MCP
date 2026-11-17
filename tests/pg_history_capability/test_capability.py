"""B5 incremental synthetic preservation checks: no PG, source history or service."""
import importlib,json,os,sqlite3,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ['B5_NATIVE_PACKAGE_ROOT']) if os.environ.get('B5_NATIVE_PACKAGE_ROOT') else ROOT.parent/'pg-history-capture-integration-20261101/candidate'
sys.path.insert(0,str(BASE))
import evolvmem.history_migration as package
if not os.environ.get('B5_NATIVE_PACKAGE_ROOT'):
 package.__path__.insert(0,str(ROOT.parent/'pg-history-governance-20261101/candidate/evolvmem/history_migration'))
 package.__path__.insert(0,str(ROOT/'candidate/evolvmem/history_migration'))

def row(m,table,identity=1,**changes):
 r={'source_rowid':identity}
 for name,kind,nullable in m.SPECS[table]:r[name]=None if nullable else (1 if kind=='INTEGER' else 0.0 if kind=='REAL' else '')
 alias=m._EVIDENCE['namespaces']['owner'][table]['rowid_alias']
 if alias:r[alias]=identity
 r.update(changes);return r

class Preservation(unittest.TestCase):
 def setUp(self):
  try:self.m=importlib.import_module('evolvmem.history_migration.capability_mapping')
  except ModuleNotFoundError:self.fail('B5 fixed preservation implementation missing')
 def test_nullable_category_and_raw_manual_assessment_fields_survive(self):
  m=self.m
  r=row(m,'capability_categories',-7,id=None,label='人工',created_at='verbatim',updated_at='')
  self.assertEqual(m.unmap_row('capability_categories',m.map_row('capability_categories',r)),r)
  a=row(m,'capability_assessments',8,details=' {"x":1, "x":2} ',value='review',verification_status='unverified',score=-8,legacy_active_preserved=1)
  self.assertEqual(m.map_row('capability_assessments',a),a)
  self.assertNotEqual(m.chunk_hash('capability_assessments',[a]),m.chunk_hash('capability_assessments',[dict(a,details='{}')]))
 def test_all_nonstandard_integer_aliases_are_bound_to_rowid(self):
  for t in self.m.AUTO:
   r=row(self.m,t,-9223372036854775808)
   self.assertEqual(self.m.map_row(t,r),r)
   a=self.m._EVIDENCE['namespaces']['owner'][t]['rowid_alias']
   with self.assertRaises(self.m.ImportError):self.m.map_row(t,dict(r,**{a:2}))
 def test_namespace_absence_is_explicit(self):
  self.assertEqual(len(self.m.applicable('owner')),7)
  for ns in ('kane','public'):self.assertEqual(self.m.absent(ns),('existing_capability_assessment_runs',))
  self.assertEqual(len(self.m.AUTO),5)
 def test_lease_nulls_real_and_composite_audit_id_preserved(self):
  r=row(self.m,'existing_capability_assessment_runs',-1,plan_sha256='original',memory_id=3,source_id=4,state='result_unknown',lease_token='unchanged',lease_until=123.25,assessment_id=None,result_hash='raw')
  self.assertEqual(self.m.map_row('existing_capability_assessment_runs',r),r)
  with self.assertRaises(self.m.ImportError):self.m.map_row('existing_capability_assessment_runs',dict(r,lease_until=float('inf')))
 def test_reviewed_columns_fk_order_and_absent_schema(self):
  scan=importlib.import_module('evolvmem.history_migration.capability_scan')
  for ns in ('owner','kane','public'):
   with sqlite3.connect(':memory:') as c:
    for spec in self.m._EVIDENCE['namespaces'][ns].values():
     c.execute(spec['ddl'])
     for index in reversed(spec['indexes']):
      if index['ddl']:c.execute(index['ddl'])
    scan.validate_schema(c,ns)
    if ns!='owner':c.execute('CREATE TABLE existing_capability_assessment_runs(fake TEXT)')
    else:c.execute('ALTER TABLE capability_assessments ADD COLUMN fake TEXT')
    with self.assertRaises(self.m.ImportError):scan.validate_schema(c,ns)
 def test_latest_assessment_remains_latest_even_if_rejected(self):
  with sqlite3.connect(':memory:') as c:
   c.execute(self.m._EVIDENCE['namespaces']['owner']['capability_assessments']['ddl'])
   for identity,value in ((8,'reusable'),(12,'ignore')):
    r=row(self.m,'capability_assessments',identity,memory_id=1,source_id=3,assessment_key=str(identity),revision=identity,value=value,verification_status='unverified',details='raw manual')
    mapped=self.m.unmap_row('capability_assessments',self.m.map_row('capability_assessments',r));mapped.pop('source_rowid')
    c.execute('INSERT INTO capability_assessments('+','.join(mapped)+') VALUES('+','.join('?' for _ in mapped)+')',tuple(mapped.values()))
   self.assertEqual(c.execute('SELECT value FROM capability_assessments ORDER BY assessment_id DESC LIMIT 1').fetchone(),('ignore',))
 def test_absent_zero_highwater_and_exhausted_sequences(self):
  s=importlib.import_module('evolvmem.history_migration.capability_sequences')
  absent=dict(present=False,value=None,storage_type=None,autoincrement_ddl_verified=True,allocation_history_verified=False)
  zero=dict(absent,present=True,value=0,storage_type='integer')
  self.assertNotEqual(absent,zero)
  self.assertEqual(s.allocation(absent,0),(1,False));self.assertEqual(s.allocation(zero,0),(1,False))
  self.assertEqual(s.allocation(dict(zero,value=9000),2),(9001,False))
  self.assertEqual(s.allocation(dict(zero,value=9223372036854775807),2),(9223372036854775807,True))
  with self.assertRaises(self.m.ImportError):s.allocation(dict(absent,value=0),0)
class CheckPreservation(unittest.TestCase):
 def test_original_six_checks_accept_only_reviewed_postgres_renderings(self):
  m=importlib.import_module('evolvmem.history_migration.capability_constraints')
  valid=[('capability_assessments',"CHECK ((value = ANY (ARRAY['reusable'::text, 'reference'::text, 'review'::text, 'ignore'::text])))",True),
   ('capability_assessments',"CHECK ((verification_status = 'unverified'::text))",True),
   ('capability_category_suggestions',"CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text])))",True),
   ('capability_intake',"CHECK ((status = ANY (ARRAY['pending'::text, 'assigned'::text, 'ignored'::text])))",True),
   ('capability_memories',"CHECK ((status = ANY (ARRAY['active'::text, 'pending'::text, 'ignored'::text, 'archived'::text])))",True),
   ('capability_sources',"CHECK ((source_kind = ANY (ARRAY['context'::text, 'unit'::text, 'reference'::text, 'manual'::text])))",True)]
  m.validate_checks(valid)
  for broken in (valid[:-1],valid+valid[:1],[(t,x.replace('unverified','verified'),v) for t,x,v in valid],[(t,x,False) for t,x,v in valid]):
   with self.assertRaises(m.M.ImportError):m.validate_checks(broken)
 def test_check_normalization_preserves_literals_including_escaped_quotes(self):
  m=importlib.import_module('evolvmem.history_migration.capability_constraints')
  checks=[]
  for table,items in m.CHECKS.items():
   for field,values in items:
    literals=["'"+v+"'::text" for v in values]
    expression=field+' = '+literals[0] if len(values)==1 else field+' = ANY (ARRAY['+', '.join(literals)+'])'
    checks.append((table,'CHECK (('+expression+'))',True))
  # Whitespace outside literals, including line breaks, remains harmless.
  formatted=[(t,expression.replace('CHECK ((', 'CHECK \n ( ( ').replace(' = ', ' \t = \n '),v) for t,expression,v in checks]
  m.validate_checks(formatted)
  for literal in ("'archived '","'archi ved'","'archi(ved)'", "'archi\"ved'", "'archi::textved'", "'archi''ved'", "'Archived'"):
   changed=[(t,expression.replace("'archived'",literal),v) for t,expression,v in formatted]
   with self.subTest(literal=literal):
    with self.assertRaises(m.M.ImportError):m.validate_checks(changed)
  # Doubled quotes must stay part of the literal token; even synthetic values
  # exercising this tokenizer cannot lose their whitespace, cast or quote text.
  from unittest.mock import patch
  with patch.object(m,'CHECKS',{'capability_memories':[('status',("archi'' ved",))]}):
   m.validate_checks([('capability_memories',"CHECK ((status = 'archi'' ved'::text))",True)])
   with self.assertRaises(m.M.ImportError):m.validate_checks([('capability_memories',"CHECK ((status = 'archi''ved'::text))",True)])
if __name__=='__main__':unittest.main()
