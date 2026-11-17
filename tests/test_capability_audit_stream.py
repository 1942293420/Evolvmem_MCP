"""Real SQLite audit verdicts and payload lifetime, without a model/native index.

The proof observer is deliberate: existing native tests cover vector hashing.
Here it observes the actual row passed by the unchanged proof call boundary.
"""
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import weakref

from evolvmem.capability_index import CapabilityIndex


class Payload(str):
    pass


class Vectors:
    def __init__(self, ids): self.keys = set(ids)
    def ids(self): return self.keys


class AuditStreamTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE capability_memories(memory_id INTEGER PRIMARY KEY, '
            'status TEXT,title TEXT,content TEXT,keywords TEXT)')
        self.refs = []
        def row_factory(cursor, row):
            result = dict(zip((d[0] for d in cursor.description), row))
            if 'content' in result:
                result['content'] = Payload(result['content'])
                self.refs.append(weakref.ref(result['content']))
            return result
        self.conn.row_factory = row_factory
        self.index = CapabilityIndex.__new__(CapabilityIndex)
        self.index.context_store = SimpleNamespace(_connection=lambda: self.conn)
        self.index.eligible = None
        self.index.eligible_ids = None
        self.index.vector_index = Vectors(())

    def add(self, ids, *, status='active', content='body'):
        self.conn.executemany('INSERT INTO capability_memories VALUES(?,?,?,?,?)',
            [(i, status, 'title', content, '[]') for i in ids])
        self.conn.commit()

    def proof(self, matched, observer=None):
        def check(ident, memory, proofs, *, vectors=None):
            if observer: observer(ident, memory)
            return ident in matched
        self.index._proof_matches = check

    def test_full_bodies_do_not_survive_across_streamed_proofs(self):
        # Deliberately large synthetic text makes lifetime evidence visible;
        # it is not a claim about a production row's normal maximum size.
        ids = list(range(1, 9))
        self.add(ids, content='x' * (1024 * 1024))
        calls, alive_at_proof, alive_at_eligibility = [], [], []
        def eligible(values):
            calls.append(values)
            gc.collect()
            alive_at_eligibility.append(sum(ref() is not None for ref in self.refs))
            return values
        self.index.eligible_ids = eligible
        def observe(ident, memory):
            gc.collect()
            alive_at_proof.append(sum(ref() is not None for ref in self.refs))
        self.proof(set(ids), observe)
        actual = self.index._audit({'items': {}}, vectors=Vectors(ids))
        self.assertEqual({'eligible': 8, 'indexed': 8, 'missing': 0, 'outdated': 0,
            'obsolete': 0, 'remaining': 0, 'pending_ids': []}, actual)
        self.assertEqual([ids], calls, 'one batch callback with all ordered active IDs')
        self.assertEqual([0], alive_at_eligibility, 'eligibility must not retain every body')
        self.assertEqual([1] * len(ids), alive_at_proof, 'only current row payload may survive')
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in self.refs))

    def test_missing_outdated_obsolete_and_filtered_ids_keep_original_verdict(self):
        self.add([1, 2, 3, 5])
        self.add([8], status='archived')
        calls = []
        self.index.eligible_ids = lambda ids: calls.append(ids) or [2, 3, 5, 999]
        self.proof({2})
        actual = self.index._audit({'items': {'2': {}, '3': {}, '7': {}, '8': {}}}, vectors=Vectors([2, 3, 9]))
        self.assertEqual({'eligible': 3, 'indexed': 3, 'missing': 1, 'outdated': 1,
            'obsolete': 3, 'remaining': 5, 'pending_ids': [3, 5, 7, 8, 9]}, actual)
        self.assertEqual([[1, 2, 3, 5]], calls)

    def test_no_callback_and_empty_active_table_keep_obsolete_cleanup(self):
        self.proof(set())
        self.assertEqual({'eligible': 0, 'indexed': 1, 'missing': 0, 'outdated': 0,
            'obsolete': 2, 'remaining': 2, 'pending_ids': [4, 7]},
            self.index._audit({'items': {'7': {}}}, vectors=Vectors([4])))

    def test_empty_eligibility_still_calls_batch_once(self):
        calls = []
        self.index.eligible_ids = lambda ids: calls.append(ids) or []
        self.proof(set())
        self.assertEqual(0, self.index._audit({'items': {}}, vectors=Vectors([]))['remaining'])
        self.assertEqual([[]], calls)

    def test_filtered_malformed_keyword_json_still_fails_closed(self):
        self.add([1])
        self.conn.execute("UPDATE capability_memories SET keywords='invalid'")
        self.conn.commit()
        self.index.eligible_ids = lambda ids: []
        self.proof(set())
        with self.assertRaises(json.JSONDecodeError):
            self.index._audit({'items': {}}, vectors=Vectors([]))

    def test_custom_eligible_keeps_original_cross_row_mutation_and_phase_order(self):
        self.add([1, 2])
        callbacks, rows, proof_values = [], {}, []
        def batch(ids):
            callbacks.append(('batch', tuple(ids)))
            return ids
        def eligible(row):
            callbacks.append(('eligible', row['memory_id']))
            rows[row['memory_id']] = row
            if row['memory_id'] == 2:
                rows[1]['content'] = 'changed by later eligible callback'
            return True
        def observe(ident, memory):
            callbacks.append(('proof', ident))
            proof_values.append((ident, memory['content']))
        self.index.eligible_ids = batch
        self.index.eligible = eligible
        self.proof({1, 2}, observe)
        result = self.index._audit({'items': {}}, vectors=Vectors([1, 2]))
        self.assertEqual(0, result['remaining'])
        self.assertEqual([('batch', (1, 2)), ('eligible', 1), ('eligible', 2)], callbacks[:3])
        self.assertEqual('changed by later eligible callback', dict(proof_values)[1])
        self.assertTrue(all(phase == 'proof' for phase, _ in callbacks[3:]))

    def test_batch_failure_never_starts_proof_reads(self):
        self.add([1, 2])
        def batch(ids): raise RuntimeError('synthetic eligibility failure')
        self.index.eligible_ids = batch
        proof_calls = []
        self.proof(set(), lambda ident, row: proof_calls.append(ident))
        with self.assertRaisesRegex(RuntimeError, 'synthetic eligibility failure'):
            self.index._audit({'items': {}}, vectors=Vectors([1, 2]))
        self.assertEqual([], proof_calls)

    def test_id_and_body_queries_use_the_same_actual_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memory.db'
            writer = sqlite3.connect(path, isolation_level=None)
            reader = None
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE capability_memories(memory_id INTEGER PRIMARY KEY, '
                    'status TEXT,title TEXT,content TEXT,keywords TEXT)')
                writer.execute("INSERT INTO capability_memories VALUES(1,'active','title','before','[]')")
                reader = sqlite3.connect(path, isolation_level=None)
                reader.row_factory = sqlite3.Row
                reader.execute('BEGIN')
                self.index.context_store = SimpleNamespace(_connection=lambda: reader)
                calls, bodies = [], []
                def eligible(ids):
                    calls.append(ids)
                    # Controlled external commit between metadata and body
                    # queries; the audited connection itself remains read-only.
                    writer.execute("UPDATE capability_memories SET content='after' WHERE memory_id=1")
                    return ids
                self.index.eligible_ids = eligible
                self.proof({1}, lambda ident, row: bodies.append(row['content']))
                actual = self.index._audit({'items': {}}, vectors=Vectors([1]))
                self.assertEqual(0, actual['remaining'])
                self.assertEqual([[1]], calls)
                self.assertEqual(['before'], bodies)
                reader.rollback()
                self.assertEqual('after', reader.execute('SELECT content FROM capability_memories').fetchone()[0])
            finally:
                if reader is not None: reader.close()
                writer.close()


if __name__ == '__main__': unittest.main()
