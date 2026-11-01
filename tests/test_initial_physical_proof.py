"""Canonical physical proof owner with actual tiny SQLite and config files."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from evolvmem import physical_integrity as physical

class InitialProofTests(unittest.TestCase):
    def setUp(self):
        self.m=physical
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'memory.db';self.file=Path(self.temp.name)/'config.json'
        self.file.write_text('{"synthetic":true}')
        self.conn=sqlite3.connect(self.path);self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE tiny(id INTEGER)');self.conn.commit()
        self.config=SimpleNamespace(db_path=self.path,synthetic=True)
        self.service=SimpleNamespace(config=self.config,store=SimpleNamespace(_connection=lambda:self.conn))
        self.source=object();self.now=[120.]

    def boundary(self):return self.m.initial_physical_boundary(self.service,self.file)
    def make(self,**changes):
        boundary=self.boundary()
        args=dict(source=self.source,before=boundary,after=boundary,started=100.,completed=110.,codes=(),generation=0)
        args.update(changes)
        return self.m.issue_initial_physical_proof(**args)
    def owner(self):
        b=self.boundary()
        return self.m.PhysicalProofOwner(b.identity,b.contract,refresh=lambda:None,clock=lambda:self.now[0])
    def adopt(self,owner,proof,**changes):
        args=dict(source=self.source,boundary=self.boundary(),generation=0,ttl=300.)
        args.update(changes);return owner.adopt_initial(proof,**args)

    def test_real_quickcheck_adopts_without_renewing_completion(self):
        before=self.boundary()
        result=() if self.conn.execute('PRAGMA quick_check').fetchall()==[('ok',)] else ('quick_check_failed',)
        proof=self.make(before=before,codes=result)
        owner=self.owner();self.assertTrue(self.adopt(owner,proof))
        self.assertEqual(owner.check(before.identity,before.contract,300.,lambda:self.fail('second physical scan'),
            current_config=lambda:before.contract),((),110.))
        self.now[0]=410.
        self.assertEqual(owner.observe(before.identity,before.contract,300.).state,'expired')

    def test_failed_or_changed_observation_cannot_issue(self):
        self.assertIsNone(self.make(codes=('quick_check_failed',)))
        before=self.boundary();self.conn.execute('CREATE TABLE other(id)');self.conn.commit()
        self.assertIsNone(self.make(before=before))

    def test_source_and_resident_generation_must_match(self):
        proof=self.make()
        self.assertFalse(self.adopt(self.owner(),proof,source=object()))
        self.assertFalse(self.adopt(self.owner(),proof,generation=1))

    def test_config_file_change_with_same_config_object_rejects(self):
        proof=self.make();owner=self.owner();self.file.write_text('{"synthetic":false}')
        self.assertFalse(self.adopt(owner,proof))

    def test_missing_config_file_then_created_rejects(self):
        self.file.unlink();proof=self.make();owner=self.owner();self.file.write_text('{}')
        self.assertFalse(self.adopt(owner,proof))

    def test_schema_cookie_change_even_identical_schema_rejects(self):
        proof=self.make();owner=self.owner()
        self.conn.executescript('CREATE TABLE transient(id); DROP TABLE transient;')
        self.assertFalse(self.adopt(owner,proof))

    def test_expiry_disabled_ttl_future_and_invalid_time_reject(self):
        proof=self.make()
        for now,ttl in ((410.,300.),(109.,300.),(120.,0.),(120.,1.)):
            self.now[0]=now;self.assertFalse(self.adopt(self.owner(),proof,ttl=ttl))
        self.now[0]=120.
        for start,end in ((111.,110.),(float('nan'),110.),(100.,float('inf'))):
            self.assertIsNone(self.make(started=start,completed=end))

    def test_no_success_can_replace_closed_running_or_previous_record(self):
        proof=self.make();owner=self.owner();owner.close()
        self.assertFalse(self.adopt(owner,proof))
        owner=self.owner();owner.bind_predecessor(SimpleNamespace(is_alive=lambda:True))
        self.assertFalse(self.adopt(owner,proof))
        owner=self.owner();self.assertTrue(self.adopt(owner,proof));self.assertFalse(self.adopt(owner,proof))

    def test_transaction_cannot_capture_committed_boundary(self):
        self.conn.execute('BEGIN');self.conn.execute('INSERT INTO tiny VALUES(1)')
        with self.assertRaises(RuntimeError):self.boundary()
        self.conn.rollback()

    def test_malformed_reconstructed_receipt_is_not_accepted(self):
        proof=self.make()
        for fields in ({'started':111.},{'completed':float('nan')},{'seal':object()}):
            self.assertFalse(self.adopt(self.owner(),replace(proof,**fields)))

if __name__=='__main__':unittest.main()
