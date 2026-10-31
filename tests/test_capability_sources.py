"""Multi-source provenance must survive content deduplication without verifying claims."""
from dataclasses import replace

import pytest

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.context_store import ContextStore


def memory(**changes):
    fields = dict(category="backend", title="Atomic writes", content="Use a transaction.",
                  keywords=("SQLite", "transactions"))
    fields.update(changes)
    return CapabilityMemoryDraft(**fields)


def source(**changes):
    fields = dict(source_kind="context", source_key="context:11", source_ref="context:11",
                  context_item_id=11)
    fields.update(changes)
    return CapabilitySourceDraft(**fields)


def test_content_dedup_keeps_both_context_and_unit_sources(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        first = store.upsert(memory(), source=source(projects=("project-a",)))
        second = store.upsert(memory(), source=source(
            source_kind="unit", source_key="task:22:abc", source_ref="conversation:3#unit:abc",
            context_item_id=None, task_id=22, unit_digest="abc", unit_revision=1,
            source_revision="archive-v3", projects=("project-b", "project-a")))

        assert first == second
        assert len(store.search("Atomic")) == 1
        rows = store.sources(first)
        assert [(row["source_kind"], row["projects"]) for row in rows] == [
            ("context", ["project-a"]), ("unit", ["project-b", "project-a"])]
        assert rows[1]["source_ref"] == "conversation:3#unit:abc"
        assert rows[1]["unit_revision"] == 1
        result = store.get(first)
        assert result["source_projects"] == ["project-a", "project-b"]
        assert result["sources_count"] == 2
        assert result["keywords"] == ["SQLite", "transactions"]
        assert result["source_project"] == ""
        assert result["status"] == "active"
        assert all(row["evidence_status"] == "unverified" for row in rows)


def test_replay_is_idempotent_but_changed_source_has_its_own_revision(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        origin = source()
        memory_id = store.upsert(memory(), source=origin)
        old = store.sources(memory_id)[0]
        assert old["revision"] == 1
        assert store.bind_source(memory_id, origin) == old["source_id"]
        assert store.upsert(memory(), source=origin) == memory_id
        assert store.sources(memory_id)[0] == old

        changed = replace(origin, source_revision="v2", projects=("project-a",),
                          evidence_status="confirmed")
        assert store.bind_source(memory_id, changed) == old["source_id"]
        new = store.sources(memory_id)[0]
        assert new["revision"] == 2
        assert new["created_at"] == old["created_at"]
        assert new["source_revision"] == "v2"
        assert new["projects"] == ["project-a"]
        assert store.get(memory_id)["source_project"] == "project-a"
        store.bind_source(memory_id, changed)
        assert store.sources(memory_id)[0] == new


def test_confirmed_unique_project_repairs_empty_legacy_label_only(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(source_ref="legacy:7"))
        store.upsert(memory(), source=source(projects=("project-a",), evidence_status="confirmed"))
        row = store.get(memory_id)
        assert row["source_project"] == "project-a"
        assert row["source_ref"] == "legacy:7"
        assert row["revision"] == 2
        store.bind_source(memory_id, source(source_key="context:12", context_item_id=12,
                                           projects=("project-b",), evidence_status="confirmed"))
        assert store.get(memory_id)["source_project"] == "project-a"
        assert store.get(memory_id)["revision"] == 2
        assert store.get(memory_id)["source_projects"] == ["project-a", "project-b"]


@pytest.mark.parametrize("projects,status,scope", [
    (("project-a",), "unverified", "project"),
    (("project-a", "project-b"), "confirmed", "project"),
    ((), "confirmed", "project"),
    ((), "unverified", "global"),
])
def test_nonunique_or_unconfirmed_scope_never_fills_project(test_config, projects, status, scope):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(), source=source(
            projects=projects, evidence_status=status, knowledge_scope=scope))
        assert store.get(memory_id)["source_project"] == ""
        assert store.sources(memory_id)[0]["knowledge_scope"] == scope


def test_existing_nonempty_project_never_overwritten_by_source(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(source_project="original"), source=source(
            projects=("new",), evidence_status="confirmed"))
        assert store.get(memory_id)["source_project"] == "original"
        assert store.get(memory_id)["revision"] == 1


def test_legacy_call_keeps_compatibility_without_fabricating_source(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(source_project="legacy-project", source_ref="legacy:9"))
        assert store.upsert(memory(source_project="different")) == memory_id
        assert store.sources(memory_id) == []
        row = store.get(memory_id)
        assert row["source_project"] == "legacy-project"
        assert row["source_projects"] == []
        assert row["sources_count"] == 0
        assert row["revision"] == 1
        assert row["keywords"] == ["SQLite", "transactions"]
        assert store.get(999999) is None
        assert store.sources(999999) == []


@pytest.mark.parametrize("changes", [
    {"source_kind": "archive"}, {"source_kind": None},
    {"source_key": ""}, {"source_key": "  "}, {"source_key": 12}, {"source_ref": None},
    {"source_revision": 2}, {"projects": ["project-a"]},
    {"projects": "project-a"}, {"projects": ("project-a", 7)},
    {"projects": ("",)}, {"projects": ("   ",)},
    {"context_item_id": None}, {"context_item_id": True},
    {"context_item_id": 0}, {"context_item_id": "11"},
    {"task_id": False}, {"task_id": -1}, {"unit_digest": None},
    {"unit_revision": True}, {"unit_revision": 0},
    {"knowledge_scope": None}, {"knowledge_scope": ""}, {"knowledge_scope": "unknown"},
    {"evidence_status": None}, {"evidence_status": ""}, {"evidence_status": "verified"},
    {"source_kind": "unit", "context_item_id": None},
    {"source_kind": "unit", "task_id": 22, "unit_digest": ""},
])
def test_invalid_source_rejected_without_inserting_memory(test_config, changes):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        with pytest.raises((TypeError, ValueError)):
            store.upsert(memory(), source=source(**changes))
        assert context._connection().execute("SELECT COUNT(*) FROM capability_memories").fetchone()[0] == 0
        assert store.search("Atomic") == []


def test_invalid_source_type_and_missing_memory_are_rejected(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        with pytest.raises(TypeError):
            store.upsert(memory(), source={"source_key": "bad"})
        with pytest.raises(ValueError, match="capability_memory_not_found"):
            store.bind_source(999999, source())


@pytest.mark.parametrize("kind", ["manual", "reference"])
def test_manual_and_reference_bindings_do_not_invent_native_evidence(test_config, kind):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(), source=source(
            source_kind=kind, source_key="link:1", source_ref="docs/design.md", context_item_id=None))
        row = store.sources(memory_id)[0]
        assert row["context_item_id"] is None
        assert row["task_id"] is None
        assert row["unit_revision"] is None
        assert row["unit_digest"] == ""
        assert row["evidence_status"] == "unverified"


def test_source_and_memory_writes_join_outer_transaction(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        with pytest.raises(RuntimeError, match="abort"):
            with context.transaction():
                memory_id = store.upsert(memory(), source=source())
                assert store.sources(memory_id)
                raise RuntimeError("abort")
        assert store.get(memory_id) is None
        assert store.sources(memory_id) == []
        assert store.search("Atomic") == []


def test_failed_source_sql_rolls_back_memory_and_fts(test_config):
    import sqlite3
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        context._connection().execute("""CREATE TRIGGER reject_sources BEFORE INSERT
            ON capability_sources BEGIN SELECT RAISE(ABORT, 'reject source'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="reject source"):
            store.upsert(memory(), source=source())
        assert store.search("Atomic") == []
        assert context._connection().execute("SELECT COUNT(*) FROM capability_memories").fetchone()[0] == 0


def test_caught_source_failure_rolls_back_only_failed_upsert_inside_batch(test_config):
    import sqlite3
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        context._connection().execute("""CREATE TRIGGER reject_sources BEFORE INSERT
            ON capability_sources BEGIN SELECT RAISE(ABORT, 'reject source'); END""")
        with context.transaction():
            keeper = store.upsert(memory(title="Keep independent work"))
            with pytest.raises(sqlite3.IntegrityError, match="reject source"):
                store.upsert(memory(), source=source())
        assert store.get(keeper) is not None
        assert store.search("Atomic") == []
        assert context._connection().execute("SELECT COUNT(*) FROM capability_memories").fetchone()[0] == 1


def test_caught_label_repair_failure_does_not_commit_partial_source(test_config):
    import sqlite3
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory())
        context._connection().execute("""CREATE TRIGGER reject_label BEFORE UPDATE OF source_project
            ON capability_memories BEGIN SELECT RAISE(ABORT, 'reject label'); END""")
        with context.transaction():
            with pytest.raises(sqlite3.IntegrityError, match="reject label"):
                store.bind_source(memory_id, source(projects=("project-a",), evidence_status="confirmed"))
        assert store.sources(memory_id) == []
        assert store.get(memory_id)["source_project"] == ""
        assert store.get(memory_id)["revision"] == 1


def test_old_database_gains_source_table_without_rewriting_legacy_memories(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        memory_id = store.upsert(memory(source_project="legacy-project", source_ref="legacy:9"))
        before = store.get(memory_id)
        context._connection().execute("DROP TABLE capability_sources")
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        assert store.get(memory_id) == before
        store.bind_source(memory_id, source())
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        assert store.get(memory_id)["sources_count"] == 1
        assert store.get(memory_id)["source_project"] == "legacy-project"



def test_source_can_support_distinct_memories_without_reactivating_ignored_one(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        first = store.upsert(memory())
        assert store.set_status(first, "ignored", expected_revision=1)
        origin = source(projects=("project-a",), evidence_status="confirmed")
        assert store.upsert(memory(), source=origin) == first
        second = store.upsert(memory(title="Second insight"), source=origin)
        assert first != second
        assert store.get(first)["status"] == "ignored"
        assert store.search("Atomic") == []
        assert store.sources(first)[0]["source_id"] != store.sources(second)[0]["source_id"]


def test_global_source_cannot_claim_project_ownership(test_config):
    with ContextStore(test_config) as context:
        store = CapabilityStore(context)
        with pytest.raises(ValueError, match="global_project_conflict"):
            store.upsert(memory(), source=source(projects=("wrong-project",),
                knowledge_scope="global", evidence_status="confirmed"))
        assert store.search("Atomic") == []
