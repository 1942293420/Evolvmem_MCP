from types import SimpleNamespace

import pytest

from evolvmem.capability_retriever import CapabilityRetriever
from evolvmem.capability_store import CapabilityStore
from evolvmem.project_module_pipeline import ProjectModulePipeline, candidates
from tests.test_project_modules import modules, create, bind
from tests.test_capability_provenance import provenance_db, add_memory, unit_source


def task(modules):
    return dict(modules.conn.execute('SELECT * FROM organization_tasks WHERE id=1').fetchone())


def judgment(module):
    return {'items': [{'module_id': module['module_id'], 'decision': 'include', 'role': 'owner',
                       'quote': 'user: a fact', 'chain': {'change': 'user: a fact'}}]}


def test_registered_module_judgment_is_cached_idempotent_and_recallable(modules):
    module = create(modules)
    service = SimpleNamespace(store=modules.store)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        return judgment(module)
    pipeline = ProjectModulePipeline(service)
    assert pipeline.process_task(task(modules), provider)['bindings'] == 1
    assert pipeline.process_task(task(modules), provider)['bindings'] == 0
    assert len(calls) == 1
    memory = add_memory(modules.store, source=unit_source(modules.store))
    hits = CapabilityRetriever(service).search('Fact', project='alpha', module_id=module['module_id'])
    assert hits[0]['memory_id'] == memory and hits[0]['modules'][0]['module_id'] == module['module_id']
    block, _ = CapabilityRetriever(service).render('Fact', max_chars=1000)
    assert '业务模块：alpha/Order handling' in block
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET project='beta',projects='[\"beta\"]',revision=2")
    assert CapabilityRetriever(service).search('Fact', project='alpha', module_id=module['module_id']) == []
    assert CapabilityRetriever(service).search('Fact', project='beta')[0]['modules'] == []


def test_empty_or_unconfirmed_registry_does_not_call_model(modules):
    pipeline = ProjectModulePipeline(SimpleNamespace(store=modules.store))
    def unexpected(_):
        raise AssertionError('must not call model')
    assert pipeline.process_task(task(modules), unexpected)['status'] == 'modules_unconfigured'
    create(modules)
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET decision='review'")
    assert pipeline.process_task(task(modules), unexpected)['bindings'] == 0


def test_manual_binding_and_withdrawal_are_never_replaced(modules):
    module = create(modules)
    manual = bind(modules, module, decision='manual', explicit=True, chain={})
    modules.unbind(manual['binding_id'], expected_revision=1, explicit=True)
    def unexpected(_):
        raise AssertionError('manual origin is protected')
    assert ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(task(modules), unexpected)['bindings'] == 0


@pytest.mark.parametrize('change', [
    {'quote': 'fabricated'}, {'chain': {}}, {'role': 'related'}, {'module_id': 'mod_unknown'},
    {'role': []}, {'role': {}}, {'decision': []}, {'module_id': []}])
def test_candidate_cannot_publish_without_current_source_role_and_chain(modules, change):
    module = create(modules)
    value = judgment(module)
    value['items'][0].update(change)
    calls = []
    result = ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(
        task(modules), lambda prompt: calls.append(prompt) or value)
    assert len(calls) == 2 and result["technical_review"] == 1 and result["review"] == 1
    assert modules.conn.execute('SELECT count(*) FROM project_module_bindings').fetchone()[0] == 0


def test_model_cannot_race_manual_registry_change(modules):
    module = create(modules)
    def provider(_):
        modules.deactivate(module['module_id'], expected_revision=1, explicit=True)
        return judgment(module)
    with pytest.raises(ValueError, match='module_registry_changed'):
        ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(task(modules), provider)
    assert modules.conn.execute('SELECT count(*) FROM project_module_bindings').fetchone()[0] == 0


