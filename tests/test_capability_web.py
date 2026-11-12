"""Web-facing capability management and project archive read models."""
from tests.test_project_archive_store import save_manual_event
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

        result = capabilities(svc, {"status": "pending"})

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
        save_manual_event(archives, ProjectEventDraft(
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

        result = capabilities(svc, {"source_project":"bluewhale", "source_scope":"historical", "page":"2", "page_size":"20"})

        assert result["total"] == 12
        assert result["pages"] == 1
        assert result["page"] == 1
        assert len(result["items"]) == 12
        assert all(item["source_project"] == "bluewhale" for item in result["items"])
        assert result["counts"]["frontend"] == 12
        assert result["page_size"] == 20


def test_capability_sources_include_later_pages_and_ignore_current_filters(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        add_capability(svc, "Old source", "z-later-page")
        for index in range(21):
            add_capability(svc, f"Recent {index}", "alpha")
        archived_id = add_capability(svc, "Archived source", "archived-only")
        CapabilityStore(store).set_status(archived_id, "archived")
        add_capability(svc, "No source", "")

        result = capabilities(svc, {"page_size": "20"})

        assert len(result["items"]) == 20
        assert all(item["source_project"] != "z-later-page" for item in result["items"])
        assert result["source_projects"] == ["alpha", "z-later-page"]

        empty = capabilities(svc, {"q": "no-such-record", "source_project": "alpha"})
        assert empty["items"] == []
        assert empty["source_projects"] == ["alpha", "z-later-page"]
        assert (empty["total"], empty["page"], empty["pages"]) == (0, 1, 1)


def test_capability_queue_totals_include_pending_rows_beyond_display_limit(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        for index in range(205):
            intake(store, fingerprint=f"pending-{index}")
        assigned_id = intake(store, fingerprint="assigned")
        with store.transaction():
            store._connection().execute(
                "UPDATE capability_intake SET status='assigned' WHERE intake_id=?",
                (assigned_id,),
            )
        for index in range(2):
            create_category_suggestion(svc, {"label": f"Pending {index}", "reason": "Reason"})
        rejected_id = create_category_suggestion(svc, {"label": "Rejected", "reason": "Reason"})
        review_category_suggestion(svc, rejected_id, "reject")

        result = capabilities(svc, {"q": "no-such-record"})

        assert len(result["intake"]) == 200
        assert result["intake_total"] == 205
        assert len(result["suggestions"]) == result["suggestions_total"] == 2


def test_capability_paging_materializes_only_requested_memory_rows(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ids = [add_capability(svc, f"Record {index}", "project") for index in range(9)]
        connection = store._connection()
        original_factory = connection.row_factory
        materialized = []

        def counted_row(cursor, values):
            row = original_factory(cursor, values)
            if {"memory_id", "content", "keywords", "status"} <= set(row.keys()):
                materialized.append(row["memory_id"])
            return row

        connection.row_factory = counted_row
        try:
            result = capabilities(svc, {"page": "3", "page_size": "2"})
        finally:
            connection.row_factory = original_factory

        assert [item["memory_id"] for item in result["items"]] == [ids[4], ids[3]]
        assert (result["total"], result["pages"], result["page"]) == (9, 5, 3)
        assert materialized == [ids[4], ids[3]]


@pytest.mark.parametrize(
    ("params", "expected_page", "expected_size", "expected_indexes"),
    [
        ({"page": "999", "page_size": "2"}, 3, 2, [0]),
        ({"page": "0", "page_size": "2"}, 1, 2, [4, 3]),
        ({"page": "1", "page_size": "0"}, 1, 1, [4]),
        ({"page": "1", "page_size": "999"}, 1, 50, [4, 3, 2, 1, 0]),
    ],
)
def test_capability_sql_paging_preserves_clamping(
    test_config, params, expected_page, expected_size, expected_indexes,
):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        ids = [add_capability(svc, f"Record {index}", "project") for index in range(5)]

        result = capabilities(svc, params)

        assert result["total"] == 5
        assert result["page"] == expected_page
        assert result["page_size"] == expected_size
        assert [item["memory_id"] for item in result["items"]] == [ids[i] for i in expected_indexes]


def test_capability_paging_preserves_category_counts_and_status_filters(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        frontend_id = add_capability(svc, "Match frontend", "project")
        database_id = add_capability(svc, "Match database", "project")
        with store.transaction():
            store._connection().execute(
                "UPDATE capability_memories SET category='database' WHERE memory_id=?",
                (database_id,),
            )
        archived_id = add_capability(svc, "Match archived", "project")
        CapabilityStore(store).set_status(archived_id, "archived")
        add_capability(svc, "Unrelated title", "project")
        add_capability(svc, "Match other project", "elsewhere")

        result = capabilities(svc, {
            "q": "Match ", "category": "frontend", "source_project": "project", "source_scope": "historical",
            "page": "999", "page_size": "1",
        })

        assert result["total"] == 1
        assert result["counts"] == {"frontend": 1, "database": 1}
        assert result["page"] == 1
        assert [item["memory_id"] for item in result["items"]] == [frontend_id]
