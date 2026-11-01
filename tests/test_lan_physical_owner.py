"""Native SQLite/LAN ownership regressions. No model or production database."""
from contextlib import closing
from types import SimpleNamespace
import copy
import threading
import pytest
from tests.test_lan_sharing import lan
from evolvmem.context_service import ContextService


def claim(manager):
    # Before this feature there is no claim operation: leave the original real
    # behavior intact so baseline failure demonstrates duplicate ownership.
    operation = getattr(manager, 'claim_resident_physical', None)
    if operation is not None:
        operation()


def manager_for(runtime):
    old = runtime._background_health
    old.close()
    return type(old)(old.servers, start=False)


def test_handoff_does_not_import_unstamped_startup_success_or_scan_foreground(lan, monkeypatch):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    scans = []
    original = service._quick_check_diagnostics
    monkeypatch.setattr(service, '_quick_check_diagnostics',
                        lambda: (scans.append(True), original())[1])
    try:
        claim(manager)
        # A successful resident startup proof is not an owner-owned baseline.
        assert service._integrity_diagnostics() == ('quick_check_pending',)
        assert service._integrity_timer is None
        assert scans == []
        assert manager.status('kane')['hard_gate'] == 'health_baseline_pending'
    finally:
        manager.close()


def test_completed_owner_check_is_consumed_by_resident_without_another_scan(lan, monkeypatch):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        assert manager.status('kane')['hard_gate'] == 'allowed'
        # Clearing the obsolete private field cannot erase a real owner proof.
        service._integrity_snapshot = None
        def no_foreground_scan():
            raise AssertionError('foreground physical scan')
        monkeypatch.setattr(service, '_quick_check_diagnostics', no_foreground_scan)
        assert service._integrity_diagnostics() == ()
    finally:
        manager.close()


def test_physical_failure_revokes_before_semantic_evaluation_finishes(lan, monkeypatch):
    from evolvmem import background_health
    runtime, _ = lan
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ContextService, '_integrity_ttl', staticmethod(lambda: 300.0))
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        clock[0] += 181
        assert manager.status('kane')['hard_gate'] == 'allowed'
        prior = service._integrity_cache_state()
        monkeypatch.setattr(ContextService, '_scan_integrity_readonly',
                            staticmethod(lambda *args: ('quick_check_failed',)))
        # Same TTL and clock before/after failure: the previous success is not
        # expired. Only physical failure may invalidate the consumer token.
        with closing(manager._connect('kane')) as conn:
            expected = (*manager._identities['kane'], conn.execute('PRAGMA schema_version').fetchone()[0])
        result, _ = manager._physical_result('kane', service.config.db_path, expected)
        assert result == ('quick_check_failed',)
        assert service._integrity_cache_state() != prior
        assert manager.status('kane')['hard_gate'] == 'health_physical_failed'
        assert service._integrity_diagnostics() == ('quick_check_failed',)
    finally:
        manager.close()


def test_actual_old_worker_must_exit_and_cannot_publish_after_handoff(lan, monkeypatch):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    entered, release = threading.Event(), threading.Event()
    scans = []
    original = ContextService._scan_integrity_readonly
    def scanner(path, expected):
        scans.append(threading.current_thread().name)
        if threading.current_thread().name == 'evolvmem-integrity-check':
            entered.set()
            assert release.wait(5)
            return ()
        return original(path, expected)
    monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(scanner))
    old_worker = None
    try:
        with service._integrity_lock:
            service._start_integrity_refresh(service._integrity_snapshot[0])
            old_worker = service._integrity_worker
        assert entered.wait(2)
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        assert scans == ['evolvmem-integrity-check']
        assert service._integrity_diagnostics() == ('quick_check_pending',)
        assert service._integrity_snapshot is None
        release.set()
        old_worker.join(3)
        assert not old_worker.is_alive()
        assert service._integrity_snapshot is None
        assert service._integrity_timer is None
        manager._next['kane'] = 0
        manager.refresh('kane')
        assert manager.run_once()
        assert len(scans) == 2
        assert manager.status('kane')['hard_gate'] == 'allowed'
    finally:
        release.set()
        if old_worker is not None:
            old_worker.join(3)
        manager.close()


