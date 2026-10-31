"""Existing capability assessment keeps identity/content and binds current originals."""
import copy
from dataclasses import replace
import json
import pytest

from evolvmem import capability_assessment as assessment
from evolvmem import capability_assessment_migration as migration
from evolvmem.capability_provenance import context_revision
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.capability_recall_eligibility import recall_facts
from tests.test_auto_organization import base_service, service
from tests.test_primary_ownership import prepare
from tests.test_ownership_candidates import confirmed_context

TEXT = "SQLite 写入需要使用事务，失败时回滚本批次。"


def old_unit_memory(service, monkeypatch, *, content=TEXT):
    _, task, _, _ = prepare(service, monkeypatch, [TEXT])
    conn = service.store._connection()
    unit = dict(conn.execute("SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    source = CapabilitySourceDraft(source_kind="unit", source_key=task["source_key"] + "#" + unit["digest"],
        source_ref=f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}",
        source_revision=task["source_revision"], task_id=task["id"], unit_digest=unit["digest"],
        unit_revision=unit["revision"], projects=(), knowledge_scope="project", evidence_status="unverified")
    memories = CapabilityStore(service.store)
    identity = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=content, source_ref=source.source_ref), source=source)
    return identity, task, unit


def plan(service):
    return migration.build_plan(service.store._connection(),
        rule_revision=service.knowledge().rules.read()["revision"])


def fields(quote=TEXT):
    return {"kind": "fact", "trigger": "", "conditions": [], "action_steps": [], "result": "",
            "evidence_quotes": [quote], "value_reason": "明确的事务约束"}


def memory_row(service, ident):
    return dict(service.store._connection().execute("SELECT * FROM capability_memories WHERE memory_id=?", (ident,)).fetchone())


