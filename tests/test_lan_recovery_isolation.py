"""The background recovery worker must not share request transaction ownership."""
import threading
import time

from tests.test_lan_sharing import lan
from tests.test_lan_capture import transcript, upload


def restart_recovery(runtime, monkeypatch, tick):
    import evolvmem.lan_runtime as module
    runtime._recovery_stop.set()
    runtime._recovery_worker.join(3)
    assert not runtime._recovery_worker.is_alive()
    runtime._recovery_worker = None
    monkeypatch.setattr(module, '_RECOVERY_POLL_SECONDS', .01)
    monkeypatch.setattr(runtime._vector_recovery, 'tick', tick)
    runtime.start_recovery_worker()


def test_upload_receipt_cannot_be_rolled_back_by_recovery_transaction(lan, monkeypatch):
    runtime, adapter = lan
    resident = runtime.server_for('jiangli').context_service
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    uploaded, failures = [], []
    def tick(services):
        worker = next(s for s in services if s.config.data_dir == resident.config.data_dir)
        try:
            with worker.store.transaction():
                entered.set()
                assert release.wait(3)
                raise RuntimeError('rollback only background work')
        except RuntimeError:
            pass
        return ()
    restart_recovery(runtime, monkeypatch, tick)
    assert entered.wait(3)
    def request():
        try:
            uploaded.append(upload(adapter, transcript(), end=64))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
    request_thread = threading.Thread(target=request)
    request_thread.start()
    try:
        # A real second writer waits briefly for this transaction; it must
        # never join its depth and report an uncommitted receipt as success.
        acknowledged_before_release = finished.wait(.05)
    finally:
        runtime._recovery_stop.set()
        release.set()
        request_thread.join(3)
        runtime._recovery_worker.join(3)
    assert not request_thread.is_alive() and not runtime._recovery_worker.is_alive()
    assert not failures
    assert uploaded[0].get('status') == 'receiving', uploaded
    row = resident.store._connection().execute(
        "SELECT received_bytes FROM lan_session_uploads WHERE device_id='windows-main' AND session_id='session-1'").fetchone()
    assert row is not None and row[0] == 64, 'a successful upload was lost to recovery rollback'
    assert not acknowledged_before_release


def test_recovery_staging_owns_store_and_index_without_blocking_upload(lan, monkeypatch):
    runtime, adapter = lan
    resident = runtime.server_for('jiangli').context_service
    entered, release = threading.Event(), threading.Event()
    observed = []
    def tick(services):
        worker = next(s for s in services if s.config.data_dir == resident.config.data_dir)
        observed.append((worker.store, worker.store._connection(), worker.vector_index, worker.embedding_engine))
        entered.set()
        assert release.wait(3)  # Slow preparation/encoding must not own dispatch lock.
        return ()
    restart_recovery(runtime, monkeypatch, tick)
    assert entered.wait(3)
    try:
        began = time.monotonic()
        result = upload(adapter, transcript(), end=64)
        elapsed = time.monotonic()-began
    finally:
        runtime._recovery_stop.set()
        release.set()
        runtime._recovery_worker.join(3)
    assert result.get('status') == 'receiving'
    assert elapsed < 1.0
    worker_store, worker_conn, worker_index, engine = observed[0]
    assert worker_store is not resident.store
    assert worker_conn is not resident.store._connection()
    assert worker_index is not resident.vector_index
    assert engine is runtime._shared_engine
    assert worker_store._conn is None, 'worker connection must close when its thread exits'
    assert worker_index._index is None, 'worker index must close when its thread exits'


def stopped_worker(runtime):
    runtime._recovery_stop.set()
    runtime._recovery_worker.join(3)
    assert not runtime._recovery_worker.is_alive()
    runtime._vector_recovery._next_attempt_at = 0


def loaded_fixture_engine(runtime):
    engine = runtime._shared_engine
    engine.is_loaded = True
    engine.encode_document = lambda text: [1.0] + [0.0] * (runtime._base_config.embedding_dim-1)


