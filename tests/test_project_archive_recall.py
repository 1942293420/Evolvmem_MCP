"""Exact project archive queries stay separate from capability memories."""
from tests.test_project_archive_store import save_manual_event
from types import SimpleNamespace

import pytest

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.context_store import _now_iso
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.project_archive_pipeline import get_project_archive, list_project_events, recall_project_archive
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft


@pytest.fixture
def service(test_config):
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    with service.store.transaction():
        now = _now_iso()
        service.store._connection().execute(
            "INSERT INTO context_project_registry(project,display_name,created_at,updated_at) "
            "VALUES('bluewhale','蓝鲸',?,?)", (now, now))
    yield service
    service.close()


def seed(service):
    CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category="frontend", title="表单校验", content="提交前校验。",
        source_project="bluewhale", source_ref="archive:memory"))
    archives = ProjectArchiveStore(service.store)
    save_manual_event(archives, ProjectEventDraft(
        project="bluewhale", event_type="feature", title="新增权限管理",
        content="完成统一权限管理。", source_ref="archive:event",
        occurred_at="2026-01-01 00:00:00"))
    archives.rebuild("bluewhale")


def test_project_query_returns_archive_not_capability_memories(service):
    seed(service)

    result = get_project_archive(service, "蓝鲸")

    assert "新增权限管理" in result["summary"]
    assert "表单校验" not in result["summary"]
    assert result["development_days"] > 0
    assert list_project_events(service, "蓝鲸")[0]["source_ref"] == "archive:event"


def test_project_mention_recall_uses_archive_in_primary_mode(service):
    seed(service)
    service.config.knowledge_structure_mode = "primary"

    result = recall_project_archive(service, "请介绍蓝鲸项目", max_chars=4000)

    assert "新增权限管理" in result["block"]
    assert "表单校验" not in result["block"]
    assert result["matched_projects"] == ["bluewhale"]


def test_project_archive_get_tool_returns_exact_project(service):
    seed(service)
    server = MemoryMCPServer.__new__(MemoryMCPServer)
    server.context_service = service

    result = server._project_archive_get({"project":"蓝鲸"})

    assert result["project"] == "bluewhale"
    assert "新增权限管理" in result["summary"]
    assert len(result["events"]) == 1


def test_unknown_project_has_stable_error(service):
    with pytest.raises(ValueError, match="project_not_registered"):
        get_project_archive(service, "missing")


def test_primary_recall_handler_returns_archive_dict(service):
    seed(service)
    service.config.knowledge_structure_mode = "primary"
    server = MemoryMCPServer.__new__(MemoryMCPServer)
    server.context_service = service
    server.config = service.config
    server.context_mode = ContextMode.SHADOW
    server.adapter = "codex"

    result = server._context_project_recall({"query": "bluewhale"})

    assert "新增权限管理" in result["block"]
    assert result["matched_projects"] == ["bluewhale"]
    assert result["selected_ids"] == []
    assert result["event_ids"]
    assert result["used_chars"] == len(result["block"])


@pytest.mark.parametrize("query,expected", [
    ("missing-project", "project_not_registered"),
    ("bluewhale orchard", "project_ambiguous"),
])
def test_primary_recall_handler_preserves_resolution_error(service, query, expected):
    seed(service)
    with service.store.transaction():
        now = _now_iso()
        service.store._connection().execute(
            "INSERT INTO context_project_registry(project,display_name,created_at,updated_at) "
            "VALUES('orchard','orchard',?,?)", (now, now))
    service.config.knowledge_structure_mode = "primary"
    server = MemoryMCPServer.__new__(MemoryMCPServer)
    server.context_service = service
    server.config = service.config
    server.context_mode = ContextMode.SHADOW
    server.adapter = "codex"

    assert server._context_project_recall({"query": query})["error"] == expected


def test_primary_legacy_recall_explains_supported_routes(service):
    service.config.knowledge_structure_mode = "primary"
    server = MemoryMCPServer.__new__(MemoryMCPServer)
    server.context_service = service

    result = server._knowledge_recall({"project": "bluewhale", "query": "history"})

    assert result["error"] == "legacy_read_only"
    assert result["alternatives"] == [
        "context_project_recall", "project_archive_get", "capability_search"]
    assert "kind" in result["message"]