def test_prepare_metadata_only_and_apply_keeps_old_memory_identity(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    conn = service.store._connection()
    conn.execute("PRAGMA query_only=ON")
    try:
        prepared = plan(service)
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert prepared["summary"]["model_ready_sources"] == 1
    assert TEXT not in json.dumps(prepared, ensure_ascii=False)
    before = memory_row(service, ident)
    calls = []
    result = migration.apply_plan(service, prepared, provider=lambda prompt: calls.append(prompt) or fields())
    assert result["applied"] == 1
    assert memory_row(service, ident) == before
    assert conn.execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 1
    saved = assessment.assessments(service.store, ident)
    assert len(saved) == 1 and saved[0]["value"] == "reference" and not saved[0]["stale"]
    assert saved[0]["verification_status"] == "unverified"
    assert migration.apply_plan(service, prepared, provider=lambda _: pytest.fail("must resume idempotently"))["applied"] == 0


def test_model_cannot_replace_existing_title_content_or_fingerprint(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    prepared = plan(service)
    before = memory_row(service, ident)
    calls = []
    result = migration.apply_plan(service, prepared,
        provider=lambda prompt: calls.append(prompt) or {**fields(), "content": "新的结论"})
    assert len(calls) == 2 and result["applied"] == 1
    assert memory_row(service, ident) == before
    saved = assessment.assessments(service.store, ident)[0]
    assert saved["value"] == "review" and saved["verification_status"] == "unverified"
    assert saved["details"]["reasons"] == ["model_response_invalid"]
    assert saved["details"]["model_response"]["error_code"] == "invalid_existing_assessment_fields"
    assert saved["details"]["model_response"]["attempts"] == 2
    assert saved["details"]["evidence"] == []
    assert not recall_facts(service.store, [ident])[ident]["recall_eligible"]


@pytest.mark.parametrize("change", ["unit", "memory", "binding", "policy"])
def test_model_time_source_memory_manual_or_policy_change_cannot_publish(service, monkeypatch, change):
    ident, task, _ = old_unit_memory(service, monkeypatch)
    prepared = plan(service)
    conn = service.store._connection()
    def provider(_):
        if change == "unit":
            conn.execute("UPDATE organization_units SET decision='manual',revision=revision+1 WHERE task_id=?", (task["id"],))
        elif change == "memory":
            conn.execute("UPDATE capability_memories SET status='ignored',revision=revision+1 WHERE memory_id=?", (ident,))
        elif change == "binding":
            conn.execute("UPDATE capability_sources SET revision=revision+1 WHERE memory_id=?", (ident,))
        else:
            rules = service.knowledge().rules.read()
            service.knowledge().rules.save({"expected_revision": rules["revision"], "instructions": rules["instructions"] + "\n新规则"})
        conn.commit()
        return fields()
    with pytest.raises(ValueError, match="assessment_migration_revision_conflict"):
        migration.apply_plan(service, prepared, provider=provider)
    assert not assessment.assessments(service.store, ident)


def test_generated_context_body_is_reviewed_without_becoming_an_original_quote(service):
    item = confirmed_context(service, TEXT)
    source = CapabilitySourceDraft(source_kind="context", source_key=f"context:{item.id}",
        source_ref=f"context_item:{item.id}", context_item_id=item.id,
        source_revision=context_revision(service.store._connection(), item.id),
        projects=("evo",), evidence_status="confirmed")
    ident = CapabilityStore(service.store).upsert(
        CapabilityMemoryDraft(category="database", title="旧生成正文", content=TEXT), source=source)
    prepared = plan(service)
    assert prepared["summary"]["review_sources"] == 1
    result = migration.apply_plan(service, prepared, provider=lambda _: pytest.fail("L2 is not original dialogue"))
    assert result["applied"] == 1
    row = assessment.assessments(service.store, ident)[0]
    assert row["value"] == "review"
    assert "context_original_unproven" in row["details"]["reasons"]
    assert row["details"]["evidence"] == []
    assert not recall_facts(service.store, [ident])[ident]["recall_eligible"]


def test_manual_original_context_can_be_assessed_with_unknown_role(service):
    item = service.knowledge().create({"title": "人工录入", "body": TEXT, "project": "evo", "action": "publish"})
    source = CapabilitySourceDraft(source_kind="context", source_key=f"context:{item['id']}",
        source_ref=f"context_item:{item['id']}", context_item_id=item["id"],
        source_revision=context_revision(service.store._connection(), item["id"]),
        projects=("evo",), evidence_status="confirmed")
    ident = CapabilityStore(service.store).upsert(
        CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT), source=source)
    migration.apply_plan(service, plan(service), provider=lambda _: fields())
    row = assessment.assessments(service.store, ident)[0]
    assert row["value"] == "reference" and not row["stale"]
    assert row["details"]["evidence"][0]["roles"] == ["unknown"]
    assert row["verification_status"] == "unverified"


def test_bindings_required_and_invalid_sources_are_explicit_not_resurrected(service, monkeypatch):
    ident, task, _ = old_unit_memory(service, monkeypatch)
    conn = service.store._connection()
    conn.execute("DELETE FROM capability_sources WHERE memory_id=?", (ident,)); conn.commit()
    prepared = plan(service)
    assert prepared["summary"]["bindings_required"] == 1
    assert migration.apply_plan(service, prepared, provider=lambda _: pytest.fail("no binding"))["applied"] == 0


def test_source_failure_and_fabricated_support_never_become_verified(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch, content="事务能消除所有生产故障。")
    prepared = plan(service)
    result = migration.apply_plan(service, prepared, provider=lambda _: fields(),
        reviewer=lambda request: {"input_hash": request["input_hash"], "candidate_hash": request["candidate_hash"],
            "supported": True, "reason": "伪造引文", "evidence_quotes": ["不存在的证据"]})
    assert result["applied"] == 1
    row = assessment.assessments(service.store, ident)[0]
    assert row["value"] == "review" and row["verification_status"] == "unverified"
    assert not recall_facts(service.store, [ident])[ident]["recall_eligible"]


def test_tampered_plan_is_rejected(service, monkeypatch):
    old_unit_memory(service, monkeypatch)
    prepared = plan(service)
    tampered = copy.deepcopy(prepared)
    tampered["entries"][0]["memory_id"] += 100
    with pytest.raises(ValueError, match="invalid_assessment_migration_plan"):
        migration.apply_plan(service, tampered, provider=lambda _: fields())


def test_rebound_context_source_ref_reenters_assessment_plan(service):
    item = service.knowledge().create({"title": "人工录入", "body": TEXT, "project": "evo", "action": "publish"})
    source = CapabilitySourceDraft(source_kind="context", source_key=f"context:{item['id']}",
        source_ref=f"context_item:{item['id']}", context_item_id=item["id"],
        source_revision=context_revision(service.store._connection(), item["id"]),
        projects=("evo",), evidence_status="confirmed")
    memories = CapabilityStore(service.store)
    ident = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT), source=source)
    migration.apply_plan(service, plan(service), provider=lambda _: fields())
    source = replace(source, source_ref=f"context_item:{item['id']}:corrected")
    memories.bind_source(ident, source)
    assert assessment.assessments(service.store, ident)[0]["stale"]
    assert len(plan(service)["entries"]) == 1


