"""Bounded primary extraction keeps invalid model structure out of knowledge."""
import json
import sqlite3

import pytest

from evolvmem import auto_organization as org
from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.context_store import ContextStore
from tests.test_capability_pipeline import service_with_task
from tests.test_pipeline_governance import candidate
from tests.test_primary_ownership import prepare, service, base_service


def payloads(store):
    return [json.loads(r[0]) for r in store._connection().execute(
        "SELECT payload FROM extraction_receipts WHERE pipeline LIKE 'capability:v3:%' AND payload IS NOT NULL")]


def published(store):
    return {table: [tuple(row) for row in store._connection().execute("SELECT * FROM " + table)]
            for table in ("capability_memories", "capability_sources", "capability_assessments",
                          "capability_intake", "capability_category_suggestions")}


def test_invalid_quote_corrected_once_without_publishing_bad_candidate(test_config):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        calls = []
        def provider(prompt):
            calls.append(prompt)
            return candidate(prompt) if len(calls) == 2 else {**candidate(prompt), "evidence_quotes": ["PRIVATE INVENTED QUOTE"]}
        result = CapabilityPipeline(svc).process_task(task, provider=provider)
        assert len(calls) == 2 and len(result["memory_ids"]) == 1
        assert result["technical_review"] == 0 and result["pending"] == 0
        assert "PRIVATE INVENTED QUOTE" not in calls[1]
        assert all("PRIVATE INVENTED QUOTE" not in json.dumps(p) for p in payloads(store))
        assert store._connection().execute("SELECT count(*) FROM capability_assessments").fetchone()[0] == 1


def test_two_invalid_responses_cache_only_technical_outcome_and_preserve_old_knowledge(test_config):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        CapabilityPipeline(svc).process_task(task, provider=candidate)
        before = published(store)
        with store.transaction():
            store._connection().execute("DELETE FROM extraction_receipts")
        calls = []
        def bad(prompt):
            calls.append(prompt)
            return {**candidate(prompt), "action_steps": [{"private": "DO NOT STORE"}]}
        first = CapabilityPipeline(svc).process_task(task, provider=bad)
        assert len(calls) == 2
        assert (first["technical_review"], first["pending"], first["memory_ids"]) == (1, 1, [])
        assert first["unit_outcomes"][0]["reason"] == "model_response_invalid"
        assert first["unit_outcomes"][0]["errors"] == ["invalid_capability_action_steps"] * 2
        assert published(store) == before
        receipt = payloads(store)[0]
        assert receipt["action"] == "technical_review" and "DO NOT STORE" not in json.dumps(receipt)
        second = CapabilityPipeline(svc).process_task(task, provider=lambda _: pytest.fail("technical receipt must be idempotent"))
        assert second == first and published(store) == before


@pytest.mark.parametrize("fake", [
    {"action": "technical_review", "technical_review": {"reason": "model_response_invalid", "attempts": 2}},
    {**candidate(""), "technical_review": {"reason": "model_response_invalid", "attempts": 2}},
    {"action": "technical_review", "technical_review": {"reason": "model_response_invalid", "attempts": 2,
     "errors": ["invalid_capability_action"] * 2, "error_code": "invalid_capability_action",
     "diagnostics": [{"error_code": "invalid_capability_action", "fields": [{"field": "action", "type": "string"}]}] * 2}},
])
def test_model_cannot_forge_server_technical_outcome(test_config, fake):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        calls = []
        result = CapabilityPipeline(svc).process_task(task, provider=lambda p: (calls.append(p), fake)[1])
        assert len(calls) == 2 and result["technical_review"] == 1
        assert result["unit_outcomes"][0]["errors"] == ["invalid_capability_action"] * 2
        assert not published(store)["capability_memories"]


@pytest.mark.parametrize("error", [OSError("connection failed"), RuntimeError("provider unavailable"),
                                    ValueError("permission_denied"), ValueError("capability_quote_not_in_source"),
                                    ValueError("task_invalid_response"), IndexError("provider failed"), sqlite3.OperationalError("database is locked")])
def test_transport_and_storage_failures_are_never_corrected(test_config, error):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        calls = []
        def broken(prompt):
            calls.append(prompt)
            raise error
        with pytest.raises(type(error), match=str(error)):
            CapabilityPipeline(svc).process_task(task, provider=broken)
        assert len(calls) == 1 and payloads(store) == []
        assert not published(store)["capability_memories"]


