"""Prove a unit is exact frozen source text, including disjoint source ranges.

This is a text proof, not a source-eligibility or result-verification decision.
Callers still require the live provenance/current-source gate before publishing.
Snapshot slices are read with SQLite substr; the full snapshot is not loaded.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy


MAX_PROOF_CACHE_ENTRIES = 1024
MAX_PROOF_CACHE_BYTES = 8 * 1024 * 1024


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def prove_unit_row(conn, task, unit, *, messages=None):
    """Shared implementation for a transient ProvenanceRead request cache."""
    base = {"valid": False, "reason": "unit_source_text_unproven", "text": "",
            "ranges": [], "source_roles": [], "input_hash": "", "content_hash": "",
            "source_key": task["source_key"], "source_revision": task["source_revision"],
            "task_id": task["id"], "unit_digest": unit["digest"],
            "unit_revision": unit["revision"], "source_start": unit["source_start"],
            "source_end": unit["source_end"]}
    if task["status"] == "superseded" or task.get("superseded_by") is not None:
        return {**base, "reason": "task_superseded"}
    size = conn.execute("SELECT length(source_snapshot) FROM organization_tasks WHERE id=?",
                        (task["id"],)).fetchone()
    text = unit.get("text")
    if not size or not size[0] or not isinstance(text, str) or not text:
        return base
    try:
        ranges = unit.get("source_ranges") or []
        ranges = json.loads(ranges) if isinstance(ranges, str) else ranges
        if not ranges:
            ranges = [[unit["source_start"], unit["source_end"]]]
        if (not isinstance(ranges, list) or any(
                not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(value) is not int for value in pair)
                or not 0 <= pair[0] < pair[1] <= size[0] for pair in ranges)
                or any(ranges[index-1][1] > ranges[index][0] for index in range(1, len(ranges)))
                or ranges[0][0] != unit["source_start"] or ranges[-1][1] != unit["source_end"]
                or sum(b-a for a, b in ranges) + 2 * (len(ranges)-1) != len(text)):
            return base
    except (ValueError, TypeError, KeyError):
        return base
    # Each read is bounded by the stored unit's own text length, even when its
    # full source snapshot contains many complete conversations.
    pieces = [conn.execute("SELECT substr(source_snapshot,?,?) FROM organization_tasks WHERE id=?",
                           (a+1, b-a, task["id"])).fetchone()[0] for a, b in ranges]
    if "\n\n".join(pieces) != text:
        return base
    from evolvmem.auto_organization import unit_digest
    if unit_digest(task["source_key"], unit["source_start"], unit["source_end"], text) != unit["digest"]:
        return base
    roles = []
    if messages is not None:
        from evolvmem.topic_segmentation import message_spans
        spans = message_spans(messages)
        rendered = "\n\n".join(span["part"] for span in spans)
        matched = conn.execute("SELECT source_snapshot=? FROM organization_tasks WHERE id=?",
                               (rendered, task["id"])).fetchone()[0]
        if matched:
            offset = 0
            for a, b in ranges:
                for span in spans:
                    start, end = max(a, span["content_start"]), min(b, span["content_end"])
                    if end > start:
                        roles.append({"start": offset+start-a, "end": offset+end-a,
                                      "role": str(span.get("role") or "unknown")})
                offset += b-a+2
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    normalized = [list(pair) for pair in ranges]
    return {**base, "valid": True, "reason": "", "text": text, "ranges": normalized,
            "source_roles": roles, "content_hash": content_hash,
            "input_hash": _hash([task["source_key"], task["source_revision"], unit["digest"],
                                 normalized, content_hash, roles])}


def unit_source_proof(store, task_id, digest, *, messages=None):
    """Return source text and local role spans only after exact slice proof.

    Optional messages must be obtained from the real structured source, never
    model role labels. If their rendered snapshot differs, roles stay unknown.
    The proof hash excludes ownership-only revisions and includes every range.
    """
    # Roles supplied by a caller have a different proof identity; they never
    # read or populate the role-free cache. Outside an explicit read snapshot
    # there is no memoization, including during organization writes.
    from evolvmem.capability_provenance import scoped_provenance_reader
    read = scoped_provenance_reader(store) if messages is None else None
    key = (task_id, digest)
    if read is not None and key in read.unit_proofs:
        return deepcopy(read.unit_proofs[key])
    proof = _unit_source_proof(store._connection(), task_id, digest, messages=messages)
    if read is not None and len(read.unit_proofs) < MAX_PROOF_CACHE_ENTRIES:
        size = len(json.dumps(proof, ensure_ascii=False, separators=(',', ':')).encode())
        if read.unit_proof_bytes + size <= MAX_PROOF_CACHE_BYTES:
            read.unit_proofs[key] = deepcopy(proof)
            read.unit_proof_bytes += size
    return proof


def _unit_source_proof(conn, task_id, digest, *, messages=None):
    task = conn.execute("SELECT id,source_key,source_revision,status,superseded_by FROM organization_tasks WHERE id=?",
                        (task_id,)).fetchone()
    units = conn.execute("SELECT digest,revision,text,source_start,source_end,source_ranges FROM organization_units WHERE task_id=? AND digest=?",
                         (task_id, digest)).fetchall()
    if task is None or len(units) != 1:
        return {"valid": False, "reason": "unit_missing_or_ambiguous", "text": "",
                "ranges": [], "source_roles": [], "input_hash": "", "content_hash": ""}
    return prove_unit_row(conn, dict(task), dict(units[0]), messages=messages)