def test_completed_claim_is_rechecked_inside_transaction(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    prepared = plan(service)
    original = migration._audit
    pending = [True]
    calls = []
    def interleaved(conn, prepared_plan, memory_id=None):
        rows = original(conn, prepared_plan, memory_id)
        if memory_id is None and pending[0]:
            pending[0] = False
            migration.apply_plan(service, prepared, provider=lambda _: calls.append("winner") or fields())
        return rows
    monkeypatch.setattr(migration, "_audit", interleaved)
    result = migration.apply_plan(service, prepared, provider=lambda _: calls.append("loser") or fields())
    assert result["applied"] == 0 and result["remaining"] == 0
    assert calls == ["winner"]
    assert len(assessment.assessments(service.store, ident)) == 1
    assert original(service.store._connection(), prepared)[0]["state"] == "done"


def manual_source(service, *, title="人工原件"):
    item = service.knowledge().create({"title": title, "body": TEXT, "project": "evo", "action": "publish"})
    return CapabilitySourceDraft(source_kind="context", source_key=f"context:{item['id']}",
        source_ref=f"context_item:{item['id']}", context_item_id=item["id"],
        source_revision=context_revision(service.store._connection(), item["id"]),
        projects=("evo",), evidence_status="confirmed")


def test_default_limit_and_resume_assess_each_existing_source_without_new_memories(service):
    memories = CapabilityStore(service.store)
    for i in range(7):
        ident = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT),
            source=manual_source(service, title=f"原件{i}"))
    prepared = plan(service)
    assert prepared["summary"]["sources"] == 7
    before = memory_row(service, ident)
    calls = []
    assert migration.apply_plan(service, prepared, provider=lambda p: calls.append(p) or fields())["remaining"] == 2
    assert len(calls) == 5
    resumed = migration.apply_plan(service, prepared, provider=lambda p: calls.append(p) or fields())
    assert resumed["applied"] == 2 and resumed["remaining"] == 0
    assert len(calls) == 7 and len(assessment.assessments(service.store, ident)) == 7
    assert memory_row(service, ident) == before
    assert not plan(service)["entries"]


def test_direct_current_archive_proves_original_not_generated_context_layer(service):
    from evolvmem.session_archive import SessionArchiver
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "assessment-direct", json.dumps({"messages": [{"role": "user", "content": TEXT}]}))
    item = confirmed_context(service, "这是生成摘要，不能作为旧知识的逐字引文。")
    with service.store.transaction():
        service.store.record_session_source(item.id, archive.id, extraction_version="fixture")
    source = CapabilitySourceDraft(source_kind="context", source_key=f"context:{item.id}",
        source_ref=f"context_item:{item.id}", context_item_id=item.id,
        source_revision=context_revision(service.store._connection(), item.id),
        projects=("evo",), evidence_status="confirmed")
    ident = CapabilityStore(service.store).upsert(
        CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT), source=source)
    prepared = plan(service)
    assert prepared["entries"][0]["original_kind"] == "archive_original"
    migration.apply_plan(service, prepared, provider=lambda _: fields())
    saved = assessment.assessments(service.store, ident)[0]
    assert saved["value"] == "reference" and not saved["stale"]
    assert saved["details"]["evidence"][0]["roles"] == ["user"]
    assert saved["verification_status"] == "unverified"
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE conversation_history SET body='Changed original' WHERE archive_id=?", (archive.id,))
    assert assessment.assessments(service.store, ident)[0]["stale"]


