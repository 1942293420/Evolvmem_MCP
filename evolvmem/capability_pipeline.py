"""Extract development memories independently of project ownership."""
from __future__ import annotations

import hashlib
import json
import time

from evolvmem.capability_models import CAPABILITY_CATEGORIES
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import _now_iso
from evolvmem.task_segmentation import decode


def _call_provider(prompt, provider=None):
    if provider is None:
        from evolvmem.auto_organization import _load_llm
        config, call = _load_llm()
        provider = lambda text: call(text, config, deadline=time.monotonic() + 90)
    response = provider(prompt)
    return response if isinstance(response, dict) else decode(response)


def _units(service, task) -> list[dict]:
    return [dict(row) for row in service.store._connection().execute(
        "SELECT * FROM organization_units WHERE task_id=? AND disposition!='set_aside' ORDER BY ordinal",
        (task["id"],),
    )]


def source_project(service, task, unit) -> str:
    if unit.get("project"):
        return str(unit["project"])
    source_key = str(task["source_key"])
    if source_key.startswith("archive:"):
        try:
            archive_id = int(source_key.split(":", 1)[1])
        except ValueError:
            return ""
        row = service.store._connection().execute(
            "SELECT project FROM session_archives WHERE id=?", (archive_id,)
        ).fetchone()
        return str(row[0]) if row else ""
    return ""


def source_ref(task, unit) -> str:
    return f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}"


def _fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class CapabilityPipeline:
    def __init__(self, service):
        self.service = service

    def _extract(self, unit, provider):
        prompt = (
            "你是开发知识整理器。只处理开发任务，不判断项目归属，不判断是否跨项目经验。"
            "过滤闲聊、重复输出、无信息量命令日志和临时过程。保留明确事实、决策、方法、注意事项和解决结果。"
            "分类只能是 frontend、backend、business_logic、database、architecture、quality、delivery；"
            "不属于现有分类时返回原建议名称，系统会进入待分类，不得强行归类。"
            "只返回JSON：{\"action\":\"keep|ignore\",\"category\":\"分类或建议名称\","
            "\"title\":\"简短标题\",\"content\":\"有依据的记忆正文\",\"keywords\":[\"技术词\"],"
            "\"occurred_at\":\"已知发生时间或null\"}。\n原始开发记录：\n" + unit["text"]
        )
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

    def process_task(self, task, provider=None) -> dict:
        result = {"memory_ids": [], "pending": 0, "suggestions": []}
        capabilities = CapabilityStore(self.service.store)
        for unit in _units(self.service, task):
            candidate = self._extract(unit, provider)
            if candidate.get("action") == "ignore":
                continue
            category = str(candidate.get("category", "")).strip()
            if category not in CAPABILITY_CATEGORIES:
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
            result["memory_ids"].append(capabilities.upsert(draft))
        return result
