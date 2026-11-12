import unittest
import os
from dataclasses import asdict
from evolvmem.history_migration.operator_config import load_config, private_read
from evolvmem.history_migration.operator_scope import OperatorRuntime, LeaseConnection
from evolvmem.history_migration.history_core.importer import Target

class BoundaryTests(unittest.TestCase):
    def test_unbound_formal_target_rejected(self):
        with self.assertRaisesRegex(Exception, 'synthetic_target_required'):
            Target('em_history_owner','owner','login','00000000-0000-0000-0000-000000000001',1,1)
    def test_old_manifest_shape(self):
        target=Target('em_pilot_test','owner','login','00000000-0000-0000-0000-000000000001',1,1)
        self.assertEqual(set(asdict(target)), {'database','owner_role','login_role','library_id','route_epoch','policy_epoch'})
    @unittest.skipUnless(os.name=="nt", "Windows refusal only")
    def test_windows_private_files_fail_closed(self):
        import os
        if os.name=='nt':
            with self.assertRaisesRegex(Exception,'operator_posix_required'):
                private_read(__file__)


class Cursor:
    def __init__(self,rows=()):self.rows=rows
    def fetchall(self):return self.rows
class Port:
    def __init__(self,rows=()):
        from types import SimpleNamespace
        self.closed=False;self.autocommit=True;self.info=SimpleNamespace(transaction_status=0)
        self.rows=rows;self.calls=[];self.commits=0
    def execute(self,sql,args=None):self.calls.append(sql);return Cursor(self.rows)
    def close(self):self.closed=True
    def rollback(self):self.calls.append('rollback')
    def commit(self):self.commits+=1

