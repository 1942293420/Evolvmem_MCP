"""Source-checked extraction cache; a saved response is not published knowledge.

The cache excludes ownership from its key so a human assignment can reuse the
same extraction. Every reuse rechecks the current unit and binds its current
ownership. Leases serialize expensive calls without holding a database lock.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime

from evolvmem import knowledge_scope as ks


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS extraction_receipts(
        input_key TEXT PRIMARY KEY, pipeline TEXT NOT NULL,
        source_key TEXT NOT NULL, source_revision TEXT NOT NULL,
        unit_digest TEXT NOT NULL, payload TEXT,
        lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
)


def validate_occurred_at(value):
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 50 or not value.strip():
        raise ValueError("invalid_occurred_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_occurred_at") from None


def assert_unit_current(service, task, unit):
    """Compare both revisions and fields, including manual edits during a call."""
    conn = service.store._connection()
    current_task = conn.execute("SELECT * FROM organization_tasks WHERE id=?", (task["id"],)).fetchone()
    current = conn.execute("SELECT * FROM organization_units WHERE task_id=? AND digest=?",
                           (task["id"], unit["digest"])).fetchone()
    if current_task is None or current is None or current_task["status"] == "superseded":
        raise ValueError("revision_conflict")
    for key in ("source_key", "source_revision", "rule_revision"):
        if current_task[key] != task[key]:
            raise ValueError("revision_conflict")
    for key in ("revision", "text", "source_start", "source_end", "decision", "disposition",
                "project", "projects", "knowledge_scope"):
        if current[key] != unit[key]:
            raise ValueError("revision_conflict")
    # Full ContextService also checks the underlying archive/body and policy,
    # rather than trusting an unchanged task row. Minimal store fixtures omit it.
    if callable(getattr(service, "knowledge", None)):
        from evolvmem.auto_organization import _assert_current
        _assert_current(service, task)


def unit_scope(service, unit):
    """Only a current confirmed unit establishes ownership; source location does not."""
    projects = ks.projects(unit)
    scope = ks.scope(unit)
    confirmed = (unit["decision"] in ("auto", "manual")
                 and unit["disposition"] in ("keep", "history_only"))
    if scope == "global":
        confirmed = confirmed and not projects
    else:
        active = {r[0] for r in service.store._connection().execute(
            "SELECT project FROM context_project_registry WHERE status='active'")}
        confirmed = confirmed and bool(projects) and set(projects) <= active
    return {"projects": tuple(projects) if confirmed else (),
            "knowledge_scope": scope if confirmed else "project",
            "evidence_status": "confirmed" if confirmed else "unverified"}


def extraction_key(task, unit, pipeline):
    """One identity for extraction, replay and read-only terminal accounting."""
    stamp = [pipeline, task["source_key"], task["source_revision"], task["rule_revision"],
             unit["digest"], unit["text"], unit["source_start"], unit["source_end"]]
    return hashlib.sha256(json.dumps(stamp, ensure_ascii=False).encode()).hexdigest()


def cached_payload(service, task, unit, pipeline):
    """Read an exact current-input receipt without claiming or calling a model."""
    row = service.store._connection().execute(
        "SELECT payload FROM extraction_receipts WHERE input_key=?",
        (extraction_key(task, unit, pipeline),)).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] is not None else None


def extraction_payload(service, task, unit, pipeline, extract, validate=lambda p: None, *, guarded_extract=None):
    assert_unit_current(service, task, unit)
    key = extraction_key(task, unit, pipeline)
    conn = service.store._connection()
    token = uuid.uuid4().hex
    now = time.time()
    with service.store.transaction():
        row = conn.execute("SELECT * FROM extraction_receipts WHERE input_key=?", (key,)).fetchone()
        if row and row["payload"] is not None:
            payload = json.loads(row["payload"])
            validate(payload)
            return payload
        if row and row["lease_until"] > now:
            raise RuntimeError("extraction_in_progress")
        conn.execute(
            "INSERT INTO extraction_receipts(input_key,pipeline,source_key,source_revision,unit_digest,"
            "lease_token,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(input_key) DO UPDATE SET lease_token=excluded.lease_token,"
            "lease_until=excluded.lease_until,updated_at=excluded.updated_at",
            (key, pipeline, task["source_key"], task["source_revision"], unit["digest"], token, now + 300, now, now))
    def check_current():
        """Optional bounded extractors may fence every model attempt."""
        assert_unit_current(service, task, unit)
        lease = conn.execute(
            "SELECT lease_token,lease_until FROM extraction_receipts WHERE input_key=?", (key,)).fetchone()
        if lease is None or lease["lease_token"] != token or lease["lease_until"] <= time.time():
            raise ValueError("extraction_lease_conflict")
    try:
        payload = guarded_extract(check_current) if guarded_extract is not None else extract()
        validate(payload)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with service.store.transaction():
            if guarded_extract is not None:
                check_current()
            else:
                assert_unit_current(service, task, unit)
            updated = conn.execute("UPDATE extraction_receipts SET payload=?,lease_token='',lease_until=0,updated_at=? "
                                   "WHERE input_key=? AND lease_token=?",
                                   (encoded, time.time(), key, token))
            if updated.rowcount != 1:
                raise ValueError("extraction_lease_conflict")
        return payload
    except BaseException:
        with service.store.transaction():
            conn.execute("UPDATE extraction_receipts SET lease_token='',lease_until=0 WHERE input_key=? AND lease_token=?", (key, token))
        raise
