"""Project events and rebuildable current summaries."""
import pytest

from evolvmem.context_store import ContextStore
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft



def save_manual_event(archives, draft):
    """Explicit human-origin event fixture, never inferred from a missing source."""
    from evolvmem.project_archive_store import ProjectEventSourceDraft
    with archives.context_store.transaction():
        archives.conn.execute(
            "INSERT OR IGNORE INTO context_project_registry(project,created_at,updated_at) VALUES(?,'now','now')",
            (draft.project,))
    return archives.record_event(draft, source=ProjectEventSourceDraft(
        source_kind="manual", source_key="test:manual:" + draft.fingerprint(),
        source_ref="test:explicit-human-event", projects=(draft.project,),
        evidence_status="confirmed"))


def event(**changes):
    values = dict(
        project="bluewhale",
        event_type="feature",
        title="新增权限管理",
        content="完成统一权限管理并投入使用。",
        source_ref="archive:1",
        occurred_at="2026-02-01 00:00:00",
    )
    values.update(changes)
    return ProjectEventDraft(**values)


def test_archive_rebuilds_from_events_and_computes_dynamic_period(test_config):
    with ContextStore(test_config) as store:
        archives = ProjectArchiveStore(store)
        event_id = save_manual_event(archives, event())

        current = archives.rebuild("bluewhale")

        assert current["project"] == "bluewhale"
        assert current["started_at"] == "2026-02-01 00:00:00"
        assert current["development_days"] > 0
        assert current["revision"] == 1
        assert "新增权限管理" in current["summary"]
        assert "完成统一权限管理并投入使用。" in current["summary"]
        for section in ("项目概述", "开发时间", "重大改动", "功能更新", "已解决的重要问题", "当前进展", "最近更新"):
            assert section in current["summary"]
        assert archives.events("bluewhale")[0]["event_id"] == event_id


def test_duplicate_event_does_not_duplicate_history_or_summary(test_config):
    with ContextStore(test_config) as store:
        archives = ProjectArchiveStore(store)
        first = save_manual_event(archives, event())
        second = save_manual_event(archives, event())

        assert second == first
        assert len(archives.events("bluewhale")) == 1
        assert archives.rebuild("bluewhale")["summary"].count("新增权限管理") == 1


def test_rebuild_uses_only_active_events_and_checks_revision(test_config):
    with ContextStore(test_config) as store:
        archives = ProjectArchiveStore(store)
        save_manual_event(archives, event())
        archives.rebuild("bluewhale")
        save_manual_event(archives, event(
            event_type="major_change", title="替换核心模块", content="完成核心模块替换。",
            source_ref="archive:2", occurred_at="2026-03-01 00:00:00",
        ))
        current = archives.rebuild("bluewhale", expected_revision=1)
        assert current["revision"] == 2
        assert "替换核心模块" in current["summary"]

        with pytest.raises(ValueError, match="revision_conflict"):
            archives.rebuild("bluewhale", expected_revision=1)
        assert archives.get("bluewhale")["revision"] == 2


def test_empty_rebuild_preserves_existing_summary(test_config):
    with ContextStore(test_config) as store:
        archives = ProjectArchiveStore(store)
        save_manual_event(archives, event())
        before = archives.rebuild("bluewhale")

        with pytest.raises(ValueError, match="empty_summary"):
            archives.rebuild("missing")

        assert archives.get("bluewhale")["summary"] == before["summary"]
        assert archives.get("missing") is None
