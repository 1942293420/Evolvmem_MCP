"""Capability-only retrieval and primary session-start isolation."""
import pytest

from evolvmem.capability_retriever import CapabilityRetriever
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_models import ContextMode, ContextSessionStartRequest
from evolvmem.context_service import ContextService
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft


@pytest.fixture
def service(test_config):
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    yield service
    service.close()


def add_capability(service, category="frontend", title="Vue表单校验", content="统一在提交前校验字段。"):
    return CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
        category=category, title=title, content=content,
        keywords=("Vue", "表单"), source_project="bluewhale", source_ref="archive:1"))


def test_capability_retriever_uses_categories_and_keeps_source_project(service):
    memory_id = add_capability(service)
    add_capability(service, category="database", title="MySQL事务", content="使用事务保证写入一致。")

    hits = CapabilityRetriever(service).search("Vue校验", categories=("frontend",))

    assert [hit["memory_id"] for hit in hits] == [memory_id]
    assert hits[0]["source_project"] == "bluewhale"
    assert CapabilityRetriever(service).infer_categories("开发Vue前端表单") == ("frontend",)


def test_primary_session_start_excludes_project_archive_summary(service):
    add_capability(service)
    archives = ProjectArchiveStore(service.store)
    archives.record_event(ProjectEventDraft(
        project="bluewhale", event_type="feature", title="统一权限管理",
        content="蓝鲸完成统一权限管理。", source_ref="archive:2"))
    archives.rebuild("bluewhale")
    service.config.knowledge_structure_mode = "primary"

    result = service.session_start(ContextSessionStartRequest(
        project="bluewhale", query="开发Vue前端表单", max_chars=2000))

    assert "Vue表单校验" in result.block
    assert "统一权限管理" not in result.block
    assert result.selected_ids
