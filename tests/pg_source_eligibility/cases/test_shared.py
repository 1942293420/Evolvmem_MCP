"""Real SQLite oracle against the unmodified f594 function bodies."""
import ast
import importlib.util
import json
from pathlib import Path
import sqlite3
import types
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
BASE=HERE/'reference'

def original(filename, names, **values):
    tree=ast.parse((BASE/filename).read_text(encoding='utf-8-sig'))
    code=ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.Assign))
        and (getattr(n,'name',None) in names or isinstance(n,ast.Assign)
             and any(isinstance(t,ast.Name) and t.id in names for t in n.targets))],type_ignores=[])
    namespace=dict(json=json,**values);exec(compile(code,str(BASE/filename),'exec'),namespace)
    return namespace

def shared():
    path=Path(importlib.util.find_spec('evolvmem.source_qualification').origin)
    spec=importlib.util.spec_from_file_location('qualification_candidate',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class Shared(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript('''CREATE TABLE context_items(id,project,projects,scope);
          CREATE TABLE unit_derivations(unit_task_id,unit_digest,item_id);
          CREATE TABLE organization_tasks(id,source_key,status);
          CREATE TABLE organization_units(task_id,digest,decision,disposition,project,projects,knowledge_scope);
          CREATE TABLE organization_conversations(source_key,members);
          CREATE TABLE context_sources(item_id,source_kind,archive_id,source_ref);
          CREATE TABLE context_project_resolutions(item_id,decision_source,review_state);
          INSERT INTO context_items VALUES(1,'evo','["evo"]','project');
          INSERT INTO organization_tasks VALUES(11,'conversation:x','complete');
          INSERT INTO organization_conversations VALUES('conversation:x','["archive:5"]');
          INSERT INTO organization_units VALUES(11,'d','manual','keep','evo','["evo"]','project');
          INSERT INTO unit_derivations VALUES(11,'d',1);
          INSERT INTO context_sources VALUES(1,'session',5,'archive:5');''')
        ks=original('knowledge_scope.py',{'projects'})
        self.old=original('unit_derivations.py',{'_BACKING_SQL','currently_backed'},ks=types.SimpleNamespace(projects=ks['projects']))
        self.rule=original('knowledge_provenance.py',{'unconfirmed_rule'})['unconfirmed_rule']
        self.store=types.SimpleNamespace(_connection=lambda:self.c)
    def tearDown(self):self.c.close()
    def compare(self):
        m=shared();sql=self.old['_BACKING_SQL'];seen=[]
        def members(key):
            seen.append('conversation');row=self.c.execute('SELECT members FROM organization_conversations WHERE source_key=?',(key,)).fetchone()
            return set() if row is None else {int(k.split(':')[1]) for k in json.loads(row['members']) if k.startswith('archive:')}
        def sources():
            seen.append('sources');return self.c.execute('SELECT * FROM context_sources WHERE item_id=1')
        expected=self.old['currently_backed'](self.store,1)
        got=m.currently_backed(self.c.execute(sql+' WHERE d.item_id=1').fetchall(),members,sources)
        self.assertEqual(expected,got);return got,seen
    def test_current_then_withdrawn_and_independent_source(self):
        self.assertEqual((True,[]),self.compare())
        self.c.execute("UPDATE organization_units SET decision='review'")
        self.assertEqual((False,['conversation','sources']),self.compare())
        self.c.execute("INSERT INTO context_sources VALUES(1,'manual',NULL,'manual confirmation')")
        self.assertTrue(self.compare()[0])
    def test_real_backing_read_parses_conversation_once_without_cached_json_roundtrip(self):
        from evolvmem.unit_derivations import BackingRead,currently_backed
        self.c.execute("UPDATE organization_units SET decision='review'")
        self.c.execute("INSERT INTO context_items VALUES(2,'evo','[\"evo\"]','project')")
        self.c.execute("INSERT INTO unit_derivations VALUES(11,'d',2)")
        self.c.execute("INSERT INTO context_sources VALUES(2,'session',5,'archive:5')")
        read=BackingRead(self.store,[1,2],preserve_text_decoding=True)
        with patch('json.loads',wraps=json.loads) as loads, patch('json.dumps',wraps=json.dumps) as dumps:
            self.assertFalse(currently_backed(self.store,1,read=read))
            self.assertEqual(1,loads.call_count)
            loads.reset_mock()
            self.assertFalse(currently_backed(self.store,2,read=read))
            self.assertEqual(0,loads.call_count)
            self.assertEqual(0,dumps.call_count)
    def test_unconfirmed_wrapper_retains_one_lazy_connection_for_both_queries(self):
        from evolvmem.knowledge_provenance import unconfirmed_rule
        calls=[]
        store=types.SimpleNamespace(_connection=lambda:(calls.append(1),self.c)[1])
        self.assertFalse(unconfirmed_rule(store,1,'other','{}'));self.assertEqual([],calls)
        payload='{"basis":"inferred","evidence":[{"role":"assistant"}]}'
        self.assertTrue(unconfirmed_rule(store,1,'habit',payload));self.assertEqual([1],calls)
    def test_same_archive_extraction_is_not_independent(self):
        self.c.execute("UPDATE organization_units SET disposition='history_only'")
        self.c.execute("INSERT INTO context_sources VALUES(1,'extraction',5,'conversation:x#d')")
        self.assertFalse(self.compare()[0])
    def test_scope_and_no_derivation_follow_original(self):
        for statement in ("UPDATE organization_units SET projects='[\"other\"]'", "UPDATE organization_units SET projects='[]',knowledge_scope='global'", "UPDATE context_items SET scope='global'", "UPDATE organization_tasks SET status='superseded'",'DELETE FROM unit_derivations'):
            self.c.execute(statement);self.compare()
    def test_invalid_unread_sources_are_not_touched(self):
        self.c.execute("UPDATE organization_conversations SET members='bad json'")
        self.assertEqual((True,[]),self.compare())
        self.c.execute("UPDATE organization_units SET decision='review'")
        with self.assertRaises(json.JSONDecodeError):self.compare()
    def test_inferred_rule_manual_and_human_exceptions(self):
        m=shared();raw=json.dumps({'basis':'inferred','evidence':[{'role':'assistant'}]})
        for category,payload in [('habit',raw),('reference',raw),('habit','bad json'),('habit','[]'),('habit',json.dumps({'basis':'manual'}))]:
            expected=self.rule(self.store,1,category,payload)
            result=m.unconfirmed_rule(category,payload,
                lambda:bool(self.c.execute("SELECT 1 FROM context_project_resolutions WHERE item_id=1 AND decision_source='human' AND review_state='accepted'").fetchone()),
                lambda:bool(self.c.execute("SELECT 1 FROM context_sources WHERE item_id=1 AND source_kind='manual'").fetchone()))
            self.assertEqual(expected,result)
        self.c.execute("INSERT INTO context_project_resolutions VALUES(1,'human','accepted')")
        self.assertFalse(m.unconfirmed_rule('habit',raw,lambda:True,lambda:False))
        self.assertFalse(m.unconfirmed_rule('habit',raw,lambda:False,lambda:True))

if __name__=='__main__':unittest.main()