def test_vector_candidates_use_only_current_eligible_source_and_never_bind(modules):
    module = create(modules)
    bind(modules, module)
    memory = add_memory(modules.store, source=unit_source(modules.store))
    service = SimpleNamespace(store=modules.store, capability_index=lambda: SimpleNamespace(
        search=lambda *a: [{'memory_id': memory, 'vector_score': .9}]))
    before = modules.conn.total_changes
    result = candidates(service, 'alpha', 'semantic unrelated tokens')
    assert result['items'][0]['module_id'] == module['module_id']
    assert result['items'][0]['vector_score'] == .9 and not result['binding_authorized']
    assert modules.conn.total_changes == before
    with modules.store.transaction():
        modules.conn.execute("UPDATE capability_memories SET status='pending'")
    assert candidates(service, 'alpha', 'semantic unrelated tokens')['items'] == []


def test_revised_registry_can_retract_automatic_binding_without_deletion(modules):
    module = create(modules)
    pipeline = ProjectModulePipeline(SimpleNamespace(store=modules.store))
    assert pipeline.process_task(task(modules), lambda _: judgment(module))['bindings'] == 1
    modules.update(module['module_id'], expected_revision=1, description='Narrowed business scope', explicit=True)
    pipeline.process_task(task(modules), lambda _: {'items': [{'module_id': module['module_id'], 'decision': 'exclude'}]})
    assert modules.conn.execute('SELECT status FROM project_module_bindings').fetchone()[0] == 'inactive'
    assert modules.conn.execute("SELECT count(*) FROM project_module_audit WHERE action='auto_retract'").fetchone()[0] == 1


