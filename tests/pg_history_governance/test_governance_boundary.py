"""Only new B3 preservation boundaries; no PostgreSQL or production data access."""
import hashlib,json,os,pathlib,sqlite3,sys,tempfile,unittest
from unittest.mock import patch
from types import SimpleNamespace
BASE=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'candidate'))
from evolvmem.history_migration import governance_mapping as M,governance_scan as S,static_wal as W
PACKAGE_ROOT=pathlib.Path(M.__file__).resolve().parents[2]

def data_row(table,ident):
 row={'source_rowid':ident}
 choices={'state':'reusable','status':'active','scope':'future','verdict':'incorrect','source_kind':'unit','decision':'manual'}
 for name,kind,nullable in M.SPECS[table]:
  if name==M.ALIASES.get(table):row[name]=ident
  elif nullable:row[name]=None
  elif kind=='INTEGER':row[name]=17
  elif kind=='REAL':row[name]=.125
  else:row[name]=choices.get(name,' raw\n原文 " 不规范 JSON { } ')
 if table=='context_project_workspace_bindings':row.update(state='active',is_default=0)
 if table=='context_project_rollups':row['status']='ready'
 return row

class GovernanceBoundary(unittest.TestCase):
 def test_real_sealed_reader_all_three_namespace_layouts(self):
  default='D:/EvolvMem-test-tmp/governance-boundary' if os.name=='nt' else str(pathlib.Path(tempfile.gettempdir())/'evolvmem-governance-boundary')
  scratch=pathlib.Path(os.environ.get('EVOLVMEM_TEST_TMP_ROOT',default))
  self.assertTrue(scratch.is_absolute());scratch.mkdir(parents=True,exist_ok=True)
  scratch=scratch.resolve()
  for ns in ('owner','kane','public'):
   with self.subTest(namespace=ns),tempfile.TemporaryDirectory(dir=scratch) as directory:
    self.assertEqual(pathlib.Path(directory).resolve().parent,scratch)
    path=pathlib.Path(directory)/'source.db';c=sqlite3.connect(path);c.execute('PRAGMA journal_mode=WAL')
    expected={};specs=M._EVIDENCE['namespaces'][ns]
    for table in M.TABLES:
     if table not in specs:continue
     e=specs[table];c.execute(e['ddl'])
     for index in reversed(e['indexes']):
      if index['ddl']:c.execute(index['ddl'])
     ident=1 if table in M.ALIASES else -(2**63)
     row=data_row(table,ident)
     names=M.columns(table)
     c.execute('INSERT INTO "'+table+'"(_rowid_,'+','.join('"'+n+'"' for n in names)+') VALUES('+','.join('?' for _ in range(len(names)+1))+')',(ident,*(row[n] for n in names)))
     expected[table]=[row]
     if table=='capability_provenance_repairs':
      row2=dict(row,source_rowid=2);c.execute('INSERT INTO "'+table+'"(_rowid_,'+','.join('"'+n+'"' for n in names)+') VALUES('+','.join('?' for _ in range(len(names)+1))+')',(2,*(row2[n] for n in names)));expected[table].append(row2)
    # Preserve deleted allocation history, not just max currently visible id.
    c.execute("UPDATE sqlite_sequence SET seq=9001 WHERE name='organization_guidance'")
    c.commit();c.close()
    for suffix,n in (('-wal',0),('-shm',32768)):pathlib.Path(str(path)+suffix).write_bytes(bytes(n))
    path.chmod(0o400)
    try:
     files={k:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'stat':W._stat_info(p.stat())} for k,p in (('db',path),('wal',pathlib.Path(str(path)+'-wal')),('shm',pathlib.Path(str(path)+'-shm')))}
     raw=M.canonical(dict(format='controlled-retained-empty-wal-v1',namespace=ns,path=str(path.resolve()),basis='controlled_retained_copy_no_writers',os_immutable_proven=False,retention_evidence_sha256='a'*64,files=files))
     source=W.SealedWalSource.from_receipt(raw,hashlib.sha256(raw).hexdigest())
     with patch.object(W,'_active_identities',return_value=set()):
      with S.Phase(source) as phase:
       for i,table in enumerate(M.TABLES):
        page,args,seq,schema=phase.page(table,None,i)
        self.assertEqual(page['row_count'],len(expected.get(table,[])))
        self.assertTrue(page['exhausted']);self.assertEqual(schema,M.schema_hash(ns))
        self.assertEqual(args['rows'] if args else [],expected.get(table,[]))
        self.assertEqual(seq['organization_guidance'],dict(present=True,value=9001))
        if ns!='owner':self.assertEqual(seq['corrections'],dict(present=False,value=None))
      self.assertTrue(phase.proven_complete);self.assertEqual(phase.hash_passes,2)
     self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),files['db']['sha256'])
    finally:path.chmod(0o600)

 def test_null_keys_raw_decisions_and_types(self):
  # Two nullable legacy primary keys remain distinct original row identities.
  row=data_row('capability_provenance_repairs',-(2**63));other=dict(row,source_rowid=8)
  self.assertIsNone(row['plan_sha256'])
  self.assertEqual(M.ordered_rows('capability_provenance_repairs',[other,row]),[row,other])
  row=data_row('organization_guidance',3);self.assertEqual(M.unmap_row('organization_guidance',M.map_row('organization_guidance',row)),row)
  with self.assertRaisesRegex(M.ImportError,'alias'):M.map_row('organization_guidance',dict(row,id=4))
  with self.assertRaisesRegex(M.ImportError,'integer'):M.map_row('organization_guidance',dict(row,negative=True))
  with self.assertRaisesRegex(M.ImportError,'duplicate'):M.prepare_chunk('organization_guidance',[row,row],None)

 def test_new_domain_routes_use_actual_modules_and_fixed_sql(self):
  from evolvmem.history_migration import governance_importer as I,governance_plan as P,run_governance_batch as B,operator as O,operator_scope as R
  self.assertIs(I.Plan,P.Plan);self.assertIs(B.Plan,P.Plan);self.assertIs(B.GovernanceImporter,I.GovernanceImporter)
  self.assertEqual(O.PARENTS['governance'],('core','sources','organization'))
  self.assertEqual(R.DOMAIN_SCRIPTS['governance'],(7,))
  sql=(PACKAGE_ROOT/'evolvmem/history_migration/sql/001_history_governance.sql').read_text()
  self.assertEqual(sql.count('text COLLATE "C"'),sum(k=='TEXT' for t in M.TABLES for _,k,_ in M.SPECS[t]))
  self.assertNotIn('lan_session_uploads',sql)
  self.assertIn('history_governance_organization_parent_required',sql)

 def test_exhausted_allocation_is_preserved_not_rejected(self):
  from evolvmem.history_migration.governance_importer import sequence_restart
  top=2**63-1
  self.assertEqual(sequence_restart(top,1,False),(top,True))
  self.assertEqual(sequence_restart(0,top,True),(top,True))
  self.assertEqual(sequence_restart(top-1,1,False),(top,False))
  self.assertEqual(sequence_restart(9001,1,False),(9002,False))
  self.assertEqual(sequence_restart(0,20,True),(21,False))
  self.assertEqual(sequence_restart(0,1,False),(1,False))

 def test_replay_rejects_oversize_target_before_fetching_text(self):
  from evolvmem.history_migration import governance_importer as I
  target=I.C.Target('em_pilot_governance','owner','maint','00000000-0000-0000-0000-000000000001',1,1)
  port=I.GovernanceImporter(lambda:None,target,capacity_guard=lambda:None)
  port._receipt=lambda *_:('a'*64,[[1]],1,2)
  seen=[]
  class Result:
   def fetchall(self):return [(M.MAX_BYTES+1,)]
   def close(self):pass
  class Connection:
   def execute(self,sql,args):
    seen.append(sql)
    if 'octet_length' not in sql:raise AssertionError('fetched raw content before budget rejection')
    return Result()
  with self.assertRaisesRegex(M.ImportError,'governance_target_readback_budget'):
   port._checked(Connection(),SimpleNamespace(run_id='unused'),dict(row_count=1,input_hash='a'*64,input_bytes=2,table='organization_guidance'))
  self.assertEqual(len(seen),1)

if __name__=='__main__':unittest.main(verbosity=2)
