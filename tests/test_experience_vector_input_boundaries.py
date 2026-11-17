"""Actual input cache with SQLite; no fabricated eligibility outcomes."""
from contextvars import copy_context
from dataclasses import replace
from evolvmem import context_retrieval_inputs as input_module
import sqlite3
import threading
import unittest
from unittest.mock import patch
from tests import test_experience_vector_inputs as T


class InputBoundaryTests(unittest.TestCase):
    setUp = T.VectorInputScopeTests.setUp
    tearDown = T.VectorInputScopeTests.tearDown
    _count = T.VectorInputScopeTests._count
    search = T.VectorInputScopeTests.search

    def module(self):
        return input_module

    def test_entry_and_byte_caps_fall_back_without_losing_qualifications(self):
        module = self.module()
        expected = self.search()
        for entries, size in ((0, 8388608), (1024, 0), (1, 1), (8, 256)):
            with self.subTest(entries=entries, size=size), patch.object(module, "MAX_ENTRIES", entries), patch.object(module, "MAX_BYTES", size):
                with T.initial_read(self.store):
                    inputs = module.current_inputs(self.store)
                    self.assertEqual(self.search(), expected)
                    self.assertEqual(self.search(), expected)
                    self.assertLessEqual(len(inputs.cache), entries)
                    self.assertLessEqual(inputs.bytes, size)
                self.assertEqual(inputs.cache, {})
                self.assertEqual(inputs.bytes, 0)

    def test_oversize_raw_text_and_record_skip_encoding_and_copy_before_retention(self):
        module = self.module()
        record = self.store.get_retrieval_records([self.items[0].id])[0]
        long_text = "x" * 16384
        values = ({"payload": long_text}, replace(record, l0=long_text))
        with patch.object(module, "MAX_BYTES", 1024), T.initial_read(self.store):
            inputs = module.current_inputs(self.store)
            with patch("json.dumps", side_effect=AssertionError("cache must not encode raw text")), \
                 patch.object(module, "deepcopy", side_effect=AssertionError("oversize must not be copied")):
                for i, value in enumerate(values):
                    inputs._retain(("oversize", i), value)
            self.assertEqual(inputs.cache, {})
            self.assertEqual(inputs.bytes, 0)

    def test_oversize_sqlite_record_keeps_original_content_and_falls_back(self):
        module = self.module()
        long_text = "synthetic-large-l0-" * 1024
        with self.store.transaction():
            self.conn.execute("UPDATE context_layers SET content=? WHERE item_id=? AND layer='l0'",
                              (long_text, self.items[0].id))
        with patch.object(module, "MAX_BYTES", 1024), T.initial_read(self.store):
            inputs = module.current_inputs(self.store)
            for _ in range(2):
                self.assertEqual(inputs.records([self.items[0].id])[0].l0, long_text)
            self.assertNotIn(("record", self.items[0].id), inputs.cache)
        self.assertEqual(self.record_ids[self.items[0].id], 2)

    def test_missing_row_is_remembered_but_never_becomes_eligible(self):
        module = self.module()
        with T.initial_read(self.store):
            inputs = module.current_inputs(self.store)
            self.assertEqual(inputs.records([999999]), ())
            self.assertEqual(inputs.records([999999]), ())
            self.assertFalse(inputs.eligible(999999))
            self.assertFalse(inputs.eligible(999999))
        self.assertEqual(self.record_ids[999999], 1)

    def test_project_summary_dependency_time_is_not_a_cached_verdict(self):
        from evolvmem import rollup_provenance
        module = self.module()
        summary, child = self.items[0].id, self.items[1].id
        with self.store.transaction():
            self.conn.execute("UPDATE context_items SET content_type='project_summary',created_at='2030-01-02' WHERE id=?", (summary,))
            self.conn.execute("UPDATE context_items SET updated_at='2030-01-01',effective_until='2030-01-03 00:00:00' WHERE id=?", (child,))
            self.conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'context_reference',?,'fixture','2030-01-01')",
                              (summary, str(child)))
        moment = ['2030-01-02 00:00:00']
        with patch.object(rollup_provenance, '_now_iso', lambda: moment[0]), T.initial_read(self.store):
            inputs = module.current_inputs(self.store)
            self.assertTrue(inputs.eligible(summary))
            before = self.conn.total_changes
            moment[0] = '2030-01-03 00:00:00'
            self.assertFalse(inputs.eligible(summary))
            self.assertEqual(self.conn.total_changes, before)

    def test_two_stores_same_id_never_share_raw_inputs(self):
        from pathlib import Path
        module = self.module()
        other = T.ContextStore(T.Config(data_dir=Path(self.temp.name) / 'other'))
        other.initialize()
        try:
            other_item = other.create_item(T.ContextItemDraft(identity_key='other',
                content_type=T.ContextContentType.EXPERIENCE, project='fixture',
                layers=T.ContextLayers(l0='Other namespace', l1='Other', l2='Other', generator='test'),
                status=T.ContextStatus.ACTIVE, confidence=.9))
            self.assertEqual(other_item.id, self.items[0].id)
            with T.initial_read(self.store), T.initial_read(other):
                left, right = module.current_inputs(self.store), module.current_inputs(other)
                self.assertIsNot(left, right)
                self.assertTrue(left.eligible(other_item.id))
                self.assertFalse(right.eligible(other_item.id))
                self.assertNotEqual(left.records([other_item.id])[0].l0, right.records([other_item.id])[0].l0)
        finally:
            other.close()

    def test_mutating_returned_item_does_not_poison_retained_record(self):
        module = self.module()
        with T.initial_read(self.store):
            inputs = module.current_inputs(self.store)
            record = inputs.records([self.items[0].id])[0]
            object.__setattr__(record.item, "status", T.ContextStatus.DELETED)
            self.assertIs(inputs.records([self.items[0].id])[0].item.status, T.ContextStatus.ACTIVE)

    def test_closed_scope_cannot_read_again_and_keeps_owner_transaction(self):
        module = self.module()
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.execute("BEGIN")
        try:
            with module.read_scope(self.store) as inputs:
                self.assertTrue(inputs.eligible(self.items[0].id))
            self.assertTrue(self.conn.in_transaction)
            self.assertIsNone(module.current_inputs(self.store))
            with self.assertRaisesRegex(module.RetrievalInputError, "scope_changed"):
                inputs.eligible(self.items[0].id)
        finally:
            self.conn.rollback()
            self.conn.execute("PRAGMA query_only=OFF")

    def test_bare_or_writable_transaction_does_not_opt_in(self):
        module = self.module()
        for begin in (False, True):
            if begin:
                self.conn.execute("BEGIN")
            try:
                with self.assertRaisesRegex(module.RetrievalInputError, "scope_unavailable"):
                    with module.read_scope(self.store): pass
                self.assertEqual(self.conn.in_transaction, begin)
            finally:
                self.conn.rollback()

    def test_replaced_transaction_with_committed_change_cannot_reuse_inputs(self):
        module = self.module()
        with self.assertRaisesRegex(module.RetrievalInputError, "scope_changed"):
            with T.initial_read(self.store):
                self.assertTrue(module.current_inputs(self.store).eligible(self.items[-1].id))
                self.conn.rollback()
                other = sqlite3.connect(self.config.db_path)
                try:
                    other.execute("UPDATE context_items SET success_count=0 WHERE id=?", (self.items[-1].id,))
                    other.commit()
                finally:
                    other.close()
                self.conn.execute("BEGIN")
                module.current_inputs(self.store).eligible(self.items[-1].id)

    def test_text_factory_change_invalidates_retained_decoded_rows(self):
        module = self.module()
        try:
            with self.assertRaisesRegex(module.RetrievalInputError, "scope_changed"):
                with T.initial_read(self.store):
                    inputs = module.current_inputs(self.store)
                    self.assertTrue(inputs.eligible(self.items[0].id))
                    self.conn.text_factory = bytes
                    inputs.eligible(self.items[0].id)
        finally:
            self.conn.text_factory = str

    def test_copied_context_cannot_borrow_inputs_in_another_thread(self):
        module = self.module()
        outcomes = []
        with T.initial_read(self.store):
            self.assertTrue(module.current_inputs(self.store).eligible(self.items[0].id))
            context = copy_context()
            def worker():
                try:
                    module.current_inputs(self.store).eligible(self.items[0].id)
                except module.RetrievalInputError as exc:
                    outcomes.append(str(exc))
                else:
                    outcomes.append("incorrectly_reused")
            thread = threading.Thread(target=lambda: context.run(worker))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ["context_retrieval_scope_changed"])


if __name__ == "__main__": unittest.main(verbosity=2)
