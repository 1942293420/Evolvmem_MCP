"""Only valid independent origins present at first assessment keep legacy admission."""
from dataclasses import replace
import hashlib

import pytest

from evolvmem import capability_assessment as assessment
from evolvmem import capability_assessment_migration as migration
from evolvmem.capability_provenance import annotate, context_revision
from evolvmem.capability_recall_eligibility import recall_facts
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from tests.test_auto_organization import base_service, service
from tests.test_capability_assessment_migration import TEXT, fields, memory_row, old_unit_memory
from tests.test_ownership_candidates import confirmed_context


def bind_context(service, memory_id, project):
    item = confirmed_context(service, TEXT, project=project)
    source = CapabilitySourceDraft(source_kind="context", source_key=f"context:{item.id}",
        source_ref=f"context_item:{item.id}", context_item_id=item.id,
        source_revision=context_revision(service.store._connection(), item.id),
        projects=(project,), evidence_status="confirmed")
    source_id = CapabilityStore(service.store).bind_source(memory_id, source)
    return item, source, source_id


def review_source(service, memory_id, source_id):
    store = service.store
    source = dict(store._connection().execute(
        "SELECT * FROM capability_sources WHERE source_id=?", (source_id,)).fetchone())
    memory = memory_row(service, memory_id)
    proof = migration.OriginalRead(store).read(source)
    result = assessment.evaluate({"action": "keep", "category": memory["category"],
        "title": memory["title"], "content": memory["content"], **fields(), "evidence_quotes": []},
        source_text=proof["text"], source_roles=proof["source_roles"])
    result["original_proof"] = migration.public_proof(proof)
    assert result["value"] == "review"
    assessment.persist(store, memory_id, source_id, result, existing_status="active")


def source_ids(service, memory_id):
    return [source["source_id"] for source in CapabilityStore(service.store).sources(memory_id)]


