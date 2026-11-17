"""Source budget regressions only; no PostgreSQL or existing suite rerun."""
import test_stream
from pathlib import Path
import unittest
from unittest.mock import patch
from evolvmem.history_migration import continuity_scan as N
from evolvmem.history_migration import sqlite_source as S


class PreReadBudget(test_stream.Fixture):
    def test_utf16_source_uses_utf8_payload_upper_bound(self):
        body='\u4e2d'*300000
        original_connect=test_stream.sqlite3.connect
        for encoding in ('UTF-16le','UTF-16be'):
            folder=Path(self.temp.name)/encoding;folder.mkdir()
            def connect(*args,**kwargs):
                connection=original_connect(*args,**kwargs)
                connection.execute("PRAGMA encoding='"+encoding+"'")
                return connection
            def change(c):
                c.execute('DELETE FROM continuity_workstreams')
                for i in (1,2):
                    c.execute('''INSERT INTO continuity_workstreams(id,project,workspace_fingerprint,
                        parent_id,current_context_id,checkpoint_revision,state_version,status,created_at,updated_at)
                        VALUES(?,?,?,?,7,1,1,'open','raw','raw')''',('w'+str(i),'p','fingerprint',body))
            with patch.object(test_stream.sqlite3,'connect',connect):ref=test_stream.source(folder,mutate=change)
            materialized=[];original=S._text
            def witness(raw):
                if len(raw)>=len(body.encode('utf8')):materialized.append(len(raw))
                return original(raw)
            with patch.object(S,'_text',witness),N.Phase(ref) as phase,N.Snapshot(phase,page_bytes=1536*1024) as reader:
                first=reader.page('continuity_workstreams')
                self.assertEqual(materialized,[900000],encoding+' bytes must not underestimate UTF8 output')
                self.assertEqual(first.rows[0]['parent_id'],body)
                second=reader.page('continuity_workstreams',first.next_cursor)
                self.assertTrue(second.exhausted)
                self.assertEqual(materialized,[900000,900000])

    def test_utf8_byte_budget_and_nullable_identity_keep_original_values(self):
        body='\u4e2d'*1500
        def change(c):
            for rowid in (-5,7):
                c.execute('INSERT INTO project_history_date_repairs(rowid,plan_sha256,project,applied_at,plan_json) VALUES(?,NULL,?,?,?)',
                          (rowid,'p','raw',body))
        ref=test_stream.source(self.temp.name,mutate=change)
        materialized=[];original=S._text
        def witness(raw):
            if len(raw)>=len(body.encode('utf8')):materialized.append(len(raw))
            return original(raw)
        with patch.object(S,'_text',witness),N.Phase(ref) as phase:
            with N.Snapshot(phase,page_bytes=2500) as reader:
                with self.assertRaises(S.SourceIncomplete) as raised:reader.page('project_history_date_repairs')
                self.assertEqual(raised.exception.code,'source_row_over_budget')
                self.assertEqual(materialized,[],'character count must not substitute for UTF8 byte length')
            with N.Snapshot(phase,page_rows=1) as reader:
                first=reader.page('project_history_date_repairs')
                self.assertIsNone(first.rows[0]['plan_sha256'])
                self.assertEqual(first.rows[0]['plan_json'],body)
                self.assertEqual(first.next_cursor._after,-5)
                second=reader.page('project_history_date_repairs',first.next_cursor)
                self.assertTrue(second.exhausted)
                self.assertEqual(second.rows[0],first.rows[0])
            self.assertEqual(materialized,[4500,4500])

    def test_two_large_rows_are_split_before_second_body_is_materialized(self):
        body='x'*(3*1024*1024)
        def change(c):
            c.execute('DELETE FROM continuity_workstreams')
            for i in (1,2):
                c.execute('''INSERT INTO continuity_workstreams(id,project,workspace_fingerprint,
                    parent_id,current_context_id,checkpoint_revision,state_version,status,created_at,updated_at)
                    VALUES(?,?,?,?,7,1,1,'open','raw','raw')''',('w'+str(i),'p','fingerprint',body))
        ref=test_stream.source(self.temp.name,mutate=change)
        materialized=[]
        original=S._text
        def witness(raw):
            if len(raw)>=len(body):materialized.append(len(raw))
            return original(raw)
        with patch.object(S,'_text',witness),N.Phase(ref) as phase,N.Snapshot(phase) as reader:
            first=reader.page('continuity_workstreams')
            self.assertEqual(materialized,[len(body)],'second large row reached text_factory before its own page')
            self.assertEqual(len(first.rows),1)
            self.assertFalse(first.exhausted)
            self.assertEqual(first.rows[0]['parent_id'],body)
            second=reader.page('continuity_workstreams',first.next_cursor)
            self.assertEqual(materialized,[len(body),len(body)])
            self.assertEqual(len(second.rows),1)
            self.assertTrue(second.exhausted)
            self.assertEqual(second.rows[0]['id'],'w2')

    def test_over_budget_or_wrong_storage_refused_before_payload_fetch(self):
        for index,wrong_type in enumerate((False,True)):
            folder=Path(self.temp.name)/str(index);folder.mkdir()
            body='y'*2048
            def change(c):
                c.execute('DELETE FROM continuity_workstreams')
                c.execute('''INSERT INTO continuity_workstreams(id,project,workspace_fingerprint,
                    parent_id,current_context_id,checkpoint_revision,state_version,status,created_at,updated_at)
                    VALUES('w','p','fingerprint',?,7,1,?,'open','raw','raw')''',(body,'bad' if wrong_type else 1))
            ref=test_stream.source(folder,mutate=change)
            materialized=[];original=S._text
            def witness(raw):
                if len(raw)>=len(body):materialized.append(len(raw))
                return original(raw)
            with patch.object(S,'_text',witness),N.Phase(ref) as phase,N.Snapshot(phase,page_bytes=4096 if wrong_type else 1024) as reader:
                with self.assertRaises(S.SourceIncomplete) as raised:reader.page('continuity_workstreams')
                self.assertEqual(raised.exception.code,'source_probe_storage_changed' if wrong_type else 'source_row_over_budget')
                self.assertEqual(materialized,[],'payload fetched before metadata budget/type rejection')


if __name__=='__main__':unittest.main()
