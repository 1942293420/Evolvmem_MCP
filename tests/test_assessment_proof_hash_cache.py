"""Native module regressions for bounded original-proof caching.

The SQLite fixture uses the actual capability schema. The explicit OriginalRead
argument is an observation seam: source eligibility algorithms are not replaced
in production, and these tests do not claim end-to-end eligibility coverage.
"""
import gc
import json
import sqlite3
import weakref
from collections import Counter

import pytest

from evolvmem import capability_assessment as assessment
from evolvmem.capability_models import CAPABILITY_SCHEMA


class Store:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for ddl in CAPABILITY_SCHEMA[:4]:
            self.conn.execute(ddl)
        self.conn.execute("""CREATE TABLE organization_units(
            task_id INTEGER,digest TEXT,revision INTEGER,text TEXT,
            disposition TEXT,source_start INTEGER,source_end INTEGER)""")
        self.conn.execute("""CREATE TABLE organization_tasks(
            id INTEGER,source_revision TEXT,status TEXT,source_key TEXT)""")

    def _connection(self):
        return self.conn

    def add_source(self, source_id, saved_hashes):
        now = "2026-11-01T00:00:00Z"
        self.conn.execute("""INSERT INTO capability_memories(
            memory_id,category,title,content,fingerprint,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (source_id, "backend", "synthetic", "body", str(source_id), now, now))
        source = {"source_kind": "context", "source_key": str(source_id),
                  "source_ref": "synthetic-ref", "unit_digest": ""}
        self.conn.execute("""INSERT INTO capability_sources(
            source_id,memory_id,source_kind,source_key,source_ref,source_revision,
            created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            (source_id, source_id, "context", str(source_id), "synthetic-ref", "r1", now, now))
        for revision, saved_hash in enumerate(saved_hashes, 1):
            details = {
                "memory_content_hash": assessment._hash(["backend", "synthetic", "body"]),
                "source": source, "original_proof": {"proof_hash": saved_hash},
            }
            self.conn.execute("""INSERT INTO capability_assessments(
                memory_id,source_id,source_binding_revision,source_revision,
                unit_revision,unit_text_hash,policy_version,assessment_key,value,
                score,details,revision,created_at)
                VALUES(?,?,1,'r1',NULL,'',?,?,'reusable',90,?,?,?)""",
                (source_id, source_id, assessment.POLICY_VERSION, str(revision),
                 json.dumps(details), revision, now))


class Payload(dict):
    """A weak-referenceable proof response holding synthetic original text."""


class ObservedReader:
    def __init__(self):
        self.calls = []
        self.refs = []
        self.alive_before_read = []

    def read(self, source):
        gc.collect()
        self.alive_before_read.append(sum(ref() is not None for ref in self.refs))
        self.calls.append(source["source_id"])
        payload = Payload(
            proof_hash="current", text="x" * (1024 * 1024),
            source_roles=[{"role": "user"}], state={"synthetic": True})
        self.refs.append(weakref.ref(payload))
        return payload


@pytest.mark.parametrize("status_only", [False, True])
def test_original_payload_does_not_survive_into_next_source(status_only):
    store = Store()
    try:
        for source_id in range(1, 9):
            # Detail history must still mark the old proof stale; status-only
            # keeps the already established old-row behavior unchanged.
            store.add_source(source_id, ["old", "current"])
        reader = ObservedReader()
        rows = assessment._rows_read(store, None, reader, status_only=status_only)
        assert len(rows) == 16
        assert Counter(reader.calls) == Counter(range(1, 9))
        for row in rows:
            expected = [] if status_only or row["revision"] == 2 else ["original_proof_changed"]
            assert row["stale_reasons"] == expected
            assert row["stale"] is bool(expected)
        # These assertions distinguish bounded hash caching from full text
        # retention, without allocator/RSS thresholds or machine-speed timing.
        assert reader.alive_before_read == [0] * 8
        assert all(ref() is None for ref in reader.refs)
    finally:
        store.conn.close()


@pytest.mark.parametrize("current,saved,stale,error", [
    ({"proof_hash": "current"}, "current", False, None),
    ({"proof_hash": "changed"}, "current", True, None),
    (None, "current", True, None),
    ({}, None, True, None),
    ({"proof_hash": None}, None, False, None),
    ({"proof_hash": ""}, "", False, None),
    ({"text": "malformed"}, "current", None, KeyError),
])
def test_missing_and_hash_values_preserve_existing_verdict(current, saved, stale, error):
    store = Store()
    calls = []

    class Reader:
        def read(self, source):
            calls.append(source["source_id"])
            return current

    try:
        store.add_source(1, [saved, saved])
        if error:
            with pytest.raises(error):
                assessment._rows_read(store, None, Reader())
        else:
            rows = assessment._rows_read(store, None, Reader())
            assert len(rows) == 2
            assert [row["stale"] for row in rows] == [stale, stale]
            assert [row["stale_reasons"] for row in rows] == [
                ["original_proof_changed"] if stale else []] * 2
        assert calls == [1]
    finally:
        store.conn.close()
