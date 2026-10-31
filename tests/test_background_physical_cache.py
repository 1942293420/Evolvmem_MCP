"""Physical proof reuse must not renew its age or bypass hard identity gates."""
from types import SimpleNamespace
import threading
import pytest
from tests.test_lan_sharing import lan
from evolvmem.context_service import ContextService


@pytest.fixture
def physical(lan, monkeypatch):
    import evolvmem.background_health as module
    runtime, _ = lan
    manager = runtime._background_health
    clock, scans = [100.0], []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original = ContextService._scan_integrity_readonly
    def scan(path, expected):
        scans.append(expected)
        return original(path, expected)
    monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(scan))
    return runtime, manager, clock, scans


def test_repeated_evaluation_reuses_physical_proof_without_renewing_time(physical):
    _, manager, clock, scans = physical
    first = manager._evaluate('kane')
    clock[0] += 40
    second = manager._evaluate('kane')
    clock[0] += 40
    third = manager._evaluate('kane')
    assert len(scans) == 1
    assert first['physical_checked'] == second['physical_checked'] == third['physical_checked'] == 100
    assert third['completed'] == 180


@pytest.mark.parametrize('elapsed', [180, 301])
def test_near_ttl_and_expired_proof_are_rescanned(physical, elapsed):
    _, manager, clock, scans = physical
    first = manager._evaluate('kane')
    clock[0] += elapsed
    second = manager._evaluate('kane')
    assert len(scans) == 2
    assert second['physical_checked'] > first['physical_checked']


def test_expired_refresh_failure_cannot_reuse_prior_success(physical, monkeypatch):
    _, manager, clock, scans = physical
    manager.refresh('kane')
    manager.run_once()
    clock[0] += 301
    assert manager.status('kane')['hard_gate'] == 'health_physical_expired'
    failures = []
    def fail(*_):
        failures.append(True)
        return ('quick_check_failed',)
    monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(fail))
    manager.run_once()
    assert manager.status('kane')['hard_gate'] == 'health_physical_failed'
    manager._evaluate('kane')
    assert len(failures) == 2, 'failed results must be retried, never reused as a success'
    assert len(scans) == 1


def test_identical_schema_recreation_invalidates_physical_cache(physical):
    from evolvmem.health_generation import prepare
    runtime, manager, clock, scans = physical
    first = manager._evaluate('kane')
    store = runtime.server_for('kane').context_service.store
    with store.transaction():
        store._connection().execute('DROP TRIGGER health_generation_memories_insert')
    prepare(store)
    clock[0] += 1
    second = manager._evaluate('kane')
    assert len(scans) == 2
    assert scans[0][-1] != scans[1][-1]
    assert first['physical_checked'] != second['physical_checked']


def test_database_replacement_never_reuses_physical_success(physical):
    runtime, manager, _, scans = physical
    manager._evaluate('kane')
    path = runtime.server_for('kane').config.db_path
    path.rename(path.with_suffix('.retained'))
    path.write_bytes(b'replaced database')
    with pytest.raises(RuntimeError, match='health_identity_changed'):
        manager._evaluate('kane')
    assert len(scans) == 1
    assert manager.status('kane')['hard_gate'] != 'allowed'


def test_current_resident_failure_forces_new_physical_check_within_ttl(physical, monkeypatch):
    runtime, manager, _, scans = physical
    manager._evaluate('kane')
    service = runtime.server_for('kane').context_service
    service._integrity_snapshot = ((id(service.store._connection()), *scans[0]), 100.0, ('quick_check_failed',))
    result = manager._evaluate('kane')
    assert len(scans) == 2
    assert result['physical_codes'] == ()
    assert manager.status('kane')['hard_gate'] != 'allowed'


def test_shutdown_during_physical_scan_cannot_cache_or_return_a_proof(physical, monkeypatch):
    _, manager, _, _ = physical
    entered, release = threading.Event(), threading.Event()
    original = ContextService._scan_integrity_readonly
    def blocked(path, expected):
        entered.set()
        assert release.wait(3)
        return original(path, expected)
    monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(blocked))
    values, errors = [], []
    def evaluate():
        try:
            values.append(manager._evaluate('kane'))
        except Exception as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=evaluate)
    worker.start()
    assert entered.wait(2)
    manager.close()
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert not values and errors
    assert not getattr(manager, '_physical_cache', {})

def test_failed_early_refresh_discards_prior_success_even_if_resident_recovers(physical, monkeypatch):
    runtime, manager, _, scans = physical
    manager.refresh('kane')
    assert manager.run_once()
    service = runtime.server_for('kane').context_service
    current_key = (id(service.store._connection()), *scans[0])
    service._integrity_snapshot = (current_key, 100.0, ('quick_check_failed',))
    attempts = []
    def failure(*_):
        attempts.append(True)
        raise RuntimeError('physical checker unavailable')
    monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(failure))
    manager._next.clear()
    manager.refresh('kane')
    assert manager.run_once()
    assert manager.status('kane')['hard_gate'] != 'allowed'
    # A separate resident recovery cannot resurrect this worker's discarded proof.
    service._integrity_snapshot = (current_key, 100.0, ())
    with pytest.raises(RuntimeError, match='physical checker unavailable'):
        manager._evaluate('kane')
    assert len(attempts) == 2
    assert manager.status('kane')['hard_gate'] == 'health_evaluation_failed'


def test_disabled_integrity_ttl_never_reuses_physical_proof(physical, monkeypatch):
    _, manager, _, scans = physical
    monkeypatch.setenv('EVOLVMEM_INTEGRITY_CACHE_SECONDS', '0')
    manager._evaluate('kane')
    manager._evaluate('kane')
    assert len(scans) == 2
