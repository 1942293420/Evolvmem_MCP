"""Source-bound ownership candidates; vectors nominate, never decide."""
import pytest

from evolvmem.context_store import _now_iso
from tests.test_auto_organization import base_service, service
from tests.test_context_retriever import add_item, FakeVectorIndex, QueryEmbeddingEngine
from tests.test_primary_ownership import prepare


def recall(service, text, **kwargs):
    from evolvmem.ownership_candidates import recall as implementation
    return implementation(service, text, **kwargs)


def confirmed_context(service, text, *, project="evo", state="resolved", decision="human", l1=None):
    item = add_item(service.store, text, project=project, l0=text, l1=l1 or text, l2=text)
    now = _now_iso()
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,"
            "review_state,resolved_project,created_at,updated_at) VALUES(?,?,?,'accepted',?,?,?)",
            (item.id, state, decision, project if state == "resolved" else "", now, now))
    return item


def candidates(result):
    return {row["project"]: row for row in result["candidates"]}


def test_exact_slug_and_alias_nominate_projects_without_deciding(service):
    service.knowledge().save_project({"project": "bluewhale", "display_name": "蓝鲸",
                                      "aliases": ["蓝鲸 BI"]})
    result = recall(service, "bluewhale 和 dsh 共同调整导出边界。", use_vectors=False)
    assert set(candidates(result)) == {"bluewhale", "dsh"}
    assert result["requires_reasoning"] is True
    assert "decision" not in result
    alias = recall(service, "在蓝鲸 BI 查询库存。", use_vectors=False)
    evidence = candidates(alias)["bluewhale"]["evidence"]
    assert any(e["origin"] == "alias_exact" and e["quote"] == "蓝鲸 BI" for e in evidence)
    assert all(e["ref"] and e["source"]["kind"] == "registry" for e in evidence)


def test_short_generic_aliases_and_identifier_substrings_do_not_match(service):
    service.knowledge().save_project({"project": "evo", "aliases": ["api", "项目", "数据", "UI"]})
    result = recall(service, "evolution 的 API 数据项目 UI 页面。", use_vectors=False)
    assert result["candidates"] == []
    assert result["fallback"] is True
    assert {p["project"] for p in result["registry"]} == {"evo", "shop", "dsh"}


def test_archived_registry_and_its_aliases_are_never_candidates(service):
    service.knowledge().save_project({"project": "retired", "aliases": ["旧平台"]})
    service.store._connection().execute(
        "UPDATE context_project_registry SET status='archived' WHERE project='retired'")
    result = recall(service, "retired 旧平台 evo", use_vectors=False)
    assert "retired" not in candidates(result)
    assert "retired" not in {p["project"] for p in result["registry"]}


def test_single_or_tied_candidates_fall_back_to_complete_registry(service):
    for text in ("evo", "evo dsh shop", "没有归属线索"):
        result = recall(service, text, limit=1, use_vectors=False)
        assert result["fallback"] is True
        assert {p["project"] for p in result["registry"]} == {"evo", "shop", "dsh"}
    mixed = recall(service, "evo dsh shop", limit=1, use_vectors=False)
    assert set(candidates(mixed)) == {"evo", "dsh", "shop"}


def test_fts_uses_confirmed_context_and_returns_namespaced_source(service):
    item = confirmed_context(service, "仓储库存校验与补货规则")
    result = recall(service, "库存校验", use_vectors=False)
    evidence = candidates(result)["evo"]["evidence"]
    matched = [e for e in evidence if e["origin"] == "context_fts"]
    assert matched and matched[0]["source"]["kind"] == "context"
    assert matched[0]["source"]["id"] == item.id
    assert matched[0]["ref"] == f"context:{item.id}"
    assert matched[0]["score"] > 0


@pytest.mark.parametrize("status,resolution", [
    ("deleted", "resolved"), ("archived", "resolved"), ("superseded", "resolved"),
    ("active", "unresolved"), ("active", "conflict")])
def test_invalid_context_cannot_seed_candidates(service, status, resolution):
    item = confirmed_context(service, "仓储库存校验", state=resolution)
    service.store._connection().execute("UPDATE context_items SET status=? WHERE id=?", (status, item.id))
    result = recall(service, "库存校验", use_vectors=False)
    assert result["candidates"] == []
    assert result["fallback"] is True


def test_unconfirmed_automatic_label_alone_is_not_ownership_evidence(service):
    confirmed_context(service, "仓储库存校验", decision="automatic")
    assert recall(service, "库存校验", use_vectors=False)["candidates"] == []


