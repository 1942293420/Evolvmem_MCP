"""Canonical native owner/probe regression: real imports, no AST/artifact loader.

Actual SQLite quick_check/lock tests use only synthetic files. One additional
long-VM case substitutes a fixed recursive SQL statement and is labeled apart.
"""
import inspect,sqlite3,tempfile,time,unittest
from pathlib import Path
from functools import wraps
from unittest.mock import patch
from evolvmem import physical_integrity as P
from evolvmem.context_service import ContextService
CONNECT=sqlite3.connect

def probe(connect):
 @wraps(ContextService._scan_integrity_readonly)
 def call(*a,**k):
  with patch('evolvmem.context_service.sqlite3.connect',connect):
   return ContextService._scan_integrity_readonly(*a,**k)
 return call

class OwnerBudgetTests(unittest.TestCase):
 def setUp(self):
  self.now=0.;self.identity=('synthetic',1,2,3);self.config='fixed'
  self.owner=P.PhysicalProofOwner(self.identity,self.config,refresh=lambda:None,clock=lambda:self.now)
 def check(self,fn,deadline=300.):
  kw={'current_config':lambda:self.config}
  if 'deadline' in inspect.signature(self.owner.check).parameters:kw['deadline']=deadline
  return self.owner.check(self.identity,self.config,300.,fn,**kw)
 def view(self):return self.owner.observe(self.identity,self.config,300.)
 def test_overbudget_eventual_ok_never_renews_success(self):
  def fn():self.now=2770.858;return ()
  codes,stamp=self.check(fn)
  self.assertEqual(('quick_check_timeout',),codes);self.assertIsNone(stamp)
  self.assertEqual('timeout',self.view().state);self.assertIsNone(self.view().checked_at)
 def test_timeout_retires_old_success_and_changes_token_without_renewal(self):
  self.check(lambda:());before=self.owner.token(self.identity,self.config,300.);self.now=180.
  def fn():raise getattr(P,'PhysicalScanTimeout',TimeoutError)()
  self.check(fn,480.);after=self.owner.token(self.identity,self.config,300.)
  self.assertNotEqual(before,after);self.assertEqual('timeout',self.view().state);self.assertIsNone(self.view().checked_at)
  self.assertEqual(('quick_check_timeout',),self.view().codes)
 def test_legacy_no_deadline_keeps_existing_eventual_success_semantics(self):
  def fn():self.now=301.;return ()
  self.owner.check(self.identity,self.config,300.,fn,current_config=lambda:self.config)
  self.assertEqual('ok',self.view().state)
 def test_late_close_and_boundary_cannot_publish(self):
  def fn():self.owner.close();return ()
  with self.assertRaisesRegex(RuntimeError,'physical_owner_closed'):self.check(fn)
  self.assertEqual('closed',self.view().state)
 def test_expired_scan_failure_does_not_allow_overlap_and_can_recover(self):
  self.now=180.
  def fn():
   with self.assertRaisesRegex(RuntimeError,'physical_scan_running'):self.check(lambda:())
   self.now=481.;return ()
  self.check(fn,480.);self.assertEqual('timeout',self.view().state)
  self.now=500.;self.check(lambda:(),800.);self.assertEqual('ok',self.view().state);self.assertEqual(500.,self.view().checked_at)

class ProbeBudgetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'synthetic.db'
  c=CONNECT(self.path);c.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)');c.execute("INSERT INTO sample VALUES(1,'synthetic')");c.commit();cookie=c.execute('PRAGMA schema_version').fetchone()[0];c.close()
  st=self.path.stat();self.identity=(str(self.path.resolve()),st.st_dev,st.st_ino,cookie)
 def tearDown(self):self.tmp.cleanup()
 def require(self,fn):self.assertIn('deadline',inspect.signature(fn).parameters,'probe deadline API missing')
 def test_real_long_vm_is_interrupted_and_closed(self):
  seen=[];clock=[0.];calls=[]
  def now():clock[0]+=.01;return clock[0]
  class Conn(sqlite3.Connection):
   def __init__(s,*a,**k):super().__init__(*a,**k);s.closed=False;seen.append(s)
   def execute(s,sql,*a):
    if sql=='PRAGMA quick_check':sql="WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2000000) SELECT 'ok' FROM (SELECT sum(x) FROM n)"
    try:return super().execute(sql,*a)
    except sqlite3.Error as exc:calls.append(getattr(exc,'sqlite_errorcode',None));raise
   def close(s):super().close();s.closed=True
  fn=probe(lambda *a,**k:CONNECT(*a,**k,factory=Conn));self.require(fn)
  with self.assertRaises(getattr(P,'PhysicalScanTimeout',TimeoutError)):fn(self.path,self.identity,deadline=1.,clock=now)
  self.assertIn(sqlite3.SQLITE_INTERRUPT,calls);self.assertTrue(seen[0].closed)
 def test_lock_wait_is_short_and_never_certifies_health(self):
  calls=[];seen=[]
  class Conn(sqlite3.Connection):
   def __init__(s,*a,**k):super().__init__(*a,**k);s.closed=False;seen.append(s)
   def execute(s,*a,**k):
    try:return super().execute(*a,**k)
    except sqlite3.Error as exc:calls.append(getattr(exc,'sqlite_errorcode',None));raise
   def close(s):super().close();s.closed=True
  fn=probe(lambda *a,**k:CONNECT(*a,**k,factory=Conn));self.require(fn)
  writer=CONNECT(self.path);writer.execute('BEGIN EXCLUSIVE');start=time.monotonic()
  try:
   with self.assertRaises(getattr(P,'PhysicalScanBusy',RuntimeError)):fn(self.path,self.identity,deadline=start+1.)
  finally:writer.rollback();writer.close()
  self.assertIn(sqlite3.SQLITE_BUSY,calls);self.assertLess(time.monotonic()-start,.8);self.assertTrue(seen[0].closed)
 def test_actual_quick_check_vm_can_be_interrupted(self):
  seed=CONNECT(self.path)
  seed.executemany('INSERT INTO sample VALUES(?,?)',((i,'synthetic')for i in range(2,4001)))
  seed.commit();seed.close();codes=[];closed=[];clock=[0.]
  class Conn(sqlite3.Connection):
   def execute(s,*a,**k):
    try:return super().execute(*a,**k)
    except sqlite3.Error as exc:codes.append(exc.sqlite_errorcode);raise
   def close(s):super().close();closed.append(True)
  def now():clock[0]+=.1;return clock[0]
  fn=probe(lambda *a,**k:CONNECT(*a,**k,factory=Conn));self.require(fn)
  with self.assertRaises(getattr(P,'PhysicalScanTimeout',TimeoutError)):fn(self.path,self.identity,deadline=1.,clock=now)
  self.assertIn(sqlite3.SQLITE_INTERRUPT,codes);self.assertEqual([True],closed)
 def test_final_close_crossing_deadline_cannot_return_ok(self):
  clock=[0.];seen=[]
  class Conn(sqlite3.Connection):
   def close(s):super().close();clock[0]=2.;seen.append(True)
  fn=probe(lambda *a,**k:CONNECT(*a,**k,factory=Conn));self.require(fn)
  with self.assertRaises(getattr(P,'PhysicalScanTimeout',TimeoutError)):fn(self.path,self.identity,deadline=1.,clock=lambda:clock[0])
  self.assertEqual([True],seen)
 def test_real_shared_cache_table_lock_uses_primary_sqlite_code(self):
  calls=[];seen=[]
  class Conn(sqlite3.Connection):
   def execute(s,*a,**k):
    try:return super().execute(*a,**k)
    except sqlite3.Error as exc:calls.append(getattr(exc,'sqlite_errorcode',None));raise
   def close(s):super().close();seen.append(True)
  writer=CONNECT(self.path.resolve().as_uri()+'?cache=shared',uri=True)
  writer.execute('BEGIN');writer.execute('CREATE TABLE held_schema(value TEXT)')
  fn=probe(lambda address,**k:CONNECT(address+'&cache=shared',**k,factory=Conn));self.require(fn)
  try:
   with self.assertRaises(getattr(P,'PhysicalScanBusy',RuntimeError)):fn(self.path,self.identity,deadline=time.monotonic()+1.)
  finally:writer.rollback();writer.close()
  self.assertTrue(calls and (calls[0]&255)==sqlite3.SQLITE_LOCKED,calls);self.assertEqual([True],seen)
 def test_unbounded_default_retains_original_call_and_complete_result(self):
  seen=[]
  def connect(*a,**k):seen.append(k.copy());return CONNECT(*a,**k)
  self.assertEqual((),probe(connect)(self.path,self.identity));self.assertEqual([{'uri':True}],seen)
