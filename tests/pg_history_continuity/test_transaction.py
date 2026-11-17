"""Real SQLite effects; bypasses PostgreSQL gate/catalog/sequence behavior explicitly."""
import test_continuity
from contextlib import closing
import json,sqlite3,unittest
from pathlib import Path
from test_stream import Fixture,M,N,P,TARGET
from sql_port import Connection
from evolvmem.history_migration.continuity_importer import ContinuityImporter,COUNT_TABLES

class Effects(Fixture):
    def build(self):
        self.manifest=self.plan();self.target=Path(self.temp.name)/'effect-target.db';self.sql=[];self.fault=None
        with closing(sqlite3.connect(self.target)) as c:
            c.execute('CREATE TABLE context_items(library_id TEXT,id INTEGER,UNIQUE(library_id,id))')
            c.execute('INSERT INTO context_items VALUES(?,7)',(TARGET.library_id,))
            for table in M.TABLES:
                fields=['library_id TEXT NOT NULL','project_id TEXT','source_rowid INTEGER NOT NULL']
                fields += [M.quote(n)+' '+kind+('' if nullable else ' NOT NULL') for n,kind,nullable in M.SPECS[table]]
                spec=M._EVIDENCE['namespaces']['owner'][table]
                keys=[r['name'] for r in spec['columns'] if r['pk_position']]
                if keys:fields.append('UNIQUE(library_id,'+','.join(M.quote(n) for n in keys)+')')
                for fk in spec['foreign_keys']:
                    fields.append('FOREIGN KEY(library_id,'+fk['from_column']+') REFERENCES '+fk['target_table']+'(library_id,'+fk['to_column']+')')
                c.execute('CREATE TABLE '+table+'('+','.join(fields)+',PRIMARY KEY(library_id,source_rowid))')
            c.execute('CREATE TABLE history_continuity_runs(run_id TEXT PRIMARY KEY,library_id TEXT UNIQUE,parent_run_id TEXT,manifest_hash TEXT,manifest TEXT,loaded_counts TEXT,loaded_bytes INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE history_continuity_chunks(run_id TEXT,table_code TEXT,chunk_key TEXT,input_hash TEXT,row_keys TEXT,row_count INTEGER,input_bytes INTEGER,PRIMARY KEY(run_id,table_code,chunk_key))')
            c.execute('CREATE TABLE history_continuity_progress(run_id TEXT PRIMARY KEY,manifest_hash TEXT,stage TEXT,next_load INTEGER DEFAULT 0,next_restore INTEGER DEFAULT 0,next_verify INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,verify_generation INTEGER,count_table INTEGER DEFAULT 0,count_rows INTEGER DEFAULT 0,count_last TEXT)')
            c.execute('CREATE TABLE history_continuity_links(run_id TEXT,source_rowid INTEGER,parent_id TEXT,PRIMARY KEY(run_id,source_rowid))')
            for table in M.TABLES+('history_continuity_chunks','history_continuity_links'):
                for event in ('INSERT','UPDATE','DELETE'):
                    c.execute('CREATE TRIGGER '+table+'_'+event+' AFTER '+event+' ON '+table+' BEGIN UPDATE history_continuity_progress SET generation=generation+1; END')
            c.commit()
        class Port(ContinuityImporter):
            def _gate(self,c):pass # PostgreSQL security and catalogs not simulated.
            def _parent(self,c,plan):
                M.check(type(plan) is P.Plan,'trusted_continuity_plan_required');plan.require_importable()
        self.port=Port(lambda:Connection(self),TARGET,capacity_guard=lambda:None)
        self.port.begin(self.manifest)
        with N.Phase(self.ref,plan=self.manifest) as phase:
            self.arguments={i:phase.read(self.manifest.page(i)) for i in range(self.manifest.page_count)}
    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.target)) as c:
            result=c.execute(sql,args).fetchall();c.commit();return result
    def load(self):
        for i,args in self.arguments.items():self.port.load_page(self.manifest,i,args)
    def restore(self):
        for i in range(self.manifest.page_count):self.port.restore_page(self.manifest,i)
    def test_unknown_commit_replay_and_circular_parents_across_pages(self):
        self.build();self.port.load_page(self.manifest,0,self.arguments[0]);self.fault='after_commit'
        with self.assertRaisesRegex(M.ImportError,'commit_outcome_unknown'):self.port.load_page(self.manifest,1,self.arguments[1])
        self.fault=None
        self.assertTrue(self.port.load_page(self.manifest,1,self.arguments[1])['replayed'])
        self.load()
        self.assertEqual(self.query('SELECT count(*),count(parent_id) FROM continuity_workstreams'),[(130,0)])
        self.assertEqual(self.query('SELECT count(*) FROM history_continuity_links'),[(130,)])
        self.restore()
        self.assertEqual(self.query("SELECT parent_id,current_context_id,checkpoint_revision,state_version FROM continuity_workstreams WHERE id='ws_0'"),[('ws_129',7,37,41)])
        self.assertEqual(self.query("SELECT parent_id FROM continuity_workstreams WHERE id='ws_129'"),[('ws_0',)])
        self.assertEqual(self.query('SELECT revision FROM continuity_focus'),[(77,)])
        for i in range(self.manifest.page_count):self.port.verify_page(self.manifest,i)
        p=self.port.status(self.manifest)
        while p['count_table']<len(COUNT_TABLES):p=self.port.count_page(self.manifest)['progress']
        self.assertEqual((p['stage'],p['count_table'],p['next_restore']),('counting',9,self.manifest.page_count))
        self.query("UPDATE continuity_workstreams SET checkpoint_revision=38 WHERE id='ws_0'")
        with self.assertRaisesRegex(M.ImportError,'generation_changed'):self.port.status(self.manifest)
    def test_row_receipt_escrow_and_restore_frontier_are_atomic(self):
        self.build();self.port.load_page(self.manifest,0,self.arguments[0]);self.fault='before_receipt'
        with self.assertRaises(M.ImportError):self.port.load_page(self.manifest,1,self.arguments[1])
        self.assertEqual(self.query('SELECT count(*) FROM continuity_workstreams'),[(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM history_continuity_links'),[(0,)])
        self.assertEqual(self.query('SELECT next_load FROM history_continuity_progress'),[(1,)])
        self.fault=None;self.load();self.port.restore_page(self.manifest,0);self.fault='before_restore_frontier'
        with self.assertRaises(M.ImportError):self.port.restore_page(self.manifest,1)
        self.assertEqual(self.query('SELECT count(parent_id) FROM continuity_workstreams'),[(0,)])
        self.assertEqual(self.query('SELECT next_restore FROM history_continuity_progress'),[(1,)])
        self.fault=None;self.restore()
    def test_escrow_mutation_and_text_budgets_reject_before_materializing(self):
        self.build();self.load()
        self.query('UPDATE history_continuity_links SET parent_id=? WHERE source_rowid=1',('x'*(M.MAX_BYTES+1),))
        before=len(self.sql)
        with self.assertRaisesRegex(M.ImportError,'link_readback_budget'):self.port.load_page(self.manifest,1,self.arguments[1])
        self.assertFalse(any('SELECT source_rowid,parent_id' in s for s in self.sql[before:]))
        self.query("UPDATE history_continuity_links SET parent_id='changed' WHERE source_rowid=1")
        with self.assertRaisesRegex(M.ImportError,'digest_changed'):self.port.load_page(self.manifest,1,self.arguments[1])

if __name__=='__main__':unittest.main()
