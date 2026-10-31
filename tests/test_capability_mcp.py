"""Capability search is registered in the MCP contract and handler."""
from types import SimpleNamespace

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_models import ContextMode, ContextServiceStatus
from evolvmem.context_store import ContextStore
from evolvmem.mcp_contract import tool_specs
from evolvmem.mcp_server import MemoryMCPServer


def test_capability_search_is_exposed_with_ready_context_tools():
    health = ContextServiceStatus(
        mode=ContextMode.PRIMARY, adapter="codex", ready=True, status_counts={},
        mapping_count=0, projection_lag=0, context_vector_ready=True,
        context_vector_dirty=False, legacy_vector_ready=False,
        legacy_vector_dirty=False, diagnostics=(), reason_codes=())
    specs = tool_specs(adapter="codex", mode=ContextMode.PRIMARY, health=health)
    assert "capability_search" in {spec.name for spec in specs}


def test_capability_search_handler_returns_memory_and_source(test_config):
    with ContextStore(test_config) as store:
        from tests.test_capability_retriever import add_capability
        service = SimpleNamespace(store=store, config=test_config)
        memory_id = add_capability(service)
        server = MemoryMCPServer.__new__(MemoryMCPServer)
        server.context_service = service

        result = server._capability_search({"query":"Vue", "categories":["frontend"], "top_k":5})

        assert result["count"] == 1
        assert result["results"][0]["memory_id"] == memory_id
        assert result["results"][0]["source_project"] == "bluewhale"
