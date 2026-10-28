"""One current project summary rebuilt from source-backed project events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json

from evolvmem.context_store import _now_iso


_EVENT_TYPES = {"major_change", "feature", "problem", "milestone"}
_SECTION_LABELS = {
    "major_change": "重大改动",
    "feature": "功能更新",
    "problem": "已解决的重要问题",
    "milestone": "当前进展",
}


@dataclass(frozen=True, slots=True)
class ProjectEventDraft:
    project: str
    event_type: str
    title: str
    content: str
    source_ref: str = ""
    occurred_at: str | None = None

    def fingerprint(self) -> str:
        raw = json.dumps(
            [self.project, self.event_type, self.title.strip(), self.content.strip()],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()


def _parse_moment(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _development_days(started_at, ended_at=None) -> int:
    start = _parse_moment(started_at)
    end = _parse_moment(ended_at) or datetime.now(timezone.utc).replace(tzinfo=None)
    if start is None:
        return 0
    return max(0, (end - start).days)


def render_summary(project: str, events: list[dict]) -> str:
    """Render the fixed seven sections without inventing project history."""
    started = min(
        (event["occurred_at"] or event["recorded_at"] for event in events),
        default="",
    )
    lines = [
        f"# {project}项目摘要",
        "",
        "## 项目概述",
        f"本项目累计记录 {len(events)} 项有效历史事件。",
        "",
        "## 开发时间",
        f"- 开始时间：{started or '未确认'}",
        "- 开发周期：查询时根据开始时间和当前状态计算",
    ]
    for event_type, section in _SECTION_LABELS.items():
        lines.extend(("", f"## {section}"))
        selected = [event for event in events if event["event_type"] == event_type]
        if selected:
            for event in selected:
                moment = event["occurred_at"] or event["recorded_at"]
                lines.append(f"- {event['title']}（{moment}）：{event['content']}")
        elif event_type == "milestone":
            lines.append(f"- 当前共有 {len(events)} 项有效项目事件，最新进展见最近更新。")
        else:
            lines.append("- 暂无已确认记录。")
    latest = max(events, key=lambda event: event["recorded_at"])
    lines.extend(("", "## 最近更新", f"- 最近事件记录时间：{latest['recorded_at']}"))
    return "\n".join(lines)


class ProjectArchiveStore:
    def __init__(self, context_store):
        self.context_store = context_store

    @property
    def conn(self):
        return self.context_store._connection()

    def record_event(self, draft: ProjectEventDraft) -> int:
        if not isinstance(draft, ProjectEventDraft):
            raise TypeError("project_event_draft_required")
        if not draft.project.strip() or not draft.title.strip() or not draft.content.strip():
            raise ValueError("invalid_project_event")
        if draft.event_type not in _EVENT_TYPES:
            raise ValueError("invalid_project_event_type")
        now = _now_iso()
        with self.context_store.transaction():
            self.conn.execute(
                "INSERT INTO project_events(project,event_type,occurred_at,recorded_at,title,content,"
                "source_ref,fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO NOTHING",
                (
                    draft.project,
                    draft.event_type,
                    draft.occurred_at,
                    now,
                    draft.title.strip(),
                    draft.content.strip(),
                    draft.source_ref,
                    draft.fingerprint(),
                    now,
                    now,
                ),
            )
            row = self.conn.execute(
                "SELECT event_id FROM project_events WHERE fingerprint=?",
                (draft.fingerprint(),),
            ).fetchone()
        return int(row[0])

    def events(self, project: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM project_events WHERE project=? AND status='active' "
            "ORDER BY COALESCE(occurred_at,recorded_at),event_id",
            (project,),
        )]

    def rebuild(self, project: str, expected_revision=None) -> dict:
        events = self.events(project)
        if not events:
            raise ValueError("empty_summary")
        summary = render_summary(project, events)
        started_at = min(event["occurred_at"] or event["recorded_at"] for event in events)
        now = _now_iso()
        with self.context_store.transaction():
            current = self.conn.execute(
                "SELECT revision FROM project_archives WHERE project=?", (project,)
            ).fetchone()
            revision = int(current[0]) if current else 0
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("revision_conflict")
            self.conn.execute(
                "INSERT INTO project_archives(project,display_name,summary,started_at,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,1,?,?) ON CONFLICT(project) DO UPDATE SET "
                "summary=excluded.summary,started_at=excluded.started_at,"
                "revision=project_archives.revision+1,updated_at=excluded.updated_at",
                (project, project, summary, started_at, now, now),
            )
        return self.get(project)

    def get(self, project: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM project_archives WHERE project=?", (project,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["development_days"] = _development_days(result["started_at"], result["ended_at"])
        return result
