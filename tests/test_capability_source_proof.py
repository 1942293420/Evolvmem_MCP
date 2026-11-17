import json

import pytest

from evolvmem.auto_organization import unit_digest
from evolvmem.capability_provenance import annotate
from evolvmem.capability_source_proof import unit_source_proof
from evolvmem.topic_segmentation import message_spans
from tests.test_capability_provenance import provenance_db, add_memory, unit_source, DIGEST


def save(store, snapshot, text, ranges):
    start, end = ranges[0][0], ranges[-1][1]
    digest = unit_digest("archive:1", start, end, text)
    with store.transaction():
        conn = store._connection()
        conn.execute("UPDATE organization_tasks SET source_snapshot=? WHERE id=1", (snapshot,))
        conn.execute("UPDATE organization_units SET text=?,digest=?,source_start=?,source_end=?,source_ranges=? WHERE task_id=1", (text,digest,start,end,json.dumps(ranges)))
    return digest


def test_contiguous_and_ordered_noncontiguous_original_text_proof(provenance_db):
    store = provenance_db
    digest = save(store, "first middle final", "first\n\nfinal", [[0,5],[13,18]])
    result = unit_source_proof(store, 1, digest)
    assert result["valid"] and result["text"] == "first\n\nfinal"
    assert result["ranges"] == [[0,5],[13,18]]
    assert result["source_roles"] == []
    identity = add_memory(store, source=unit_source(store, unit_digest=digest))
    assert annotate(store, [identity])[identity]["eligible"]
    assert result["input_hash"] == unit_source_proof(store, 1, digest)["input_hash"]
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET revision=2,project='beta',projects='[\"beta\"]'")
    assert unit_source_proof(store, 1, digest)["input_hash"] == result["input_hash"]


@pytest.mark.parametrize("ranges", [[[0,5],[4,8]], [[5,9],[0,5]], [[False,5]], [[0,100]]])
def test_invalid_range_proof_is_closed_without_providing_text(provenance_db, ranges):
    store = provenance_db
    digest = save(store,"first middle final","invented",ranges)
    result = unit_source_proof(store,1,digest)
    assert not result["valid"] and result["text"] == ""


def test_digest_of_fabricated_unit_is_not_original_text_evidence(provenance_db):
    store = provenance_db
    digest = save(store,"original text", "fabricated!!!",[[0,13]])
    identity = add_memory(store, source=unit_source(store, unit_digest=digest))
    result = annotate(store,[identity])[identity]
    assert not result["eligible"]
    assert result["sources"][0]["reason"] == "unit_source_text_unproven"


def test_role_offsets_follow_noncontiguous_ranges_from_real_messages(provenance_db):
    store = provenance_db
    messages=[{"role":"assistant","content":"first claim"}, {"role":"user","content":"skip this"}, {"role":"user","content":"confirmed last"}]
    spans=message_spans(messages)
    snapshot="\n\n".join(s["part"] for s in spans)
    ranges=[[spans[0]["content_start"],spans[0]["content_end"]],[spans[2]["content_start"],spans[2]["content_end"]]]
    digest=save(store,snapshot,"first claim\n\nconfirmed last",ranges)
    result=unit_source_proof(store,1,digest,messages=messages)
    assert result["valid"]
    assert result["source_roles"] == [{"start":0,"end":11,"role":"assistant"},{"start":13,"end":27,"role":"user"}]
    forged=unit_source_proof(store,1,digest,messages=[{"role":"user","content":"first claim"}])
    assert forged["source_roles"] == []


def test_proof_reads_only_bounded_snapshot_slices(provenance_db):
    store=provenance_db
    digest=save(store,"first"+"x"*100000+"final","first\n\nfinal",[[0,5],[100005,100010]])
    calls=[]
    store._connection().set_trace_callback(calls.append)
    try:assert unit_source_proof(store,1,digest)["valid"]
    finally:store._connection().set_trace_callback(None)
    assert any("substr(source_snapshot" in sql for sql in calls)
    assert not any("SELECT source_snapshot " in sql or "SELECT * FROM organization_tasks" in sql for sql in calls)
