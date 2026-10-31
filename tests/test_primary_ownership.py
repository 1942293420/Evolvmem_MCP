"""Primary extraction must consume source-bound, current unit ownership.

These tests use real temporary stores and source snapshots. Only the external
model and vector engine are replaced; both new extraction pipelines run.
"""
import json

import pytest

from evolvmem import auto_organization as org, conversation_tasks
from evolvmem.context_models import ContextMode
from evolvmem.session_archive import SessionArchiver
from evolvmem.topic_segmentation import message_spans
from tests.test_auto_organization import base_service, service
from tests.test_knowledge_cleaning import ready


class Provider:
    def __init__(self, service, *, after_assignment=None):
        self.service = service
        self.after_assignment = after_assignment
        self.ownership_batches = []
        self.extractions = []

    def __call__(self, prompt, *args, **kwargs):
        if "业务资料归类核对员" in prompt:
            rows = json.loads(prompt.split("待判断资料：", 1)[1])
            self.ownership_batches.append(rows)
            items = []
            for row in rows:
                text = row["text"]
                projects = [p for p in ("evo", "dsh") if p in text]
                global_scope = "个人通用" in text
                items.append({
                    "id": row["id"], "decision": "include" if projects or global_scope else "uncertain",
                    "reason": "按本条原文的明确业务对象判断。",
                    "quote": text, "chain": {"need": text},
                    "knowledge_scope": "global" if global_scope else "project",
                    "general_quote": text if global_scope else "",
                    "targets": [{"project": p, "role": "owner", "quote": text} for p in projects],
                })
            if self.after_assignment:
                self.after_assignment()
            return json.dumps({"items": items}, ensure_ascii=False)
        if "开发知识整理器" in prompt:
            text = prompt.split("原始开发记录：\n", 1)[1]
            self.extractions.append(("capability", text))
            return json.dumps({"action": "keep", "category": "backend",
                               "title": text[:60], "content": text, "keywords": ["校验"],
                               "kind": "fact", "trigger": "", "conditions": [], "action_steps": [],
                               "result": "", "evidence_quotes": [text], "value_reason": "原始要求"}, ensure_ascii=False)
        if "项目历史整理器" in prompt:
            text = prompt.split("原始项目记录：\n", 1)[1]
            self.extractions.append(("project", text))
            return json.dumps({"events": [{"event_type": "feature", "title": text[:60],
                                           "content": text}]}, ensure_ascii=False)
        raise AssertionError("unexpected model contract: " + prompt[:100])


def prepare(service, monkeypatch, texts, *, mode="primary"):
    service.config.knowledge_structure_mode = mode
    # The real index loads an external embedding model; ownership tests only
    # verify SQLite writes and leave index consistency to its own test suite.
    monkeypatch.setattr(service, "capability_index", None)
    messages = [{"role": "user", "content": text} for text in texts]
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "primary-ownership", json.dumps({"messages": messages}, ensure_ascii=False))
    ready(service)
    task = conversation_tasks.enqueue(service, [f"archive:{archive.id}"])["items"][0]
    worker = org.OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    row = org._task_row(service, task["id"])
    spans = message_spans(org.source_messages(service, task["source_key"]))
    worker._store_units(service, row, [{
        "source_start": span["start"], "source_end": span["end"],
        "text": row["source_snapshot"][span["start"]:span["end"]],
        "title": texts[index], "category": "task_requirement",
        "project_hint": "", "evidence_quote": texts[index],
    } for index, span in enumerate(spans)])
    worker._touch(service, task["id"], "assignment", "running")
    provider = Provider(service)
    monkeypatch.setattr(org, "_load_llm", lambda: ({}, provider))
    return worker, org.task_view(service, task["id"]), provider, archive


def decisions(service, task):
    return [(u["decision"], u["project"]) for u in org.unit_views(service, task["id"])]


def event_projects(service):
    return [row[0] for row in service.store._connection().execute(
        "SELECT project FROM project_events ORDER BY event_id")]


def forbid_legacy(monkeypatch, worker):
    def forbidden(*args, **kwargs):
        raise AssertionError("primary must not materialize legacy knowledge")
    monkeypatch.setattr(worker, "_ingest", forbidden)
    monkeypatch.setattr(org, "_refresh_task_state", forbidden)
    monkeypatch.setattr("evolvmem.knowledge_facts.sync_ownership_relations", forbidden)