def test_confirmed_unit_is_lexical_evidence_but_superseded_or_withdrawn_is_not(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    result = recall(service, "库存校验", use_vectors=False)
    evidence = candidates(result)["evo"]["evidence"]
    assert any(e["origin"] == "unit_lexical" and e["source"]["task_id"] == task["id"]
               and e["ref"].startswith(task["source_key"] + "#") for e in evidence)
    service.store._connection().execute(
        "UPDATE organization_tasks SET status='superseded' WHERE id=?", (task["id"],))
    assert recall(service, "库存校验", use_vectors=False)["candidates"] == []


def test_changed_source_cannot_reinforce_an_old_unit_label(service, monkeypatch):
    worker, task, _, archive = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    service.store._connection().execute(
        "UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?",
        (f"archive:{archive.id}",))
    result = recall(service, "库存校验", use_vectors=False)
    assert result["candidates"] == []
    assert any(x["reason"] == "source_changed" for x in result["diagnostics"]["excluded"])


def test_vectors_reuse_context_ids_only_and_do_not_encode_documents(service):
    from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
    item = confirmed_context(service, "货件查询的调度约束")
    memory_id = CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category="backend", title="不同ID空间", content="能力来源不应参与这里的Context路由。",
        source_project="dsh"))
    assert memory_id == item.id
    engine = QueryEmbeddingEngine([1., 0.])
    service.embedding_engine = engine
    service.vector_index = FakeVectorIndex(service.config, [{"id": item.id, "distance": 0.02}])
    service.vector_index.document_ids = [d.item_id for d in service.store.list_vector_documents()]

    result = recall(service, "语义改写但没有相同词语")

    assert set(candidates(result)) == {"evo"}
    evidence = candidates(result)["evo"]["evidence"]
    assert any(e["origin"] == "context_vector" and e["source"]["kind"] == "context"
               and e["source"]["id"] == item.id for e in evidence)
    assert result["fallback"] is True
    assert engine.queries == ["语义改写但没有相同词语"]


def test_model_unavailable_keeps_lexical_results_and_full_registry(service):
    confirmed_context(service, "仓储库存校验")
    service.embedding_engine = None
    result = recall(service, "库存校验")
    assert "evo" in candidates(result)
    assert result["diagnostics"]["vector_status"] == "unavailable"
    assert result["fallback"] is True
    assert {p["project"] for p in result["registry"]} == {"evo", "shop", "dsh"}


def test_candidate_recall_performs_no_database_writes(service):
    confirmed_context(service, "仓储库存校验")
    conn = service.store._connection()
    before = conn.total_changes
    result = recall(service, {"text": "库存校验", "title": "查询需求"}, use_vectors=False)
    assert result["candidates"]
    assert conn.total_changes == before


def test_fts_evidence_quotes_the_matching_layer_and_term_even_after_long_prefix(service):
    body = "这里是无关的介绍。" * 100 + "库存校验必须保留来源编号。"
    item = confirmed_context(service, "归属资料概览", l1=body)
    result = recall(service, "库存校验", use_vectors=False)
    evidence = [e for e in candidates(result)["evo"]["evidence"] if e["origin"] == "context_fts"]
    assert evidence and "库存校验" in evidence[0]["quote"]
    assert evidence[0]["source"]["layer"] == "l1"


def test_independent_manual_source_survives_an_old_unit_derivation(service, monkeypatch):
    from evolvmem.unit_derivations import record_derivation
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    item = confirmed_context(service, "仓储库存校验规则")
    unit = worker._reload_units(service, task["id"])[0]
    record_derivation(service, task["id"], unit["digest"], item.id, kind="fact", project="evo")
    service.store._connection().execute(
        "UPDATE organization_tasks SET status='superseded' WHERE id=?", (task["id"],))
    service.store._connection().execute(
        "INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) "
        "VALUES(?,'manual','confirmed-independent-note','v1',?)", (item.id, _now_iso()))
    result = recall(service, "库存校验", use_vectors=False)
    assert "evo" in candidates(result)
    assert all(e["origin"] == "context_fts" for e in candidates(result)["evo"]["evidence"])


def test_capability_index_cannot_be_used_as_context_even_when_id_matches(service):
    item = confirmed_context(service, "货件查询的调度约束")
    engine = QueryEmbeddingEngine([1., 0.])
    service.embedding_engine = engine
    service.vector_index = FakeVectorIndex(service.config, [{"id": item.id, "distance": 0.01}],
                                           path=service.config.capability_vector_path)
    result = recall(service, "完全不同的输入")
    assert result["candidates"] == []
    assert result["diagnostics"]["vector_status"] == "incompatible_namespace"
    assert engine.queries == []


@pytest.mark.parametrize("decision,disposition", [("review", "keep"), ("manual", "set_aside")])
def test_unresolved_or_withdrawn_unit_never_seeds_lexical_candidates(service, monkeypatch, decision, disposition):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    service.store._connection().execute(
        "UPDATE organization_units SET decision=?,disposition=? WHERE task_id=?",
        (decision, disposition, task["id"]))
    assert recall(service, "库存校验", use_vectors=False)["candidates"] == []


