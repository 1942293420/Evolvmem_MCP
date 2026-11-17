"""First initialization shares one actual health observation, never skips it."""
import pytest

from evolvmem import context_service as service_module
from evolvmem import context_vector_health as vector_module
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore


@pytest.mark.parametrize("mode,ready", [
    (ContextMode.PRIMARY, False),
    (ContextMode.SHADOW, True),
    (ContextMode.COMPAT, False),
    (ContextMode.LEGACY, False),
])
def test_first_initialize_shares_only_its_own_health_observation(test_config, monkeypatch, mode, ready):
    service = ContextService(test_config, store=ContextStore(test_config))
    scans = {"projection": 0, "vector": 0, "physical": 0, "documents": 0, "counts": 0}

    def observe(name, original):
        def wrapped(*args, **kwargs):
            scans[name] += 1
            return original(*args, **kwargs)
        return wrapped

    monkeypatch.setattr(service_module, "check_projection_lag",
                        observe("projection", service_module.check_projection_lag))
    monkeypatch.setattr(vector_module, "inspect_context_vector",
                        observe("vector", vector_module.inspect_context_vector))
    monkeypatch.setattr(service, "_quick_check_diagnostics",
                        observe("physical", service._quick_check_diagnostics))
    monkeypatch.setattr(service.store, "list_vector_documents",
                        observe("documents", service.store.list_vector_documents))
    monkeypatch.setattr(service.store, "count_by_status",
                        observe("counts", service.store.count_by_status))
    outer = (object(), {"outer": "unchanged"})
    token = service_module._health_observations.set(outer)
    try:
        first = service.initialize(mode=mode, adapter="codex")
        assert first.ready is ready
        if mode is ContextMode.PRIMARY:
            # Initialization still checks the real database and retains the
            # real uninitialized-vector rejection; it cannot announce ready.
            assert first.reason_codes == ("degraded_legacy",)
            assert scans["physical"] == 1
            assert scans["documents"] == 1
        else:
            assert scans["physical"] == 0
            assert scans["documents"] == 0
        assert scans["projection"] == scans["vector"] == scans["counts"] == 1
        assert service_module._health_observations.get() is outer

        # A later public status call must make its own observations. Nothing
        # from initialize is installed as a resident/request-wide health cache.
        second = service.status()
        assert second == first
        assert scans["projection"] == scans["vector"] == scans["counts"] == 2
        assert service_module._health_observations.get() is outer

        # Same-pair initialize remains its existing idempotent status read.
        assert service.initialize(mode=mode, adapter="codex") == first
        assert scans["projection"] == scans["vector"] == scans["counts"] == 3
        assert scans["physical"] == (1 if mode is ContextMode.PRIMARY else 0)
    finally:
        service.close()
        service_module._health_observations.reset(token)


def test_initialize_restores_outer_observation_after_status_failure(test_config, monkeypatch):
    service = ContextService(test_config, store=ContextStore(test_config))
    outer = (object(), {"outer": "unchanged"})
    token = service_module._health_observations.set(outer)

    def failing_status():
        raise RuntimeError("synthetic status failure")

    monkeypatch.setattr(service, "status", failing_status)
    try:
        with pytest.raises(RuntimeError, match="synthetic status failure"):
            service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
        assert service._ready is False
        assert service._reason_codes == ("degraded_legacy",)
        assert service_module._health_observations.get() is outer
    finally:
        service.close()
        service_module._health_observations.reset(token)