def test_failure_invalidates_actual_request_health_cache_at_same_ttl_and_clock(lan, monkeypatch):
    from evolvmem import background_health, request_health
    runtime, _ = lan
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ContextService, '_integrity_ttl', staticmethod(lambda: 300.0))
    manager = manager_for(runtime)
    server = runtime.server_for('kane')
    service = server.context_service
    evaluations = []
    original = server._evaluate_live_status
    def evaluate():
        evaluations.append(True)
        return original()
    monkeypatch.setattr(server, '_evaluate_live_status', evaluate)
    try:
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        clock[0] += 181
        request_health._entry(server).snapshot = None
        assert server._live_status().ready
        count = len(evaluations)
        assert server._live_status().ready
        assert len(evaluations) == count
        prior = service._integrity_cache_state()
        monkeypatch.setattr(ContextService, '_scan_integrity_readonly',
                            staticmethod(lambda *args: ('quick_check_failed',)))
        with closing(manager._connect('kane')) as conn:
            expected = (*manager._identities['kane'], conn.execute('PRAGMA schema_version').fetchone()[0])
        manager._physical_result('kane', service.config.db_path, expected)
        assert service._integrity_cache_state() != prior
        assert not server._live_status().ready
        assert len(evaluations) > count
        assert manager.status('kane')['hard_gate'] == 'health_physical_failed'
    finally:
        manager.close()


def test_physical_success_does_not_clear_semantic_error_or_rewrite_diagnostic(lan, monkeypatch):
    from evolvmem import background_health
    runtime, _ = lan
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        manager._errors['kane'] = 'health_evaluation_failed'
        prior = copy.deepcopy(manager._snapshots['kane']['diagnostic'])
        clock[0] += 181
        with closing(manager._connect('kane')) as conn:
            expected = (*manager._identities['kane'], conn.execute('PRAGMA schema_version').fetchone()[0])
        assert manager._physical_result('kane', service.config.db_path, expected)[0] == ()
        status = manager.status('kane')
        assert status['hard_gate'] == 'health_evaluation_failed'
        assert status['diagnostic'] == prior
    finally:
        manager.close()


def test_transaction_keeps_its_actual_check_instead_of_borrowing_committed_proof(lan, monkeypatch):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    calls = []
    original = service._quick_check_diagnostics
    def checked():
        calls.append(service.store._connection().in_transaction)
        return original()
    monkeypatch.setattr(service, '_quick_check_diagnostics', checked)
    try:
        claim(manager)
        assert service._integrity_diagnostics() == ('quick_check_pending',)
        with service.store.transaction():
            assert service._integrity_diagnostics() == ()
        assert calls == [True]
        assert service._integrity_diagnostics() == ('quick_check_pending',)
    finally:
        manager.close()


def test_close_cannot_restore_private_scanner_or_old_cached_success(lan, monkeypatch):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    claim(manager)
    manager.refresh('kane')
    assert manager.run_once()
    before = service._integrity_cache_state()
    manager.close()
    def forbidden():
        raise AssertionError('closed owner fell back to foreground scan')
    monkeypatch.setattr(service, '_quick_check_diagnostics', forbidden)
    assert service._integrity_diagnostics() == ('quick_check_failed',)
    assert service._integrity_cache_state() != before
    assert service._integrity_timer is None
    assert manager.status('kane')['hard_gate'] == 'health_closed'


def test_actual_lan_host_claims_before_health_start_without_changing_standalone_constructor():
    from evolvmem.lan_runtime import LanRuntime
    calls = []
    health = SimpleNamespace(claim_resident_physical=lambda: calls.append('claim'),
                             start=lambda: calls.append('start'))
    runtime = SimpleNamespace(_background_health=health, _capture_worker=object())
    LanRuntime.start_capture_worker(runtime, None)
    assert calls == ['claim', 'start']


