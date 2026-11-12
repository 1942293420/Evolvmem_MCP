"""Synthetic organization pages only; no services or retained business data."""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from evolvmem.history_migration import organization_complete as O
from evolvmem.history_migration import resume_planning as P

class OrganizationResumeTests(unittest.TestCase):
 def test_resumed_org_matches_original_summary_and_closes_each_page(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'synthetic.sqlite';final=Path(tmp)/'plan.json'
   c=sqlite3.connect(db)
   for evidence in O.S.M.EVIDENCE.values():c.execute(evidence['ddl'])
   for i in range(385):
    c.execute('INSERT INTO organization_tasks(source_key,source_revision,rule_revision,created_at,updated_at) VALUES(?,?,?,?,?)',
        (str(i),'r','r','original-time','original-time'))
   c.commit()
   real_page=O.S.page
   def pages():
    for table in O.S.TABLES:
     after=None
     while True:
      p=real_page(c,table,after);yield p
      if p['exhausted']:break
      after=p['end']
   expected=O.OP.summarize(pages(),{'physical_bytes':db.stat().st_size},385);c.close()
   source=SimpleNamespace(path=db,sha256='a'*64,receipt_sha256='b'*64,namespace='owner')
   class Parent:
    sha256='c'*64;run=SimpleNamespace(core_binding_hash='d'*64)
    header={'source':{'seal':'same'}};document={'source':{'seal':'same'}}
    def require_importable(self):pass
   parent=Parent()
   class Phase:
    def __init__(self,source,plan,seconds):self.seconds=seconds;self.source=source
    def __enter__(self):
     self.deadline=time.monotonic()+self.seconds;self.binding=parent.header['source'];self.proven_complete=False;return self
    def tick(self):
     if time.monotonic()>=self.deadline:raise RuntimeError('phase_deadline')
    def __exit__(self,*args):self.tick();self.proven_complete=args[0] is None
   class Plan:
    def __init__(self,d):self.document=d;self.sha256=hashlib.sha256(O.S.canonical(d)).hexdigest()
   seen=[];connections=[];real_connect=sqlite3.connect
   def page(connection,table,after):
    seen.append((table,after));p=real_page(connection,table,after);time.sleep(.02);return p
   def connect(*args,**kwargs):
    connection=real_connect(*args,**kwargs);connections.append(connection);return connection
   with patch.object(O.SP,'Plan',Parent),patch.object(O,'Plan',Plan),patch.object(O.W,'SealedPhase',Phase),patch.object(O.S,'page',page),patch.object(O.sqlite3,'connect',connect):
    for _ in range(30):
     result=O.make_plan(source,parent,run_id='00000000-0000-0000-0000-000000000001',path=final,seconds=.2)
     for connection in connections:
      with self.assertRaises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
     if not isinstance(result,P.PlanningProgress):break
     self.assertFalse(final.exists())
   self.assertTrue(final.exists());self.assertEqual(10,len(seen));self.assertEqual(len(seen),len(set(seen)))
   expected.update(format='organization-bounded-v1',source=parent.header['source'],parent=parent.document,
       run_id='00000000-0000-0000-0000-000000000001')
   self.assertEqual(expected,result.document)
   draft=json.loads(next(Path(tmp).glob('*.draft.json')).read_bytes())
   self.assertTrue(all('rows' not in page for page in draft['state']['pages']))

 def test_page_timer_closes_transaction_before_python_mapping_can_continue(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'synthetic.sqlite'
   c=sqlite3.connect(db)
   for evidence in O.S.M.EVIDENCE.values():c.execute(evidence['ddl'])
   c.close()
   phase=SimpleNamespace(source=SimpleNamespace(path=db),deadline=time.monotonic()+60,tick=lambda:None)
   callbacks=[]
   class Timer:
    def __init__(self,seconds,callback):self.seconds=seconds;callbacks.append(self);self.callback=callback
    def start(self):pass
    def cancel(self):pass
   with patch.object(O.threading,'Timer',Timer):
    with self.assertRaisesRegex(Exception,'organization_page_deadline'):
     with O._planning_connection(phase) as port:
      self.assertLessEqual(callbacks[0].seconds,10)
      callbacks[0].callback()
      port.execute('SELECT 1')

if __name__=='__main__':unittest.main()
