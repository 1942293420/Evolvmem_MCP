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


def resolve_archive_project(service, name: str) -> str:
    value = str(name).strip()
    if not value:
        raise ValueError("project_not_registered")
    rows = service.store._connection().execute(
        "SELECT project FROM context_project_registry WHERE status='active' AND "
        "(project=? OR display_name=?) UNION "
        "SELECT project FROM context_project_aliases WHERE alias=?",
        (value, value, value),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("project_not_registered" if not rows else "project_ambiguous")
    return str(rows[0][0])


def get_project_archive(service, project: str) -> dict:
    resolved = resolve_archive_project(service, project)
    archive = ProjectArchiveStore(service.store).get(resolved)
    if archive is None:
        raise ValueError("project_archive_not_found")
    return archive


def list_project_events(service, project: str, limit: int = 100) -> list[dict]:
    resolved = resolve_archive_project(service, project)
    events = ProjectArchiveStore(service.store).events(resolved)
    return events[-max(1, min(int(limit), 500)):]


def recall_project_archive(service, query: str, *, max_chars: int = 6000) -> dict:
    text = str(query).strip().casefold()
    if not text:
        raise ValueError("invalid_query")
    names = set()
    for row in service.store._connection().execute(
        "SELECT project,display_name FROM context_project_registry WHERE status='active'"
    ):
        names.add((row["project"], row["project"]))
        if row["display_name"]:
            names.add((row["display_name"], row["project"]))
    for row in service.store._connection().execute("SELECT alias,project FROM context_project_aliases"):
        names.add((row["alias"], row["project"]))
    matched = sorted({project for name, project in names if name.casefold() in text})
    if len(matched) != 1:
        raise ValueError("project_not_registered" if not matched else "project_ambiguous")
    archive = get_project_archive(service, matched[0])
    events = list_project_events(service, matched[0], limit=20)
    block = archive["summary"][:max_chars]
    return {
        "block": block,
        "selected_ids": [event["event_id"] for event in events],
        "matched_projects": [matched[0]],
        "used_chars": len(block),
        "unverified_ids": [],
        "excluded": [],
        "diagnostics": {},
    }
