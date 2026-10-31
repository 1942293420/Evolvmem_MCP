import json

import pytest

from evolvmem.capability_support_review import configured_reviewer
from evolvmem.capability_assessment import _hash, POLICY_VERSION
from evolvmem.context_store import ContextStore
from tests.test_capability_pipeline import service_with_task


def test_support_review_is_separate_and_cached_with_both_hashes(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        unit = dict(store._connection().execute("SELECT * FROM organization_units").fetchone())
        request = {"input_hash": _hash([unit["text"], []]), "candidate_hash": _hash(["表单校验", "蓝鲸前端新增表单校验"]),
                   "source_text": unit["text"], "source_roles": [],
                   "claims": ["表单校验", "蓝鲸前端新增表单校验"]}
        calls = []
        def provider(prompt):
            calls.append(prompt)
            data = json.loads(prompt.split("待审阅：")[1])
            return {"input_hash": data["input_hash"], "candidate_hash": data["candidate_hash"],
                    "supported": True, "reason": "仅忠实概括原文。", "evidence_quotes": [unit["text"]]}
        reviewer = configured_reviewer(service, task, unit, provider)
        assert reviewer(request)["supported"]
        assert reviewer(request)["supported"]
        assert len(calls) == 1
        reviewer({**request, "candidate_hash": "claims2"})
        assert len(calls) == 2


def test_wrong_hash_or_stale_source_never_caches_support(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        unit = dict(store._connection().execute("SELECT * FROM organization_units").fetchone())
        request = {"input_hash": _hash([unit["text"], []]), "candidate_hash": _hash(["表单校验"]),
                   "source_text": unit["text"], "source_roles": [], "claims": ["表单校验"]}
        def provider(_):
            return {"input_hash": "wrong", "candidate_hash": "claims1", "supported": True, "reason": "支持", "evidence_quotes": [unit["text"]]}
        with pytest.raises(ValueError, match="invalid_support_review"):
            configured_reviewer(service, task, unit, provider)(request)
        assert store._connection().execute("SELECT count(*) FROM extraction_receipts WHERE payload IS NOT NULL").fetchone()[0] == 0
        with store.transaction():
            store._connection().execute("UPDATE organization_units SET disposition='set_aside',revision=revision+1")
        with pytest.raises(ValueError, match="revision_conflict"):
            configured_reviewer(service, task, unit, provider)(request)


def test_title_policy_never_reuses_a_v1_success_receipt(test_config):
    from evolvmem.pipeline_state import extraction_payload

    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        unit = dict(store._connection().execute("SELECT * FROM organization_units").fetchone())
        request = {"input_hash": _hash([unit["text"], []]),
                   "candidate_hash": _hash(["表单校验能消除所有生产故障", unit["text"]]),
                   "source_text": unit["text"], "source_roles": [],
                   "claims": ["表单校验能消除所有生产故障", unit["text"]]}
        legacy = {"input_hash": request["input_hash"], "candidate_hash": request["candidate_hash"],
                  "supported": True, "reason": "旧规则未检查标题。", "evidence_quotes": [unit["text"]]}
        old_key = "capability-support:assessment-policy:v1:" + request["input_hash"] + ":" + request["candidate_hash"]
        extraction_payload(service, task, unit, old_key, lambda: legacy)
        calls = []
        def reviewer(prompt):
            current = json.loads(prompt.split("待审阅：")[1])
            calls.append(current)
            return {"input_hash": current["input_hash"], "candidate_hash": current["candidate_hash"],
                    "supported": False, "reason": "标题扩大了原文效果。", "evidence_quotes": []}
        response = configured_reviewer(service, task, unit, reviewer)(request)
        assert response["supported"] is False
        assert calls == [request]
        assert POLICY_VERSION != "assessment-policy:v1"
        pipelines = {row[0] for row in store._connection().execute("SELECT pipeline FROM extraction_receipts")}
        assert old_key in pipelines
        assert "capability-support:" + POLICY_VERSION + ":" + request["input_hash"] + ":" + request["candidate_hash"] in pipelines


@pytest.mark.parametrize("changed,field,code", [
    ({"supported": "true"}, "supported", "boolean_required"),
    ({"input_hash": "PRIVATE rejected hash"}, "input_hash", "hash_mismatch"),
    ({"candidate_hash": "PRIVATE rejected hash"}, "candidate_hash", "hash_mismatch"),
    ({"reason": []}, "reason", "string_required"),
    ({"reason": " "}, "reason", "length_out_of_range"),
    ({"reason": "PRIVATE" * 150}, "reason", "length_out_of_range"),
    ({"evidence_quotes": "PRIVATE rejected quote"}, "evidence_quotes", "array_required"),
    ({"evidence_quotes": ["PRIVATE"] * 21}, "evidence_quotes", "too_many_items"),
    ({"evidence_quotes": [None]}, "evidence_quotes", "string_required"),
    ({"evidence_quotes": [" "]}, "evidence_quotes", "empty_quote"),
    ({"evidence_quotes": ["PRIVATE rejected quote"]}, "evidence_quotes", "quote_not_in_source"),
    ({"supported": True, "evidence_quotes": []}, "evidence_quotes", "support_requires_quotes"),
])
def test_rejected_support_has_field_diagnostics_without_rejected_values(changed, field, code):
    from evolvmem.capability_support_review import _validate
    request = {"input_hash": "input", "candidate_hash": "candidate"}
    response = {**request, "supported": False, "reason": "依据不足", "evidence_quotes": []}
    with pytest.raises(ValueError) as caught:
        _validate({**response, **changed}, request, "真实原文")
    assert str(caught.value) == "invalid_support_review"
    diagnostics = caught.value.diagnostics
    assert any(d["field"] == field and d["code"] == code for d in diagnostics)
    assert "PRIVATE" not in json.dumps(diagnostics, ensure_ascii=False)


def test_support_prompt_contract_matches_validated_bounds_and_false_is_not_retried():
    text = "原文"
    roles = ()
    request = {"input_hash": _hash([text, roles]), "candidate_hash": "fixed-candidate",
               "source_text": text, "source_roles": roles, "claims": ["不能证明的效果"]}
    calls = []
    def provider(prompt):
        calls.append(prompt)
        contract = json.loads(prompt.split("输出约束：", 1)[1].split("\n待审阅：", 1)[0])
        assert contract["reason"] == {"type": "string", "trimmed_min_length": 1, "trimmed_max_length": 1000}
        assert contract["supported"] == {"type": "boolean"}
        assert contract["input_hash"] == {"equals": request["input_hash"]}
        assert contract["candidate_hash"] == {"equals": request["candidate_hash"]}
        assert contract["evidence_quotes"]["max_items"] == 20
        assert contract["evidence_quotes"]["supported_true_min_items"] == 1
        return {"input_hash": request["input_hash"], "candidate_hash": request["candidate_hash"],
                "supported": False, "reason": "x" * 1000, "evidence_quotes": [text] * 20}
    reviewer = configured_reviewer(None, source_text=text, source_roles=roles,
                                   check_current=lambda: None, provider=provider)
    result = reviewer(request)
    assert result["supported"] is False and len(calls) == 1


def test_non_object_support_response_diagnostic_has_no_payload():
    from evolvmem.capability_support_review import _validate
    with pytest.raises(ValueError) as caught:
        _validate(["PRIVATE"], {"input_hash": "input", "candidate_hash": "candidate"}, "text")
    assert caught.value.diagnostics == [{"field": "response", "code": "object_required"}]