def test_lexical_queries_share_a_total_candidate_budget_and_bound_like_ids(service, monkeypatch):
    from evolvmem import ownership_candidates as module
    confirmed_context(service, "库存校验货件调度权限边界原文清洗")
    calls = []
    original = service.store.search_fts
    def bounded(query, **kwargs):
        calls.append((query, kwargs))
        assert kwargs.get("item_ids"), "CJK LIKE fallback must not scan every Context body"
        return original(query, **kwargs)
    monkeypatch.setattr(service.store, "search_fts", bounded)
    result = recall(service, "库存校验货件调度权限边界原文清洗", use_vectors=False)
    assert len(calls) <= 3
    assert result["diagnostics"]["contexts_examined"] <= module.POOL_LIMIT


def test_unit_lexical_recall_does_not_scan_all_text_rows(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    queries = []
    conn = service.store._connection()
    conn.set_trace_callback(queries.append)
    try:
        result = recall(service, "库存校验", use_vectors=False)
    finally:
        conn.set_trace_callback(None)
    assert "evo" in candidates(result)
    assert not any("instr(lower(u.text)" in sql for sql in queries)
    assert result["diagnostics"]["unit_rows_examined"] <= 256


def test_bounded_source_pool_keeps_full_registry_when_older_evidence_is_outside_pool(service, monkeypatch):
    from evolvmem import ownership_candidates as module
    monkeypatch.setattr(module, "SOURCE_POOL_LIMIT", 2, raising=False)
    for text in ("仓储库存校验", "另一种任务", "最新的任务"):
        confirmed_context(service, text)
    result = recall(service, "库存校验", use_vectors=False)
    assert result["diagnostics"]["pool_truncated"] is True
    assert result["fallback"] is True
    assert {p["project"] for p in result["registry"]} == {"evo", "shop", "dsh"}


def test_overlong_query_skips_retrieval_and_never_hides_an_exact_project_at_tail(service, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("overlong input must retain the registry, not scan a prefix")
    monkeypatch.setattr(service.store, "search_fts", forbidden)
    result = recall(service, "长正文。" * 5000 + " dsh", use_vectors=False)
    assert "dsh" in candidates(result)
    assert result["fallback"] is True
    assert "input_over_budget" in result["diagnostics"]["budget_reasons"]
    assert {p["project"] for p in result["registry"]} == {"evo", "shop", "dsh"}


def test_request_reader_reuses_current_source_proof_and_invalidates_after_manual_change(service, monkeypatch):
    from evolvmem import ownership_candidates as module
    worker, task, _, archive = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    reader = module.CandidateRead(service)
    assert "evo" in candidates(recall(service, "库存校验", use_vectors=False, read=reader))
    first_task = reader.tasks[task["id"]]
    assert "evo" in candidates(recall(service, "仓储库存", use_vectors=False, read=reader))
    assert reader.tasks[task["id"]] is first_task
    service.store._connection().execute(
        "UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?", (f"archive:{archive.id}",))
    assert recall(service, "库存校验", use_vectors=False, read=reader)["candidates"] == []


def test_assignment_chunk_shares_one_candidate_reader(service, monkeypatch):
    from evolvmem import ownership_candidates as module
    worker, task, _, _ = prepare(service, monkeypatch, [
        "evo 仓储库存校验规则。", "evo 货件查询边界。", "evo 原文清洗规则。", "evo 重试必须保留来源。"])
    seen = []
    original = module.recall
    def observed(*args, **kwargs):
        seen.append(kwargs.get("read"))
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "recall", observed)
    worker._assign(service, task, materialize=False)
    assert len(seen) == 4 and seen[0] is not None
    assert all(reader is seen[0] for reader in seen)


def test_candidate_metadata_plan_never_reads_all_frozen_task_snapshots(service, monkeypatch):
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    worker._assign(service, task, materialize=False)
    conn = service.store._connection()
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        recall(service, "库存校验", use_vectors=False)
    finally:
        conn.set_trace_callback(None)
    metadata = next(q for q in queries if q.startswith("SELECT u.task_id,u.ordinal"))
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + metadata)]
    assert any("COVERING INDEX idx_organization_tasks_status" in row for row in plan), plan


def test_candidate_reader_failure_falls_back_without_blocking_assignment(service, monkeypatch):
    from evolvmem import ownership_candidates as module
    worker, task, _, _ = prepare(service, monkeypatch, ["evo 仓储库存校验规则。"])
    def failed_reader(*args, **kwargs):
        raise RuntimeError("candidate read unavailable")
    monkeypatch.setattr(module, "CandidateRead", failed_reader)
    worker._assign(service, task, materialize=False)
    assert worker._reload_units(service, task["id"])[0]["project"] == "evo"
