"""Extract project history and rebuild current summaries independently."""
from __future__ import annotations

from evolvmem.capability_pipeline import _call_provider, _units, source_project, source_ref
from evolvmem.context_store import _now_iso
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft


class ProjectArchivePipeline:
    def __init__(self, service):
        self.service = service

    def _extract(self, unit, provider):
        prompt = (
            "你是项目历史整理器。只记录项目实际发生的重大改动、功能更新、已解决问题和里程碑。"
            "不要提取开发经验，不判断项目归属。没有项目历史价值时返回空列表。"
            "只返回JSON：{\"events\":[{\"event_type\":\"major_change|feature|problem|milestone\","
            "\"title\":\"事件标题\",\"content\":\"改动、原因、影响和结果\","
            "\"occurred_at\":\"原文明确时间或null\"}]}。\n原始项目记录：\n" + unit["text"]
        )
        return _call_provider(prompt, provider)

    def process_task(self, task, provider=None) -> dict:
        archives = ProjectArchiveStore(self.service.store)
        result = {"event_ids": [], "summary_revision": 0}
        touched = set()
        for unit in _units(self.service, task):
            project = source_project(self.service, task, unit)
            if not project:
                continue
            payload = self._extract(unit, provider)
            events = payload.get("events") or []
            if not isinstance(events, list):
                raise ValueError("invalid_project_events")
            for event in events:
                draft = ProjectEventDraft(
                    project=project,
                    event_type=str(event.get("event_type", "")),
                    title=str(event.get("title", "")),
                    content=str(event.get("content", "")),
                    source_ref=source_ref(task, unit),
                    occurred_at=event.get("occurred_at"),
                )
                result["event_ids"].append(archives.record_event(draft))
                touched.add(project)
        watermark = f"{task['source_key']}:{task['id']}"
        for project in sorted(touched):
            archive = archives.rebuild(project)
            result["summary_revision"] = archive["revision"]
            with self.service.store.transaction():
                self.service.store._connection().execute(
                    "UPDATE project_archives SET source_watermark=?,updated_at=? WHERE project=?",
                    (watermark, _now_iso(), project),
                )
        return result