def test_primary_assigns_conversation_units_before_both_pipelines(service, monkeypatch):
    worker, task, provider, archive = prepare(service, monkeypatch, [
        "evo 增加写入校验。", "dsh 增加重试上限。"])
    forbid_legacy(monkeypatch, worker)

    worker._process(service, task)

    assert decisions(service, task) == [("auto", "evo"), ("auto", "dsh")]
    assert event_projects(service) == ["evo", "dsh"]
    assert {row[0] for row in service.store._connection().execute(
        "SELECT source_project FROM capability_memories")} == {"evo", "dsh"}
    current = org.task_view(service, task["id"])
    assert (current["status"], current["stage"], current["review_count"]) == ("completed", "done", 0)
    assert len(provider.ownership_batches) == 1
    assert service.store._connection().execute(
        "SELECT project FROM session_archives WHERE id=?", (archive.id,)).fetchone()[0] == ""
    assert service.store._connection().execute(
        "SELECT count(*) FROM organization_units WHERE item_id IS NOT NULL").fetchone()[0] == 0


def test_primary_keeps_unresolved_units_in_review_and_processes_confirmed_units(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, [
        "evo 增加写入校验。", "这个功能需要调整，尚未说明在哪个项目。"])

    worker._process(service, task)

    assert decisions(service, task) == [("auto", "evo"), ("review", "")]
    assert event_projects(service) == ["evo"]
    current = org.task_view(service, task["id"])
    assert (current["status"], current["stage"], current["review_count"],
            current["error_code"]) == ("review", "done", 1, "units_need_review")
    assert len(provider.ownership_batches) == 1


