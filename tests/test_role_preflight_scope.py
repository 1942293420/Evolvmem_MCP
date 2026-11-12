"""Real SQLite budget scopes and actual role/original proof boundaries."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import importlib
import json
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

M = importlib.import_module("evolvmem.capability_assessment_migration")
P = importlib.import_module("evolvmem.capability_provenance")
R = importlib.import_module("evolvmem.capability_role_proof")
C = importlib.import_module("evolvmem.conversation")
E = importlib.import_module("evolvmem.extraction_policy")
K = importlib.import_module("evolvmem.knowledge_rules")


class RolePreflightScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE context_project_registry(project TEXT,status TEXT);
            CREATE TABLE organization_tasks(id INTEGER PRIMARY KEY,source_key TEXT,
              source_revision TEXT,rule_revision TEXT,status TEXT,superseded_by INTEGER,
              source_snapshot TEXT);
            CREATE TABLE organization_units(task_id INTEGER,digest TEXT,text TEXT);
            CREATE TABLE organization_conversations(source_key TEXT,members TEXT);
            CREATE TABLE session_archives(id INTEGER PRIMARY KEY,payload_sha256 TEXT,state TEXT);
            CREATE TABLE conversation_history(archive_id INTEGER,content_hash TEXT,messages TEXT,body TEXT);
        """)
        self.store = SimpleNamespace(_connection=lambda: self.conn,
                                     config=SimpleNamespace(data_dir=Path(self.temp.name)))
        policy = K.KnowledgeRules(self.temp.name).read()
        messages = [{"role": "user", "content": "Synthetic budget proof fixture."}]
        raw = json.dumps(messages, ensure_ascii=False)
        body = C.render(messages)
        safe, _ = E.redact_messages(C.clean_messages(messages, policy=policy))
        snapshot = C.render(safe)
        self.conn.execute("INSERT INTO organization_tasks VALUES(1,?,?,?,?,NULL,?)",
                          ("conversation:fixture", "synthetic-revision", policy["revision"], "ready", snapshot))
        self.conn.execute("INSERT INTO organization_units VALUES(1,'small',?)", (messages[0]["content"],))
        self.conn.execute("INSERT INTO organization_units VALUES(1,'large',?)", ("x" * (R.MAX_UNIT_CHARS + 1),))
        self.conn.execute("INSERT INTO organization_conversations VALUES(?,?)",
                          ("conversation:fixture", json.dumps(["archive:1"])))
        self.conn.execute("INSERT INTO session_archives VALUES(1,'synthetic-payload','available')")
        self.conn.execute("INSERT INTO conversation_history VALUES(1,?,?,?)",
                          (hashlib.sha256(raw.encode()).hexdigest(), raw, body))
        self.conn.commit()
        self.counts = Counter()
        self.conn.set_trace_callback(self._count)

    def tearDown(self):
        self.conn.set_trace_callback(None)
        self.conn.close()
        self.temp.cleanup()

    def _count(self, sql):
        # Do not retain/print expanded SQL or values, even for synthetic inputs.
        text = " ".join(sql.lower().split())
        if "as snapshot_bytes" in text and "from organization_tasks" in text:
            self.counts["task_budget_reads"] += 1
        if "from session_archives a left join conversation_history h" in text:
            self.counts["member_budget_reads"] += 1
        if text.startswith("select messages,body,content_hash from conversation_history"):
            self.counts["structured_body_reads"] += 1

    @contextmanager
    def read_scope(self, *, checked=True):
        @contextmanager
        def begin_reader():
            reader = M.OriginalRead(self.store)
            reader._begin_scope()
            try:
                yield reader
                reader._assert_scope()
            finally:
                reader._end_scope()
        if checked:
            with P.provenance_snapshot(self.store):
                with begin_reader() as reader:
                    yield reader
        else:
            with begin_reader() as reader:
                yield reader

    def test_same_task_and_digest_budget_does_not_rescan_within_checked_scope(self):
        with self.read_scope() as reader:
            first = reader.roles.preflight(1, "small")
            self.assertTrue(first["ready"])
            for _ in range(4):
                self.assertEqual(reader.roles.preflight(1, "small"), first)
        self.assertEqual(self.counts["task_budget_reads"], 1, dict(self.counts))
        self.assertEqual(self.counts["member_budget_reads"], 1, dict(self.counts))

    def test_valid_role_cache_hit_does_not_repeat_full_task_member_budget(self):
        with self.read_scope() as reader:
            first = reader.roles.read(1)
            self.assertTrue(first["ready"], first["reason"])
            self.assertEqual({span["role"] for span in first["spans"]}, {"user"})
            initial = dict(self.counts)
            self.assertEqual(reader.roles.read(1), first)
            self.assertEqual(dict(self.counts), initial)
        self.assertEqual(self.counts["structured_body_reads"], 1)

    def test_distinct_oversized_digest_cannot_borrow_small_unit_gate(self):
        with self.read_scope() as reader:
            self.assertTrue(reader.roles.preflight(1, "small")["ready"])
            rejected = reader.roles.preflight(1, "large")
            self.assertFalse(rejected["ready"])
            self.assertEqual(rejected["reason"], "unit_source_roles_unproven:unit_over_budget")
        self.assertEqual(self.counts["structured_body_reads"], 0)

    def test_scope_end_and_same_connection_commit_recompute_budget(self):
        with self.read_scope() as reader:
            self.assertTrue(reader.roles.preflight(1, "small")["ready"])
        self.conn.execute("UPDATE organization_units SET text=? WHERE digest='small'",
                          ("x" * (R.MAX_UNIT_CHARS + 1),))
        self.conn.commit()
        with self.read_scope() as reader:
            rejected = reader.roles.preflight(1, "small")
            self.assertEqual(rejected["reason"], "unit_source_roles_unproven:unit_over_budget")

    def test_borrowed_writable_transaction_cannot_reuse_success_after_write(self):
        self.conn.execute("BEGIN")
        reader = M.OriginalRead(self.store)
        reader._begin_scope()
        try:
            self.assertTrue(reader.roles.preflight(1, "small")["ready"])
            self.conn.execute("UPDATE organization_units SET text=? WHERE digest='small'",
                              ("x" * (R.MAX_UNIT_CHARS + 1),))
            rejected = reader.roles.preflight(1, "small")
            self.assertEqual(rejected["reason"], "unit_source_roles_unproven:unit_over_budget")
            with self.assertRaisesRegex(ValueError, "scope_changed"):
                reader._assert_scope()
        finally:
            reader._end_scope()
        self.assertTrue(self.conn.in_transaction)  # Never end caller's transaction.
        self.conn.rollback()
        with self.read_scope() as fresh:
            self.assertTrue(fresh.roles.preflight(1, "small")["ready"])

    def test_new_snapshot_rejects_body_hash_change_from_other_connection(self):
        with self.read_scope() as reader:
            self.assertTrue(reader.roles.read(1)["ready"])
        other = sqlite3.connect(self.path)
        try:
            other.execute("UPDATE conversation_history SET content_hash='invalid'")
            other.commit()
        finally:
            other.close()
        with self.read_scope() as reader:
            failed = reader.roles.read(1)
            self.assertFalse(failed["ready"])
            self.assertEqual(failed["reason"], "unit_source_roles_unproven:structured_history_mismatch")

    def test_policy_change_is_detected_before_scope_exit(self):
        with self.assertRaisesRegex(ValueError, "scope_changed"):
            with self.read_scope() as reader:
                self.assertTrue(reader.roles.read(1)["ready"])
                rules = K.KnowledgeRules(self.temp.name)
                current = rules.read()
                # Actual on-disk policy change; do not invoke Linux-only fcntl
                # writer in this Windows read-path fixture.
                rules.path.parent.mkdir(parents=True, exist_ok=True)
                rules.path.write_text(K.render_skill(
                    {**current["settings"], "cleaning_drop_lines": ["Synthetic"]}, current["instructions"]))
                reader._assert_scope()

    def test_returned_budget_metadata_cannot_poison_another_read(self):
        with self.read_scope() as reader:
            first = reader.roles.preflight(1, "small")
            expected = deepcopy(first)
            first["task"]["snapshot_bytes"] = 0
            first["members"].clear()
            self.assertEqual(reader.roles.preflight(1, "small"), expected)

    def test_replaced_transaction_rejects_or_reads_new_invalid_unit(self):
        with self.assertRaisesRegex((RuntimeError, ValueError), "scope_changed|snapshot_changed"):
            with self.read_scope() as reader:
                self.assertTrue(reader.roles.preflight(1, "small")["ready"])
                self.conn.rollback()
                other = sqlite3.connect(self.path)
                try:
                    other.execute("UPDATE organization_units SET text=? WHERE digest='small'",
                                  ("x" * (R.MAX_UNIT_CHARS + 1),))
                    other.commit()
                finally:
                    other.close()
                self.conn.execute("BEGIN")
                gate = reader.roles.preflight(1, "small")
                self.assertFalse(gate["ready"])
                self.assertEqual(gate["reason"], "unit_source_roles_unproven:unit_over_budget")

    def test_budget_rejection_precedes_structured_body_read(self):
        self.conn.execute("UPDATE organization_tasks SET source_snapshot=?", ("x" * (R.MAX_SOURCE_BYTES + 1),))
        self.conn.commit()
        with self.read_scope() as reader:
            failed = reader.roles.read(1)
            self.assertEqual(failed["reason"], "unit_source_roles_unproven:over_budget")
        self.assertEqual(self.counts["structured_body_reads"], 0)


from unittest.mock import patch
from evolvmem import request_trace as tracing


class RoleTraceTests(unittest.TestCase):
    setUp = RolePreflightScopeTests.setUp
    tearDown = RolePreflightScopeTests.tearDown
    read_scope = RolePreflightScopeTests.read_scope
    _count = RolePreflightScopeTests._count

    def test_real_role_proof_records_only_fixed_phase_timings(self):
        with patch.object(tracing, 'SLOW_MS', 0):
            with self.assertLogs('evolvmem.request_trace', level='WARNING') as captured:
                with tracing.request_trace('capability_search'):
                    with self.read_scope() as reader:
                        self.assertTrue(reader.roles.read(1)['ready'])
        record = json.loads(captured.records[-1].getMessage().split('request_trace ', 1)[1])
        self.assertEqual(set(record), {'trace_id', 'tool', 'stages_ms', 'error', 'total_ms'})
        for label in ('capability_role_preflight', 'capability_role_structure'):
            self.assertIn(label, record['stages_ms'])
            self.assertIsInstance(record['stages_ms'][label], (int, float))
        self.assertIsNone(record['error'])
        self.assertNotIn('Synthetic budget proof fixture.', json.dumps(record))
        self.assertNotIn('conversation:fixture', json.dumps(record))
