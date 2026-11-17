"""Pause only the retired capability index; retain Context recovery."""
import json
from types import SimpleNamespace
import pytest


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(path))
    path.write_text(json.dumps({'legacy_web_enabled': False, 'ai_processing_enabled': False}))
    return path


def test_paused_capability_recovery_does_not_visit_sources_or_mutate_backoff(policy_file):
    from evolvmem.capability_recovery import CapabilityVectorRecoveryLoop
    def factory(service):
        pytest.fail('paused recovery constructed a capability index')
    def sources():
        pytest.fail('paused recovery inspected source namespaces')
        yield None
    loop = CapabilityVectorRecoveryLoop(clock=lambda: 100, index_factory=factory)
    loop.failures, loop.stale_rounds, loop.next_attempt_at = 2, 3, 77
    assert loop.tick(sources()) == ()
    assert (loop.failures, loop.stale_rounds, loop.next_attempt_at) == (2, 3, 77)


def test_same_recovery_instance_resumes_on_runtime_legacy_toggle(policy_file):
    from evolvmem.capability_recovery import CapabilityVectorRecoveryLoop
    events = []
    class Index:
        vector_index = SimpleNamespace(close=lambda: events.append('close'))
        def reconcile(self, *, limit):
            events.append(('reconcile', limit))
            return {'status': 'ready', 'processed': 0}
    def factory(service):
        events.append('construct')
        return Index()
    loop = CapabilityVectorRecoveryLoop(clock=lambda: 100, index_factory=factory)
    assert loop.tick((object(),)) == ()
    assert events == []
    # This gate belongs to the retired capability library, independently of
    # the generation-provider flag; legacy=true retains previous behavior.
    policy_file.write_text(json.dumps({'legacy_web_enabled': True, 'ai_processing_enabled': False}))
    assert loop.tick((object(),)) == ({'status': 'ready', 'processed': 0},)
    assert events == ['construct', ('reconcile', 32), 'close']
    assert loop.next_attempt_at == 160


def run_runtime_round(events, *, context_due, during_context=None):
    from evolvmem.lan_runtime import LanRuntime
    class Stop:
        calls = 0
        def wait(self, interval):
            self.calls += 1
            return self.calls > 1
    def context_tick(services):
        events.append('context_tick')
        if during_context:
            during_context()
        return ()
    def capability_due():
        events.append('capability_due')
        return True
    def capability_tick(services):
        events.append('capability_tick')
        return ()
    runtime = SimpleNamespace(
        _recovery_stop=Stop(),
        _vector_recovery=SimpleNamespace(due=lambda: context_due, tick=context_tick),
        _capability_recovery=SimpleNamespace(due=capability_due, tick=capability_tick),
        _open_recovery_services=lambda previous: events.append('open_services') or (object(),),
        _close_recovery_services=lambda services: events.append('close_services'),
        _worker_finished=lambda kind: events.append(('finished', kind)))
    LanRuntime._recovery_loop(runtime)


def test_runtime_paused_capability_does_not_open_service_when_context_not_due(policy_file):
    events = []
    run_runtime_round(events, context_due=False)
    assert events == ['close_services', ('finished', 'recovery')]


def test_runtime_preserves_due_context_recovery_when_legacy_paused(policy_file):
    events = []
    run_runtime_round(events, context_due=True)
    assert events == ['open_services', 'context_tick', 'close_services', ('finished', 'recovery')]


def test_runtime_rereads_gate_after_long_context_round(policy_file):
    policy_file.write_text(json.dumps({'legacy_web_enabled': True}))
    events = []
    run_runtime_round(events, context_due=True,
                      during_context=lambda: policy_file.write_text(json.dumps({'legacy_web_enabled': False})))
    assert 'context_tick' in events
    assert 'capability_tick' not in events