def test_old_expanded_title_requires_independent_support_review(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE capability_memories SET title='Transactions eliminate every production failure' WHERE memory_id=?", (ident,))
    requests = []
    def reviewer(request):
        requests.append(request)
        return {"input_hash": request["input_hash"], "candidate_hash": request["candidate_hash"],
                "supported": False, "reason": "The title expands beyond the source.", "evidence_quotes": [TEXT]}
    migration.apply_plan(service, plan(service), provider=lambda _: fields(), reviewer=reviewer)
    assert len(requests) == 1 and requests[0]["claims"][0].startswith("Transactions eliminate")
    assert assessment.assessments(service.store, ident)[0]["value"] == "review"


def test_plan_current_source_change_before_apply_does_not_invoke_model(service, monkeypatch):
    ident, task, _ = old_unit_memory(service, monkeypatch)
    prepared = plan(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='superseded' WHERE id=?", (task["id"],))
    with pytest.raises(ValueError, match="revision_conflict"):
        migration.apply_plan(service, prepared, provider=lambda _: pytest.fail("stale plan"))
    assert not assessment.assessments(service.store, ident)


def test_context_original_link_identity_change_invalidates_existing_assessment(service):
    source = manual_source(service)
    ident = CapabilityStore(service.store).upsert(
        CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT), source=source)
    migration.apply_plan(service, plan(service), provider=lambda _: fields())
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_sources SET source_ref='different-original' WHERE item_id=?", (source.context_item_id,))
    assert assessment.assessments(service.store, ident)[0]["stale"]
    assert not recall_facts(service.store, [ident])[ident]["recall_eligible"]


def test_source_attached_during_model_call_aborts_before_freezing_legacy_exemption(service):
    source = manual_source(service)
    memories = CapabilityStore(service.store)
    ident = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT), source=source)
    prepared = plan(service)
    added = manual_source(service, title="后来加入的独立原件")
    def provider(_):
        memories.bind_source(ident, added)
        return fields()
    with pytest.raises(ValueError, match="revision_conflict"):
        migration.apply_plan(service, prepared, provider=provider)
    assert not assessment.assessments(service.store, ident)


def shard_fixture(service):
    memories = CapabilityStore(service.store)
    for identity in (2, 9, 1003, 2**40):
        with service.store.transaction():
            service.store._connection().execute(
                "INSERT INTO capability_memories(memory_id,category,title,content,fingerprint,created_at,updated_at) "
                "VALUES(?,'database','SQLite',?,?,'old','old')", (identity, TEXT, f"legacy-{identity}"))
        memories.bind_source(identity, manual_source(service, title=f"原件{identity}"))
    memories.bind_source(9, manual_source(service, title="同一旧memory的第二来源"))
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO capability_memories(memory_id,category,title,content,fingerprint,created_at,updated_at) "
            "VALUES(77,'database','SQLite',?,'unbound-legacy','old','old')", (TEXT,))
    return plan(service)


