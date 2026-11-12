"""Authenticated revision checks never imply that Context is healthy."""
from tests.test_project_archive_store import save_manual_event
import pytest
from tests.test_lan_sharing import lan, add


def test_revision_probe_skips_health_and_counts_but_observes_new_knowledge(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, "kane")
    server = runtime.server_for("kane")
    def forbidden(*args, **kwargs):
        pytest.fail("revision probe must not scan health or memory bodies")
    monkeypatch.setattr(server, "_evaluate_live_status", forbidden)
    monkeypatch.setattr(server.context_service.legacy_facade(), "all_ids", forbidden)
    first = adapter.call_tool("kane", "memory_status", {"detail": "revision"})
    assert first.get("authenticated_user") == "kane"
    assert first.get("detail") == "revision"
    assert "memory_revision" in first
    assert not {"ready", "context_ready", "active_memories", "total_records"} & first.keys()
    with server.context_service.store.transaction():
        server.context_service.store._connection().execute(
            "UPDATE context_items SET confidence=confidence-0.1 WHERE id=?", (item["context_id"],))
    second = adapter.call_tool("kane", "memory_status", {"detail": "revision"})
    assert second["memory_revision"] != first["memory_revision"]


def test_revision_probe_does_not_authorize_degraded_business_reads(lan):
    runtime, adapter = lan
    item = add(adapter, "kane")
    store = runtime.server_for("kane").context_service.store
    with store.transaction():
        store._connection().execute("DELETE FROM context_layers WHERE item_id=? AND layer='l1'", (item["context_id"],))
    probe = adapter.call_tool("kane", "memory_status", {"detail": "revision"})
    assert probe.get("detail") == "revision"
    assert probe["authenticated_user"] == "kane"
    assert "context_ready" not in probe
    denied = adapter.call_tool("kane", "context_project_recall", {"query": "lan"})
    assert denied.get("error") in {"degraded_legacy", "unknown_tool"}
    assert not denied.get("block")


@pytest.mark.parametrize("space", ["personal", "public"])
def test_revision_probe_keeps_namespace_and_private_revision_isolation(lan, space):
    runtime, adapter = lan
    before = adapter.call_tool("kane", "memory_status", {"detail": "revision", "space": space})
    assert before.get("detail") == "revision"
    add(adapter, "jiangli")
    after = adapter.call_tool("kane", "memory_status", {"detail": "revision", "space": space})
    assert after["authenticated_user"] == "kane"
    assert after["memory_revision"] == before["memory_revision"]
    assert "active_memories" not in after


@pytest.mark.parametrize("arguments", [{"detail": "other"}, {"detail": "revision", "extra": True},
                                        {"detail": "revision", "user": "jiangli"}])
def test_revision_probe_preserves_argument_and_identity_validation(lan, arguments):
    _, adapter = lan
    assert "error" in adapter.call_tool("kane", "memory_status", arguments)


def test_default_status_keeps_full_diagnostic_shape(lan):
    _, adapter = lan
    add(adapter, "kane")
    result = adapter.call_tool("kane", "memory_status", {})
    assert result["active_memories"] == 1
    assert result["total_records"] == 1
    assert "context_ready" in result
    assert "detail" not in result


def test_primary_archive_and_capability_changes_invalidate_revision(lan):
    from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft
    from evolvmem.capability_store import CapabilityStore, CapabilityMemoryDraft
    runtime, adapter = lan
    store = runtime.server_for("kane").context_service.store
    def revision():
        return adapter.call_tool("kane", "memory_status", {"detail": "revision"})["memory_revision"]
    before = revision()
    archives = ProjectArchiveStore(store)
    event_id = save_manual_event(archives, ProjectEventDraft(project="probe", event_type="feature", title="new event", content="new project history"))
    after_event = revision()
    assert after_event != before
    archives.rebuild("probe")
    after_archive = revision()
    assert after_archive != after_event
    capabilities = CapabilityStore(store)
    memory_id = capabilities.upsert(CapabilityMemoryDraft(category="frontend", title="new capability", content="verified fixture behavior"))
    after_capability = revision()
    assert after_capability != after_archive
    with store.transaction():
        store._connection().execute("UPDATE capability_memories SET status='archived' WHERE memory_id=?", (memory_id,))
    assert revision() != after_capability


def test_revision_probe_is_explicitly_lan_only(lan):
    runtime, _ = lan
    server = runtime.server_for("kane")
    assert server.handle_tool_call("memory_status", {"detail": "revision"}) == {"error": "revision_requires_authenticated_lan"}
