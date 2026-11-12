"""One current project summary rebuilt from source-backed project events."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from evolvmem.context_store import _now_iso
from evolvmem.capability_store import CapabilitySourceDraft as ProjectEventSourceDraft


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


def source_progress_date(identity_key: str) -> str | None:
    """A legacy log key proves a source day, not its timezone or exact event time."""
    match = re.fullmatch(r"project:[^:]+:progress:log:(\d{4}-\d{2}-\d{2})-(\d{4})", str(identity_key or ""))
    if not match:
        return None
    try:
        datetime.strptime("".join(match.groups()), "%Y-%m-%d%H%M")
    except ValueError:
        return None
    return match[1]


def event_date_precision(value) -> str:
    if not value or _parse_moment(value) is None:
        return "unknown"
    return "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) else "datetime"


def earliest_event_date(events) -> str | None:
    known = [event["occurred_at"] for event in events if event_date_precision(event["occurred_at"]) != "unknown"]
    return min(known, key=_parse_moment, default=None)


def event_date_label(event) -> str:
    value = event.get("occurred_at")
    precision = event_date_precision(value)
    if precision == "unknown":
        return "发生时间未确认；入库时间：" + event["recorded_at"]
    if precision == "date":
        return f"日期证据：{value}（仅到日，时区未确认）"
    return "发生时间：" + value


def _development_days(started_at, ended_at=None) -> int | None:
    start = _parse_moment(started_at)
    end = _parse_moment(ended_at) or datetime.now(timezone.utc).replace(tzinfo=None)
    if start is None:
        return None
    return max(0, (end - start).days)


def render_summary(project: str, events: list[dict]) -> str:
    """Render the fixed seven sections without inventing project history."""
    started = earliest_event_date(events)
    lines = [
        f"# {project}项目摘要",
        "",
        "## 项目概述",
        f"本项目累计记录 {len(events)} 项有效历史事件。",
        "",
        "## 开发时间",
        f"- 最早有日期记录：{started or '未确认'}",
        "- 日期范围按已知记录计算，不代表项目实际开始时间或完整开发周期。",
    ]
    for event_type, section in _SECTION_LABELS.items():
        lines.extend(("", f"## {section}"))
        selected = [event for event in events if event["event_type"] == event_type]
        if selected:
            for event in selected:
                moment = event_date_label(event)
                lines.append(f"- {event['title']}（{moment}）：{event['content']}")
        elif event_type == "milestone":
            lines.append(f"- 当前共有 {len(events)} 项有效项目事件，最新进展见最近更新。")
        else:
            lines.append("- 暂无已确认记录。")
    latest = max(events, key=lambda event: event["recorded_at"], default=None)
    lines.extend(("", "## 最近更新", f"- 最近事件入库时间：{latest['recorded_at'] if latest else '未确认'}"))
    return "\n".join(lines)


class ProjectArchiveStore:
    def __init__(self, context_store):
        self.context_store = context_store

    @property
    def conn(self):
        return self.context_store._connection()

    @contextmanager
    def _event_write(self):
        with self.context_store.transaction():
            self.conn.execute("SAVEPOINT project_event_write")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK TO project_event_write")
                self.conn.execute("RELEASE project_event_write")
                raise
            else:
                self.conn.execute("RELEASE project_event_write")

    def record_event(self, draft: ProjectEventDraft, *, source: ProjectEventSourceDraft | None = None) -> int:
        if not isinstance(draft, ProjectEventDraft):
            raise TypeError("project_event_draft_required")
        if not draft.project.strip() or not draft.title.strip() or not draft.content.strip():
            raise ValueError("invalid_project_event")
        if draft.event_type not in _EVENT_TYPES:
            raise ValueError("invalid_project_event_type")
        from evolvmem.pipeline_state import validate_occurred_at
        validate_occurred_at(draft.occurred_at)
        if source is not None:
            if not isinstance(source, ProjectEventSourceDraft):
                raise TypeError("project_event_source_required")
            source.validate()
        now = _now_iso()
        with self._event_write():
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
            event_id = int(row[0])
            if source is not None:
                fields = asdict(source)
                binding_key = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                fields["projects"] = json.dumps(fields["projects"], ensure_ascii=False)
                columns = list(fields)
                self.conn.execute(
                    "INSERT OR IGNORE INTO project_event_sources(event_id,binding_key,"
                    + ",".join(columns) + ",created_at) VALUES("
                    + ",".join("?" for _ in range(len(columns)+3)) + ")",
                    (event_id, binding_key, *[fields[k] for k in columns], now))
        return event_id

    def sources(self, event_id):
        return [{**dict(row), "projects": json.loads(row["projects"])}
                for row in self.conn.execute(
                    "SELECT * FROM project_event_sources WHERE event_id=? ORDER BY source_id", (event_id,))]

    def display_name(self, project: str) -> str:
        row = self.conn.execute("SELECT display_name FROM context_project_registry WHERE project=?", (project,)).fetchone()
        return str(row[0]) if row and row[0] else project

    def events(self, project: str, *, include_historical=False) -> list[dict]:
        rows = [dict(row) for row in self.conn.execute(
            "SELECT * FROM project_events WHERE project=? "
            "ORDER BY COALESCE(occurred_at,recorded_at),event_id", (project,))]
        from evolvmem.project_event_provenance import annotate
        support = annotate(self.context_store, rows)
        return [{**row, "occurred_at_precision": event_date_precision(row["occurred_at"]),
                 **support[row["event_id"]]} for row in rows
                if include_historical or support[row["event_id"]]["current_support"]]

    def rebuild(self, project: str, expected_revision=None) -> dict:
        events = self.events(project)
        if not events and not self.conn.execute(
                "SELECT 1 FROM project_events WHERE project=? LIMIT 1", (project,)).fetchone():
            raise ValueError("empty_summary")
        name = self.display_name(project)
        summary = render_summary(name, events)
        started_at = earliest_event_date(events)
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
                "display_name=excluded.display_name,summary=excluded.summary,started_at=excluded.started_at,"
                "revision=project_archives.revision+1,updated_at=excluded.updated_at",
                (project, name, summary, started_at, now, now),
            )
        return self.get(project)

    def get(self, project: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM project_archives WHERE project=?", (project,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        # Stored summaries may predate date-provenance fixes. Read a corrected
        # projection without mutating records or re-running historical extraction.
        events = self.events(project)
        result["display_name"] = self.display_name(project)
        result["started_at"] = earliest_event_date(events)
        result["started_at_basis"] = "earliest_dated_record" if result["started_at"] else "unknown"
        result["summary"] = render_summary(result["display_name"], events)
        result["development_days"] = _development_days(result["started_at"], result["ended_at"])
        return result
