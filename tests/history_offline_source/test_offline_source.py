"""Only new offline-source cases, using real small SQLite backup files."""
from pathlib import Path
import hashlib,importlib.util,json,os,sqlite3,sys,tempfile,unittest
from unittest.mock import patch

ROOT=Path(os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT',str(Path(__file__).resolve().parents[2])))
sys.path[:0]=[str(ROOT)]
import evolvmem.history_migration
from evolvmem.history_migration import static_wal as W

class OfflineSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='evolvmem-offline-source-')
        self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name);self.path=root/'retained.db'
        live=sqlite3.connect(root/'original.db')
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE original(id INTEGER PRIMARY KEY, value TEXT)')
        live.execute('INSERT INTO original VALUES(17,?)',('原文 {"decision":"accepted"}',));live.commit()
        target=sqlite3.connect(self.path);live.backup(target);target.close();live.close()
        self.path.chmod(0o400)
        self.addCleanup(lambda:self.path.chmod(0o600) if self.path.exists() else None)

    def receipt(self,kind='controlled-retained-db-only-v1'):
        st=self.path.stat()
        return W.M.canonical(dict(format=kind,namespace='owner',path=str(self.path),
            basis='controlled_retained_copy_no_writers',os_immutable_proven=False,
            retention_evidence_sha256='e'*64,files={'db':dict(
                stat={k:getattr(st,'st_'+k) for k in W.STAT_FIELDS},
                sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())}))

    def source(self,kind='controlled-retained-db-only-v1'):
        raw=self.receipt(kind)
        return W.SealedWalSource.from_receipt(raw,hashlib.sha256(raw).hexdigest())

    def test_real_backup_reads_without_creating_sidecars(self):
        source=self.source();before=self.path.read_bytes()
        with W.SealedPhase(source) as phase:
            self.assertEqual('controlled_retained_db_only',phase.binding['source_kind'])
            with sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
                self.assertEqual([(17,'原文 {"decision":"accepted"}')],conn.execute('SELECT * FROM original').fetchall())
        self.assertTrue(phase.proven_complete)
        self.assertEqual(before,self.path.read_bytes())
        self.assertFalse(Path(str(self.path)+'-wal').exists())
        self.assertFalse(Path(str(self.path)+'-shm').exists())

    def test_writable_active_or_sidecar_source_cannot_be_sealed_offline(self):
        self.path.chmod(0o600)
        with self.assertRaisesRegex(W.M.ImportError,'sealed_source_not_readonly'):self.source()
        self.path.chmod(0o400);source=self.source()
        with patch.object(W,'_active_identities',return_value={(self.path.stat().st_dev,self.path.stat().st_ino)}):
            with self.assertRaisesRegex(W.M.ImportError,'active_source_inode'):
                with W.SealedPhase(source):pass
        Path(str(self.path)+'-wal').write_bytes(b'')
        with self.assertRaisesRegex(W.M.ImportError,'offline_sidecar_present'):
            with W.SealedPhase(source):pass

    def test_old_wal_format_still_requires_all_three_files(self):
        with self.assertRaisesRegex(W.M.ImportError,'sealed_three_files_required'):
            self.source('controlled-retained-empty-wal-v1')

if __name__=='__main__':unittest.main()
