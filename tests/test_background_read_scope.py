"""Actual request_health module; fake metadata port only, no fake proof/store."""
from types import SimpleNamespace
import threading

import pytest
from evolvmem import request_health as health_module
from evolvmem.context_models import ContextMode


class Metadata:
    active = True
    def __init__(self):
        self.servers = {}
        self.calls = []
        self.gates = {}
        for name in ('kane', 'jiangli', 'public'):
            service = SimpleNamespace(_mode=ContextMode.PRIMARY, _adapter='codex', _ready=False)
            self.servers[name] = SimpleNamespace(context_service=service,
                context_mode=ContextMode.PRIMARY, adapter='codex')
            self.gates[name] = {'hard_gate': 'allowed', 'diagnostic': {'last_checked_ready': True}}
    def status(self, name):
        self.calls.append(name)
        return self.gates[name]


def scope(port, *names, tool='experience_recall'):
    assert hasattr(health_module, 'background_read_scope'), 'explicit request read admission is missing'
    return health_module.background_read_scope(port, tool, tuple((name, port.servers[name]) for name in names))


def test_scope_checks_actual_namespace_before_after_and_never_writes_ready():
    port = Metadata()
    service = port.servers['kane'].context_service
    with scope(port, 'kane', 'public'):
        assert health_module.background_read_ready(service) is True
        assert service._ready is False
    assert health_module.background_read_ready(service) is None
    assert port.calls == ['kane', 'public', 'kane', 'public']


@pytest.mark.parametrize('mode', ['hard', 'ready', 'inactive', 'uninitialized', 'mismatch'])
def test_fail_closed_metadata_never_enters_body(mode):
    port = Metadata()
    if mode == 'hard': port.gates['kane']['hard_gate'] = 'health_physical_failed'
    if mode == 'ready': port.gates['kane']['diagnostic']['last_checked_ready'] = False
    if mode == 'inactive': port.active = False
    if mode == 'uninitialized': port.servers['kane'].context_service._mode = None
    if mode == 'mismatch': port.servers['kane'].context_service._adapter = 'other'
    with pytest.raises(RuntimeError):
        with scope(port, 'kane'):
            pytest.fail('invalid admission entered body')


def test_other_service_namespace_and_nested_admission_cannot_borrow():
    port = Metadata()
    with scope(port, 'kane'):
        with pytest.raises(RuntimeError):
            health_module.background_read_ready(port.servers['jiangli'].context_service)
        with pytest.raises(RuntimeError):
            with scope(port, 'public'): pass
    with pytest.raises(RuntimeError):
      with scope(port, 'kane'):
        port.servers['kane'].context_service = SimpleNamespace(_mode=ContextMode.PRIMARY, _adapter='codex')
        with pytest.raises(RuntimeError):
            health_module.background_read_ready(port.servers['kane'].context_service)
        # Replacement after yielding must also be rejected by the exit check.


def test_exit_change_discards_return_and_cleanup_after_error():
    port = Metadata()
    service = port.servers['kane'].context_service
    def operation():
        with scope(port, 'kane'):
            port.gates['kane']['hard_gate'] = 'health_boundary_changed'
            return {'result': 'not returned'}
    with pytest.raises(RuntimeError): operation()
    assert health_module.background_read_ready(service) is None


def test_context_is_thread_and_request_local_and_nonexperience_is_rejected():
    port = Metadata()
    service = port.servers['kane'].context_service
    with scope(port, 'kane'):
        observations = []
        thread = threading.Thread(target=lambda: observations.append(health_module.background_read_ready(service)))
        thread.start(); thread.join(timeout=1)
        assert observations == [None]
        with health_module.request_health_scope():
            assert health_module.background_read_ready(service) is None
        assert health_module.background_read_ready(service) is True
    with pytest.raises(RuntimeError):
        with scope(port, 'kane', tool='memory_add'): pass


def test_provider_exception_has_fixed_safe_error_and_no_leak():
    port = Metadata()
    def fail(name): raise ValueError('secret SQL/path/token')
    port.status = fail
    with pytest.raises(RuntimeError) as error:
        with scope(port, 'kane'): pass
    assert str(error.value) == 'health_boundary_unavailable'
