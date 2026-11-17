from dataclasses import replace
from datetime import timedelta
import unittest
from tests.pg_context_read_slice.tests.fixture import Fixture,ContextRetriever


class SliceTests(unittest.TestCase):
    def setUp(self):self.f=Fixture();self.addCleanup(self.f.close)

    def test_actual_sqlite_candidates_pg_metadata_and_full_typed_dto_match(self):
        a=self.f.add('first-ref','Synthetic reference: alpha release needs a second reviewer.')
        b=self.f.add('second-ref','Synthetic reference: beta release stores rollback evidence.')
        self.f.mirror();request=self.f.request()
        batch=self.f.candidates(request,({'id':a['context_id'],'distance':.1},{'id':b['context_id'],'distance':.8}))
        expected=self.f.original_result(batch)
        actual=self.f.recall(batch)
        self.assertTrue(actual)
        self.assertEqual(expected,actual)
        self.assertTrue(all(x.available_layers for x in actual))

    def test_final_raw_body_drift_without_revision_or_hash_update_rejects(self):
        from evolvmem.library_foundation import FoundationError
        a=self.f.add('raw-drift','Synthetic reference: retain the original source body.')
        self.f.mirror();batch=self.f.candidates(self.f.request())
        def drift():self.f.port.databases['content_0'][1].execute("UPDATE context_layers SET content='corrupted raw bytes' WHERE item_id=? AND layer='l2'",(a['context_id'],))
        published=[]
        with self.assertRaises(FoundationError):self.f.recall(batch,between=drift,publish=published.append)
        self.assertEqual([],published)

    def test_imported_history_without_completed_business_receipt_is_unsupported(self):
        from evolvmem.library_foundation import FoundationError
        self.f.add('missing-receipt','Synthetic reference: this item requires its supported-write receipt.')
        self.f.port.databases['content_0'][1].execute('DELETE FROM lan_write_requests')
        self.f.mirror();batch=self.f.candidates(self.f.request())
        with self.assertRaisesRegex(FoundationError,'unsupported_recall_origin'):self.f.recall(batch)

    def test_current_unsupported_source_rejects_even_with_receipt(self):
        from evolvmem.library_foundation import FoundationError
        a=self.f.add('unit-source','Synthetic reference: source ownership is checked separately.')
        db=self.f.port.databases['content_0'][1]
        db.execute("INSERT INTO context_sources(library_id,item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,?,'organization','task:1','synthetic','2030-01-01 00:00:00')",(self.f.port.rows[next(iter(self.f.port.rows))]['library_id'],a['context_id']))
        self.f.mirror();batch=self.f.candidates(self.f.request())
        with self.assertRaisesRegex(FoundationError,'unsupported_context_evidence'):self.f.recall(batch)

    def test_no_commit_time_window_crossing_rejects_selected(self):
        from evolvmem.library_foundation import FoundationError
        self.f.add('expiry','Synthetic reference: window boundaries remain current.')
        stamp=(self.f.now+timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        self.f.port.databases['content_0'][1].execute('UPDATE context_items SET effective_until=?',(stamp,))
        self.f.mirror();batch=self.f.candidates(self.f.request())
        def advance():self.f.now+=timedelta(seconds=2)
        with self.assertRaisesRegex(FoundationError,'recall_selected_changed'):self.f.recall(batch,between=advance)

    def test_actual_replace_receipt_and_supersession_match_original_search(self):
        self.f.add('replace-ref','Synthetic reference: earlier statement for replacement.')
        new=self.f.write('memory_replace',{'key':'replace-ref','value':'Synthetic reference: the current replacement statement.'})
        self.f.mirror();batch=self.f.candidates(self.f.request())
        expected=self.f.original_result(batch)
        self.assertEqual((new['context_id'],),tuple(r.id for r in expected))
        self.assertEqual(expected,self.f.recall(batch))


if __name__=='__main__':unittest.main()
