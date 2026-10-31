"""Extract development memories independently of project ownership."""
from __future__ import annotations

import hashlib
import json
import time

from evolvmem import capability_assessment
from evolvmem.capability_models import CAPABILITY_CATEGORIES
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.context_store import _now_iso
from evolvmem.pipeline_state import assert_unit_current, cached_payload, extraction_payload, unit_scope, validate_occurred_at
from evolvmem.task_segmentation import decode


class _ResponseDecodeError(ValueError):
    """Only a returned payload failed JSON decoding; provider errors stay intact."""


def _call_provider(prompt, provider=None):
    if provider is None:
        from evolvmem.auto_organization import _load_llm
        config, call = _load_llm()
        provider = lambda text: call(text, config, deadline=time.monotonic() + 90)
    response = provider(prompt)
    if not isinstance(response, (dict, str)):
        raise _ResponseDecodeError("task_invalid_response")
    if isinstance(response, str) and response.strip().startswith("`" * 3) and "\n" not in response.strip():
        raise _ResponseDecodeError("task_invalid_response")
    try:
        return response if isinstance(response, dict) else decode(response)
    except ValueError as error:
        if str(error) != "task_invalid_response":
            raise
        raise _ResponseDecodeError("task_invalid_response") from None


def _units(service, task) -> list[dict]:
    return [dict(row) for row in service.store._connection().execute(
        "SELECT * FROM organization_units WHERE task_id=? AND disposition IN ('keep','history_only') ORDER BY ordinal",
        (task["id"],),
    )]


def source_project(service, task, unit) -> str:
    projects = unit_scope(service, unit)["projects"]
    return projects[0] if len(projects) == 1 else ""


def source_ref(task, unit) -> str:
    return f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}"


def _fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


# Only validation of a returned model payload is correctable. Do not broaden
# this to all ValueError: source, lease and storage failures must still abort.
_RESPONSE_FIELDS = {
    "task_invalid_response": ("response",),
    "invalid_capability_action": ("action",),
    "invalid_capability_memory": ("category", "title", "content"),
    "invalid_capability_keywords": ("keywords",),
    "invalid_occurred_at": ("occurred_at",),
    "capability_quote_not_in_source": ("evidence_quotes",),
    **{"invalid_capability_" + name: (name,) for name in
       ("kind", "trigger", "result", "value_reason", "conditions", "action_steps", "evidence_quotes")},
}
_VALUE_TYPES = frozenset({"missing", "null", "boolean", "string", "array", "object", "number", "other"})


def _response_diagnostic(code, value):
    """Describe fixed field types, never model values or exception messages."""
    def field_type(field):
        if not isinstance(value, dict) or field not in value:
            return "missing"
        item = value[field]
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "boolean"
        for kind, label in ((str, "string"), (list, "array"), (dict, "object"), ((int, float), "number")):
            if isinstance(item, kind):
                return label
        return "other"
    return {"error_code": code, "fields": [
        {"field": field, "type": field_type(field)} for field in _RESPONSE_FIELDS[code]]}


def _validate_technical(value):
    note = value.get("technical_review")
    valid = (set(value) == {"action", "technical_review"} and isinstance(note, dict)
             and set(note) == {"reason", "attempts", "errors", "error_code", "diagnostics"}
             and note.get("reason") == "model_response_invalid"
             and type(note.get("attempts")) is int and note["attempts"] == 2
             and isinstance(note.get("errors"), list) and len(note["errors"]) == 2
             and all(isinstance(e, str) and e in _RESPONSE_FIELDS for e in note["errors"])
             and note.get("error_code") == note["errors"][-1]
             and isinstance(note.get("diagnostics"), list) and len(note["diagnostics"]) == 2)
    if valid:
        for code, diagnostic in zip(note["errors"], note["diagnostics"]):
            if (not isinstance(diagnostic, dict) or set(diagnostic) != {"error_code", "fields"}
                    or diagnostic["error_code"] != code or not isinstance(diagnostic["fields"], list)
                    or len(diagnostic["fields"]) != len(_RESPONSE_FIELDS[code])):
                valid = False
                break
            for field, row in zip(_RESPONSE_FIELDS[code], diagnostic["fields"]):
                if (not isinstance(row, dict) or set(row) != {"field", "type"} or row["field"] != field
                        or not isinstance(row["type"], str) or row["type"] not in _VALUE_TYPES):
                    valid = False
                    break
    if not valid:
        raise ValueError("invalid_capability_technical_receipt")