@pytest.mark.parametrize("change", ["manual", "source", "lease"])
def test_second_attempt_current_or_lease_change_aborts_without_technical_cache(test_config, change):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        conn = store._connection()
        calls = []
        def racing(prompt):
            calls.append(prompt)
            if len(calls) == 2:
                with store.transaction():
                    if change == "manual":
                        conn.execute("UPDATE organization_units SET decision='manual',disposition='set_aside',revision=revision+1")
                    elif change == "source":
                        conn.execute("UPDATE organization_tasks SET source_revision='new-original'")
                    else:
                        conn.execute("UPDATE extraction_receipts SET lease_token='successor',lease_until=9999999999")
            return {**candidate(prompt), "evidence_quotes": ["invented"]}
        expected = "extraction_lease_conflict" if change == "lease" else "revision_conflict"
        with pytest.raises(ValueError, match=expected):
            CapabilityPipeline(svc).process_task(task, provider=racing)
        assert len(calls) == 2 and payloads(store) == []
        assert not published(store)["capability_memories"]
        if change == "lease":
            assert conn.execute("SELECT lease_token FROM extraction_receipts").fetchone()[0] == "successor"
        if change == "manual":
            assert tuple(conn.execute("SELECT decision,disposition FROM organization_units").fetchone()) == ("manual", "set_aside")


def test_valid_unsupported_claim_is_reviewed_once_without_correction(test_config):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        calls, reviews = [], []
        def extract(prompt):
            calls.append(prompt)
            return {**candidate(prompt), "title": "A generalized proposal"}
        def unsupported(request):
            reviews.append(request)
            return {"input_hash": request["input_hash"], "candidate_hash": request["candidate_hash"],
                    "supported": False, "reason": "The generalization is unsupported.", "evidence_quotes": []}
        result = CapabilityPipeline(svc).process_task(task, provider=extract, support_reviewer=unsupported)
        assert len(calls) == len(reviews) == 1
        assert result["technical_review"] == 0 and result["pending"] == 1
        assert len(result["memory_ids"]) == 1


@pytest.mark.parametrize("mode", ["primary", "dual"])
def test_worker_counts_technical_unit_and_processes_later_units(service, monkeypatch, mode):
    worker, task, normal, _ = prepare(service, monkeypatch, ["evo 第一条要求。", "evo 第二条要求。"], mode=mode)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]',revision=7,extraction_stage='done'")
    before = [dict(r) for r in conn.execute("SELECT * FROM organization_units ORDER BY ordinal")]
    calls = []
    def provider(prompt, *args, **kwargs):
        if "开发知识整理器" in prompt:
            calls.append(prompt)
            if "第一条要求" in prompt:
                return json.dumps({**candidate(""), "evidence_quotes": ["invented"]})
        return normal(prompt, *args, **kwargs)
    monkeypatch.setattr(org, "_load_llm", lambda: ({}, provider))
    if mode == "dual":
        monkeypatch.setattr(worker, "_ingest", lambda *a, **k: None)
    worker._process(service, task)
    current = org.task_view(service, task["id"])
    assert (current["status"], current["stage"], current["review_count"]) == ("review", "done", 1)
    assert current["error_code"] == "capability_response_invalid"
    assert len(calls) == 3
    assert [dict(r) for r in conn.execute("SELECT * FROM organization_units ORDER BY ordinal")] == before
    assert conn.execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM project_events").fetchone()[0] == 2

@pytest.mark.parametrize("response", [None, [], 42, "not JSON", "\x60\x60\x60json"])
def test_nonobject_model_response_is_bounded_and_not_published(test_config, response):
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        calls = []
        result = CapabilityPipeline(svc).process_task(task, provider=lambda p: (calls.append(p), response)[1])
        assert len(calls) == 2 and result["technical_review"] == 1
        assert result["unit_outcomes"][0]["errors"] == ["task_invalid_response"] * 2
        assert not published(store)["capability_memories"]


def test_cached_technical_outcome_rechecks_manual_edit_after_cache_read(test_config, monkeypatch):
    from evolvmem import capability_pipeline as pipeline
    with ContextStore(test_config) as store:
        svc, task = service_with_task(test_config, store)
        CapabilityPipeline(svc).process_task(task, provider=lambda _: {"action": "bad"})
        original = pipeline.extraction_payload
        def after_read(*args, **kwargs):
            value = original(*args, **kwargs)
            with store.transaction():
                store._connection().execute("UPDATE organization_units SET decision='manual',revision=revision+1")
            return value
        monkeypatch.setattr(pipeline, "extraction_payload", after_read)
        with pytest.raises(ValueError, match="revision_conflict"):
            CapabilityPipeline(svc).process_task(task, provider=lambda _: pytest.fail("cached"))
        assert not published(store)["capability_memories"]


