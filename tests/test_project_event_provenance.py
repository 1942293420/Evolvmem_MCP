"""Project history requires a current supporting source in the same project."""
import json

import pytest

from evolvmem import auto_organization as org
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import _now_iso
from evolvmem.project_archive_pipeline import ProjectArchivePipeline
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft
from tests.test_auto_organization import base_service, service
from tests.test_primary_ownership import prepare
from tests.test_ownership_candidates import confirmed_context


PAYLOAD = {"events": [{"event_type": "feature", "title": "可追溯更新", "content": "新增校验并保留来源。"}]}


def run(service, monkeypatch, texts=None):
    worker, task, _, archive = prepare(service, monkeypatch, texts or ["evo 增加写入校验。"])
    worker._assign(service, task, materialize=False)
    result = ProjectArchivePipeline(service).process_task(task, provider=lambda _: PAYLOAD)
    return worker, task, archive, result


def test_manual_move_removes_old_current_event_and_reuses_cached_content(service, monkeypatch):
    _, task, _, result = run(service, monkeypatch)
    archives = ProjectArchiveStore(service.store)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='dsh',projects='[\"dsh\"]',"
            "revision=revision+1 WHERE task_id=?", (task["id"],))
    ProjectArchivePipeline(service).process_task(task, provider=lambda _: pytest.fail("cached extraction required"))
    assert archives.events("evo") == []
    assert len(archives.events("dsh")) == 1
    assert "可追溯更新" not in archives.get("evo")["summary"]
    historical = archives.events("evo", include_historical=True)
    assert historical[0]["event_id"] == result["event_ids"][0]
    assert historical[0]["current_support"] is False
    assert historical[0]["source_diagnostics"]


def test_withdrawal_keeps_raw_history_but_not_current_summary(service, monkeypatch):
    _, task, _, _ = run(service, monkeypatch)
    conn = service.store._connection()
    conn.execute("UPDATE organization_units SET disposition='set_aside',revision=revision+1 WHERE task_id=?", (task["id"],))
    archives = ProjectArchiveStore(service.store)
    assert archives.events("evo") == []
    assert conn.execute("SELECT count(*) FROM project_events").fetchone()[0] == 1
    conn.commit()
    assert "可追溯更新" not in archives.rebuild("evo")["summary"]
    from evolvmem.project_archive_web import project_archive
    web = project_archive(service, "evo")
    assert web["events_total"] == 1
    assert web["events"][0]["current_support"] is False


def test_shared_content_retains_each_source_and_survives_one_withdrawal(service, monkeypatch):
    worker, task, _, result = run(service, monkeypatch, ["evo 增加写入校验。", "evo 修复导出校验。"])
    assert len(set(result["event_ids"])) == 1
    archives = ProjectArchiveStore(service.store)
    event_id = result["event_ids"][0]
    assert len(archives.sources(event_id)) == 2
    units = worker._reload_units(service, task["id"])
    conn = service.store._connection()
    conn.execute("UPDATE organization_units SET disposition='set_aside',revision=revision+1 "
                 "WHERE task_id=? AND digest=?", (task["id"], units[0]["digest"]))
    assert [e["event_id"] for e in archives.events("evo")] == [event_id]
    conn.execute("UPDATE organization_units SET disposition='set_aside',revision=revision+1 WHERE task_id=?", (task["id"],))
    assert archives.events("evo") == []
    assert len(archives.sources(event_id)) == 2


def test_changed_underlying_source_invalidates_event_read_without_mutation(service, monkeypatch):
    _, _, archive, _ = run(service, monkeypatch)
    conn = service.store._connection()
    conn.execute("UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?",
                 (f"archive:{archive.id}",))
    before = conn.total_changes
    rows = ProjectArchiveStore(service.store).events("evo", include_historical=True)
    assert not rows[0]["current_support"]
    assert "source_revision_changed" in {s["reason"] for s in rows[0]["source_diagnostics"]}
    assert conn.total_changes == before


def test_legacy_event_context_mapping_preserves_verified_source_without_id_collision(service):
    item = confirmed_context(service, "新增校验并保留来源。")
    archives = ProjectArchiveStore(service.store)
    event_id = archives.record_event(ProjectEventDraft(
        project="evo", event_type="feature", title="可追溯更新", content="新增校验并保留来源。",
        source_ref="legacy:opaque"))
    service.store._connection().execute(
        "INSERT INTO knowledge_restructure_map VALUES(?,'event',?,'evo',?)", (item.id, str(event_id), _now_iso()))
    service.store._connection().commit()
    memory_id = CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category="backend", title="无关能力", content="不同ID空间", source_project="dsh"))
    assert memory_id == event_id
    assert [r["event_id"] for r in archives.events("evo")] == [event_id]
    service.store._connection().execute("UPDATE context_items SET status='superseded' WHERE id=?", (item.id,))
    assert archives.events("evo") == []
    assert len(archives.events("evo", include_historical=True)) == 1


