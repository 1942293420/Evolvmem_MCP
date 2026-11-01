"""Native small SQLite startup/claim regressions, never production or models."""
from contextlib import closing
from types import SimpleNamespace
import pytest

from tests.test_lan_sharing import lan
from evolvmem.context_service import ContextService
from evolvmem.lan_context import prepare_memory_revision
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_tools import LanTools


def test_identical_revision_prepare_preserves_schema_cookie(lan):
    runtime, _ = lan
    server = runtime.server_for('kane')
    conn = server.context_service.store._connection()
    before = conn.execute('PRAGMA schema_version').fetchone()[0]
    prepare_memory_revision(server)
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == before


def test_changed_revision_trigger_is_repaired_and_retains_increment_semantics(lan):
    runtime, _ = lan
    server = runtime.server_for('kane')
    store = server.context_service.store
    conn = store._connection()
    with store.transaction():
        conn.execute('DROP TRIGGER lan_revision_context_items_insert')
        conn.execute('CREATE TRIGGER lan_revision_context_items_insert AFTER INSERT ON context_items '
                     'BEGIN UPDATE lan_memory_revision SET revision=revision+9 WHERE id=1; END')
    before = conn.execute('PRAGMA schema_version').fetchone()[0]
    prepare_memory_revision(server)
    assert conn.execute('PRAGMA schema_version').fetchone()[0] > before
    original = conn.execute('SELECT revision FROM lan_memory_revision WHERE id=1').fetchone()[0]
    with store.transaction():
        conn.execute("INSERT INTO context_items(identity_key,content_type,created_at,updated_at) "
                     "VALUES('synthetic:initial-proof','fact','2026-11-01','2026-11-01')")
    assert conn.execute('SELECT revision FROM lan_memory_revision WHERE id=1').fetchone()[0] == original + 1


def test_warm_lan_claim_reuses_actual_initial_scan_timestamp(lan, monkeypatch):
    previous, _ = lan
    settings = previous.settings
    previous.close()
    # Isolate the physical lifecycle from unrelated vector repair; both actual
    # initial and background physical scans still run on real fixture SQLite.
    monkeypatch.setattr(LanRuntime, 'start_recovery_worker', lambda self: None)
    runtime = LanRuntime(settings)
    try:
        runtime.initialize()
        service = runtime.server_for('kane').context_service
        assert service._integrity_snapshot is not None
        checked_at = service._integrity_snapshot[1]
        LanTools(runtime)
        manager = runtime._background_health
        manager.claim_resident_physical()
        scans = []
        original = ContextService._scan_integrity_readonly
        def counted(path, expected):
            scans.append(True)
            return original(path, expected)
        monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(counted))
        manager.refresh('kane')
        assert manager.run_once()
        assert manager.status('kane')['hard_gate'] == 'allowed'
        assert scans == []
        with closing(manager._connect('kane')) as conn:
            identity = (*manager._identities['kane'], conn.execute('PRAGMA schema_version').fetchone()[0])
        from evolvmem.physical_integrity import config_fingerprint
        view = manager._physical_owners['kane'].observe(identity, config_fingerprint(service.config), 300.)
        assert view.state == 'ok'
        assert view.checked_at == checked_at
    finally:
        runtime.close()


@pytest.mark.parametrize('change',['expired','config_file','config_object','same_schema_new_cookie'])
def test_changed_startup_proof_requires_actual_background_scan(lan, monkeypatch, change):
    previous, _ = lan
    settings = previous.settings
    previous.close()
    monkeypatch.setattr(LanRuntime, 'start_recovery_worker', lambda self: None)
    runtime = LanRuntime(settings)
    try:
        runtime.initialize()
        service = runtime.server_for('kane').context_service
        assert service._initial_physical_proof is not None
        completed = service._integrity_snapshot[1]
        if change == 'config_file':
            (settings.owner_data_dir / 'config.json').write_text('{}')
        elif change == 'config_object':
            service.config.context_inject_max_chars += 1
        elif change == 'same_schema_new_cookie':
            with service.store.transaction():
                service.store._connection().execute('CREATE TABLE initial_proof_cookie(id INTEGER)')
                service.store._connection().execute('DROP TABLE initial_proof_cookie')
        else:
            from evolvmem import background_health
            monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda:completed+300.))
        LanTools(runtime)
        manager = runtime._background_health
        manager.claim_resident_physical()
        owner = manager._physical_owners['kane']
        assert owner._record is None
        scans = []
        original = ContextService._scan_integrity_readonly
        def counted(path, expected):
            scans.append(True)
            return original(path, expected)
        monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(counted))
        assert manager.status('kane')['hard_gate'] != 'allowed'
        manager.refresh('kane')
        assert manager.run_once()
        assert scans == [True]
        assert manager.status('kane')['hard_gate'] == 'allowed'
    finally:
        runtime.close()


def test_failed_initial_scan_never_provides_a_success_receipt(lan, monkeypatch):
    previous, _ = lan
    settings = previous.settings
    previous.close()
    monkeypatch.setattr(LanRuntime, 'start_recovery_worker', lambda self: None)
    original = ContextService._quick_check_diagnostics
    target = settings.data_dir / 'users' / 'kane' / 'memory.db'
    def fail_target(service):
        result = original(service)
        return ('quick_check_failed',) if service.config.db_path == target else result
    monkeypatch.setattr(ContextService, '_quick_check_diagnostics', fail_target)
    runtime = LanRuntime(settings)
    try:
        runtime.initialize()
        service = runtime.server_for('kane').context_service
        assert service._integrity_snapshot[2] == ('quick_check_failed',)
        assert service._initial_physical_proof is None
        LanTools(runtime)
        manager = runtime._background_health
        manager.claim_resident_physical()
        assert manager._physical_owners['kane']._record is None
        assert manager.status('kane')['hard_gate'] != 'allowed'
    finally:
        runtime.close()


def test_finished_failed_timer_invalidates_the_old_startup_receipt(lan, monkeypatch):
    previous, _ = lan
    settings = previous.settings
    previous.close()
    monkeypatch.setattr(LanRuntime, 'start_recovery_worker', lambda self: None)
    runtime = LanRuntime(settings)
    try:
        runtime.initialize()
        service = runtime.server_for('kane').context_service
        assert service._initial_physical_proof is not None
        original = ContextService._scan_integrity_readonly
        def failed(path, expected):
            assert original(path, expected) == ()
            return ('quick_check_failed',)
        monkeypatch.setattr(ContextService, '_scan_integrity_readonly', staticmethod(failed))
        with service._integrity_lock:
            service._start_integrity_refresh(service._integrity_snapshot[0])
        service._integrity_worker.join(3)
        assert not service._integrity_worker.is_alive()
        assert service._integrity_snapshot[2] == ('quick_check_failed',)
        LanTools(runtime)
        manager = runtime._background_health
        manager.claim_resident_physical()
        assert manager._physical_owners['kane']._record is None
        assert service._integrity_diagnostics() == ('quick_check_pending',)
        assert manager.status('kane')['hard_gate'] != 'allowed'
    finally:
        runtime.close()
