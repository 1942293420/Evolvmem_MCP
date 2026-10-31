import json

from evolvmem import ownership_reasoning
from tests.test_auto_organization import base_service, service


def test_vector_candidate_cannot_supply_the_units_ownership_evidence(service, monkeypatch):
    unit = {"task_id": 0, "digest": "candidate", "text": "这个入口需要优化。", "title": "入口优化"}
    recalled = {"version": "test", "registry": [{"project": "evo"}], "fallback": False,
                "candidates": [{"project": "evo", "score": 0.99,
                    "evidence": [{"origin": "context_vector", "ref": "context:77", "quote": "evo负责采购入口。"}]}]}
    monkeypatch.setattr("evolvmem.ownership_candidates.recall", lambda *a, **k: recalled)
    def call(prompt, *a, **kw):
        assert "候选线索" in prompt and "context:77" in prompt
        return json.dumps({"items": [{"id": 0, "decision": "include", "reason": "依据来源", "quote": unit["text"],
            "chain": {"change": "evo负责采购入口。"},
            "targets": [{"project": "evo", "role": "owner", "quote": "evo负责采购入口。"}]}]})
    monkeypatch.setattr("evolvmem.auto_organization._load_llm", lambda: ({}, call))
    result = ownership_reasoning.analyze(service, [unit])["0:candidate"]
    assert result["decision"] == "uncertain"
    assert result["projects"] == []


def test_candidate_failure_falls_back_without_losing_registered_projects(service, monkeypatch):
    calls = []
    def failed(*a, **k):
        calls.append(True)
        raise RuntimeError("embedding unavailable")
    monkeypatch.setattr("evolvmem.ownership_candidates.recall", failed)
    text = "dsh 增加执行超时保护。"
    unit = {"task_id": 0, "digest": "fallback", "text": text}
    def provider(prompt, *a, **kw):
        assert '"project": "dsh"' in prompt
        return json.dumps({"items": [{"id": 0, "decision": "include", "reason": "依据原文的实际改动", "quote": text,
            "chain": {"change": text}, "targets": [{"project": "dsh", "role": "owner", "quote": text}]}]})
    monkeypatch.setattr("evolvmem.auto_organization._load_llm", lambda: ({}, provider))
    result = ownership_reasoning.analyze(service, [unit])["0:fallback"]
    assert calls == [True]
    assert result["projects"] == ["dsh"]


def test_targeted_review_does_not_need_candidate_search(service, monkeypatch):
    monkeypatch.setattr("evolvmem.ownership_candidates.recall", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unneeded recall")))
    text = "dsh 增加执行超时保护。"
    unit = {"task_id": 0, "digest": "review", "text": text}
    monkeypatch.setattr("evolvmem.auto_organization._load_llm", lambda: ({}, lambda *a, **kw: json.dumps({"items": [{"id": 0, "decision": "include", "reason": "依据原文的实际改动", "quote": text,
        "chain": {"change": text}, "targets": [{"project": "dsh", "role": "owner", "quote": text}]}]})))
    assert ownership_reasoning.analyze(service, [unit], target={"project": "dsh"})["0:review"]["projects"] == ["dsh"]
