"""Auditable knowledge value, deliberately separate from verified experience.

Only the existing ExperienceService may establish success evidence/counters.
This module never invokes outcome() and never labels an extracted method verified.
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher

from evolvmem.context_store import _now_iso

POLICY_VERSION = "assessment-policy:v2"
_KINDS = {"fact", "decision", "procedure", "pitfall"}
_STRUCTURED = ("kind", "trigger", "conditions", "action_steps", "result", "evidence_quotes", "value_reason")
_NOISE = re.compile(r"^(?:running\b|executing\b|all tests passed|i (?:completed|finished)|已完成|正在执行|继续下一步|测试通过)", re.I)
_SLOGAN = re.compile(r"(?:improve quality(?: and follow best practices)?|follow best practices|提高效率|确保质量|保持沟通|优化体验)[。.!！]*", re.I)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate(candidate, source_text=None):
    if candidate.get("action") != "keep":
        return
    for field in ("kind", "trigger", "result", "value_reason"):
        if not isinstance(candidate.get(field), str):
            raise ValueError("invalid_capability_" + field)
    if candidate["kind"] not in _KINDS:
        raise ValueError("invalid_capability_kind")
    for field in ("conditions", "action_steps", "evidence_quotes"):
        values = candidate.get(field)
        if not isinstance(values, list) or len(values) > 20 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 8000 for value in values
        ):
            raise ValueError("invalid_capability_" + field)
    if source_text is not None and any(quote not in source_text for quote in candidate["evidence_quotes"]):
        raise ValueError("capability_quote_not_in_source")


def evaluate(candidate, *, source_text, source_roles=(), reviewer=None):
    """Evaluate server-provided source text/roles; ignore extractor scores/roles.

    An optional independent reviewer receives source/candidate hashes and must
    return those hashes, supported=true, a reason and exact evidence quotes.
    Review failure leaves a candidate; it never establishes execution success.
    """
    validate(candidate, source_text)
    structure = {field: candidate[field] for field in _STRUCTURED}
    claims = [candidate["title"], candidate["content"], candidate["trigger"], candidate["result"],
              *candidate["conditions"], *candidate["action_steps"]]
    claims = [claim.strip() for claim in claims if claim.strip()]
    evidence = []
    for quote in dict.fromkeys(candidate["evidence_quotes"]):
        occurrences = [m.start() for m in re.finditer(re.escape(quote), source_text)]
        roles = sorted({span["role"] for start in occurrences for span in source_roles
                        if span["start"] <= start and start + len(quote) <= span["end"]})
        evidence.append({"quote": quote, "offsets": occurrences, "roles": roles or ["unknown"]})
    input_hash = _hash([source_text, source_roles])
    candidate_hash = _hash({"category": candidate["category"], "title": candidate["title"],
                            "content": candidate["content"], **structure})
    direct = bool(evidence) and all(any(claim in e["quote"] for e in evidence) for claim in claims)
    support = {"method": "verbatim" if direct else "unresolved", "input_hash": input_hash,
               "candidate_hash": candidate_hash, "supported": direct}
    if evidence and not direct and reviewer is not None:
        request = {"input_hash": input_hash, "candidate_hash": candidate_hash,
                   "source_text": source_text, "source_roles": source_roles,
                   "claims": claims, "evidence_quotes": candidate["evidence_quotes"]}
        try:
            review = reviewer(request)
            quotes = review.get("evidence_quotes") if isinstance(review, dict) else None
            accepted = (isinstance(review, dict) and review.get("supported") is True
                        and review.get("input_hash") == input_hash
                        and review.get("candidate_hash") == candidate_hash
                        and isinstance(review.get("reason"), str) and bool(review["reason"].strip())
                        and isinstance(quotes, list) and bool(quotes)
                        and all(isinstance(q, str) and q.strip() and q in source_text for q in quotes))
            support = {**support, "method": "independent_review", "supported": accepted,
                       "review": review if isinstance(review, dict) else {"error": "invalid_review"}}
        except Exception as exc:
            support = {**support, "method": "independent_review", "error": type(exc).__name__}
    actionable = bool(candidate["trigger"].strip() and candidate["conditions"] and candidate["action_steps"])
    noise = bool(_NOISE.search(candidate["content"].strip()) or _SLOGAN.fullmatch(candidate["content"].strip()))
    independent = bool(candidate["result"].strip()) and any(
        candidate["result"] in e["quote"] and set(e["roles"]) & {"user", "tool"} for e in evidence)
    rubric = {"source_quote": bool(evidence), "claim_support": bool(support["supported"]),
              "actionable": actionable, "specificity": not noise, "independent_result": independent}
    reasons = []
    if noise:
        value = "ignore"
        reasons.append("generic_or_progress_only")
    elif not evidence:
        value = "review"
        reasons.append("missing_evidence")
    elif not support["supported"]:
        value = "review"
        reasons.append("unsupported_claim")
    elif candidate["kind"] in {"procedure", "pitfall"} and actionable:
        value = "reusable"
        reasons.append("supported_scoped_action")
    elif candidate["kind"] in {"fact", "decision"}:
        value = "reference"
        reasons.append("supported_reference")
    else:
        value = "review"
        reasons.append("missing_action_context")
    return {"policy_version": POLICY_VERSION, "value": value, "score": sum(rubric.values()),
            "verification_status": "unverified", "verification_reason": "independent_experience_verification_required",
            "structure": structure, "evidence": evidence, "support": support,
            "rubric": rubric, "reasons": reasons, "input_hash": input_hash,
            "candidate_hash": candidate_hash, "unit_text_hash": _hash(source_text)}


def assess_unit(service, task, unit, candidate, *, reviewer=None):
    from evolvmem.auto_organization import source_messages
    from evolvmem.capability_source_proof import unit_source_proof
    messages = None
    try:
        messages = source_messages(service, task["source_key"])
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    proof = unit_source_proof(service.store, task["id"], unit["digest"], messages=messages)
    if proof["valid"]:
        result = evaluate(candidate, source_text=proof["text"], source_roles=proof["source_roles"], reviewer=reviewer)
    else:
        # A quote in a stored unit is insufficient if its frozen original slices
        # cannot be proved. Preserve the rejected extraction receipt separately.
        result = evaluate({**candidate, "evidence_quotes": []}, source_text="", source_roles=[])
        result.update(value="review", score=0, reasons=[proof["reason"]])
    result["unit_source_proof"] = {k: v for k, v in proof.items() if k not in {"text", "source_roles"}}
    return result


def _source_stamp(store, source):
    """Content/original identity only; mutable ownership decisions are excluded."""
    from evolvmem.capability_provenance import context_revision
    conn = store._connection()
    fields = ("source_kind", "source_key", "source_ref", "source_revision") + (
        ("context_item_id",) if source.get("source_kind") == "context" else ("task_id", "unit_digest"))
    identity = {field: source.get(field) for field in fields}
    if source.get("source_kind") == "unit":
        from evolvmem.capability_source_proof import unit_source_proof
        proof = unit_source_proof(store, source.get("task_id"), source.get("unit_digest"))
        origin = {field: proof.get(field) for field in
                  ("valid", "reason", "source_key", "source_revision", "unit_digest", "ranges", "content_hash")}
    elif source.get("source_kind") == "context":
        item_id = source.get("context_item_id")
        links = [dict(r) for r in conn.execute(
            "SELECT source_kind,source_ref,archive_id,extraction_version FROM context_sources "
            "WHERE item_id=? ORDER BY id", (item_id,))]
        archives = []
        for archive_id in sorted({r["archive_id"] for r in links if r["archive_id"] is not None}):
            row = conn.execute("SELECT id,payload_sha256,state FROM session_archives WHERE id=?", (archive_id,)).fetchone()
            archives.append(dict(row) if row else {"id": archive_id, "missing": True})
        origin = [context_revision(conn, item_id), links, archives]
    else:
        origin = None
    return _hash([identity, origin])


def _source_match(source):
    # Available on both a full binding and ProvenanceRead's public source fact.
    fields = ("source_kind", "source_key") + (
        ("context_item_id",) if source.get("source_kind") == "context" else ("task_id", "unit_digest"))
    return [source.get(field) for field in fields]


def _preserved_sources(store, memory_id, source_id):
    from evolvmem.capability_provenance import ProvenanceRead
    read = ProvenanceRead(store)
    memory = dict(store._connection().execute(
        "SELECT memory_id,status,source_ref FROM capability_memories WHERE memory_id=?", (memory_id,)).fetchone())
    _, sources = read.memory(memory)
    current = next(source for source in sources if source.get("source_id") == source_id)
    assessed_origin = read._origin_identity(current)
    return [{"source_id": source.get("source_id"), "origin": _source_match(source),
             "stamp": _source_stamp(store, source)}
            for source in sources if read._origin_identity(source) != assessed_origin and read.evaluate(source)["valid"]]


def persist(store, memory_id, source_id, result, *, existing_status=None):
    """Append an idempotent source assessment and apply the new-memory recall gate.

    Call inside the same transaction as upsert/bind_source. Existing active
    independent knowledge cannot be downgraded by a new weak extraction.
    """
    conn = store._connection()
    with store.transaction():
        source = conn.execute("SELECT * FROM capability_sources WHERE source_id=? AND memory_id=?",
                              (source_id, memory_id)).fetchone()
        if source is None:
            raise ValueError("assessment_source_not_found")
        memory = conn.execute("SELECT category,title,content FROM capability_memories WHERE memory_id=?", (memory_id,)).fetchone()
        result = {**result, "source": {field: source[field] for field in (
            "source_kind", "source_key", "source_ref", "source_revision", "context_item_id",
            "task_id", "unit_digest", "unit_revision", "revision")},
            "memory_content_hash": _hash([memory["category"], memory["title"], memory["content"]])}
        previous = conn.execute("SELECT details FROM capability_assessments WHERE memory_id=? ORDER BY assessment_id LIMIT 1", (memory_id,)).fetchone()
        legacy = existing_status == "active" and previous is None
        result["source_stamp"] = _source_stamp(store, dict(source))
        if legacy:
            result["legacy_preserved_sources"] = _preserved_sources(store, memory_id, source_id)
        elif previous is not None:
            first = json.loads(previous["details"])
            if "legacy_preserved_sources" in first:
                # Reuse the first frozen whitelist; replay must neither change
                # the assessment key nor admit sources attached later.
                result["legacy_preserved_sources"] = first["legacy_preserved_sources"]
        binding = [source["revision"], source["source_revision"], source["unit_revision"]]
        key = _hash([binding, result])
        row = conn.execute("SELECT assessment_id FROM capability_assessments WHERE source_id=? AND assessment_key=?",
                           (source_id, key)).fetchone()
        if row is None:
            revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM capability_assessments WHERE source_id=?", (source_id,)).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO capability_assessments(memory_id,source_id,source_binding_revision,source_revision,"
                "unit_revision,unit_text_hash,policy_version,assessment_key,value,verification_status,score,"
                "details,legacy_active_preserved,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (memory_id, source_id, *binding, result["unit_text_hash"], POLICY_VERSION, key,
                 result["value"], "unverified", result["score"], json.dumps(result, ensure_ascii=False),
                 int(legacy), revision, _now_iso()))
            assessment_id = int(cursor.lastrowid)
        else:
            assessment_id = int(row[0])
        admissible = result["value"] in {"reusable", "reference"}
        if existing_status is None and not admissible:
            conn.execute("UPDATE capability_memories SET status='pending',revision=revision+1,updated_at=? WHERE memory_id=? AND status='active'", (_now_iso(), memory_id))
        elif existing_status == "pending" and admissible:
            conn.execute("UPDATE capability_memories SET status='active',revision=revision+1,updated_at=? WHERE memory_id=? AND status='pending'", (_now_iso(), memory_id))
        return assessment_id


def _rows(store, ids=None):
    if not store._connection().execute("SELECT 1 FROM sqlite_master WHERE name='capability_assessments'").fetchone():
        return []
    from evolvmem.capability_assessment_migration import OriginalRead
    reader = OriginalRead(store)
    reader._begin_scope()
    try:
        result = _rows_read(store, ids, reader)
        reader._assert_scope()
        return result
    finally:
        reader._end_scope()


def _rows_read(store, ids, original_read):
    conn = store._connection()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='capability_assessments'").fetchone():
        return []
    selected = None if ids is None else sorted(set(int(i) for i in ids))
    batches = [None] if selected is None else [selected[i:i+400] for i in range(0, len(selected), 400)]
    result = []
    original_cache = {}
    source_stamps = {}
    for batch in batches:
        where = "" if batch is None else " WHERE a.memory_id IN (" + ",".join("?" for _ in batch) + ")"
        rows = conn.execute(
            "SELECT a.*,s.revision AS current_binding_revision,s.source_revision AS current_source_revision,"
            "s.source_kind AS current_source_kind,s.source_key AS current_source_key,"
            "s.source_ref AS current_source_ref,s.unit_digest AS current_unit_digest,"
            "s.unit_revision AS current_bound_unit_revision,u.revision AS current_unit_revision,"
            "CASE WHEN length(u.text)<=24000 AND length(CAST(u.text AS BLOB))<=8388608 THEN u.text END AS current_text,"
            "(length(u.text)>24000 OR length(CAST(u.text AS BLOB))>8388608) AS current_text_oversized,"
            "u.disposition AS current_disposition,t.source_revision AS task_source_revision,t.status AS task_status,"
            "m.category AS current_category,m.title AS current_title,m.content AS current_content,"
            "u.source_start AS current_source_start,u.source_end AS current_source_end,"
            "t.source_key AS current_task_source_key "
            "FROM capability_assessments a JOIN capability_memories m ON m.memory_id=a.memory_id "
            "LEFT JOIN capability_sources s ON s.source_id=a.source_id "
            "LEFT JOIN organization_units u ON u.task_id=s.task_id AND u.digest=s.unit_digest "
            "LEFT JOIN organization_tasks t ON t.id=s.task_id" + where + " ORDER BY a.assessment_id DESC", batch or ())
        for raw in rows:
            row = dict(raw)
            details = json.loads(row["details"])
            reasons = []
            if details.get("memory_content_hash") != _hash([row["current_category"], row["current_title"], row["current_content"]]):
                reasons.append("derived_content_changed")
            # Ownership-only revisions do not change the source's knowledge value.
            # Bind content identity/ranges, not a mutable project label or decision.
            source_kind = details.get("source", {}).get("source_kind")
            revision_pairs = [("source_revision", "current_source_revision")]
            if source_kind == "unit":
                revision_pairs.append(("source_revision", "task_source_revision"))
            for old, current in revision_pairs:
                if row[old] != row[current]:
                    reasons.append(current + "_changed")
            for field in ("source_kind", "source_key", "source_ref", "unit_digest"):
                if details.get("source", {}).get(field) != row["current_" + field]:
                    reasons.append(field + "_changed")
            source = details.get("source", {})
            if (source.get("source_kind") == "unit" and (
                    source.get("source_key") != f"{row['current_task_source_key']}#{row['current_unit_digest']}"
                    or not str(source.get("source_ref", "")).endswith(
                        f"@{row['current_source_start']}-{row['current_source_end']}"))):
                reasons.append("source_range_or_identity_changed")
            if source_kind == "unit":
                if (row["current_unit_revision"] is None or
                        (row["unit_revision"] is not None and row["unit_revision"] > row["current_unit_revision"])):
                    reasons.append("unit_version_unavailable")
                if row["current_text"] is None or _hash(row["current_text"]) != row["unit_text_hash"]:
                    reasons.append("unit_text_changed")
                if row["task_status"] == "superseded" or row["current_disposition"] != "keep":
                    reasons.append("source_withdrawn")
                from evolvmem.capability_source_proof import unit_source_proof
                proof = ({"valid": False} if row["current_text_oversized"] else
                         unit_source_proof(store, source.get("task_id"), source.get("unit_digest")))
                if not proof["valid"]:
                    reasons.append("unit_original_unproven")
                saved_proof = details.get("unit_source_proof")
                if saved_proof and any(saved_proof.get(k) != proof.get(k) for k in
                                       ("ranges", "content_hash", "source_key", "source_revision", "unit_digest")):
                    reasons.append("unit_original_changed")
            if details.get("original_proof"):
                if original_read is None:
                    from evolvmem.capability_assessment_migration import OriginalRead
                    original_read = OriginalRead(store)
                source_id = row["source_id"]
                if source_id not in original_cache:
                    current = conn.execute("SELECT * FROM capability_sources WHERE source_id=?", (source_id,)).fetchone()
                    original_cache[source_id] = original_read.read(dict(current)) if current else None
                proof = original_cache[source_id]
                if not proof or proof["proof_hash"] != details["original_proof"]["proof_hash"]:
                    reasons.append("original_proof_changed")
            elif source_kind != "unit":
                reasons.append("context_original_proof_missing")
            if details.get("source_stamp"):
                source_id = row["source_id"]
                if source_id not in source_stamps:
                    current = conn.execute("SELECT * FROM capability_sources WHERE source_id=?", (source_id,)).fetchone()
                    source_stamps[source_id] = (_source_stamp(store, dict(current))
                                                if current and not row["current_text_oversized"] else None)
                if source_stamps[source_id] != details["source_stamp"]:
                    reasons.append("source_origin_changed")
            if row["policy_version"] != POLICY_VERSION:
                reasons.append("policy_changed")
            public = {key: value for key, value in row.items() if not key.startswith("current_") and key not in {"task_source_revision", "task_status"}}
            public.update(details=details, stale=bool(reasons), stale_reasons=reasons)
            result.append(public)
    return result


def assessments(store, memory_id):
    return _rows(store, [memory_id])


def assessment_status(store, ids=None):
    """Batch read gate; combine eligible_source_ids with live provenance validity."""
    if ids is None:
        ids = [row[0] for row in store._connection().execute("SELECT memory_id FROM capability_memories")]
    ids = sorted(set(int(i) for i in ids))
    grouped = {ident: [] for ident in ids}
    for row in _rows(store, ids):
        grouped[row["memory_id"]].append(row)
    result = {}
    for ident, rows in grouped.items():
        latest = {}
        for row in rows:
            latest.setdefault(row["source_id"], row)
        eligible_sources = sorted(source_id for source_id, row in latest.items()
                                  if not row["stale"] and row["value"] in {"reusable", "reference"})
        legacy = any(row["legacy_active_preserved"] for row in rows)
        bound_ids = {r[0] for r in store._connection().execute(
            "SELECT source_id FROM capability_sources WHERE memory_id=?", (ident,))} if rows else set()
        unassessed = sorted(bound_ids - set(latest))
        frozen = []
        for row in rows:
            if "derived_content_changed" in row["stale_reasons"]:
                continue
            frozen.extend(row["details"].get("legacy_preserved_sources", ()))
        preserved, preserved_origins = [], []
        if frozen:
            from evolvmem.capability_provenance import ProvenanceRead
            read = ProvenanceRead(store)
            memory = dict(store._connection().execute(
                "SELECT memory_id,status,source_ref FROM capability_memories WHERE memory_id=?", (ident,)).fetchone())
            _, current_sources = read.memory(memory)
            for source in current_sources:
                source_id = source.get("source_id")
                if source_id in latest or not read.evaluate(source)["valid"]:
                    continue
                origin = _source_match(source)
                # A previously implicit origin may gain its first concrete ID;
                # a previously explicit origin must retain that exact binding.
                matches = [saved for saved in frozen if saved.get("origin") == origin
                           and (saved["source_id"] is None or saved["source_id"] == source_id)]
                if matches and any(_source_stamp(store, source) == saved["stamp"] for saved in matches):
                    preserved.append(source_id)
                    preserved_origins.append(origin)
        result[ident] = {"assessment_eligible": not rows or bool(preserved) or bool(eligible_sources),
                        "unassessed_source_ids": unassessed, "legacy_preserved_source_ids": preserved,
                        "legacy_preserved_origins": preserved_origins,
                        "legacy_unassessed": not rows, "legacy_active_preserved": legacy,
                        "eligible_source_ids": eligible_sources, "assessed_source_ids": sorted(latest),
                        "stale_count": sum(row["stale"] for row in latest.values()),
                        "assessments_count": len(rows)}
    return result


def duplicate_candidates(store, content, *, exclude_memory_id=None, limit=5):
    """Bounded lexical suggestions only; never merge/delete or certify evidence."""
    rows = store._connection().execute("SELECT memory_id,content FROM capability_memories ORDER BY memory_id DESC LIMIT 500")
    hits = [{"memory_id": row["memory_id"], "similarity": SequenceMatcher(None, content, row["content"]).ratio()}
            for row in rows if row["memory_id"] != exclude_memory_id]
    return sorted((hit for hit in hits if hit["similarity"] >= .8), key=lambda hit: -hit["similarity"])[:max(0, min(int(limit), 20))]
