"""Resident integrity results must belong to the current database/schema."""
import time
import pytest
from tests.test_lan_sharing import lan


def checked_manager(runtime):
    manager = runtime._background_health
    manager.refresh('kane')
    assert manager.run_once()
    assert manager.status('kane')['diagnostic']['last_checked_ready'] is True
    return manager


def test_real_stale_schema_integrity_refresh_cannot_revoke_current_baseline(lan):
    runtime, _ = lan
    manager = checked_manager(runtime)
    service = runtime.server_for('kane').context_service
    previous = service._integrity_snapshot
    current_schema = service.store._connection().execute('PRAGMA schema_version').fetchone()[0]
    assert previous[0][-1] != current_schema, 'LAN bootstrap changes the startup schema cookie'
    assert manager.status('kane')['hard_gate'] == 'allowed'
    # Run the real owned checker as the old timer would; no fabricated failure.
    with service._integrity_lock:
        service._start_integrity_refresh(previous[0])
    service._integrity_worker.join(timeout=3)
    assert not service._integrity_worker.is_alive()
    assert service._integrity_snapshot[2] == ('quick_check_failed',)
    assert service._integrity_snapshot[0] == previous[0]
    result = manager.status('kane')
    assert result['diagnostic']['last_checked_ready'] is True
    assert result['hard_gate'] == 'allowed', result


def test_current_identity_resident_failure_still_revokes_good_worker_baseline(lan, monkeypatch):
    runtime, _ = lan
    manager = checked_manager(runtime)
    service = runtime.server_for('kane').context_service
    assert service._integrity_diagnostics() == ()
    current_key = service._integrity_snapshot[0]
    # A new failure from the current-key checker must still veto service.
    monkeypatch.setattr(service, '_scan_integrity_readonly', lambda *_: ('quick_check_failed',))
    with service._integrity_lock:
        service._start_integrity_refresh(current_key)
    service._integrity_worker.join(timeout=3)
    assert not service._integrity_worker.is_alive()
    assert service._integrity_snapshot[0] == current_key
    assert service._integrity_snapshot[2] == ('quick_check_failed',)
    assert manager.status('kane')['hard_gate'] == 'health_physical_failed'


@pytest.mark.parametrize('guard', ['missing', 'expired', 'worker_failed', 'boundary_changed'])
def test_stale_resident_failure_does_not_bypass_independent_hard_guards(lan, guard):
    runtime, _ = lan
    manager = checked_manager(runtime)
    service = runtime.server_for('kane').context_service
    old = service._integrity_snapshot
    service._integrity_snapshot = (old[0], time.monotonic(), ('quick_check_failed',))
    if guard == 'missing':
        manager._snapshots.clear()
    elif guard == 'expired':
        manager._snapshots['kane']['physical_checked'] -= 10000
    elif guard == 'worker_failed':
        manager._snapshots['kane']['physical_codes'] = ('quick_check_failed',)
    else:
        service.config.embedding_dim += 1
    assert manager.status('kane')['hard_gate'] != 'allowed'