@pytest.mark.parametrize("project, expected_status", [("dsh", "completed"), ("", "review")])
def test_primary_preserves_manual_inclusion_and_manual_unassignment(
        service, monkeypatch, project, expected_status):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project=?,projects=?,revision=9 WHERE task_id=?",
            (project, json.dumps([project] if project else []), task["id"]))

    worker._process(service, task)

    unit = dict(service.store._connection().execute(
        "SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    assert (unit["decision"], unit["project"], unit["revision"]) == ("manual", project, 9)
    assert provider.ownership_batches == []
    assert event_projects(service) == ([project] if project else [])
    assert org.task_view(service, task["id"])["status"] == expected_status


def test_primary_explicit_global_is_complete_without_project_event(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["这是个人通用的习惯：修改后检查结果。"])

    worker._process(service, task)

    unit = org.unit_views(service, task["id"])[0]
    assert (unit["decision"], unit["project"], unit["knowledge_scope"]) == ("auto", "", "global")
    assert event_projects(service) == []
    assert org.task_view(service, task["id"])["status"] == "completed"


def test_primary_source_change_during_ownership_model_blocks_all_derived_writes(service, monkeypatch):
    worker, task, provider, archive = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    def change_source():
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?",
                (f"archive:{archive.id}",))
    provider.after_assignment = change_source

    worker._process(service, task)

    current = org.task_view(service, task["id"])
    assert current["source_current"] is False
    assert (current["status"], current["error_code"]) == ("review", "revision_conflict")
    assert decisions(service, task) == [("review", "")]
    assert event_projects(service) == []
    assert service.store._connection().execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 0
    assert provider.extractions == []


def test_ownership_only_returns_current_decisions_without_finishing_or_materializing(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 增加写入校验。", "尚不确定适用项目。"])
    forbid_legacy(monkeypatch, worker)

    units = worker._assign(service, task, materialize=False)

    assert [(u["decision"], u["project"]) for u in units] == [("auto", "evo"), ("review", "")]
    assert org.task_view(service, task["id"])["stage"] == "assignment"


def test_dual_assigns_once_before_new_and_legacy_materialization(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"], mode="dual")
    # Real legacy ingestion writes the scoped source; suppress only its external
    # extraction step, which is independent of new pipeline ordering.
    ingest = worker._ingest
    def history_only(service, current):
        assert event_projects(service) == ["evo"]
        ingest(service, current, extract=False)
    monkeypatch.setattr(worker, "_ingest", history_only)

    worker._process(service, task)

    assert decisions(service, task) == [("auto", "evo")]
    assert len(provider.ownership_batches) == 1
    assert event_projects(service) == ["evo"]
    assert service.store._connection().execute(
        "SELECT count(*) FROM organization_units WHERE item_id IS NOT NULL").fetchone()[0] == 1
    assert org.task_view(service, task["id"])["stage"] == "extraction"


def test_assignment_keeps_legacy_materialization_by_default(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"], mode="legacy")
    ingest = worker._ingest
    monkeypatch.setattr(worker, "_ingest", lambda service, current: ingest(service, current, extract=False))

    worker._assign(service, task)

    assert decisions(service, task) == [("auto", "evo")]
    assert service.store._connection().execute(
        "SELECT count(*) FROM organization_units WHERE item_id IS NOT NULL").fetchone()[0] == 1
    assert org.task_view(service, task["id"])["stage"] == "extraction"


def test_manual_correction_during_model_call_wins_before_extraction(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    def correct_while_model_runs():
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_units SET decision='manual',project='dsh',projects='[\"dsh\"]',"
                "revision=revision+1 WHERE task_id=?", (task["id"],))
    provider.after_assignment = correct_while_model_runs

    worker._process(service, task)

    assert decisions(service, task) == [("manual", "dsh")]
    assert event_projects(service) == ["dsh"]
    assert service.store._connection().execute(
        "SELECT source_project FROM capability_memories").fetchone()[0] == "dsh"
    assert org.task_view(service, task["id"])["status"] == "completed"


def test_unit_revision_change_during_model_call_cannot_overwrite_newer_unit(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    def update_while_model_runs():
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_units SET revision=revision+1,reason='newer revision' WHERE task_id=?",
                (task["id"],))
    provider.after_assignment = update_while_model_runs

    worker._process(service, task)

    unit = org.unit_views(service, task["id"])[0]
    assert (unit["decision"], unit["project"], unit["reason"]) == ("review", "", "newer revision")
    current = org.task_view(service, task["id"])
    assert (current["status"], current["error_code"]) == ("review", "revision_conflict")
    assert event_projects(service) == []
    assert provider.extractions == []


def test_set_aside_unit_does_not_require_assignment_or_block_completion(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET disposition='set_aside' WHERE task_id=?", (task["id"],))

    worker._process(service, task)

    assert provider.ownership_batches == []
    assert provider.extractions == []
    assert event_projects(service) == []
    current = org.task_view(service, task["id"])
    assert (current["status"], current["review_count"]) == ("completed", 0)


@pytest.mark.parametrize("projects", [["missing"], ["evo", "missing"], ["dsh"]])
def test_manual_project_scope_requires_all_projects_to_remain_active(service, monkeypatch, projects):
    worker, task, provider, _ = prepare(service, monkeypatch, ["明确的项目要求。"])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project=?,projects=? WHERE task_id=?",
            (projects[0], json.dumps(projects), task["id"]))
        service.store._connection().execute(
            "UPDATE context_project_registry SET status='archived' WHERE project='dsh'")

    worker._process(service, task)

    assert provider.ownership_batches == []
    current = org.task_view(service, task["id"])
    assert (current["status"], current["review_count"], current["error_code"]) == (
        "review", 1, "units_need_review")


def test_dual_manual_unit_does_not_resync_old_automatic_fact_relations(service, monkeypatch):
    worker, task, provider, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"], mode="dual")
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET decision='manual',project='dsh',projects='[\"dsh\"]' WHERE task_id=?",
            (task["id"],))
    def forbidden(*args, **kwargs):
        raise AssertionError("manual ownership must not replay old automatic facts")
    monkeypatch.setattr("evolvmem.knowledge_facts.sync_ownership_relations", forbidden)
    ingest = worker._ingest
    monkeypatch.setattr(worker, "_ingest", lambda service, current: ingest(service, current, extract=False))

    worker._process(service, task)

    assert decisions(service, task) == [("manual", "dsh")]
    assert event_projects(service) == ["dsh"]
    assert org.unit_views(service, task["id"])[0]["item_id"] is not None
    assert org.task_view(service, task["id"])["stage"] == "extraction"


@pytest.mark.parametrize("mode", ["primary", "dual"])
def test_module_technical_review_keeps_task_in_review_and_preserves_manual_ownership(service, monkeypatch, mode):
    from evolvmem.project_modules import ProjectModuleStore
    worker, task, normal, _ = prepare(service, monkeypatch, ["evo 增加写入校验。"], mode=mode)
    modules = ProjectModuleStore(service.store)
    module = modules.create("evo", "Record handling", explicit=True)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]',revision=9,extraction_stage='done' WHERE task_id=?", (task["id"],))
    before = dict(conn.execute("SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    calls = []
    def provider(prompt, *args, **kwargs):
        if "判断本条原文实际负责" in prompt:
            calls.append(prompt)
            return json.dumps({"items": [{"module_id": module["module_id"], "decision": "include",
                "role": "owner", "quote": "evo 增加写入校验。", "chain": {"change": "fabricated change"}}]})
        return normal(prompt, *args, **kwargs)
    monkeypatch.setattr(org, "_load_llm", lambda: ({}, provider))
    if mode == "dual":
        monkeypatch.setattr(worker, "_ingest", lambda *a, **k: None)
    worker._process(service, task)
    current = org.task_view(service, task["id"])
    assert (current["status"], current["stage"], current["review_count"]) == ("review", "done", 1)
    assert current["error_code"] == "module_response_invalid" and len(calls) == 2
    after = dict(conn.execute("SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    assert after == before
    assert conn.execute("SELECT count(*) FROM project_module_bindings").fetchone()[0] == 0
