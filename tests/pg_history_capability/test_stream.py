import test_capability
from copy import deepcopy
from dataclasses import asdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from evolvmem.history_migration import capability_mapping as M,capability_scan as N,capability_plan as P
from evolvmem.history_migration import static_wal as W,source_plan as SP,source_mapping as SM
from evolvmem.history_migration.source_importer import SourceRun,Target

SCRATCH=Path(os.environ.get('B5_TEST_TMPDIR','D:/EvolvMem-test-tmp' if os.name=='nt' else os.environ.get('TMPDIR','/tmp')))
if not SCRATCH.is_absolute():raise ValueError('B5 scratch must be absolute')
SCRATCH=SCRATCH.resolve()
TARGET=Target('em_pilot_capability_fixture','capability_owner','capability_login','00000000-0000-0000-0000-000000000001',1,1)
RUN='00000000-0000-0000-0000-000000000004'


def source(directory,namespace='owner',mutate=None):
    path=Path(directory)/'synthetic-only.db'
    with closing(sqlite3.connect(path)) as c:
        c.execute('PRAGMA journal_mode=WAL')
        for spec in M._EVIDENCE['namespaces'][namespace].values():
            c.execute(spec['ddl'])
            for index in reversed(spec['indexes']):
                if index['ddl']:c.execute(index['ddl'])
        for i in range(130):
            c.execute('INSERT INTO capability_categories(rowid,id,label,created_at,updated_at) VALUES(?,?,?,?,?)',
                (-9223372036854775808 if i==0 else i,'c' if i==0 else None,' {"x":1, "x":2} ','verbatim',''))
        c.execute("INSERT INTO capability_memories(memory_id,category,title,content,fingerprint,created_at,updated_at) VALUES(9,'c','manual','unchanged','f','old','')")
        c.execute("INSERT INTO capability_sources(source_id,memory_id,source_kind,source_key,source_ref,created_at,updated_at) VALUES(3,9,'manual','key','raw','old','')")
        for identity,value in ((8,'reusable'),(9223372036854775807,'ignore')):
            c.execute("INSERT INTO capability_assessments(assessment_id,memory_id,source_id,source_binding_revision,source_revision,unit_text_hash,policy_version,assessment_key,value,score,details,revision,created_at) VALUES(?,9,3,1,'raw','hash','old',?,?,2,'raw manual',?,'old')",(identity,str(identity),value,identity))
        c.execute("INSERT INTO capability_category_suggestions(suggestion_id,label,reason,created_at) VALUES(9000,'label','manual','old')")
        c.execute('DELETE FROM capability_category_suggestions')
        # No capability_intake sequence is deliberately different from 0.
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
    """Metadata-only parent for B5 plan tests; never a PG completion claim."""
    run=SourceRun('00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000002','a'*64,
        binding['namespace'],binding['file_sha256'],SM.schema_hash(binding['namespace']),
        tuple((t,0) for t in SM.TABLES),tuple((t,0) for t in SM.AUTO))
    pages=[dict(table=t,ordinal=i,chunk_key='s'+str(i).zfill(6),after=None,end=None,exhausted=True,row_count=0,
        native_hash='a'*64,input_hash=None,input_bytes=0) for i,t in enumerate(SM.TABLES)]
    seq={t:dict(present=True,value=0,storage_type='integer') for t in SM.AUTO}
    return SP.Plan(dict(format='history-sources-seven-manifest-v1',complete=True,sqlite_snapshot_token_resumable=False,
        source=binding,run=run.manifest,source_schema_hash=run.source_schema_hash,sequence=seq,highwaters_ready=True,input_bytes=0,pages=pages))


class Stream(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='b5-synthetic-',dir=SCRATCH)
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(SCRATCH))
        self.patch=patch.object(W,'_active_identities',return_value=set());self.patch.start()
    def tearDown(self):
        self.patch.stop()
        for p in Path(self.temp.name).glob('*'):p.chmod(0o600)
        self.temp.cleanup()
    def test_stream_128_boundary_preserves_nullable_keys_and_max_integer(self):
        ref=source(self.temp.name)
        before=hashlib.sha256(ref.path.read_bytes()).hexdigest()
        with N.Phase(ref) as phase:
            page,args,seq,sh=phase.page('capability_categories',None,0)
            self.assertEqual(len(args['rows']),128)
            self.assertEqual(args['rows'][0]['source_rowid'],-9223372036854775808)
            self.assertEqual(args['rows'][0]['id'],'c')
            self.assertEqual(args['rows'][0]['label'],' {"x":1, "x":2} ')
            tail,rows,_,_=phase.page('capability_categories',page['end'],1)
            self.assertEqual([r['source_rowid'] for r in rows['rows']],[128,129]);self.assertTrue(tail['exhausted'])
            action,data,_,_=phase.page('capability_assessments',None,2)
            self.assertEqual(data['rows'][-1]['assessment_id'],9223372036854775807)
            self.assertFalse(seq['capability_intake']['present']);self.assertIsNone(seq['capability_intake']['value'])
            self.assertEqual(seq['capability_category_suggestions']['value'],9000)
            self.assertEqual(seq['capability_assessments']['value'],9223372036854775807)
        self.assertTrue(phase.proven_complete)
        self.assertEqual(hashlib.sha256(ref.path.read_bytes()).hexdigest(),before)
    def test_plan_preserves_absence_exhaustion_and_rejects_fake_empty_table(self):
        ref=source(self.temp.name,'public')
        with N.Phase(ref) as phase:binding=phase.binding
        previous=parent(binding)
        plan=P.plan_source(ref,previous,TARGET,Path(self.temp.name)/'plan.json',run_id=RUN)
        self.assertEqual(plan.page_count,7) # 6 tables plus one overflow page.
        self.assertEqual(plan.header['not_applicable'],['existing_capability_assessment_runs'])
        self.assertIsNone(plan.highwaters['capability_intake'])
        self.assertEqual(plan.highwaters['capability_assessments'],9223372036854775807)
        with N.Phase(ref,plan=plan) as phase:
            self.assertEqual(len(phase.read(plan.page(0))['rows']),128)
        for mutation in ('absence','sequence','parent','page'):
            d=plan.document
            if mutation=='absence':d['not_applicable']=[]
            elif mutation=='sequence':d['sequence']['capability_intake']['value']=0
            elif mutation=='parent':d['parent_manifest_sha256']='e'*64
            else:d['pages'].pop()
            with self.assertRaises(M.ImportError):P.Plan(d,previous,TARGET)
    def test_oversize_wrong_storage_and_missing_table_fail_without_completion(self):
        # Small negative fixtures; the large text is a single bounded 4 MiB boundary probe.
        mutations=(lambda c:c.execute("UPDATE capability_categories SET revision='bad' WHERE rowid=1"),
                   lambda c:c.execute('DROP TABLE capability_intake'),
                   lambda c:c.execute('UPDATE capability_categories SET label=? WHERE rowid=1',('x'*(M.MAX_BYTES+1),)))
        for i,mutation in enumerate(mutations):
            folder=Path(self.temp.name)/str(i);folder.mkdir()
            ref=source(folder,mutate=mutation);phase=N.Phase(ref)
            with self.assertRaises(M.ImportError):
                with phase:phase.page('capability_categories',None,0)
            self.assertFalse(phase.proven_complete)
            ref.path.chmod(0o600)

if __name__=='__main__':unittest.main()