def test_unanchored_event_is_preserved_but_not_promoted_to_current(service):
    archives = ProjectArchiveStore(service.store)
    event_id = archives.record_event(ProjectEventDraft(
        project="evo", event_type="feature", title="未知来源", content="未能核对。", source_ref="archive:99999"))
    assert archives.events("evo") == []
    historical = archives.events("evo", include_historical=True)
    assert historical[0]["event_id"] == event_id
    assert historical[0]["source_diagnostics"][0]["reason"] == "untraceable_legacy_source"


def test_malformed_event_date_never_poison_receipt_cache(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    worker._assign(service, task, materialize=False)
    pipeline = ProjectArchivePipeline(service)
    bad = {"events": [{**PAYLOAD["events"][0], "occurred_at": {"bad": True}}]}
    with pytest.raises(ValueError, match="invalid_occurred_at"):
        pipeline.process_task(task, provider=lambda _: bad)
    assert service.store._connection().execute(
        "SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 0
    assert pipeline.process_task(task, provider=lambda _: PAYLOAD)["event_ids"]


def test_source_failure_caught_by_batch_cannot_commit_an_unbound_event(service):
    from evolvmem.project_archive_store import ProjectEventSourceDraft
    conn = service.store._connection()
    conn.execute("CREATE TRIGGER reject_event_source BEFORE INSERT ON project_event_sources "
                 "BEGIN SELECT RAISE(ABORT,'source rejected'); END")
    draft = ProjectEventDraft(project="evo", event_type="feature", title="原子事件", content="来源保存失败。")
    source = ProjectEventSourceDraft(source_kind="manual", source_key="manual:test",
                                    source_ref="manual:explicit", projects=("evo",), evidence_status="confirmed")
    import sqlite3
    with service.store.transaction():
        with pytest.raises(sqlite3.IntegrityError, match="source rejected"):
            ProjectArchiveStore(service.store).record_event(draft, source=source)
    assert conn.execute("SELECT count(*) FROM project_events").fetchone()[0] == 0


def test_default_archive_resolver_rejects_alias_of_archived_project(service):
    from evolvmem.project_archive_pipeline import resolve_archive_project
    service.knowledge().save_project({"project": "evo", "aliases": ["retired-evo"]})
    service.store._connection().execute("UPDATE context_project_registry SET status='archived' WHERE project='evo'")
    with pytest.raises(ValueError, match="project_not_registered"):
        resolve_archive_project(service, "retired-evo")


def test_item_source_uses_real_editor_revision_and_is_invalidated_by_body_edit(service):
    from tests.test_knowledge_cleaning import ready
    item = service.knowledge().create({"title": "输入资料", "body": "evo 增加校验并保留来源。",
                                       "scope": "project", "action": "draft"})
    ready(service)
    task = org.enqueue(service, [f"item:{item['id']}"])["items"][0]
    worker = org.OrganizationWorker(service.config)
    row = org._task_row(service, task["id"])
    text = row["source_snapshot"]
    worker._store_units(service, row, [{"source_start": 0, "source_end": len(text),
        "text": text, "title": "输入资料", "category": "task_requirement",
        "project_hint": "", "evidence_quote": "evo 增加校验并保留来源。"}])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]' WHERE task_id=?",
            (task["id"],))
    ProjectArchivePipeline(service).process_task(task, provider=lambda _: PAYLOAD)
    archives = ProjectArchiveStore(service.store)
    assert len(archives.events("evo")) == 1
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_layers SET content='已更改的原文',content_hash='new' WHERE item_id=?",
            (item["id"],))
    assert archives.events("evo") == []


def test_native_dedup_does_not_hide_independent_legacy_context_support(service, monkeypatch):
    item = confirmed_context(service, PAYLOAD["events"][0]["content"])
    archives = ProjectArchiveStore(service.store)
    event_id = archives.record_event(ProjectEventDraft(
        project="evo", event_type="feature", title=PAYLOAD["events"][0]["title"],
        content=PAYLOAD["events"][0]["content"], source_ref=f"context_item:{item.id}"))
    _, task, _, result = run(service, monkeypatch)
    assert result["event_ids"] == [event_id]
    service.store._connection().execute(
        "UPDATE organization_units SET disposition='set_aside',revision=revision+1 WHERE task_id=?", (task["id"],))
    assert [row["event_id"] for row in archives.events("evo")] == [event_id]
