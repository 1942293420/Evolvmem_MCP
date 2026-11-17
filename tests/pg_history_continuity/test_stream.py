import test_continuity
from contextlib import closing
import hashlib,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from evolvmem.history_migration import continuity_mapping as M,continuity_scan as N,continuity_plan as P
from evolvmem.history_migration import static_wal as W,retained_chain as R
from evolvmem.history_migration.history_core.importer import Target
from evolvmem.history_migration.history_core.mapping import TABLES as CORE_TABLES

SCRATCH=Path(os.environ.get('B6_TEST_TMPDIR','D:/EvolvMem-test-tmp' if os.name=='nt' else os.environ.get('TMPDIR','/tmp')))
if not SCRATCH.is_absolute():raise ValueError('B6 scratch must be absolute')
SCRATCH=SCRATCH.resolve()
TARGET=Target('em_pilot_continuity_fixture','continuity_owner','continuity_login','00000000-0000-0000-0000-000000000001',1,1)
RUN='00000000-0000-0000-0000-000000000006'

def source(folder,namespace='owner',mutate=None):
    path=Path(folder)/'synthetic-only.db'
    with closing(sqlite3.connect(path)) as c:
        c.execute('PRAGMA journal_mode=WAL')
        for spec in M._EVIDENCE['namespaces'][namespace].values():
            c.execute(spec['ddl'])
            for index in reversed(spec['indexes']):
                if index['ddl']:c.execute(index['ddl'])
        for i in range(130):
            c.execute('''INSERT INTO continuity_workstreams(rowid,id,project,workspace_fingerprint,parent_id,current_context_id,
                checkpoint_revision,state_version,status,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (-2**63 if i==0 else i,'ws_'+str(i),'p','fingerprint','ws_129' if i==0 else 'ws_0',7,37,41,'completed','raw before','raw after','manual'))
        c.execute("INSERT INTO continuity_events(id,event_type,created_at) VALUES(0,'checkpoint','raw')")
        c.execute("INSERT INTO project_events(event_id,project,event_type,recorded_at,title,content,fingerprint,created_at,updated_at) VALUES(9223372036854775807,'p','problem','date','title','raw','fp','date','date')")
        c.execute('DELETE FROM project_events')
        c.execute("INSERT INTO continuity_focus(project,workspace_fingerprint,workstream_id,revision,updated_at) VALUES('p','fingerprint','ws_129',77,'raw')")
        if mutate:mutate(c)
        c.commit()
    for suffix,size in (('-wal',0),('-shm',32768)):Path(str(path)+suffix).write_bytes(bytes(size))
    path.chmod(0o400)
    files={name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'stat':{k:getattr(p.stat(),'st_'+k) for k in W.STAT_FIELDS}}
        for name,p in (('db',path),('wal',Path(str(path)+'-wal')),('shm',Path(str(path)+'-shm')))}
    raw=M.canonical(dict(format='controlled-retained-empty-wal-v1',namespace=namespace,path=str(path.resolve()),
        basis='controlled_retained_copy_no_writers',os_immutable_proven=False,retention_evidence_sha256='a'*64,files=files))
    return W.SealedWalSource.from_receipt(raw,hashlib.sha256(raw).hexdigest())

def parent(binding):
    pages=[dict(table=t,ordinal=i,chunk_key='p'+str(i).zfill(6),after=None,end=None,exhausted=True,
        row_count=0,native_hash='a'*64,input_hash=None,input_bytes=0) for i,t in enumerate(CORE_TABLES)]
    seq={t:dict(present=True,value=0,storage_type='integer') for t in ('memories','context_items','context_layers')}
    return R.Plan(dict(format='history-retained-six-v1',run_id='00000000-0000-0000-0000-000000000002',source=binding,
        source_schema_hash='a'*64,sequence=seq,highwaters_ready=True,project_mapping=[],counts={t:0 for t in CORE_TABLES},
        input_bytes=0,pages=pages,complete=True,sqlite_snapshot_token_resumable=False))

class Fixture(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='b6-synthetic-',dir=SCRATCH)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(SCRATCH.resolve()))
        # Avoid active-source discovery; the tested file is only our local synthetic fixture.
        self.patch=patch.object(W,'_active_identities',return_value=set());self.patch.start()
    def tearDown(self):
        self.patch.stop()
        for p in Path(self.temp.name).rglob('*'):
            if p.is_file():p.chmod(0o600)
        self.temp.cleanup()
    def plan(self,namespace='owner',mutate=None):
        self.ref=source(self.temp.name,namespace,mutate)
        with N.Phase(self.ref) as phase:previous=parent(phase.binding)
        return P.plan_source(self.ref,previous,TARGET,Path(self.temp.name)/'plan.json',run_id=RUN)

class Stream(Fixture):
    def test_non_id_auto_aliases_are_read_without_renaming(self):
        def change(c):
            c.execute("INSERT INTO project_events(event_id,project,event_type,recorded_at,title,content,fingerprint,created_at,updated_at) VALUES(6,'p','feature','recorded','title','body','fp6','raw','raw')")
            c.execute("INSERT INTO project_event_sources(source_id,event_id,binding_key,source_kind,source_key,source_ref,created_at) VALUES(9,6,'b','manual','key','ref','raw')")
        plan=self.plan(mutate=change)
        with N.Phase(self.ref,plan=plan) as phase:
            events=next(p for p in plan.document['pages'] if p['table']=='project_events')
            evidence=next(p for p in plan.document['pages'] if p['table']=='project_event_sources')
            self.assertEqual(phase.read(events)['rows'][0]['event_id'],6)
            self.assertEqual(phase.read(evidence)['rows'][0]['source_id'],9)
    def test_bounded_stream_cycle_and_three_sequence_states(self):
        plan=self.plan('public')
        self.assertEqual(plan.page_count,8) # 7 present tables plus overflow.
        self.assertEqual(plan.header['not_applicable'],['project_history_date_repairs'])
        self.assertEqual(plan.highwaters,{'continuity_events':0,'project_events':2**63-1,'project_event_sources':None})
        with N.Phase(self.ref,plan=plan) as phase:
            first=phase.read(plan.page(1));last=phase.read(plan.page(2))
            self.assertEqual(len(first['rows']),128)
            self.assertEqual(first['rows'][0]['source_rowid'],-2**63)
            self.assertEqual(first['rows'][0]['parent_id'],'ws_129')
            self.assertEqual(last['rows'][-1]['parent_id'],'ws_0')
            self.assertEqual((last['rows'][-1]['current_context_id'],last['rows'][-1]['checkpoint_revision'],last['rows'][-1]['state_version']),(7,37,41))
        self.assertTrue(phase.proven_complete);self.assertEqual(phase.hash_passes,2)
        for mutation in ('absence','parent','sequence'):
            d=plan.document
            if mutation=='absence':d['not_applicable']=[]
            elif mutation=='parent':d['parent_manifest_sha256']='e'*64
            else:d['sequence']['project_event_sources']['value']=0
            with self.assertRaises(M.ImportError):P.Plan(d,plan.parent_plan,TARGET)
    def test_oversize_and_wrong_storage_never_complete(self):
        for i,change in enumerate((lambda c:c.execute("UPDATE continuity_workstreams SET state_version='bad' WHERE rowid=1"),
            lambda c:c.execute('UPDATE continuity_workstreams SET parent_id=? WHERE rowid=1',('x'*(M.MAX_BYTES+1),)))):
            folder=Path(self.temp.name)/str(i);folder.mkdir();ref=source(folder,mutate=change);phase=N.Phase(ref)
            with self.assertRaises(M.ImportError):
                with phase:phase.page('continuity_workstreams',None,0)
            self.assertFalse(phase.proven_complete)

if __name__=='__main__':unittest.main()