def test_original_cleaning_change_during_correction_aborts_worker_publication(service, monkeypatch):
    worker, task, normal, archive = prepare(service, monkeypatch, ["evo 原件变化测试。"])
    calls = []
    def provider(prompt, *args, **kwargs):
        if "开发知识整理器" in prompt:
            calls.append(prompt)
            if len(calls) == 2:
                with service.store.transaction():
                    service.store._connection().execute(
                        "UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?",
                        (f"archive:{archive.id}",))
            return json.dumps({**candidate(""), "evidence_quotes": ["invented"]})
        return normal(prompt, *args, **kwargs)
    monkeypatch.setattr(org, "_load_llm", lambda: ({}, provider))
    worker._process(service, task)
    current = org.task_view(service, task["id"])
    assert len(calls) == 2 and current["source_current"] is False
    assert (current["status"], current["error_code"]) == ("review", "revision_conflict")
    assert payloads(service.store) == []
    assert not published(service.store)["capability_memories"]


@pytest.mark.parametrize("kind", ["capability", "module"])
@pytest.mark.parametrize("legacy_stage", ["pending", "failed"])
def test_dual_preserves_unfinished_legacy_stage_then_restores_technical_review(
        service, monkeypatch, kind, legacy_stage):
    from evolvmem.project_modules import ProjectModuleStore
    worker, task, normal, _ = prepare(service, monkeypatch, ["evo 双链技术响应。"], mode="dual")
    modules = ProjectModuleStore(service.store)
    module = modules.create("evo", "记录处理", explicit=True) if kind == "module" else None
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]',revision=7")
    def provider(prompt, *args, **kwargs):
        if kind == "capability" and "开发知识整理器" in prompt:
            return json.dumps({**candidate(""), "evidence_quotes": ["invented"]})
        if kind == "module" and "判断本条原文实际负责" in prompt:
            return json.dumps({"items": [{"module_id": module["module_id"], "decision": "include",
                "role": "owner", "quote": "evo 双链技术响应。", "chain": {"change": "invented"}}]})
        return normal(prompt, *args, **kwargs)
    monkeypatch.setattr(org, "_load_llm", lambda: ({}, provider))
    def ingest(*args, **kwargs):
        with service.store.transaction():
            conn.execute("UPDATE organization_units SET extraction_stage=?,extraction_diagnostic='{}'", (legacy_stage,))
    monkeypatch.setattr(worker, "_ingest", ingest)
    worker._process(service, task)
    current = org.task_view(service, task["id"])
    assert (current["status"], current["stage"]) == (legacy_stage, "extraction")
    assert current["review_count"] == 1
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET extraction_stage='done'")
    monkeypatch.setattr(org, "_load_llm", lambda: pytest.fail("status refresh cannot call model"))
    final = org._refresh_task_state(service, task["id"])
    assert (final["status"], final["stage"], final["review_count"]) == ("review", "done", 1)
    assert final["error_code"] == kind + "_response_invalid"
    if module:
        modules.update(module["module_id"], expected_revision=module["revision"],
                       description="已更新的模块定义", explicit=True)
        fresh = org._refresh_task_state(service, task["id"])
        assert (fresh["status"], fresh["review_count"]) == ("completed", 0)
    else:
        with service.store.transaction():
            conn.execute("UPDATE organization_units SET text=text || ' 新正文',revision=revision+1")
        fresh = org._refresh_task_state(service, task["id"])
        assert (fresh["status"], fresh["review_count"]) == ("completed", 0)


def test_dual_terminal_count_uses_registry_at_status_commit(service, monkeypatch):
    from contextlib import contextmanager
    from evolvmem.project_module_pipeline import ProjectModulePipeline
    from evolvmem.project_modules import ProjectModuleStore
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 提交时模块检查。"], mode="dual")
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]',extraction_stage='done'")
    modules = ProjectModuleStore(service.store)
    module = modules.create("evo", "提交时模块", explicit=True)
    result = ProjectModulePipeline(service).process_task(task, provider=lambda _: {
        "items": [{"module_id": module["module_id"], "decision": "include",
                   "role": "owner", "quote": "invented", "chain": {"need": "invented"}}]})
    assert result["technical_review"] == 1
    original, changed = service.store.transaction, []
    @contextmanager
    def change_before_commit(*args, **kwargs):
        if not changed:
            changed.append(True)
            with original():
                conn.execute("UPDATE project_modules SET revision=revision+1 WHERE module_id=?", (module["module_id"],))
        with original(*args, **kwargs):
            yield
    monkeypatch.setattr(service.store, "transaction", change_before_commit)
    final = org._refresh_task_state(service, task["id"])
    assert (final["status"], final["review_count"]) == ("completed", 0)
