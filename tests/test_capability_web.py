"""Web-facing capability management and project archive read models."""
from types import SimpleNamespace

import pytest

from evolvmem.capability_store import CapabilityStore
from evolvmem.capability_web import (
    assign_intake, capabilities, create_category_suggestion,
    review_category_suggestion, project_archive,
)
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft


def service(test_config, store):
    return SimpleNamespace(store=store, config=test_config)


def intake(store, category="mobile", fingerprint="fp1"):
    now = _now_iso()
    with store.transaction():
        return store._connection().execute(
            "INSERT INTO capability_intake(title,content,keywords,suggested_category,source_project,"
            "source_ref,fingerprint,created_at,updated_at) VALUES('移动端离线页面','离线时保留草稿。',"
            "'[\"移动端\"]',?,'bluewhale','archive:1',?,?,?)",
            (category, fingerprint, now, now)).lastrowid


def test_category_suggestion_requires_user_acceptance(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        suggestion_id = create_category_suggestion(svc, {
            "label":"移动端", "reason":"出现独立移动开发记忆"})

        assert "移动端" not in {c["label"] for c in CapabilityStore(store).categories()}
        accepted = review_category_suggestion(svc, suggestion_id, "accept")

        assert accepted["status"] == "accepted"
        assert accepted["category_id"]
        assert any(c["id"] == accepted["category_id"] for c in CapabilityStore(store).categories())


def test_assign_intake_creates_formal_memory_with_source_and_cas(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        intake_id = intake(store, "frontend")

        result = assign_intake(svc, intake_id, {"category":"frontend", "expected_revision":1})

        memory = CapabilityStore(store).get(result["memory_id"])
        assert memory["source_project"] == "bluewhale"
        assert memory["source_ref"] == "archive:1"
        assert store._connection().execute(
            "SELECT status FROM capability_intake WHERE intake_id=?", (intake_id,)).fetchone()[0] == "assigned"
        with pytest.raises(ValueError, match="revision_conflict"):
            assign_intake(svc, intake_id, {"category":"frontend", "expected_revision":1})


def test_capabilities_read_model_separates_active_intake_and_suggestions(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        intake_id = intake(store)
        assign_intake(svc, intake_id, {"category":"frontend", "expected_revision":1})
        intake(store, fingerprint="fp2")
        create_category_suggestion(svc, {"label":"移动端", "reason":"独立移动开发记忆"})

        result = capabilities(svc, {})

        assert len(result["items"]) == 1
        assert len(result["intake"]) == 1
        assert len(result["suggestions"]) == 1
        assert len(result["categories"]) == 7


def test_project_archive_read_model_excludes_capability_memories(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        now = _now_iso()
        with store.transaction():
            store._connection().execute(
                "INSERT INTO context_project_registry(project,display_name,created_at,updated_at) "
                "VALUES('bluewhale','蓝鲸',?,?)", (now, now))
        archives = ProjectArchiveStore(store)
        archives.record_event(ProjectEventDraft(
            project="bluewhale", event_type="feature", title="新增权限管理",
            content="完成统一权限管理。", source_ref="archive:2"))
        archives.rebuild("bluewhale")

        result = project_archive(svc, "蓝鲸")

        assert "新增权限管理" in result["summary"]
        assert "capability_memories" not in result
        assert len(result["events"]) == 1


def add_capability(service, title, source):
    from evolvmem.capability_store import CapabilityMemoryDraft
    return CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category="frontend", title=title, content=title + "正文",
        keywords=("Vue",), source_project=source, source_ref="archive:test"))


def test_capabilities_filters_source_and_paginates(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        for index in range(25):
            add_capability(svc, f"记忆{index}", "bluewhale" if index % 2 else "integration")

        result = capabilities(svc, {"source_project":"bluewhale", "page":"2", "page_size":"20"})

        assert result["total"] == 12
        assert result["pages"] == 1
        assert result["page"] == 1
        assert len(result["items"]) == 12
        assert all(item["source_project"] == "bluewhale" for item in result["items"])
        assert result["counts"]["frontend"] == 12
        assert result["page_size"] == 20
