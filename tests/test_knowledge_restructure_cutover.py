"""Guarded cutover and non-destructive rollback."""
from types import SimpleNamespace

import pytest

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.knowledge_restructure import migrate_batch, set_structure_mode
from evolvmem.mcp_server import MemoryMCPServer
from tests.test_knowledge_restructure_migration import add_legacy_item, provider, service as simple_service


def test_cutover_requires_verified_migration_and_rollback_preserves_new_data(test_config):
    with ContextStore(test_config) as store:
        service = simple_service(test_config, store)
        add_legacy_item(store, "fact", "Vue提交前统一校验")
        with pytest.raises(ValueError, match="migration_not_verified"):
            set_structure_mode(service, "primary")

        set_structure_mode(service, "dual")
        migrate_batch(service, provider=provider)
        memory_id = CapabilityStore(store).upsert(CapabilityMemoryDraft(
            category="frontend", title="表单校验", content="提交前校验。", source_project="bluewhale"))

        assert set_structure_mode(service, "primary")["mode"] == "primary"
        assert Config.from_file(data_dir=test_config.data_dir).knowledge_structure_mode == "primary"
        assert set_structure_mode(service, "legacy")["mode"] == "legacy"
        assert CapabilityStore(store).get(memory_id)["content"] == "提交前校验。"
        assert Config.from_file(data_dir=test_config.data_dir).knowledge_structure_mode == "legacy"


def test_invalid_structure_mode_is_rejected(test_config):
    with ContextStore(test_config) as store:
        service = simple_service(test_config, store)
        with pytest.raises(ValueError, match="invalid_knowledge_structure_mode"):
            set_structure_mode(service, "unknown")


def test_primary_old_knowledge_recall_is_explicitly_read_only(test_config):
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        service.config.knowledge_structure_mode = "primary"
        server = MemoryMCPServer.__new__(MemoryMCPServer)
        server.context_service = service

        result = server._knowledge_recall({"project":"bluewhale", "query":"历史", "kind":"knowledge"})

        assert result == {"error":"legacy_read_only"}
    finally:
        service.close()


def test_cli_set_mode_persists_choice(test_config):
    from evolvmem.knowledge_restructure_cli import main

    with ContextStore(test_config):
        pass
    assert main(["set-mode", "--data-dir", str(test_config.data_dir), "dual"]) == 0
    assert Config.from_file(data_dir=test_config.data_dir).knowledge_structure_mode == "dual"