def test_before_publish_fence_runs_inside_transaction_for_new_and_cached_payloads(modules):
    module = create(modules)
    pipeline = ProjectModulePipeline(SimpleNamespace(store=modules.store))
    phases = []
    def fence():
        phases.append(modules.conn.in_transaction)
        raise ValueError("lease_lost")
    with pytest.raises(ValueError, match="lease_lost"):
        pipeline.process_task(task(modules), lambda _: judgment(module), before_publish=fence)
    assert modules.conn.execute("SELECT count(*) FROM project_module_bindings").fetchone()[0] == 0
    assert modules.conn.execute("SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 1
    with pytest.raises(ValueError, match="lease_lost"):
        pipeline.process_task(task(modules), lambda _: pytest.fail("cached response"), before_publish=fence)
    assert modules.conn.execute("SELECT count(*) FROM project_module_bindings").fetchone()[0] == 0
    assert phases == [True, True]


def test_vector_candidates_do_not_expand_an_implicit_source_allowlist(modules):
    from evolvmem.capability_recall_eligibility import recall_facts
    from tests.test_capability_legacy_sources import weak_binding
    from tests.test_capability_provenance import context_source, DIGEST

    module = create(modules)
    bind(modules, module)
    memory = add_memory(modules.store)
    with modules.store.transaction():
        modules.conn.execute(
            "INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')",
            (str(memory),))
        modules.conn.execute(
            "INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,"
            "review_state,resolved_project,created_at,updated_at) "
            "VALUES(1,'resolved','human','accepted','alpha','old','old')")
    # Only this old independent Context origin is preserved when source 2 fails
    # value review. The unit origin is added later and has no such permission.
    weak_binding(modules.store, memory, context_source(modules.store, identity=2))
    with modules.store.transaction():
        modules.conn.execute("UPDATE capability_memories SET source_ref=? WHERE memory_id=?",
                             (f"archive:1#{DIGEST}@0-12", memory))
    fact = recall_facts(modules.store, [memory])[memory]
    assert fact["recall_eligible"] and fact["source_projects"] == ["alpha"]
    assert fact["eligible_source_ids"] == [None]
    assert [source["source_key"] for source in fact["eligible_sources"]] == ["context:1"]
    assert any(source["valid"] and source["source_kind"] == "unit"
               and source.get("source_id") is None for source in fact["sources"])

    service = SimpleNamespace(store=modules.store, capability_index=lambda: SimpleNamespace(
        search=lambda *a: [{"memory_id": memory, "vector_score": .9}]))
    before = modules.conn.total_changes
    result = candidates(service, "alpha", "semantic unrelated tokens")
    assert result["diagnostics"] == []
    assert result["items"] == []
    assert not result["binding_authorized"] and modules.conn.total_changes == before


def test_bad_chain_is_corrected_once_without_discarding_unsupported_fields(modules):
    module = create(modules)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        value = judgment(module)
        if len(calls) == 1:
            value["items"][0]["chain"]["need"] = "user: a fact"
            value["items"][0]["chain"]["change"] = "a paraphrase absent from the source"
        return value
    result = ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(task(modules), provider)
    assert len(calls) == 2 and result["bindings"] == 1 and result["technical_review"] == 0
    assert "module_role_evidence_required" in calls[1]
    assert calls[0].split("\n登记模块：", 1)[1] == calls[1].split("\n登记模块：", 1)[1]
    assert "a paraphrase absent from the source" not in calls[1]


def test_two_bad_module_responses_preserve_existing_binding_and_cache_technical_review(modules):
    import json
    module = create(modules)
    original = bind(modules, module)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        value = judgment(module)
        value["items"][0]["chain"]["change"] = "invented evidence"
        return value
    pipeline = ProjectModulePipeline(SimpleNamespace(store=modules.store))
    result = pipeline.process_task(task(modules), provider)
    assert len(calls) == 2
    assert result["technical_review"] == 1 and result["review"] == 1 and result["bindings"] == 0
    current = dict(modules.conn.execute(
        "SELECT * FROM project_module_bindings WHERE binding_id=?", (original["binding_id"],)).fetchone())
    assert current == original and modules.binding_status(original["binding_id"])["valid"]
    payload = json.loads(modules.conn.execute("SELECT payload FROM extraction_receipts").fetchone()[0])
    assert payload["items"] == [{"module_id": module["module_id"], "decision": "review"}]
    assert payload["technical_review"] == {"reason": "model_response_invalid", "attempts": 2,
        "errors": ["module_role_evidence_required", "module_role_evidence_required"],
        "error_code": "module_role_evidence_required"}
    assert "invented evidence" not in json.dumps(payload)
    repeated = pipeline.process_task(task(modules), lambda _: pytest.fail("technical review is cached"))
    assert repeated["technical_review"] == 1 and repeated["bindings"] == 0
    assert modules.conn.execute("SELECT count(*) FROM project_module_audit WHERE action='auto_retract'").fetchone()[0] == 0


@pytest.mark.parametrize("when", [1, 2])
def test_module_transport_failure_aborts_without_technical_review(modules, when):
    module = create(modules)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        if len(calls) == when:
            raise TimeoutError("provider unavailable")
        return {"items": []}
    with pytest.raises(TimeoutError, match="provider unavailable"):
        ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(task(modules), provider)
    assert len(calls) == when
    assert modules.conn.execute("SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 0
    assert modules.conn.execute("SELECT count(*) FROM project_module_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("change,error", [
    ("source", "revision_conflict"), ("registry", "module_registry_changed"),
    ("lease", "extraction_lease_conflict"), ("expiry", "extraction_lease_conflict"),
])
def test_module_correction_checks_source_registry_and_own_receipt_lease(modules, change, error):
    module = create(modules)
    calls = []
    def provider(prompt):
        calls.append(prompt)
        with modules.store.transaction():
            if change == "source":
                modules.conn.execute("UPDATE organization_units SET revision=revision+1")
            elif change == "registry":
                modules.update(module["module_id"], expected_revision=1, description="changed", explicit=True)
            elif change == "lease":
                modules.conn.execute("UPDATE extraction_receipts SET lease_token='new-worker'")
            else:
                modules.conn.execute("UPDATE extraction_receipts SET lease_until=0")
        return {"items": []}
    with pytest.raises(ValueError, match=error):
        ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(task(modules), provider)
    assert len(calls) == 1
    assert modules.conn.execute("SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 0
    assert modules.conn.execute("SELECT count(*) FROM project_module_bindings").fetchone()[0] == 0


def test_provider_cannot_supply_server_technical_review_metadata(modules):
    module = create(modules)
    value = judgment(module)
    value["technical_review"] = {"reason": "model_response_invalid", "attempts": 2}
    calls = []
    result = ProjectModulePipeline(SimpleNamespace(store=modules.store)).process_task(
        task(modules), lambda prompt: calls.append(prompt) or value)
    assert len(calls) == 2 and result["technical_review"] == 1 and result["bindings"] == 0
