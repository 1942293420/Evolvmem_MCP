from types import SimpleNamespace

from evolvmem.capability_retriever import CapabilityRetriever
from evolvmem.capability_store import CapabilityStore
from tests.test_capability_provenance import provenance_db, add_memory, unit_source


def test_untraceable_legacy_knowledge_is_browsable_but_not_recalled(provenance_db):
    identity = add_memory(provenance_db)
    service = SimpleNamespace(store=provenance_db)
    assert CapabilityStore(provenance_db).get(identity) is not None
    assert CapabilityRetriever(service).search("Fact") == []


def test_manual_project_correction_changes_rendered_projects_without_reextract(provenance_db):
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    service = SimpleNamespace(store=store)
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET revision=revision+1,project='beta',projects='[\"beta\"]'")
    hits = CapabilityRetriever(service).search("Fact")
    assert [h["memory_id"] for h in hits] == [identity]
    assert hits[0]["source_projects"] == ["beta"]
    assert hits[0]["source_project"] == "beta"
    block, ids = CapabilityRetriever(service).render("Fact", max_chars=1000)
    assert "capability:" + str(identity) in block
    assert "来源项目：beta" in block


def test_withdrawal_stops_recall_without_deleting_content(provenance_db):
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    service = SimpleNamespace(store=store)
    assert CapabilityRetriever(service).search("Fact")
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET disposition='set_aside',revision=revision+1")
    assert CapabilityRetriever(service).search("Fact") == []
    assert CapabilityStore(store).get(identity)["content"] == "A fact"
