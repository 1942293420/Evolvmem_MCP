"""Browse visibility and category assignment remain independent from recall approval."""
from types import SimpleNamespace
import pytest

from evolvmem import capability_web
from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore
from tests.test_capability_assessment import TEXT, candidate
from tests.test_auto_organization import base_service
from tests.test_capability_pipeline import service_with_task
from tests.test_capability_provenance import provenance_db, add_memory, unit_source
from tests.test_capability_web import intake, service, add_capability


def test_legacy_category_assignment_stays_pending_and_visible(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ident = intake(store)
        result = capability_web.assign_intake(svc, ident, {"category": "frontend", "expected_revision": 1})
        memory = CapabilityStore(store).get(result["memory_id"])
        assert memory["status"] == result["memory_status"] == "pending"
        assert "待核对" in result["notice"]
        assert capability_web.capabilities(svc, {})["items"] == []
        page = capability_web.capabilities(svc, {"status": "pending"})
        assert [row["memory_id"] for row in page["items"]] == [memory["memory_id"]]
        assert page["pending_total"] == 1
        assert page["intake_total"] == 0


def test_classification_of_weak_duplicate_does_not_demote_existing_active(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ident = intake(store)
        old = CapabilityStore(store).upsert(CapabilityMemoryDraft(category="frontend", title="移动端离线页面", content="离线时保留草稿。"))
        result = capability_web.assign_intake(svc, ident, {"category": "frontend", "expected_revision": 1})
        assert result["memory_id"] == old
        assert result["memory_status"] == "active"
        assert CapabilityStore(store).get(old)["status"] == "active"


@pytest.mark.parametrize("strong", [True, False])
def test_v3_intake_uses_current_source_and_value_assessment(test_config, strong):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store, TEXT)
        payload = candidate(category="mobile", evidence_quotes=[TEXT] if strong else [])
        CapabilityPipeline(svc).process_task(task, provider=lambda _: payload)
        ident = store._connection().execute("SELECT intake_id FROM capability_intake").fetchone()[0]
        result = capability_web.assign_intake(svc, ident, {"category": "database", "expected_revision": 1})
        assert result["memory_status"] == ("active" if strong else "pending")
        sources = CapabilityStore(store).sources(result["memory_id"])
        assert len(sources) == 1 and sources[0]["task_id"] == task["id"]
        assessment = store._connection().execute("SELECT value,verification_status FROM capability_assessments").fetchone()
        assert assessment["value"] == ("reusable" if strong else "review")
        assert assessment["verification_status"] == "unverified"


def test_changed_unit_cannot_reuse_v3_intake_as_approved_value(test_config):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store, TEXT)
        CapabilityPipeline(svc).process_task(task, provider=lambda _: candidate(category="mobile"))
        ident = store._connection().execute("SELECT intake_id FROM capability_intake").fetchone()[0]
        with store.transaction():
            store._connection().execute("UPDATE organization_units SET text='changed',revision=revision+1 WHERE task_id=?", (task["id"],))
        result = capability_web.assign_intake(svc, ident, {"category": "database", "expected_revision": 1})
        assert result["memory_status"] == "pending"
        assert CapabilityStore(store).sources(result["memory_id"]) == []


