"""New package/authority differences only; catalog port is not PostgreSQL proof."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'candidate').is_dir():sys.path.insert(0,str(ROOT/'candidate'))


def package():
    return importlib.import_module('evolvmem.history_migration.capture_importer')


class Result:
    def __init__(self,rows=()):self.rows=rows
    def fetchall(self):return self.rows


class Catalog:
    """Only models predicates used by the new trigger rejection checks."""
    def __init__(self,I,target,condition=None):self.I=I;self.target=target;self.condition=condition;self.queries=[]
    def execute(self,sql,args=()):
        I=self.I;M=I.M;self.queries.append(sql)
        if sql.startswith('LOCK TABLE '):return Result([])
        if sql=="SELECT current_setting('session_replication_role')":return Result([('origin',)])
        if 'SELECT version,migration_name' in sql:return Result([(1,'001_history_capture_three',self.target.library_id)])
        if "p.polname='history_capture_quarantine'" in sql:return Result([(t,)+(True,)*7 for t in sorted(M.TABLES)])
        if "p.polname='history_owner_only'" in sql:return Result([(t,)+(True,)*7 for t in sorted(I.LEDGER)])
        if "t.tgname='history_capture_generation'" in sql:
            predicates=('t.tgqual IS NULL',"t.tgattr=''::int2vector",'t.tgnargs=0','octet_length(t.tgargs)=0')
            self.trigger_sql=sql
            # Return exactly the requested predicate shape, so omission of a
            # predicate does not manufacture a rejection in the test port.
            flags=[True]*3
            flags += [p!=self.condition for p in predicates if p in sql]
            flags += [True]*5
            return Result([(t,*flags) for t in sorted(I.WATCH)])
        if 'FROM pg_attribute' in sql:
            rows=[]
            for t in sorted(M.TABLES):
                rows += [(t,'library_id','uuid',True),(t,'project_id','uuid',False),(t,'source_rowid','bigint',True)]
                rows += [(t,n,{'INTEGER':'bigint','REAL':'double precision','TEXT':'text'}[k],not null) for n,k,null in M.SPECS[t]]
            return Result(rows)
        if 'SELECT c.relname,k.contype' in sql:
            return Result([row for t in sorted(M.TABLES) for row in ((t,'p',True,False,'PRIMARY KEY (library_id, '+', '.join(M.KEYS[t])+')'),(t,'u',True,False,'UNIQUE (library_id, source_rowid)'))])
        if 'FROM pg_constraint' in sql:return Result([(True,'a','a','s',False,'FOREIGN KEY (library_id, archive_id) REFERENCES em_content.session_archives(library_id, id)')])
        raise AssertionError('Unexpected catalog query')


class CaptureIntegrationTests(unittest.TestCase):
    def target(self,I):return I.C.Target('em_pilot_capture_package','owner','maint','00000000-0000-0000-0000-000000000001',1,1)

    def test_packaged_modules_resolve_single_graph_and_pinned_resources(self):
        I=package();M=I.M;P=importlib.import_module('evolvmem.history_migration.capture_plan')
        scan=importlib.import_module('evolvmem.history_migration.capture_scan')
        runner=importlib.import_module('evolvmem.history_migration.run_capture_batch')
        self.assertIs(I.Plan,P.Plan);self.assertIs(runner.Plan,P.Plan);self.assertIs(runner.Phase,scan.Phase)
        self.assertIs(runner.CaptureImporter,I.CaptureImporter)
        self.assertEqual(hashlib.sha256((Path(I.__file__).parent/'resources/capture_reviewed_schema.json').read_bytes()).hexdigest(),'f120f586f289c4c5238a7550eb50e062c628235e4e34162eba2aab141d797016')
        self.assertEqual([len(M.columns(t)) for t in M.TABLES],[17,4,4])
        I.CaptureImporter(lambda:None,self.target(I),capacity_guard=lambda:None)
        self.assertFalse(any(name in sys.modules for name in ('capture_mapping','capture_scan','capture_plan','capture_importer','run_capture_batch')))

    def test_trigger_conditions_column_filter_and_arguments_are_rejected(self):
        I=package();target=self.target(I);port=I.CaptureImporter(lambda:None,target,capacity_guard=lambda:None)
        port._source=Mock();port._gate(Catalog(I,target))
        for condition in ('t.tgqual IS NULL',"t.tgattr=''::int2vector",'t.tgnargs=0','octet_length(t.tgargs)=0'):
            with self.subTest(condition=condition):
                c=Catalog(I,target,condition)
                with self.assertRaisesRegex(I.M.ImportError,'capture_generation_trigger_required'):port._gate(c)
                self.assertIn(condition,c.trigger_sql)

    def test_capture_keeps_existing_budget_wrapper_and_live_formal_authority(self):
        I=package();target=self.target(I)
        # Negative fixture deliberately tampers with a previously valid frozen
        # pilot target. Capture must still enter formal live authorization.
        object.__setattr__(target,'database','formal_history')
        port=I.CaptureImporter(lambda:None,target,capacity_guard=lambda:None)
        from evolvmem.history_migration.batched_importer import batch_budget,BudgetConnection
        from evolvmem.history_migration.operator_scope import LeaseConnection
        raw=SimpleNamespace(closed=False,autocommit=True,info=SimpleNamespace(transaction_status=0),close=lambda:None)
        control=SimpleNamespace(closed=False,execute=lambda *_:Result([]),rollback=lambda:None,close=lambda:None)
        seen=[]
        def authorize(conn,binding):seen.append(binding);raise RuntimeError('live-authority-reached')
        runtime=SimpleNamespace(target=target,deadline=time.monotonic()+30,guard=lambda:None,_content=authorize)
        lease=LeaseConnection(runtime,raw,control,True);port.factory=lambda:lease
        with batch_budget(port,10):
            wrapped=port.factory();self.assertIs(type(wrapped),BudgetConnection)
            with self.assertRaisesRegex(RuntimeError,'live-authority-reached'):port._gate(wrapped)
            self.assertEqual(seen,[True])
            lease.close()
            with self.assertRaisesRegex(I.M.ImportError,'operator_lease_closed'):port._gate(wrapped)
        with self.assertRaisesRegex(I.M.ImportError,'formal_operator_lease_required'):port._gate(object())

    def test_packaged_scanner_reads_new_sealed_source_without_aliases_or_mutation(self):
        I=package();M=I.M
        from evolvmem.history_migration import static_wal as W,capture_scan
        scratch=Path(os.environ.get('EVOLVMEM_TEST_TMP_ROOT','D:/EvolvMem-test-tmp/capture-package' if os.name=='nt' else str(Path.home()/'.cache/evolvmem-capture-package')))
        scratch.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            path=Path(directory)/'new-capture-only.db'
            c=sqlite3.connect(path);c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE allocation_marker(id INTEGER PRIMARY KEY AUTOINCREMENT)')
            # No archive references are inserted: only a NULL head and a
            # backfill row exercise the actual newly packaged reader/resource.
            for t in M.TABLES:
                e=M._EVIDENCE['namespaces']['owner'][t];c.execute(e['ddl'])
                for index in reversed(e['indexes']):
                    if index['ddl']:c.execute(index['ddl'])
            c.execute("INSERT INTO lan_session_heads(rowid,device_id,session_id,sha256,source_order) VALUES(?,?,?,?,?)",(-(2**63),'device','session',None,None))
            c.execute("INSERT INTO lan_session_backfills(rowid,device_id,session_id,applied_revision,workstream_id) VALUES(?,?,?,?,?)",(-(2**63),'device','session',17,'original'))
            c.commit();c.close()
            for suffix,n in (('-wal',0),('-shm',32768)):
                with open(str(path)+suffix,'xb') as f:f.write(bytes(n))
            path.chmod(0o400)
            files={}
            for k,p in (('db',path),('wal',Path(str(path)+'-wal')),('shm',Path(str(path)+'-shm'))):
                s=p.stat();files[k]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'stat':{v:getattr(s,'st_'+v) for v in W.STAT_FIELDS}}
            receipt=M.canonical(dict(format='controlled-retained-empty-wal-v1',namespace='owner',path=str(path.resolve()),basis='controlled_retained_copy_no_writers',os_immutable_proven=False,retention_evidence_sha256='a'*64,files=files))
            ref=W.SealedWalSource.from_receipt(receipt,hashlib.sha256(receipt).hexdigest())
            with patch.object(W,'_active_identities',return_value=set()):
                with capture_scan.Phase(ref) as phase:
                    _,args,_,_=phase.page('lan_session_heads',None,0)
                    self.assertEqual(args['rows'],[dict(source_rowid=-(2**63),device_id='device',session_id='session',sha256=None,source_order=None)])
                    _,args,_,_=phase.page('lan_session_backfills',None,1)
                    self.assertEqual(args['rows'][0]['applied_revision'],17)
                self.assertTrue(phase.proven_complete)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),files['db']['sha256'])
            path.chmod(0o600)  # Only this new test file, to allow Windows cleanup.


if __name__=='__main__':unittest.main()
