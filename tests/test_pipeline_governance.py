"""Retries, concurrent edits and uncertain ownership must not publish false history."""
import json
from types import SimpleNamespace

import pytest

from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.capability_store import CapabilityStore
from evolvmem.context_store import ContextStore
from evolvmem.project_archive_pipeline import ProjectArchivePipeline
from tests.test_capability_pipeline import service_with_task


def candidate(_):
    return {"action": "keep", "category": "frontend", "title": "表单校验",
            "content": "蓝鲸前端新增表单校验", "keywords": ["表单"],
            "kind": "fact", "trigger": "", "conditions": [], "action_steps": [], "result": "",
            "evidence_quotes": ["蓝鲸前端新增表单校验，并修复MySQL重复写入。"], "value_reason": "开发事实"}


def test_replay_reuses_payload_and_still_repairs_source_binding(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        calls = []
        first = CapabilityPipeline(service).process_task(task, provider=lambda p: (calls.append(p), candidate(p))[1])
        with store.transaction():
            store._connection().execute("DELETE FROM capability_sources")
        second = CapabilityPipeline(service).process_task(task, provider=lambda _: pytest.fail("repeat model call"))
        assert first == second
        assert len(calls) == 1
        assert len(CapabilityStore(store).sources(first["memory_ids"][0])) == 1


def test_source_edit_during_provider_rejects_payload_and_all_writes(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        def racing(prompt):
            with store.transaction():
                store._connection().execute("UPDATE organization_units SET revision=revision+1, text='已更正原文' WHERE task_id=?", (task["id"],))
            return candidate(prompt)
        with pytest.raises(ValueError, match="revision_conflict"):
            CapabilityPipeline(service).process_task(task, provider=racing)
        assert store._connection().execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 0


@pytest.mark.parametrize("decision,disposition", [("review", "keep"), ("auto", "review"), ("manual", "set_aside")])
def test_uncertain_unit_never_inherits_archive_or_stale_project(test_config, decision, disposition):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        with store.transaction():
            store._connection().execute("UPDATE organization_units SET decision=?,disposition=? WHERE task_id=?", (decision, disposition, task["id"]))
        events = ProjectArchivePipeline(service).process_task(task, provider=lambda _: pytest.fail("unknown ownership calls model"))
        assert events["event_ids"] == []
        result = CapabilityPipeline(service).process_task(task, provider=candidate)
        if disposition == "keep":
            memory = CapabilityStore(store).get(result["memory_ids"][0])
            assert memory["source_project"] == ""
            assert CapabilityStore(store).sources(memory["memory_id"])[0]["evidence_status"] == "unverified"
        else:
            assert result["memory_ids"] == []


def test_ownership_change_rebinds_without_second_model_call(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        with store.transaction():
            store._connection().execute("UPDATE organization_units SET decision='review',project='',projects='[]' WHERE task_id=?", (task["id"],))
        first = CapabilityPipeline(service).process_task(task, provider=candidate)
        with store.transaction():
            store._connection().execute("UPDATE organization_units SET decision='manual',project='bluewhale',projects='[\"bluewhale\"]',revision=revision+1 WHERE task_id=?", (task["id"],))
        CapabilityPipeline(service).process_task(task, provider=lambda _: pytest.fail("ownership only edit must reuse extraction"))
        memory = CapabilityStore(store).get(first["memory_ids"][0])
        assert memory["source_project"] == "bluewhale"
        assert memory["sources_count"] == 1
        assert CapabilityStore(store).sources(memory["memory_id"])[0]["revision"] == 2


@pytest.mark.parametrize("proven_owner", [False, True])
def test_multi_project_knowledge_is_not_change_history_for_every_project(test_config, proven_owner):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        with store.transaction():
            store._connection().execute("INSERT INTO context_project_registry(project,status,created_at,updated_at) VALUES('evolvmem','active','now','now')")
            store._connection().execute("UPDATE organization_units SET projects='[\"bluewhale\",\"evolvmem\"]' WHERE task_id=?", (task["id"],))
            if proven_owner:
                from evolvmem.ownership_reasoning import digest
                text = store._connection().execute("SELECT text FROM organization_units").fetchone()[0]
                analysis = {"decision": "include", "text_hash": digest(text),
                            "targets": [{"project": "evolvmem", "role": "owner", "quote": text}]}
                store._connection().execute("UPDATE organization_units SET ownership_analysis=? WHERE task_id=?", (json.dumps(analysis), task["id"]))
        result = ProjectArchivePipeline(service).process_task(task, provider=lambda _: {"events": [{"event_type": "feature", "title": "共享接口", "content": "两个项目接口联调完成。"}]})
        assert len(result["event_ids"]) == (1 if proven_owner else 0)
        assert {r[0] for r in store._connection().execute("SELECT project FROM project_events")} == ({"evolvmem"} if proven_owner else set())
        memories = CapabilityPipeline(service).process_task(task, provider=candidate)
        binding = CapabilityStore(store).sources(memories["memory_ids"][0])[0]
        assert set(binding["projects"]) == {"bluewhale", "evolvmem"}


def test_failed_save_reuses_provider_payload(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        original = CapabilityStore.upsert
        monkeypatch.setattr(CapabilityStore, "upsert", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk busy")))
        with pytest.raises(RuntimeError, match="disk busy"):
            CapabilityPipeline(service).process_task(task, provider=candidate)
        monkeypatch.setattr(CapabilityStore, "upsert", original)
        assert CapabilityPipeline(service).process_task(task, provider=lambda _: pytest.fail("failed save must not call model twice"))["memory_ids"]


def test_bad_model_action_does_not_create_active_knowledge(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        with pytest.raises(ValueError, match="invalid_capability_action"):
            CapabilityPipeline(service).process_task(task, provider=lambda _: {**candidate(""), "action": "whatever"})
        assert store._connection().execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 0


def test_concurrent_same_input_is_leased(test_config):
    from evolvmem.pipeline_state import extraction_payload
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        unit = dict(store._connection().execute("SELECT * FROM organization_units").fetchone())
        def nested():
            with pytest.raises(RuntimeError, match="extraction_in_progress"):
                extraction_payload(service, task, unit, "test:v1", lambda: {})
            return {"safe": True}
        assert extraction_payload(service, task, unit, "test:v1", nested) == {"safe": True}


@pytest.mark.parametrize("bad_date", [{"date": "2026-10-31"}, ["2026-10-31"], True, 1, "not-a-date", "2026-02-30"])
def test_invalid_date_is_not_cached_and_can_be_retried(test_config, bad_date):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        with pytest.raises(ValueError, match="invalid_occurred_at"):
            CapabilityPipeline(service).process_task(task, provider=lambda _: {**candidate(""), "occurred_at": bad_date})
        assert store._connection().execute("SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 0
        result = CapabilityPipeline(service).process_task(task, provider=candidate)
        assert len(result["memory_ids"]) == 1

