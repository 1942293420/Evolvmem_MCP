"""Legacy origins survive new independent bindings without an assessment bypass."""
from evolvmem import capability_assessment as assessment
from evolvmem.capability_provenance import annotate
from evolvmem.capability_recall_eligibility import recall_facts
from evolvmem.capability_store import CapabilityStore
from tests.test_capability_provenance import (
    provenance_db, add_memory, unit_source, context_source, DIGEST,
)
from tests.test_project_modules import modules, create, bind


def weak_binding(store, memory_id, source, text="user: a fact"):
    saved = CapabilityStore(store)
    source_id = saved.bind_source(memory_id, source)
    memory = saved.get(memory_id)
    payload = {"category": memory["category"], "title": memory["title"],
               "content": memory["content"], "kind": "fact", "trigger": "",
               "conditions": [], "action_steps": [], "result": "",
               "evidence_quotes": [], "value_reason": "Review required."}
    assessment.persist(store, memory_id, source_id,
                       assessment.evaluate(payload, source_text=text), existing_status="active")
    return source_id


def test_independent_legacy_context_survives_weak_new_unit_without_project_leak(provenance_db):
    store = provenance_db
    identity = add_memory(store)
    with store.transaction():
        conn = store._connection()
        conn.execute("INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')", (str(identity),))
        conn.execute("INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,review_state,resolved_project,created_at,updated_at) VALUES(1,'resolved','human','accepted','alpha','old','old')")
        conn.execute("UPDATE organization_units SET project='beta',projects='[\"beta\"]'")
    assert recall_facts(store, [identity])[identity]["recall_eligible"]
    source_id = weak_binding(store, identity, unit_source(store, projects=("beta",)))
    fact = recall_facts(store, [identity])[identity]
    assert fact["recall_eligible"]
    assert fact["source_projects"] == ["alpha"]
    assert fact["eligible_source_ids"] == [None]
    assert fact["assessment"]["assessed_source_ids"] == [source_id]
    assert CapabilityStore(store).get(identity)["status"] == "active"


def test_same_unit_legacy_ref_cannot_bypass_weak_explicit_assessment(provenance_db):
    store = provenance_db
    identity = add_memory(store, ref=f"archive:1#{DIGEST}@0-12")
    weak_binding(store, identity, unit_source(store))
    fact = recall_facts(store, [identity])[identity]
    assert not fact["recall_eligible"]
    assert not fact["legacy_fallback"]
    assert len(fact["sources"]) == 1
    assert fact["eligible_source_ids"] == []


def test_same_origin_in_new_task_does_not_resurrect_legacy_none_bypass(provenance_db):
    store = provenance_db
    identity = add_memory(store, ref=f"archive:1#{DIGEST}@0-12")
    weak_binding(store, identity, unit_source(store))
    with store.transaction():
        conn = store._connection()
        conn.execute("UPDATE organization_tasks SET status='superseded'")
        conn.execute("INSERT INTO organization_tasks(id,source_key,source_revision,rule_revision,status,created_at,updated_at) SELECT 2,source_key,source_revision,'rule2','completed',created_at,updated_at FROM organization_tasks WHERE id=1")
        conn.execute("INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,project,projects,decision,created_at,updated_at) SELECT 2,ordinal,digest,title,text,source_start,source_end,project,projects,decision,created_at,updated_at FROM organization_units WHERE task_id=1")
    fact = recall_facts(store, [identity])[identity]
    assert not fact["recall_eligible"]
    assert len(fact["sources"]) == 1
    assert fact["sources"][0]["reason"] == "task_superseded"


def test_invalid_legacy_cannot_rescue_weak_new_source(provenance_db):
    store = provenance_db
    identity = add_memory(store, ref=f"archive:1#{DIGEST}@0-12")
    weak_binding(store, identity, context_source(store))
    with store.transaction():
        store._connection().execute("UPDATE organization_tasks SET status='superseded'")
    fact = recall_facts(store, [identity])[identity]
    assert not fact["recall_eligible"]
    assert fact["source_projects"] == []


def test_untraceable_placeholder_is_not_added_to_bound_origins(provenance_db):
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    fact = annotate(store, [identity])[identity]
    assert not fact["legacy_fallback"]
    assert len(fact["sources"]) == 1


def test_preserved_legacy_unit_still_supplies_its_module_only(modules):
    store = modules.store
    module = create(modules)
    bind(modules, module)
    identity = add_memory(store, ref=f"archive:1#{DIGEST}@0-12")
    weak_binding(store, identity, context_source(store))
    fact = recall_facts(store, [identity])[identity]
    assert fact["recall_eligible"]
    assert fact["eligible_source_ids"] == [None]
    matches = modules.modules_for_memory(identity, fact["eligible_source_ids"])
    assert [row["module_id"] for row in matches] == [module["module_id"]]


def test_context_mapping_cannot_alias_a_weak_explicit_context_source(provenance_db):
    store = provenance_db
    identity = add_memory(store)
    with store.transaction():
        store._connection().execute("INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')", (str(identity),))
    weak_binding(store, identity, context_source(store))
    fact = recall_facts(store, [identity])[identity]
    assert not fact["recall_eligible"]
    assert not fact["legacy_fallback"]
    assert len(fact["sources"]) == 1


def test_repair_only_backfills_missing_origin_in_mixed_memory(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    store = provenance_db
    identity = add_memory(store)
    with store.transaction():
        store._connection().execute("INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')", (str(identity),))
    existing_id = weak_binding(store, identity, unit_source(store))
    before = CapabilityStore(store).sources(identity)[0]
    plan = build_plan(store._connection())
    assert len(plan["changes"]) == 1
    assert plan["changes"][0]["binding"]["source_kind"] == "context"
    assert apply_plan(store._connection(), plan)["applied"] == 1
    assert next(s for s in CapabilityStore(store).sources(identity) if s["source_id"] == existing_id) == before
    assert recall_facts(store, [identity])[identity]["recall_eligible"]
    assert build_plan(store._connection())["changes"] == []
