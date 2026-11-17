import test_operations
from contextlib import closing
import hashlib,json,os
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import patch
from evolvmem.history_migration import operations_mapping as M,operations_scan as N,operations_plan as P
from evolvmem.history_migration import static_wal as W,source_plan as SP,source_mapping as SM
from evolvmem.history_migration.source_importer import SourceRun,Target

SCRATCH=Path(os.environ.get('OPERATIONS_TEST_TMPDIR','D:/EvolvMem-test-tmp' if os.name=='nt' else '/tmp'))
if not SCRATCH.is_absolute():raise ValueError('absolute scratch required')
TARGET=Target('em_pilot_operations_fixture','operations_owner','operations_login','00000000-0000-0000-0000-000000000001',1,1)
RUN='00000000-0000-0000-0000-000000000004'

def source(folder,namespace='owner',mutate=None):
    path=Path(folder)/'synthetic-only.db'
    with closing(sqlite3.connect(path)) as c:
        c.execute('PRAGMA journal_mode=WAL')
        for spec in M._EVIDENCE['namespaces'][namespace].values():
            c.execute(spec['ddl'])
            for index in reversed(spec['indexes']):
                if index['ddl']:c.execute(index['ddl'])
        c.execute('INSERT INTO context_health_generation VALUES(1,7,92,4875)')
        c.execute('INSERT INTO lan_memory_revision VALUES(1,123)')
        if namespace=='owner':
            for i in range(130):
                c.execute('INSERT INTO organization_review_vectors(rowid,cache_key,vector) VALUES(?,?,?)',
                    (-9223372036854775808 if i==0 else i,None,b'\x00\xff\x80'+i.to_bytes(2,'big')))
            c.execute("INSERT INTO local_capture_files(id,session_id,path,device,inode,scanned_offset_bytes,archived_offset_bytes,file_state) VALUES(7,'old-session','/home/source/.codex/old.jsonl',123,456,987,765,'blocked')")
            c.execute("INSERT INTO local_capture_files(id,session_id,path,device,inode) VALUES(9223372036854775807,'deleted','/original',1,2)")
            c.execute('DELETE FROM local_capture_files WHERE id=9223372036854775807')
            c.execute("INSERT INTO local_capture_scans(id,last_scan_at,scans) VALUES(1,'old',55)")
            c.execute("INSERT INTO round_metrics(round,m1_valid_rate,measured_at) VALUES(-8,0.25,'raw-date')")
        if namespace in ('owner','kane'):
            c.execute("INSERT INTO lan_workspace_bindings(rowid,remote_key,fingerprint,workstream_id) VALUES(-8,NULL,'original','old-workstream')")
            c.execute('INSERT INTO lan_write_requests VALUES(?,?,?,?,?)',('original-user','publish','old-request','old-hash',' {"x":1,"x":2} '))
        if namespace=='public':c.execute("INSERT INTO lan_publications VALUES(8,'jiangli',0,'old-title','raw-summary','original-project')")
        if mutate:mutate(c)
        c.commit()
    for suffix,size in (('-wal',0),('-shm',32768)):Path(str(path)+suffix).write_bytes(bytes(size))
    path.chmod(0o400)
    files={name:dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),stat={k:getattr(p.stat(),'st_'+k) for k in W.STAT_FIELDS})
        for name,p in (('db',path),('wal',Path(str(path)+'-wal')),('shm',Path(str(path)+'-shm')))}
    raw=M.canonical(dict(format='controlled-retained-empty-wal-v1',namespace=namespace,path=str(path.resolve()),
        basis='controlled_retained_copy_no_writers',os_immutable_proven=False,retention_evidence_sha256='a'*64,files=files))
    return W.SealedWalSource.from_receipt(raw,hashlib.sha256(raw).hexdigest())

def parent(binding):
    run=SourceRun('00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000002','a'*64,
        binding['namespace'],binding['file_sha256'],SM.schema_hash(binding['namespace']),
        tuple((t,0) for t in SM.TABLES),tuple((t,0) for t in SM.AUTO))
    pages=[dict(table=t,ordinal=i,chunk_key='s'+str(i).zfill(6),after=None,end=None,exhausted=True,row_count=0,
        native_hash='a'*64,input_hash=None,input_bytes=0) for i,t in enumerate(SM.TABLES)]
    seq={t:dict(present=True,value=0,storage_type='integer') for t in SM.AUTO}
    return SP.Plan(dict(format='history-sources-seven-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
        source=binding,run=run.manifest,source_schema_hash=run.source_schema_hash,sequence=seq,highwaters_ready=True,input_bytes=0,pages=pages))