def test_corrupt_namespace_index_still_reaches_atomic_recovery(lan):
    from tests.test_lan_sharing import add
    runtime, adapter = lan
    stopped_worker(runtime)
    saved = add(adapter, 'jiangli')
    server = runtime.server_for('jiangli')
    server.config.context_vector_path.write_bytes(b'broken native index fixture')
    loaded_fixture_engine(runtime)
    services = runtime._open_recovery_services()
    try:
        reports = runtime._vector_recovery.tick(services)
        assert any(r.status == 'recovered' for r in reports)
        worker = next(s for s in services if s.config.data_dir == server.config.data_dir)
        assert worker.vector_index.ids() == [saved['context_id']]
        assert not worker.vector_index.is_dirty()
    finally:
        runtime._close_recovery_services(services)


def test_namespace_open_failure_does_not_starve_other_recovery(lan, monkeypatch):
    from evolvmem.context_store import ContextStore
    from tests.test_lan_sharing import add
    runtime, adapter = lan
    stopped_worker(runtime)
    saved = add(adapter, 'kane')
    loaded_fixture_engine(runtime)
    owner_dir = runtime.server_for('jiangli').config.data_dir
    original = ContextStore.initialize
    def initialize(store, **kwargs):
        if store.config.data_dir == owner_dir:
            raise OSError('namespace unavailable fixture')
        return original(store, **kwargs)
    monkeypatch.setattr(ContextStore, 'initialize', initialize)
    services = runtime._open_recovery_services()
    try:
        reports = runtime._vector_recovery.tick(services)
        assert any(r.status == 'failed' for r in reports)
        assert any(r.status == 'recovered' for r in reports)
        assert runtime._vector_recovery.consecutive_failures == 1
        assert not runtime._vector_recovery.due()
        worker = next(s for s in services if s.config.data_dir == runtime.server_for('kane').config.data_dir)
        assert worker.vector_index.ids() == [saved['context_id']]
    finally:
        runtime._close_recovery_services(services)


def test_worker_cleanup_continues_after_one_close_failure(lan):
    from types import SimpleNamespace
    runtime, _ = lan
    closed = []
    class Resource:
        def __init__(self, name, broken=False): self.name, self.broken = name, broken
        def close(self):
            closed.append(self.name)
            if self.broken: raise RuntimeError('close failed fixture')
    runtime._close_recovery_services([
        SimpleNamespace(vector_index=Resource('first-index', True), store=Resource('first-store')),
        SimpleNamespace(vector_index=Resource('second-index'), store=Resource('second-store')),
    ])
    assert closed == ['first-index', 'first-store', 'second-index', 'second-store']


def test_shutdown_keeps_shared_engine_until_both_workers_finish(lan, monkeypatch):
    import evolvmem.lan_runtime as module
    runtime, adapter = lan
    capture_entered, capture_release = threading.Event(), threading.Event()
    recovery_entered, recovery_release = threading.Event(), threading.Event()
    closed = []
    monkeypatch.setattr(runtime._shared_engine, 'close', lambda: closed.append('engine'))
    def tick(services):
        recovery_entered.set()
        assert recovery_release.wait(3)
        return ()
    restart_recovery(runtime, monkeypatch, tick)
    assert recovery_entered.wait(3)
    monkeypatch.setattr(module, '_CAPTURE_POLL_SECONDS', .01)
    def pending():
        capture_entered.set()
        assert capture_release.wait(3)
    monkeypatch.setattr(adapter, 'process_backfills', pending)
    monkeypatch.setattr(adapter, 'process_pending', lambda: None)
    runtime.start_capture_worker(adapter)
    assert capture_entered.wait(3)
    original_join = runtime._capture_worker.join
    monkeypatch.setattr(runtime._capture_worker, 'join', lambda timeout=None: recovery_release.set())
    try:
        runtime.close()
        assert runtime._capture_worker.is_alive()
        assert not closed, 'recovery exit closed the engine while capture still uses it'
    finally:
        capture_release.set()
        original_join(3)
        recovery_release.set()
        runtime._recovery_worker.join(3)
    assert not runtime._capture_worker.is_alive()
    assert closed == ['engine']