def test_partition_is_disjoint_complete_and_uses_sparse_memory_ids(service):
    base = shard_fixture(service)
    before = copy.deepcopy(base)
    partitions = [migration.partition_plan(base, shards=2, shard_index=i) for i in range(2)]
    assert base == before
    assert migration.partition_plan(base) == base
    assert partitions == [migration.partition_plan(base, shards=2, shard_index=i) for i in range(2)]
    identities = [{e["memory_id"] for e in p["coverage"]} for p in partitions]
    assert identities == [{2, 2**40}, {9, 1003, 77}]
    assert not identities[0] & identities[1]
    for field in ("entries", "blocked", "coverage"):
        assert sorted(partitions[0][field] + partitions[1][field], key=lambda e: (e["memory_id"], e.get("source_id", 0))) == base[field]
    assert sum(p["summary"]["sources"] for p in partitions) == base["summary"]["sources"]
    assert sum(p["summary"]["blocked_sources"] for p in partitions) == base["summary"]["blocked_sources"]
    for index, child in enumerate(partitions):
        assert child["partition"]["parent_plan_sha256"] == base["plan_sha256"]
        assert child["partition"]["shard_index"] == index
        assert child["plan_sha256"] != base["plan_sha256"]
        assert all(entry["memory_id"] % 2 == index for entry in child["entries"])
        for entry in child["entries"]:
            assert entry == next(e for e in base["entries"] if e["source_id"] == entry["source_id"])
    assert len([e for e in partitions[1]["entries"] if e["memory_id"] == 9]) == 2


@pytest.mark.parametrize("shards,index", [(0, 0), (-2, 0), (65, 0), (True, 0), (2.0, 0), (2, -1), (2, 2), (2, True)])
def test_partition_rejects_invalid_selection(service, shards, index):
    with pytest.raises(ValueError, match="invalid_assessment_partition"):
        migration.partition_plan(plan(service), shards=shards, shard_index=index)


def test_partition_cannot_be_repartitioned_or_accept_a_tampered_parent(service):
    base = shard_fixture(service)
    child = migration.partition_plan(base, shards=2, shard_index=0)
    with pytest.raises(ValueError, match="already_partitioned"):
        migration.partition_plan(child, shards=2, shard_index=1)
    base["entries"][0]["memory_hash"] = "changed"
    with pytest.raises(ValueError, match="invalid_assessment_migration_plan"):
        migration.partition_plan(base, shards=2, shard_index=0)


def test_shards_benchmark_and_resume_keep_original_cas_and_all_sources(service):
    base = shard_fixture(service)
    shards = [migration.partition_plan(base, shards=2, shard_index=i) for i in range(2)]
    calls = []
    # Benchmark uses the final shard hash, never the unpartitioned parent.
    first = migration.apply_plan(service, shards[0], limit=1, provider=lambda p: calls.append(p) or fields())
    assert first["applied"] == 1
    migration.apply_plan(service, shards[1], provider=lambda p: calls.append(p) or fields())
    migration.apply_plan(service, shards[0], provider=lambda p: calls.append(p) or fields())
    assert len(calls) == len(base["entries"])
    for shard in shards:
        receipt = migration.apply_plan(service, shard, provider=lambda _: pytest.fail("must resume"))
        assert receipt["applied"] == receipt["remaining"] == 0
    assert len(assessment.assessments(service.store, 9)) == 2


def test_empty_shard_is_valid_and_changed_source_still_aborts(service):
    source = manual_source(service)
    ident = CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category="database", title="SQLite", content=TEXT), source=source)
    base = plan(service)
    empty = migration.partition_plan(base, shards=2, shard_index=(ident + 1) % 2)
    assert migration.apply_plan(service, empty, provider=lambda _: pytest.fail("empty shard"))["remaining"] == 0
    target = migration.partition_plan(base, shards=2, shard_index=ident % 2)
    with service.store.transaction():
        service.store._connection().execute("UPDATE capability_sources SET revision=revision+1 WHERE memory_id=?", (ident,))
    with pytest.raises(ValueError, match="revision_conflict"):
        migration.apply_plan(service, target, provider=lambda _: pytest.fail("stale shard"))


def test_partition_cli_reads_only_frozen_plan_without_database_or_model(service, tmp_path, monkeypatch):
    base = shard_fixture(service)
    saved, output = tmp_path / "base.json", tmp_path / "shard.json"
    saved.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["assessment", "partition", "--plan", str(saved),
                                   "--shards", "2", "--shard-index", "1", "--output", str(output)])
    monkeypatch.setattr(migration.sqlite3, "connect", lambda *a, **k: pytest.fail("partition must not open a DB"))
    migration.main()
    assert json.loads(output.read_text()) == migration.partition_plan(base, shards=2, shard_index=1)