class Fixture(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='b78-synthetic-',dir=SCRATCH)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(SCRATCH.resolve()))
        self.patch=patch.object(W,'_active_identities',return_value=set());self.patch.start()
    def tearDown(self):
        self.patch.stop()
        for p in Path(self.temp.name).rglob('*'):
            if p.is_file():p.chmod(0o600)
        self.temp.cleanup()

class Stream(Fixture):
    def test_blob_stream_128_boundary_and_local_identity(self):
        ref=source(self.temp.name);before=hashlib.sha256(ref.path.read_bytes()).hexdigest()
        with N.Phase(ref) as phase:
            page,args,seq,_=phase.page('organization_review_vectors',None,0)
            self.assertEqual(len(args['rows']),128);self.assertFalse(page['exhausted'])
            self.assertEqual(args['rows'][0],dict(source_rowid=M.INT64_MIN,cache_key=None,vector=b'\x00\xff\x80\x00\x00'))
            tail,data,_,_=phase.page('organization_review_vectors',page['end'],1)
            self.assertEqual([r['source_rowid'] for r in data['rows']],[128,129]);self.assertTrue(tail['exhausted'])
            _,data,_,_=phase.page('local_capture_files',None,2)
            row=data['rows'][0]
            self.assertEqual((row['path'],row['device'],row['inode'],row['scanned_offset_bytes'],row['archived_offset_bytes'],row['file_state']),('/home/source/.codex/old.jsonl',123,456,987,765,'blocked'))
            self.assertEqual(seq['local_capture_files']['value'],M.INT64_MAX)
        self.assertTrue(phase.proven_complete);self.assertEqual(hashlib.sha256(ref.path.read_bytes()).hexdigest(),before)
    def test_all_namespaces_plans_and_parent_binding(self):
        for ns in ('owner','kane','public'):
            folder=Path(self.temp.name)/ns;folder.mkdir();ref=source(folder,ns)
            with N.Phase(ref) as phase:previous=parent(phase.binding)
            plan=P.plan_source(ref,previous,TARGET,folder/'plan.json',run_id=RUN)
            self.assertEqual(plan.page_count,len(M.applicable(ns))+(ns=='owner'))
            self.assertEqual(plan.header['not_applicable'],list(M.absent(ns)))
            self.assertEqual(set(plan.highwaters),{'local_capture_files'} if ns=='owner' else set())
            with N.Phase(ref,plan=plan) as phase:
                for i in range(plan.page_count):phase.read(plan.page(i))
            for kind in ('absence','parent','page'):
                changed=plan.document
                if kind=='absence':changed['not_applicable']=[]
                elif kind=='parent':changed['parent_manifest_sha256']='e'*64
                else:changed['pages'].pop()
                with self.assertRaises(M.ImportError):P.Plan(changed,previous,TARGET)
    def test_source_storage_drift_and_binary_expansion_are_rejected(self):
        mutations=(lambda c:c.execute("UPDATE organization_review_vectors SET vector='text' WHERE rowid=1"),
            lambda c:c.execute('UPDATE organization_review_vectors SET vector=? WHERE rowid=1',(bytes(M.MAX_BYTES//2+128),)),
            lambda c:c.execute('ALTER TABLE lan_write_requests ADD COLUMN drift TEXT'))
        for i,mutation in enumerate(mutations):
            folder=Path(self.temp.name)/str(i);folder.mkdir();ref=source(folder,mutate=mutation);phase=N.Phase(ref)
            with self.assertRaises(M.ImportError):
                with phase:phase.page('organization_review_vectors',None,0)
            self.assertFalse(phase.proven_complete)
    def test_source_absent_auto_and_explicit_zero_remain_distinct(self):
        found=[]
        for i,present in enumerate((False,True)):
            folder=Path(self.temp.name)/str(i);folder.mkdir()
            def mutate(c):
                c.execute('DELETE FROM local_capture_files')
                c.execute("DELETE FROM sqlite_sequence WHERE name='local_capture_files'")
                if present:c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('local_capture_files',0)")
            ref=source(folder,mutate=mutate)
            with N.Phase(ref) as phase:
                page,args,seq,_=phase.page('local_capture_files',None,0)
                self.assertTrue(page['exhausted']);self.assertIsNone(args)
                found.append(seq['local_capture_files'])
        self.assertFalse(found[0]['present']);self.assertIsNone(found[0]['value'])
        self.assertTrue(found[1]['present']);self.assertEqual(found[1]['value'],0)

if __name__=='__main__':unittest.main()
