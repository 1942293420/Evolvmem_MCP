"""Canonical retrieval-input scope tests with real SQLite and domain code.

The ANN port returns deterministic overlapping prefixes. It deliberately does
not claim to exercise native distance search or global vector-health inspection.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayers, ContextSearchRequest, ContextStatus
from evolvmem.context_retriever import ContextRetriever as Retriever
from evolvmem.context_retrieval_inputs import read_scope


@contextmanager
def initial_read(store):
    conn = store._connection()
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    conn.execute("PRAGMA schema_version").fetchone()
    try:
        with read_scope(store):
            yield
    finally:
        conn.rollback()
        conn.execute("PRAGMA query_only=OFF")


class PrefixIndex:
    def __init__(self, ids):
        self.ids = ids
        self.calls = []
    def count(self):
        return len(self.ids)
    def prefix(self, size):
        self.calls.append(size)
        return [{"id": ident, "distance": .02} for ident in self.ids[:size]]


class ExternalANN(Retriever):
    def _vector_candidates(self, query, pool_size, *, select=None, prepared=None):
        if select is not None:
            raise AssertionError("This fixture tests the actual cross_project expansion branch")
        return self.vector_index.prefix(pool_size)


class VectorInputScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name))
        self.store = ContextStore(self.config)
        self.store.initialize()
        self.conn = self.store._connection()
        self.items = []
        for number in range(30):
            item = self.store.create_item(ContextItemDraft(
                identity_key=f"fixture:{number}", content_type=ContextContentType.EXPERIENCE,
                layers=ContextLayers(l0=f"Stored synthetic item {number}", l1=f"Synthetic detail {number}",
                                     l2=f"Synthetic source {number}", generator="test"),
                project="fixture", status=ContextStatus.ACTIVE, confidence=.9))
            self.items.append(item)
        self.conn.execute("UPDATE context_items SET success_count=1,experience_payload='{}'")
        self.conn.commit()
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.index = PrefixIndex([item.id for item in self.items])
        self.retriever = ExternalANN(self.config, self.store, self.index, None, clock=lambda: self.now)
        self.request = ContextSearchRequest("different natural question", top_k=1, cross_project=True,
                                            content_types=(ContextContentType.EXPERIENCE,))
        self.counts = Counter()
        self.record_ids = Counter()
        self.conn.set_trace_callback(self._count)

    def tearDown(self):
        self.conn.set_trace_callback(None)
        self.store.close()
        self.temp.cleanup()

    def _count(self, sql):
        text = " ".join(sql.lower().split())
        if text in ("pragma query_only", "pragma data_version", "pragma schema_version"):
            self.counts["boundary_pragmas"] += 1
        if text.startswith("select i.*, l.content as l0 from context_items"):
            self.counts["record_queries"] += 1
            ids = re.search(r"where i.id in \(([^)]+)\)", text)
            self.record_ids.update(int(i) for i in ids.group(1).split(","))
        if text.startswith("select d.item_id,t.source_key,t.status"):
            self.counts["backing_queries"] += 1
        if text.startswith("select i.content_type,i.status,i.success_count,i.experience_payload"):
            self.counts["eligibility_rows"] += 1

    def search(self):
        return self.retriever.search(self.request, eligible_ids=(self.items[-1].id,), allow_rerank=False)

    def withdrawn(self, item_id):
        task = self.conn.execute("INSERT INTO organization_tasks(source_key,source_revision,rule_revision,status,created_at,updated_at) "
                                 "VALUES('archive:1','fixture','fixture','superseded','2030-01-01','2030-01-01')").lastrowid
        self.conn.execute("INSERT INTO unit_derivations(unit_task_id,unit_digest,item_id,created_at) VALUES(?,'missing-unit',?,'2030-01-01')",
                          (task, item_id))

    def test_scope_preserves_complete_result_with_fixed_clock(self):
        expected = self.search()
        self.assertEqual([r.id for r in expected], [self.items[-1].id])
        with initial_read(self.store):
            self.assertEqual(self.search(), expected)
            self.assertEqual(self.search(), expected)

    def test_expanding_prefix_reuses_retrieval_rows_but_preserves_tail_selection(self):
        with initial_read(self.store):
            result = self.search()
        self.assertEqual([item.id for item in result], [self.items[-1].id])
        self.assertEqual(self.index.calls, [5, 10, 20, 30])
        self.assertEqual(set(self.record_ids), {item.id for item in self.items})
        self.assertEqual(max(self.record_ids.values()), 1, dict(self.counts))

    def test_expanding_prefix_does_not_repeat_backing_or_raw_eligibility_rows(self):
        with initial_read(self.store):
            result = self.search()
        self.assertEqual([item.id for item in result], [self.items[-1].id])
        self.assertEqual(self.index.calls, [5, 10, 20, 30])
        # Each item may need its first lazy row read. Prefix/final/family repeats
        # must use raw inputs, while canonical judgments still run every time.
        self.assertLessEqual(self.counts["backing_queries"], len(self.items), dict(self.counts))
        self.assertLessEqual(self.counts["eligibility_rows"], len(self.items), dict(self.counts))

    def test_boundary_checks_do_not_multiply_for_nested_raw_inputs(self):
        with initial_read(self.store):
            self.assertEqual([item.id for item in self.search()], [self.items[-1].id])
        # This 67-qualification trace must not repeat three metadata PRAGMAs
        # at every nested raw cache/get call. The outer operation and scope
        # boundaries still verify them, and all reads check local mutation.
        self.assertLessEqual(self.counts["boundary_pragmas"], 250, dict(self.counts))

    def test_same_snapshot_expiry_without_commit_is_rechecked(self):
        boundary = self.now + timedelta(seconds=1)
        self.conn.execute("UPDATE context_items SET expires_at=? WHERE id=?",
                          (boundary.strftime("%Y-%m-%d %H:%M:%S"), self.items[-1].id))
        self.conn.commit()
        with initial_read(self.store):
            self.assertEqual([r.id for r in self.search()], [self.items[-1].id])
            changes = self.conn.total_changes
            self.now = boundary
            self.assertEqual(self.search(), ())
            self.assertEqual(self.conn.total_changes, changes)

    def test_new_snapshot_observes_success_withdrawal(self):
        with initial_read(self.store):
            self.assertEqual([r.id for r in self.search()], [self.items[-1].id])
        other = sqlite3.connect(self.config.db_path)
        try:
            other.execute("UPDATE context_items SET success_count=0 WHERE id=?", (self.items[-1].id,))
            other.commit()
        finally:
            other.close()
        with initial_read(self.store):
            self.assertEqual(self.search(), ())

    def test_unscoped_search_keeps_current_read_behavior(self):
        self.assertEqual([r.id for r in self.search()], [self.items[-1].id])
        self.conn.execute("UPDATE context_items SET success_count=0 WHERE id=?", (self.items[-1].id,))
        self.conn.commit()
        self.assertEqual(self.search(), ())

    def test_withdrawn_backing_does_not_decode_unread_learning_payload(self):
        self.withdrawn(self.items[0].id)
        self.conn.execute("INSERT INTO learning_memories(item_id,category,payload) VALUES(?,'experience',CAST(X'80' AS TEXT))",
                          (self.items[0].id,))
        self.conn.commit()
        with initial_read(self.store):
            self.assertEqual([r.id for r in self.search()], [self.items[-1].id])
            self.assertEqual([r.id for r in self.search()], [self.items[-1].id])

    def test_backing_source_short_circuit_does_not_decode_later_corrupt_source(self):
        target = self.items[-1].id
        self.withdrawn(target)
        self.conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) "
                          "VALUES(?,'manual','first','fixture','2030-01-01')", (target,))
        self.conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) "
                          "VALUES(?,'session','second',CAST(X'80' AS TEXT),'2030-01-01')", (target,))
        self.conn.commit()
        with initial_read(self.store):
            self.assertEqual([r.id for r in self.search()], [target])
            self.assertEqual([r.id for r in self.search()], [target])

    def test_whole_family_winner_outside_ann_is_not_ignored(self):
        old = self.items[-1]
        self.conn.execute("UPDATE context_items SET content_type='decision',status='superseded',"
                          "effective_from='2020-01-01 00:00:00',effective_until='2040-01-01 00:00:00' WHERE id=?", (old.id,))
        self.conn.commit()
        self.store.create_item(ContextItemDraft(
            identity_key=old.identity_key, content_type=ContextContentType.DECISION,
            layers=ContextLayers(l0="New unindexed family version", l1="New detail", l2="New source", generator="test"),
            project="fixture", status=ContextStatus.ACTIVE, confidence=.9,
            effective_from="2025-01-01 00:00:00"))
        self.conn.execute("UPDATE context_items SET success_count=1,experience_payload='{}'")
        self.conn.commit()
        self.request = ContextSearchRequest("different natural question", top_k=1, cross_project=True,
            content_types=(ContextContentType.DECISION,), as_of="2030-01-01 00:00:00")
        with initial_read(self.store):
            self.assertEqual(self.search(), ())

    def test_same_connection_mutation_cannot_return_old_success(self):
        result = None
        try:
            with initial_read(self.store):
                self.assertEqual([r.id for r in self.search()], [self.items[-1].id])
                self.conn.execute("PRAGMA query_only=OFF")
                self.conn.execute("UPDATE context_items SET success_count=0 WHERE id=?", (self.items[-1].id,))
                result = self.search()
        except RuntimeError as exc:
            # An explicit fixed scope-boundary refusal is also safe; do not
            # swallow arbitrary SQLite/programming errors as a positive result.
            self.assertIn(str(exc), {"context_retrieval_scope_changed", "context_retrieval_scope_unavailable"})
        else:
            self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