def test_rehashed_wrong_shard_membership_is_rejected_before_apply(service):
    base = shard_fixture(service)
    child = migration.partition_plan(base, shards=2, shard_index=0)
    child["entries"].append(next(e for e in base["entries"] if e["memory_id"] == 9))
    child["plan_sha256"] = migration._plan_hash(child)
    with pytest.raises(ValueError, match="invalid_assessment_partition"):
        migration.apply_plan(service, child, provider=lambda _: pytest.fail("wrong shard"))


@pytest.mark.parametrize("invalid,error", [
    ({**fields(), "kind": "unsupported_kind"}, "invalid_capability_kind"),
    ({**fields(), "action_steps": "not an array"}, "invalid_capability_action_steps"),
    ({**fields(), "extra": "untrusted field"}, "invalid_existing_assessment_fields"),
    (fields(quote="invented quote"), "capability_quote_not_in_source"),
    ('{"kind":', "task_invalid_response"),
])
def test_one_invalid_model_response_is_corrected_with_same_original(service, monkeypatch, invalid, error):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    before = memory_row(service, ident)
    prompts = []
    def provider(prompt):
        prompts.append(prompt)
        return invalid if len(prompts) == 1 else fields()
    migration.apply_plan(service, plan(service), provider=provider)
    assert len(prompts) == 2
    assert '"kind": "fact"' in prompts[0] and '"evidence_quotes": []' in prompts[0]
    assert error in prompts[1]
    assert prompts[0].split("\n固定旧知识：", 1)[1] == prompts[1].split("\n固定旧知识：", 1)[1]
    assert memory_row(service, ident) == before
    saved = assessment.assessments(service.store, ident)[0]
    assert saved["value"] == "reference" and saved["verification_status"] == "unverified"
    assert saved["details"]["model_response"]["attempts"] == 2
    assert saved["details"]["model_response"]["errors"] == [error]
    assert saved["details"]["original_proof"]["ready"]
    assert saved["details"]["structure"]["evidence_quotes"] == [TEXT]


def test_second_invalid_response_records_technical_review_and_continues_other_source(service):
    memories = CapabilityStore(service.store)
    ident = memories.upsert(CapabilityMemoryDraft(category="database", title="SQLite", content=TEXT),
                            source=manual_source(service, title="first original"))
    memories.bind_source(ident, manual_source(service, title="second original"))
    before = memory_row(service, ident)
    prepared = plan(service)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        return fields(quote="invented quote") if len(calls) <= 2 else fields()
    result = migration.apply_plan(service, prepared, provider=provider)
    assert len(calls) == 3 and result["applied"] == 2 and result["remaining"] == 0
    assert result["values"] == {"review": 1, "reference": 1}
    assert memory_row(service, ident) == before
    saved = assessment.assessments(service.store, ident)
    failed = next(row for row in saved if row["source_id"] == prepared["entries"][0]["source_id"])
    assert failed["value"] == "review" and failed["verification_status"] == "unverified"
    details = failed["details"]
    assert details["reasons"] == ["model_response_invalid"]
    assert details["score"] == 0 and details["evidence"] == []
    assert details["structure"] == {"kind": "fact", "trigger": "", "conditions": [], "action_steps": [],
        "result": "", "evidence_quotes": [], "value_reason": ""}
    assert details["original_proof"]["ready"] and details["original_proof"]["input_hash"]
    assert details["model_response"] == {"stage": "extraction", "attempts": 2,
        "errors": ["capability_quote_not_in_source", "capability_quote_not_in_source"],
        "error_code": "capability_quote_not_in_source"}
    assert [row["state"] for row in migration._audit(service.store._connection(), prepared)] == ["done", "done"]
    assert migration.apply_plan(service, prepared, provider=lambda _: pytest.fail("done sources must not re-call"))["applied"] == 0