@pytest.mark.parametrize('withdrawal', ['closed', 'boundary', 'failed'])
def test_transaction_cannot_bypass_owner_hard_withdrawal(lan, monkeypatch, withdrawal):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        if withdrawal == 'closed':
            manager.close()
        elif withdrawal == 'boundary':
            original = service.config.context_session_summary_ttl_days
            service.config.context_session_summary_ttl_days = original + 1
            assert service._integrity_diagnostics() == ('quick_check_failed',)
            service.config.context_session_summary_ttl_days = original
        else:
            monkeypatch.setattr(ContextService, '_scan_integrity_readonly',
                                staticmethod(lambda *args: ('quick_check_failed',)))
            with closing(manager._connect('kane')) as conn:
                expected = (*manager._identities['kane'], conn.execute('PRAGMA schema_version').fetchone()[0])
            manager._physical_result('kane', service.config.db_path, expected)
        # The transaction's own healthy image must not restore a closed or
        # permanently withdrawn owner. Healthy/pending owner cases above still
        # execute a real transaction-local quick_check.
        with service.store.transaction():
            assert service._integrity_diagnostics() == ('quick_check_failed',)
    finally:
        manager.close()


def test_uncommitted_schema_change_then_rollback_does_not_poison_shared_owner(lan):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        manager.refresh('kane')
        assert manager.run_once()
        assert service._integrity_diagnostics() == ()
        before = service._integrity_cache_state()
        with pytest.raises(RuntimeError, match='synthetic rollback'):
            with service.store.transaction():
                service.store._connection().execute('CREATE TABLE synthetic_uncommitted_physical_probe(id INTEGER)')
                assert service._integrity_diagnostics() == ()
                raise RuntimeError('synthetic rollback')
        assert service._integrity_diagnostics() == ()
        assert service._integrity_cache_state() == before
        assert manager.status('kane')['hard_gate'] == 'allowed'
    finally:
        manager.close()


def recreate_same_schema(store):
    from evolvmem.health_generation import prepare
    with store.transaction():
        conn = store._connection()
        conn.execute('CREATE TABLE synthetic_physical_cookie_probe(id INTEGER)')
        conn.execute('DROP TABLE synthetic_physical_cookie_probe')
    prepare(store)


def test_claim_observes_current_committed_cookie_without_replacing_hard_contract(lan):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    original = manager._contracts['kane']
    try:
        recreate_same_schema(service.store)
        claim(manager)
        with closing(manager._connect('kane')) as conn:
            current = manager._observation('kane', conn)[0]
        assert current[1] != original[1]
        assert current[:1] + current[2:] == original[:1] + original[2:]
        assert manager._contracts['kane'] == original
        assert manager._physical_owners['kane'].identity[-1] == current[1]
        manager.refresh('kane')
        assert manager.run_once()
        assert manager.status('kane')['hard_gate'] == 'allowed'
    finally:
        manager.close()


def test_claim_refuses_current_cookie_with_changed_schema_contract(lan):
    from evolvmem.health_generation import prepare
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        with service.store.transaction():
            service.store._connection().execute('CREATE TABLE synthetic_changed_contract(id INTEGER)')
        prepare(service.store)
        with pytest.raises(RuntimeError, match='health_boundary_changed'):
            manager.claim_resident_physical()
        assert service._integrity_owner is None
        assert manager._closed
    finally:
        manager.close()


def test_schema_cookie_change_after_claim_is_not_automatically_rebound(lan):
    runtime, _ = lan
    manager = manager_for(runtime)
    service = runtime.server_for('kane').context_service
    try:
        claim(manager)
        prior = manager._physical_owners['kane'].identity
        recreate_same_schema(service.store)
        assert service._integrity_diagnostics() == ('quick_check_failed',)
        assert manager._physical_owners['kane'].identity == prior
        manager.refresh('kane')
        assert manager.run_once()
        assert manager.status('kane')['hard_gate'] != 'allowed'
    finally:
        manager.close()