def test_status_paging_counts_pending_and_all_without_hiding_invalid_active(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ids = [add_capability(svc, str(i), "old-label") for i in range(5)]
        CapabilityStore(store).set_status(ids[1], "pending")
        CapabilityStore(store).set_status(ids[2], "pending")
        CapabilityStore(store).set_status(ids[3], "ignored")
        CapabilityStore(store).set_status(ids[4], "archived")
        active = capability_web.capabilities(svc, {})
        assert active["total"] == 1 and active["items"][0]["memory_id"] == ids[0]
        assert not active["items"][0]["recall_facts"]["recall_eligible"]
        assert active["items"][0]["source_projects"] == []
        assert active["items"][0]["governance"]["source_state"] == "untraceable"
        pending = capability_web.capabilities(svc, {"status": "pending", "page_size": 1, "page": 2})
        assert (pending["total"], pending["pages"], pending["page"], pending["pending_total"]) == (2, 2, 2, 2)
        assert capability_web.capabilities(svc, {"status": "all"})["total"] == 5
        with pytest.raises(ValueError, match="invalid_capability_status_filter"):
            capability_web.capabilities(svc, {"status": "whatever"})


def test_project_filter_uses_current_binding_even_if_legacy_label_is_empty(provenance_db, test_config):
    store = provenance_db
    svc = SimpleNamespace(store=store, config=test_config)
    ident = add_memory(store, source=unit_source(store))
    initial = capability_web.capabilities(svc, {"source_project": "alpha"})
    assert initial["total"] == 1
    assert initial["items"][0]["source_projects"] == ["alpha", "beta"]
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET project='beta',projects='[\"beta\"]',decision='manual',revision=revision+1")
    assert capability_web.capabilities(svc, {"source_project": "alpha"})["total"] == 0
    changed = capability_web.capabilities(svc, {"source_project": "beta"})
    assert changed["total"] == 1
    assert changed["items"][0]["memory_id"] == ident
    assert changed["items"][0]["source_projects"] == ["beta"]


def test_pending_value_can_display_confirmed_source_project(provenance_db, test_config):
    store = provenance_db
    svc = SimpleNamespace(store=store, config=test_config)
    ident = add_memory(store, source=unit_source(store))
    CapabilityStore(store).set_status(ident, "pending")
    page = capability_web.capabilities(svc, {"status": "pending", "source_project": "alpha"})
    assert page["total"] == 1
    row = page["items"][0]
    assert row["source_projects"] == ["alpha", "beta"]
    assert row["governance"]["ownership_state"] == "confirmed"
    assert not row["recall_facts"]["recall_eligible"]


def test_invalid_source_remains_browsable_with_reason_and_no_current_project(provenance_db, test_config):
    store = provenance_db
    svc = SimpleNamespace(store=store, config=test_config)
    ident = add_memory(store, source=unit_source(store))
    with store.transaction():
        store._connection().execute("UPDATE organization_tasks SET status='superseded'")
    row = capability_web.capabilities(svc, {})["items"][0]
    assert row["memory_id"] == ident
    assert row["governance"]["source_state"] == "invalid"
    assert row["governance"]["reasons"]
    assert row["source_projects"] == []


def test_listing_annotates_only_page_and_does_not_decrypt_archives(test_config, monkeypatch):
    from evolvmem.capability_recall_eligibility import recall_facts
    from evolvmem.session_archive import SessionArchiver
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ids = [add_capability(svc, str(i), "legacy") for i in range(15)]
        batches = []
        def tracked(store, selected=None):
            batches.append(list(selected))
            return recall_facts(store, selected)
        monkeypatch.setattr(capability_web, "recall_facts", tracked, raising=False)
        monkeypatch.setattr(SessionArchiver, "read_payload", lambda *a, **k: pytest.fail("web listing must not decrypt archives"))
        page = capability_web.capabilities(svc, {"page_size": 3, "page": 2})
        selected = [row["memory_id"] for row in page["items"]]
        assert selected == ids[-6:-3][::-1]
        assert batches == [selected]
        assert "details" not in page["items"][0]["governance"]


def test_technical_assessment_failure_explains_processing_error_without_discrediting_content(base_service):
    from evolvmem import capability_assessment_migration as migration
    from tests.test_capability_assessment_migration import manual_source, TEXT
    memories = CapabilityStore(base_service.store)
    identity = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT),
                               source=manual_source(base_service))
    prepared = migration.build_plan(base_service.store._connection(),
        rule_revision=base_service.knowledge().rules.read()["revision"])
    result = migration.apply_plan(base_service, prepared, provider=lambda _: {"kind": "invalid"})
    assert result["applied"] == 1
    memories.set_status(identity, "pending")
    item = capability_web.capabilities(base_service, {"status": "pending"})["items"][0]
    assert item["governance"]["value"] == "review"
    assert item["governance"]["verification_status"] == "unverified"
    assert not item["recall_facts"]["recall_eligible"]
    assert "本次 AI 评估未返回有效结果，内容已保留，等待重新评估。" in item["governance"]["reasons"]
    assert "现有引用尚不能支持全部结论。" not in item["governance"]["reasons"]