@pytest.mark.parametrize("failure", [TimeoutError("provider timed out"), RuntimeError("provider unavailable"),
                                    ValueError("unrecognized_runtime_code")])
def test_transport_and_runtime_failures_abort_without_model_correction(service, monkeypatch, failure):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        raise failure
    with pytest.raises(type(failure), match=str(failure)):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 1 and not assessment.assessments(service.store, ident)


@pytest.mark.parametrize("changed_attempt", [1, 2])
def test_source_cas_aborts_after_invalid_response_before_correction_or_review(service, monkeypatch, changed_attempt):
    ident, task, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        if len(calls) == changed_attempt:
            with service.store.transaction():
                service.store._connection().execute(
                    "UPDATE organization_units SET revision=revision+1 WHERE task_id=?", (task["id"],))
        return fields(quote="invented quote")
    with pytest.raises(ValueError, match="assessment_migration_revision_conflict"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == changed_attempt and not assessment.assessments(service.store, ident)


def test_lost_lease_aborts_before_correction_call(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE existing_capability_assessment_runs SET lease_token='replacement-worker'")
        return fields(quote="invented quote")
    with pytest.raises(ValueError, match="assessment_migration_lease_conflict"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 1 and not assessment.assessments(service.store, ident)


def test_support_transport_error_is_not_mislabeled_unsupported_knowledge(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch, content="Transactions safely cover this batch.")
    calls = []
    def provider(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return fields()
        raise TimeoutError("support provider timed out")
    with pytest.raises(TimeoutError, match="support provider timed out"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 2 and not assessment.assessments(service.store, ident)


def test_persistence_validation_error_is_not_retried_as_a_model_error(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def fail_write(*args, **kwargs):
        raise ValueError("invalid_capability_kind")
    monkeypatch.setattr(assessment, "persist", fail_write)
    with pytest.raises(ValueError, match="invalid_capability_kind"):
        migration.apply_plan(service, plan(service), provider=lambda p: calls.append(p) or fields())
    assert len(calls) == 1 and not assessment.assessments(service.store, ident)


def test_transport_error_during_correction_is_not_converted_to_review(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return fields(quote="invented quote")
        raise TimeoutError("correction provider timed out")
    with pytest.raises(TimeoutError, match="correction provider timed out"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 2 and not assessment.assessments(service.store, ident)


def test_invalid_support_response_is_technical_review_not_unsupported_knowledge(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch, content="Transactions safely cover this batch.")
    calls = []
    def provider(prompt):
        calls.append(prompt)
        return fields() if len(calls) == 1 else {}
    result = migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 2 and result["values"] == {"review": 1}
    saved = assessment.assessments(service.store, ident)[0]
    assert saved["verification_status"] == "unverified"
    assert saved["details"]["reasons"] == ["model_response_invalid"]
    assert saved["details"]["model_response"]["stage"] == "support"
    assert saved["details"]["model_response"]["error_code"] == "invalid_support_review"
    assert saved["details"]["evidence"] == []
    assert saved["details"]["original_proof"]["ready"]


def test_support_model_time_source_change_aborts_even_with_invalid_review(service, monkeypatch):
    ident, task, _ = old_unit_memory(service, monkeypatch, content="Transactions safely cover this batch.")
    calls = []
    def provider(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return fields()
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_units SET revision=revision+1 WHERE task_id=?", (task["id"],))
        return {}
    with pytest.raises(ValueError, match="assessment_migration_revision_conflict"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 2 and not assessment.assessments(service.store, ident)


def test_expired_lease_aborts_without_model_correction(service, monkeypatch):
    ident, _, _ = old_unit_memory(service, monkeypatch)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE existing_capability_assessment_runs SET lease_until=0")
        return fields(quote="invented quote")
    with pytest.raises(ValueError, match="assessment_migration_lease_conflict"):
        migration.apply_plan(service, plan(service), provider=provider)
    assert len(calls) == 1 and not assessment.assessments(service.store, ident)