def test_new_valid_origin_cannot_revive_a_reviewed_legacy_memory(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    before = memory_row(service, memory_id)
    review_source(service, memory_id, source_ids(service, memory_id)[0])
    assert memory_row(service, memory_id) == before
    _, _, added_id = bind_context(service, memory_id, "shop")
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert any(source["source_id"] == added_id and source["valid"] for source in fact["sources"])
    assert not fact["recall_eligible"]
    assert fact["source_projects"] == []
    assert fact["assessment"]["legacy_preserved_source_ids"] == []
    after = memory_row(service, memory_id)
    assert {key: after[key] for key in ("memory_id", "title", "content", "fingerprint", "status")} == {
        key: before[key] for key in ("memory_id", "title", "content", "fingerprint", "status")}


def test_preserve_only_preexisting_independent_origin_until_it_is_reviewed(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    first_id = source_ids(service, memory_id)[0]
    _, _, old_id = bind_context(service, memory_id, "evo")
    review_source(service, memory_id, first_id)
    _, _, new_id = bind_context(service, memory_id, "shop")
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert fact["recall_eligible"]
    assert fact["eligible_source_ids"] == [old_id]
    assert fact["source_projects"] == ["evo"]
    assert fact["assessment"]["legacy_preserved_source_ids"] == [old_id]
    assert set(fact["assessment"]["unassessed_source_ids"]) == {old_id, new_id}
    review_source(service, memory_id, old_id)
    after = recall_facts(service.store, [memory_id])[memory_id]
    assert not after["recall_eligible"] and after["source_projects"] == []
    assert after["assessment"]["legacy_preserved_source_ids"] == []
    assert after["assessment"]["unassessed_source_ids"] == [new_id]
    assert CapabilityStore(service.store).get(memory_id)["status"] == "active"


@pytest.mark.parametrize("change", ["source_ref", "original_content"])
def test_rebound_old_origin_cannot_use_its_preservation_stamp(service, monkeypatch, change):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    first_id = source_ids(service, memory_id)[0]
    item, source, old_id = bind_context(service, memory_id, "evo")
    review_source(service, memory_id, first_id)
    conn = service.store._connection()
    if change == "source_ref":
        source = replace(source, source_ref=source.source_ref + ":replacement")
    else:
        replacement = "SQLite 写入已有新的原始约束。"
        with service.store.transaction():
            conn.execute("UPDATE context_layers SET content=?,content_hash=? WHERE item_id=? AND layer='l2'",
                (replacement, hashlib.sha256(replacement.encode()).hexdigest(), item.id))
        source = replace(source, source_revision=context_revision(conn, item.id))
    CapabilityStore(service.store).bind_source(memory_id, source)
    provenance = annotate(service.store, [memory_id])[memory_id]
    assert any(row["source_id"] == old_id and row["valid"] for row in provenance["sources"])
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert fact["assessment"]["legacy_preserved_source_ids"] == []
    assert not fact["recall_eligible"] and fact["source_projects"] == []


def test_ownership_only_changes_preserve_old_original_and_read_current_project(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    first_id = source_ids(service, memory_id)[0]
    item, source, old_id = bind_context(service, memory_id, "evo")
    review_source(service, memory_id, first_id)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE context_items SET project='dsh',projects='[\"dsh\"]' WHERE id=?", (item.id,))
        conn.execute("UPDATE context_project_resolutions SET resolved_project='dsh',revision=revision+1 WHERE item_id=?", (item.id,))
    CapabilityStore(service.store).bind_source(memory_id, replace(source, projects=("dsh",)))
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert fact["recall_eligible"]
    assert fact["source_projects"] == ["dsh"]
    assert fact["assessment"]["legacy_preserved_source_ids"] == [old_id]
    assert not assessment.assessments(service.store, memory_id)[0]["stale"]


def test_invalid_preexisting_origin_does_not_acquire_legacy_preservation(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    first_id = source_ids(service, memory_id)[0]
    item, _, old_id = bind_context(service, memory_id, "evo")
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE context_items SET status='archived' WHERE id=?", (item.id,))
    review_source(service, memory_id, first_id)
    with service.store.transaction():
        conn.execute("UPDATE context_items SET status='active' WHERE id=?", (item.id,))
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert any(row["source_id"] == old_id and row["valid"] for row in fact["sources"])
    assert not fact["recall_eligible"] and fact["source_projects"] == []
    assert fact["assessment"]["legacy_preserved_source_ids"] == []


def test_replaying_first_legacy_review_is_idempotent_and_does_not_expand_preservation(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    first_id = source_ids(service, memory_id)[0]
    _, _, old_id = bind_context(service, memory_id, "evo")
    review_source(service, memory_id, first_id)
    original = assessment.assessments(service.store, memory_id)
    _, _, new_id = bind_context(service, memory_id, "shop")
    review_source(service, memory_id, first_id)
    replayed = assessment.assessments(service.store, memory_id)
    assert len(replayed) == len(original) == 1
    assert replayed[0]["assessment_id"] == original[0]["assessment_id"]
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert fact["assessment"]["legacy_preserved_source_ids"] == [old_id]
    assert new_id not in fact["eligible_source_ids"]
    assert fact["source_projects"] == ["evo"]


def legacy_mapping(service, memory_id, item_id):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO knowledge_restructure_map(legacy_item_id,target_kind,target_id,outcome,updated_at) "
            "VALUES(?,'capability',?,'migrated','old')", (item_id, str(memory_id)))


def test_new_implicit_context_mapping_cannot_expand_frozen_legacy_origins(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    review_source(service, memory_id, source_ids(service, memory_id)[0])
    item = confirmed_context(service, TEXT, project="shop")
    legacy_mapping(service, memory_id, item.id)
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert any(row.get("source_id") is None and row["valid"] for row in fact["sources"])
    assert not fact["recall_eligible"] and fact["source_projects"] == []


def test_changed_implicit_original_cannot_reuse_frozen_legacy_stamp(service, monkeypatch):
    memory_id, _, _ = old_unit_memory(service, monkeypatch)
    item = confirmed_context(service, TEXT, project="evo")
    legacy_mapping(service, memory_id, item.id)
    review_source(service, memory_id, source_ids(service, memory_id)[0])
    assert recall_facts(service.store, [memory_id])[memory_id]["recall_eligible"]
    replacement = "SQLite 写入已有新的原始约束。"
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_layers SET content=?,content_hash=? WHERE item_id=? AND layer='l2'",
            (replacement, hashlib.sha256(replacement.encode()).hexdigest(), item.id))
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert any(row.get("source_id") is None and row["valid"] for row in fact["sources"])
    assert not fact["recall_eligible"] and fact["source_projects"] == []


def test_same_context_explicit_alias_is_not_an_independent_legacy_origin(service):
    memories = CapabilityStore(service.store)
    memory_id = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT))
    _, source, original_id = bind_context(service, memory_id, "evo")
    alias_id = memories.bind_source(memory_id, replace(source, source_key=source.source_key + ":alias"))
    review_source(service, memory_id, original_id)
    fact = recall_facts(service.store, [memory_id])[memory_id]
    assert any(row.get("source_id") == alias_id and row["valid"] for row in fact["sources"])
    assert not fact["recall_eligible"] and fact["source_projects"] == []
    assert fact["assessment"]["legacy_preserved_source_ids"] == []