class LeaseTests(unittest.TestCase):
    def lease(self):
        from types import SimpleNamespace
        import time
        target=Target('em_pilot_test','owner','login','00000000-0000-0000-0000-000000000001',1,1)
        seen=[]
        runtime=SimpleNamespace(target=target,deadline=time.monotonic()+60,guard=lambda:None,_content=lambda raw,binding:seen.append(binding))
        raw=Port();control=Port()
        return LeaseConnection(runtime,raw,control,True),seen
    def test_known_kernel_wrappers_preserve_live_authority(self):
        from evolvmem.history_migration.operator_target import gate
        from evolvmem.history_migration.batched_importer import BudgetConnection,Budget
        from evolvmem.history_migration.organization_complete import DeadlinePort
        import time
        lease,seen=self.lease()
        wrapped=DeadlinePort(BudgetConnection(lease,Budget(5)),time.monotonic()+5)
        gate(wrapped,lease.runtime.target)
        self.assertEqual(seen,[True])
        lease.close()
        with self.assertRaisesRegex(Exception,'operator_lease_closed'):gate(wrapped,lease.runtime.target)
        self.assertTrue(lease.control.closed)
    def test_expired_lease_cannot_commit(self):
        lease,_=self.lease();lease.deadline=0
        with self.assertRaisesRegex(Exception,'operator_lease_expired'):lease.commit()
        self.assertEqual(lease.raw.commits,0)
        lease.close();self.assertTrue(lease.control.closed)
    def test_mismatched_target_cannot_authorize(self):
        lease,_=self.lease()
        other=Target('em_pilot_other','owner','login',lease.runtime.target.library_id,1,1)
        with self.assertRaisesRegex(Exception,'operator_target_mismatch'):lease.authorize(other)
        lease.close()
    def test_raw_cleanup_failure_still_closes_control(self):
        lease,_=self.lease()
        def fail():raise RuntimeError('connection lost')
        lease.raw.close=fail
        with self.assertRaises(RuntimeError):lease.close()
        self.assertTrue(lease.control.closed)
    def test_control_drift_closes_before_content_open(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        target={'library_id':'00000000-0000-0000-0000-000000000001','owner_principal_id':'00000000-0000-0000-0000-000000000002','connection_ref':'formal','route_epoch':2,'policy_epoch':3}
        expected=(target['library_id'],'legacy',target['owner_principal_id'],None,None,'postgres','formal',False,2,3,'pg_staging','none','updating')
        for index,value in ((2,'00000000-0000-0000-0000-000000000003'),(6,'wrong'),(8,9),(9,9),(10,'pg_active')):
            rows=list(expected);rows[index]=value
            control=Port([tuple(rows)]);runtime=object.__new__(OperatorRuntime)
            runtime.config=SimpleNamespace(document={'target':target});runtime.guard=lambda:None
            with patch.object(runtime,'_connect',return_value=control) as connect:
                with self.assertRaisesRegex(Exception,'operator_control_binding_mismatch'):runtime._open()
                self.assertEqual(connect.call_count,1);self.assertTrue(control.closed)
    def test_capacity_reserves_peak_and_memory_floor(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import time
        runtime=object.__new__(OperatorRuntime);runtime.deadline=time.monotonic()+60
        runtime.config=SimpleNamespace(document={'capacity_path':'/','peak_reserve_bytes':1024**3})
        with patch('evolvmem.history_migration.operator_scope.shutil.disk_usage',return_value=SimpleNamespace(free=30*1024**3)):
            with self.assertRaisesRegex(Exception,'operator_disk_floor'):runtime.guard()
    def test_sql_split_preserves_procedural_semicolons(self):
        from evolvmem.history_migration.operator_scope import sql_statements
        text="-- comment;\nDO $q$ BEGIN PERFORM 1; END $q$; SELECT 'a;''b';"
        parts=list(sql_statements(text));self.assertEqual(len(parts),2)
        self.assertIn('PERFORM 1; END $q$',parts[0])

class ConnectionIdentityTests(unittest.TestCase):
    def test_wrong_database_owner_or_login_closes_new_connection(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        endpoint={'host':'db.example.test','port':5432,'password_file':'/private/password','ca_file':'/private/ca'}
        target={'database':'history_owner','owner_role':'history_owner_role','login_role':'history_maint'}
        for index,value in ((0,'wrong_database'),(1,'wrong_owner'),(2,'wrong_login'),(3,True),(4,True),(5,True),(8,False)):
            row=[target['database'],target['owner_role'],target['login_role'],False,False,False,False,False,True];row[index]=value
            raw=Port([tuple(row)]);runtime=object.__new__(OperatorRuntime)
            runtime.config=SimpleNamespace(document={'target':target,'content':endpoint})
            kwargs=[]
            def connector(**kw):kwargs.append(kw);return raw
            runtime.connector=connector
            with patch('evolvmem.history_migration.operator_scope.private_read',return_value=b'private-test-value'):
                with self.assertRaisesRegex(Exception,'owner_connection_identity_mismatch'):runtime._connect()
            self.assertTrue(raw.closed);self.assertEqual(kwargs[0]['connect_timeout'],3)
            self.assertEqual(kwargs[0]['sslmode'],'verify-full')
    def test_duplicate_config_keys_are_rejected(self):
        from unittest.mock import patch
        with patch('evolvmem.history_migration.operator_config.private_read',return_value=b'{"version":1,"version":1}'):
            with self.assertRaisesRegex(Exception,'operator_duplicate_key'):load_config('/private/config')

@unittest.skipUnless(os.name=='posix','real POSIX ownership/mode tests; no Windows simulation')
class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile,json,hashlib,stat
        from pathlib import Path
        root=Path(os.environ.get('EVOLVMEM_TEST_TMP_ROOT',str(Path.home()/'.cache'/'evolvmem-operator-scope-tests')))
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.tmp=tempfile.TemporaryDirectory(prefix='operator-scope-',dir=root)
        self.base=Path(self.tmp.name);self.base.chmod(0o700)
        self.path=self.base/'config.json'
        self.write('password',b'synthetic-unit-secret')
        self.write('ca',b'synthetic-unit-ca')
        def meta(size):
            return {'stat':{'dev':1,'ino':size+1,'size':size,'mtime_ns':1,'ctime_ns':1,'mode':stat.S_IFREG|0o444,'nlink':1},'sha256':hashlib.sha256(b'').hexdigest()}
        seal={'format':'controlled-retained-empty-wal-v1','namespace':'owner','path':str(self.base/'retained.sqlite'),'basis':'controlled_retained_copy_no_writers','os_immutable_proven':False,'retention_evidence_sha256':'a'*64,'files':{'db':meta(100),'wal':meta(0),'shm':meta(32768)}}
        raw=json.dumps(seal).encode();self.write('seal.json',raw)
        self.doc={'version':1,'migration_id':'00000000-0000-0000-0000-000000000010','work_dir':str(self.base),'source_seal_file':str(self.base/'seal.json'),'source_seal_sha256':hashlib.sha256(raw).hexdigest(),'project_mapping':{},'target':{'database':'formal_history','owner_role':'history_owner','login_role':'history_maint','library_id':'00000000-0000-0000-0000-000000000001','route_epoch':1,'policy_epoch':1,'owner_principal_id':'00000000-0000-0000-0000-000000000002','connection_ref':'formal_history'},'control':{'host':'control.example.test','port':5432,'database':'control_db','owner_role':'control_owner','login_role':'control_maint','password_file':str(self.base/'password'),'ca_file':str(self.base/'ca')},'content':{'host':'content.example.test','port':5432,'password_file':str(self.base/'password'),'ca_file':str(self.base/'ca')},'capacity_path':str(self.base),'peak_reserve_bytes':1024**3,'command_seconds':60}
        self.save()
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,data):
        p=self.base/name;p.write_bytes(data);p.chmod(0o600);return p
    def save(self):
        import json
        self.write('config.json',json.dumps(self.doc).encode())
    def test_valid_private_config_constructs_only_bound_six_field_target(self):
        from evolvmem.history_migration.operator_target import target_for
        config=load_config(self.path);target=target_for(config)
        self.assertEqual(target.database,'formal_history')
        self.assertEqual(len(asdict(target)),6)
        self.assertNotIn('synthetic-unit-secret',repr(config))
        self.assertEqual(config.source.receipt_sha256,self.doc['source_seal_sha256'])
    def test_world_readable_config_and_symlink_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(Exception,'operator_file_not_private'):load_config(self.path)
        self.path.chmod(0o600)
        link=self.base/'linked.json';link.symlink_to(self.path)
        with self.assertRaisesRegex(Exception,'operator_absolute_private_path_required'):load_config(link)
    def test_boolean_version_is_rejected(self):
        self.doc['version']=True;self.save()
        with self.assertRaisesRegex(Exception,'operator_config_version'):load_config(self.path)
    def test_oversize_and_short_read_are_rejected(self):
        from unittest.mock import patch
        p=self.write('bounded',b'0123456789')
        with self.assertRaisesRegex(Exception,'operator_file_budget'):private_read(p,4)
        with patch('evolvmem.history_migration.operator_config.os.read',return_value=b'012'):
            with self.assertRaisesRegex(Exception,'operator_file_budget'):private_read(p,10)


class MaintenanceLoginTests(unittest.TestCase):
    def connect(self,flags,memberships):
        from types import SimpleNamespace
        from unittest.mock import patch
        endpoint={'host':'db.example.test','port':5432,'password_file':'/private/password','ca_file':'/private/ca'}
        target={'database':'history_owner','owner_role':'history_owner_role','login_role':'history_maint'}
        raw=Port();runtime=object.__new__(OperatorRuntime)
        runtime.config=SimpleNamespace(document={'target':target,'content':endpoint})
        def execute(sql,args=None):
            raw.calls.append(sql)
            if 'FROM pg_auth_members' in sql:return Cursor(memberships)
            if 'FROM pg_roles WHERE rolname=session_user' in sql:return Cursor([flags])
            return Cursor([(target['database'],target['owner_role'],target['login_role'],False,False,False,False,False,True)])
        raw.execute=execute;runtime.connector=lambda **kw:raw
        with patch('evolvmem.history_migration.operator_scope.private_read',return_value=b'unit-secret'):
            try:return runtime._connect(),raw
            except Exception:
                self.assertTrue(raw.closed);raise
    def test_limited_login_and_exact_set_membership_allowed(self):
        result,raw=self.connect((True,False,False,False,False,False,False),[('history_owner_role',False,False,True)])
        self.assertIs(result,raw);raw.close()
    def test_dangerous_login_attributes_rejected_after_set_role(self):
        for index in range(7):
            flags=[True,False,False,False,False,False,False];flags[index]=not flags[index]
            with self.assertRaisesRegex(Exception,'operator_maintenance_login_unsafe'):
                self.connect(tuple(flags),[('history_owner_role',False,False,True)])
    def test_membership_must_be_exact_set_only_owner(self):
        bad=([],[('history_owner_role',True,False,True)],[('history_owner_role',False,True,True)],[('history_owner_role',False,False,False)],[('other_role',False,False,True)],[('history_owner_role',False,False,True),('other_role',False,False,True)])
        for memberships in bad:
            with self.assertRaisesRegex(Exception,'operator_maintenance_membership_unsafe'):
                self.connect((True,False,False,False,False,False,False),memberships)

if __name__=='__main__':unittest.main()