def current_technical_reviews(service, task):
    """Count only current-version receipts; never retry extraction to get status."""
    count = 0
    for unit in _units(service, task):
        if unit["disposition"] != "keep":
            continue
        value = cached_payload(service, task, unit, "capability:v3:" + capability_assessment.POLICY_VERSION)
        if isinstance(value, dict) and value.get("action") == "technical_review":
            _validate_technical(value)
            count += 1
    return count


class CapabilityPipeline:
    def __init__(self, service):
        self.service = service

    def _extract(self, unit, provider, *, correction=None):
        prompt = (
            "你是开发知识整理器。只处理开发任务，不判断项目归属，不判断是否跨项目经验。"
            "过滤闲聊、重复输出、无信息量命令日志和临时过程。保留明确事实、决策、方法、注意事项和解决结果。"
            "每条必须给出kind=fact|decision|procedure|pitfall，trigger触发条件、conditions适用条件数组、"
            "action_steps具体操作数组、result结果原话或空字符串、evidence_quotes原文逐字引用数组、value_reason价值理由。"
            "未知字段用空字符串或空数组，不编造证据。引用不得超出当前单元；content应忠实限定在引用支持范围。"
            "方法有价值不等于已验证，助手自述完成不构成独立验证。不得输出verified或自报分数。"
            "标题与正文都必须保留原文的状态和限制：尚未实施的处理方案标题应写方案或建议，"
            "不得写成已经解决、已防止或已提升等效果结论；标题不能比原文更确定。"
            "分类只能是 frontend、backend、business_logic、database、architecture、quality、delivery；"
            "不属于现有分类时返回原建议名称，系统会进入待分类，不得强行归类。"
            "只返回JSON：{\"action\":\"keep|ignore\",\"category\":\"分类或建议名称\","
            "\"title\":\"简短标题\",\"content\":\"有依据的记忆正文\",\"keywords\":[\"技术词\"],"
            "\"occurred_at\":\"已知发生时间或null\",\"kind\":\"fact|decision|procedure|pitfall\","
            "\"trigger\":\"触发条件或空\",\"conditions\":[],\"action_steps\":[],\"result\":\"结果原话或空\","
            "\"evidence_quotes\":[],\"value_reason\":\"价值理由\"}。\n原始开发记录：\n" + unit["text"]
        )
        if correction:
            prompt = ("这是唯一一次纠正机会。上次响应校验失败，诊断只描述字段类型："
                      + json.dumps(correction, ensure_ascii=False)
                      + "。请重发完整JSON，保持同一原文与分类规则；引文必须为连续逐字原话，"
                        "不要删除坏引文后冒充有据，不得输出technical_review或编造事实。\n" + prompt)
        return _call_provider(prompt, provider)

    def _save_intake(self, task, unit, candidate) -> int:
        title = str(candidate.get("title", "")).strip()
        content = str(candidate.get("content", "")).strip()
        if not title or not content:
            raise ValueError("invalid_capability_memory")
        now = _now_iso()
        fingerprint = _fingerprint(title, content)
        with self.service.store.transaction():
            self.service.store._connection().execute(
                "INSERT OR IGNORE INTO capability_intake(title,content,keywords,suggested_category,"
                "source_project,source_ref,occurred_at,fingerprint,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (title, content, json.dumps(candidate.get("keywords") or [], ensure_ascii=False),
                 str(candidate.get("category", "")), source_project(self.service, task, unit),
                 source_ref(task, unit), candidate.get("occurred_at"), fingerprint, now, now),
            )
            row = self.service.store._connection().execute(
                "SELECT intake_id FROM capability_intake WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        return int(row[0])

    def _suggest(self, category) -> int:
        store = CapabilityStore(self.service.store)
        existing = self.service.store._connection().execute(
            "SELECT suggestion_id FROM capability_category_suggestions WHERE label=? AND status='pending'",
            (category,),
        ).fetchone()
        if existing:
            return int(existing[0])
        return store.suggest_category(category, "系统发现未归入现有七类的开发记忆")

    def process_task(self, task, provider=None, *, support_reviewer=None) -> dict:
        result = {"memory_ids": [], "pending": 0, "suggestions": [], "technical_review": 0, "unit_outcomes": []}
        capabilities = CapabilityStore(self.service.store)
        for unit in _units(self.service, task):
            # Assistant progress-only units belong to history, not reusable knowledge.
            if unit["disposition"] == "history_only":
                continue
            def validate(candidate, *, allow_technical=True):
                if isinstance(candidate, dict) and candidate.get("action") == "technical_review" and allow_technical:
                    _validate_technical(candidate)
                    return
                if (not isinstance(candidate, dict) or candidate.get("action") not in ("keep", "ignore")
                        or "technical_review" in candidate):
                    raise ValueError("invalid_capability_action")
                if candidate["action"] == "keep":
                    if not all(isinstance(candidate.get(k), str) and candidate[k].strip() for k in ("category", "title", "content")):
                        raise ValueError("invalid_capability_memory")
                    if not isinstance(candidate.get("keywords", []), list) or any(not isinstance(k, str) for k in candidate.get("keywords", [])):
                        raise ValueError("invalid_capability_keywords")
                    validate_occurred_at(candidate.get("occurred_at"))
                    capability_assessment.validate(candidate, unit["text"])
            def extract(check_current):
                errors, diagnostics = [], []
                for attempt in range(2):
                    check_current()
                    response, code = None, None
                    try:
                        response = self._extract(unit, provider, correction=diagnostics[-1] if diagnostics else None)
                    except _ResponseDecodeError:
                        code = "task_invalid_response"
                    else:
                        check_current()
                        try:
                            validate(response, allow_technical=False)
                        except ValueError as error:
                            code = str(error)
                            if code not in _RESPONSE_FIELDS:
                                raise
                    # Provider/transport exceptions are outside the validation
                    # handler even if their message resembles a response code.
                    check_current()
                    if code is None:
                        return response
                    errors.append(code)
                    diagnostics.append(_response_diagnostic(code, response))
                return {"action": "technical_review", "technical_review": {
                    "reason": "model_response_invalid", "attempts": 2, "errors": errors,
                    "error_code": errors[-1], "diagnostics": diagnostics}}

            candidate = extraction_payload(self.service, task, unit, "capability:v3:" + capability_assessment.POLICY_VERSION,
                                           None, validate, guarded_extract=extract)
            if candidate.get("action") == "technical_review":
                # Cached technical outcomes also recheck current manual/source
                # state. They do not publish, retract or evaluate any knowledge.
                with self.service.store.transaction():
                    assert_unit_current(self.service, task, unit)
                    result["technical_review"] += 1
                    result["pending"] += 1
                    result["unit_outcomes"].append({"unit_digest": unit["digest"],
                        "status": "technical_review", **candidate["technical_review"]})
                continue
            if candidate.get("action") == "ignore":
                continue
            effective_reviewer = support_reviewer
            if effective_reviewer is None and provider is None:
                from evolvmem.capability_support_review import configured_reviewer
                effective_reviewer = configured_reviewer(self.service, task, unit)
            assessment = capability_assessment.assess_unit(
                self.service, task, unit, candidate, reviewer=effective_reviewer)
            category = str(candidate.get("category", "")).strip()
            if category not in CAPABILITY_CATEGORIES:
                with self.service.store.transaction():
                    assert_unit_current(self.service, task, unit)
                    self._save_intake(task, unit, candidate)
                    result["pending"] += 1
                    if category:
                        result["suggestions"].append(self._suggest(category))
                continue
            draft = CapabilityMemoryDraft(
                category=category,
                title=str(candidate.get("title", "")),
                content=str(candidate.get("content", "")),
                keywords=tuple(candidate.get("keywords") or ()),
                source_project=source_project(self.service, task, unit),
                source_ref=source_ref(task, unit),
                occurred_at=candidate.get("occurred_at"),
            )
            source = CapabilitySourceDraft(
                source_kind="unit", source_key=f"{task['source_key']}#{unit['digest']}",
                source_ref=source_ref(task, unit), source_revision=task["source_revision"],
                task_id=task["id"], unit_digest=unit["digest"], unit_revision=unit["revision"],
                **unit_scope(self.service, unit),
            )
            with self.service.store.transaction():
                assert_unit_current(self.service, task, unit)
                prior = self.service.store._connection().execute(
                    "SELECT status FROM capability_memories WHERE fingerprint=?", (draft.fingerprint(),)
                ).fetchone()
                memory_id = capabilities.upsert(draft, source=source)
                source_id = capabilities.bind_source(memory_id, source)
                capability_assessment.persist(self.service.store, memory_id, source_id, assessment,
                                              existing_status=prior["status"] if prior else None)
            result["memory_ids"].append(memory_id)
            if assessment["value"] in {"review", "ignore"}:
                result["pending"] += 1
            index_factory = getattr(self.service, "capability_index", None)
            if callable(index_factory):
                memory = capabilities.get(memory_id)
                index_factory().sync(memory_id, memory["title"] + "\n" + memory["content"])
        return result
