"""Offline scheduler/lifecycle checks: synthetic encoders, never a real model/DB."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# A candidate can be reviewed against an unchanged source tree without copying
# its models, credentials, production data or package files into this artifact.
ROOT = Path(__file__).resolve().parents[2]
source = os.environ.get('EVOLVMEM_TEST_SOURCE_ROOT')
if source:
    sys.path.insert(0, source)
    import evolvmem
    evolvmem.__path__.insert(0, str(ROOT / 'evolvmem'))
    import evolvmem.pg_vector_port
    evolvmem.pg_vector_port.__path__.insert(0, str(ROOT / 'evolvmem/pg_vector_port'))

from evolvmem.config import Config
from evolvmem.pg_vector_port import encoder
from evolvmem.pg_vector_port.model import ModelContract, VectorError
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS


def synthetic_child(connection, config, revision, startup_seconds, cpu_offset=0):
    """Real owned IPC/process lifecycle with no engine import or model load."""
    contract = ModelContract.from_config(config, input_tokens=EMBEDDING_INPUT_TOKENS,
                                         model_revision=revision)
    connection.send(('ready', contract.key))
    try:
        while True:
            request = connection.recv()
            if request == ('close',):
                return
            kind, ident, text = request
            if text == 'hang':
                time.sleep(30)
            if text == 'unknown':
                connection.send(('error', 'embedding_engine_failed', 'RuntimeError'))
            else:
                connection.send(('embedding', ident, (float(len(text)),) + (0.,) * 767))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class ControlledWorkers:
    """Replace only the slow native provider boundary; keep the pool real."""
    def __init__(self, *, hold=False, mismatch=None, startup_delay=0):
        self.workers = []
        self.condition = threading.Condition()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.started = []
        self.maximum_active = self.active = 0
        self.mismatch, self.startup_delay = mismatch, startup_delay

    def factory(self, config, *, startup_timeout=30, encode_timeout=10, cpu_offset=0):
        owner = self
        index = len(self.workers)
        class Worker:
            def __init__(self):
                self.config = deepcopy(config)
                self.startup_timeout, self.encode_timeout = startup_timeout, encode_timeout
                self.cpu_offset = cpu_offset
                self.loaded = self.closed = False
                self.initializations = 0
                revision = 'b' * 64 if owner.mismatch == index else 'a' * 64
                self.contract = ModelContract.from_config(config, input_tokens=EMBEDDING_INPUT_TOKENS,
                                                          model_revision=revision)
                self.model_revision = revision
            @property
            def is_loaded(self):
                return self.loaded and not self.closed
            def initialize(self):
                self.initializations += 1
                if owner.startup_delay:
                    time.sleep(owner.startup_delay)
                self.loaded = True
                return self.contract
            def encode_query(self, text, *, deadline=None):
                return self._encode('query', text, deadline)
            def encode_document(self, text, *, deadline=None):
                return self._encode('document', text, deadline)
            def _encode(self, kind, text, deadline):
                with owner.condition:
                    if not self.is_loaded:
                        raise VectorError('embedding_provider_unavailable')
                    owner.active += 1
                    owner.maximum_active = max(owner.maximum_active, owner.active)
                    owner.started.append((index, kind, text, deadline))
                    owner.condition.notify_all()
                try:
                    if text == 'unknown':
                        raise RuntimeError('synthetic native failure')
                    while not owner.release.wait(.01):
                        if self.closed:
                            raise VectorError('embedding_provider_closed')
                        if time.monotonic() >= deadline:
                            raise VectorError('embedding_encode_timeout')
                    if self.closed:
                        raise VectorError('embedding_provider_closed')
                    return (float(sum(text.encode('utf-8'))),) + (0.,) * 767
                finally:
                    with owner.condition:
                        owner.active -= 1
                        owner.condition.notify_all()
            def close(self):
                self.closed = True
                self.loaded = False
                return {'stopped': True}
        worker = Worker()
        self.workers.append(worker)
        return worker

    def wait_started(self, count):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.started) >= count, 2)


class PoolChecks(unittest.TestCase):
    def make_pool(self, workers, **kwargs):
        pool_class = getattr(encoder, 'ManagedEmbeddingPool', None)
        self.assertIsNotNone(pool_class, 'missing bounded concurrent owned encoder pool')
        with patch.object(encoder, 'ManagedEmbeddingProvider', workers.factory):
            pool = pool_class(Config(apply_environment=False), **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_four_distinct_cold_requests_execute_on_four_slots(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        contract = pool.initialize()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(pool.encode_query, 'cold-' + str(i), deadline=time.monotonic() + 3)
                       for i in range(4)]
            try:
                self.assertTrue(workers.wait_started(4), 'requests were serialized or rejected')
                self.assertEqual(workers.maximum_active, 4)
                self.assertEqual(len({row[0] for row in workers.started}), 4)
                self.assertEqual([w.cpu_offset for w in workers.workers], [0, 2, 4, 6])
            finally:
                workers.release.set()
            values = [future.result(2)[0] for future in futures]
        self.assertEqual(values, [511., 512., 513., 514.])
        contract.check_provider(pool, input_tokens=EMBEDDING_INPUT_TOKENS)
        pool.initialize()
        self.assertEqual([w.initializations for w in workers.workers], [1, 1, 1, 1])

    def test_twenty_cold_queries_complete_without_more_than_four_native_calls(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(pool.encode_query, f'different-{i}', deadline=time.monotonic() + 3)
                       for i in range(20)]
            self.assertTrue(workers.wait_started(4))
            workers.release.set()
            results = [future.result(2)[0] for future in futures]
        self.assertEqual(len(results), 20)
        self.assertEqual({row[2] for row in workers.started}, {f'different-{i}' for i in range(20)})
        self.assertEqual(workers.maximum_active, 4)

    def test_queued_deadline_does_not_close_or_replace_active_workers(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=4) as executor:
            active = [executor.submit(pool.encode_document, f'active-{i}', deadline=time.monotonic() + 3)
                      for i in range(4)]
            try:
                self.assertTrue(workers.wait_started(4))
                with self.assertRaisesRegex(VectorError, 'embedding_request_deadline_exceeded'):
                    pool.encode_query('must-not-start', deadline=time.monotonic() + .04)
                self.assertTrue(pool.is_loaded)
                self.assertTrue(all(not w.closed for w in workers.workers))
                self.assertEqual(len(workers.started), 4)
            finally:
                workers.release.set()
            self.assertTrue(all(f.result(2)[0] > 0 for f in active))
        self.assertGreater(pool.encode_query('still-usable')[0], 0)

    def test_waiter_backpressure_is_bounded_and_does_not_spawn_pool_threads(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=36) as executor:
            active = [executor.submit(pool.encode_query, f'active-{i}', deadline=time.monotonic() + 4)
                      for i in range(4)]
            self.assertTrue(workers.wait_started(4))
            waiting = [executor.submit(pool.encode_query, f'waiting-{i}', deadline=time.monotonic() + 4)
                       for i in range(32)]
            try:
                with pool._condition:
                    self.assertTrue(pool._condition.wait_for(lambda: len(pool._waiters) == 32, 2))
                before = {t.ident for t in threading.enumerate()}
                with self.assertRaisesRegex(VectorError, 'embedding_provider_busy'):
                    pool.encode_query('overflow', deadline=time.monotonic() + 1)
                self.assertEqual({t.ident for t in threading.enumerate()}, before)
                self.assertEqual(len(workers.started), 4)
            finally:
                workers.release.set()
            self.assertTrue(all(f.result(2)[0] > 0 for f in active + waiting))

    def test_worker_revision_or_full_query_contract_mismatch_closes_every_worker(self):
        for mismatch in ('revision', 'query-prefix'):
            workers = ControlledWorkers(mismatch=2 if mismatch == 'revision' else None)
            pool = self.make_pool(workers)
            if mismatch == 'query-prefix':
                workers.workers[2].contract = replace(workers.workers[2].contract, query_prefix='changed: ')
            with self.assertRaisesRegex(VectorError, 'embedding_contract_changed'):
                pool.initialize()
            self.assertFalse(pool.is_loaded)
            self.assertTrue(all(w.closed for w in workers.workers))

    def test_startup_uses_one_total_deadline_and_never_retries_a_partial_start(self):
        workers = ControlledWorkers(startup_delay=.03)
        pool = self.make_pool(workers, startup_timeout=.05)
        began = time.monotonic()
        with self.assertRaisesRegex(VectorError, 'embedding_initialize_timeout'):
            pool.initialize()
        self.assertLess(time.monotonic() - began, .2)
        self.assertLessEqual(sum(w.initializations for w in workers.workers), 2)
        self.assertTrue(all(w.closed for w in workers.workers))
        with self.assertRaisesRegex(VectorError, 'embedding_provider_closed'):
            pool.initialize()

    def test_unknown_started_encode_error_fails_pool_closed_without_restart(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=3) as executor:
            active = [executor.submit(pool.encode_query, f'active-{i}', deadline=time.monotonic() + 2)
                      for i in range(3)]
            self.assertTrue(workers.wait_started(3))
            with self.assertRaisesRegex(RuntimeError, 'synthetic native failure'):
                pool.encode_query('unknown')
            for future in active:
                with self.assertRaises(VectorError):
                    future.result(2)
        self.assertTrue(all(w.closed for w in workers.workers))
        with self.assertRaises(VectorError):
            pool.encode_query('no-retry')
        with self.assertRaisesRegex(VectorError, 'embedding_provider_closed'):
            pool.initialize()
        self.assertEqual(len(workers.started), 4)
        self.assertEqual([w.initializations for w in workers.workers], [1] * 4)

    def test_close_wakes_queued_callers_and_attempts_every_owned_worker(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(pool.encode_query, f'call-{i}', deadline=time.monotonic() + 3)
                       for i in range(5)]
            self.assertTrue(workers.wait_started(4))
            pool.close()
            for future in futures:
                with self.assertRaises(VectorError):
                    future.result(2)
        self.assertTrue(all(w.closed for w in workers.workers))
        self.assertEqual(pool.close(), {'stopped': True})

    def test_one_shutdown_error_does_not_skip_other_owned_workers(self):
        workers = ControlledWorkers()
        pool = self.make_pool(workers)
        pool.initialize()
        original = workers.workers[0].close
        def fail_after_close():
            original()
            raise VectorError('synthetic_shutdown_incomplete')
        workers.workers[0].close = fail_after_close
        try:
            with self.assertRaisesRegex(VectorError, 'embedding_shutdown_incomplete'):
                pool.close()
            self.assertTrue(all(w.closed for w in workers.workers))
        finally:
            workers.workers[0].close = original
        self.assertEqual(pool.close(), {'stopped': True})

    def test_bad_input_or_expired_admission_never_reaches_a_worker(self):
        workers = ControlledWorkers()
        pool = self.make_pool(workers)
        pool.initialize()
        for text, deadline, code in ((None, None, 'invalid_embedding_input'),
                ('query', float('nan'), 'invalid_embedding_deadline'),
                ('query', True, 'invalid_embedding_deadline'),
                ('query', time.monotonic() - 1, 'embedding_request_deadline_exceeded')):
            with self.subTest(code=code), self.assertRaisesRegex(VectorError, code):
                pool.encode_query(text, deadline=deadline)
        self.assertTrue(pool.is_loaded)
        self.assertEqual(workers.started, [])

    def test_idle_child_death_retires_remaining_workers_before_encode_rejection(self):
        workers = ControlledWorkers()
        pool = self.make_pool(workers)
        pool.initialize()
        workers.workers[0].loaded = False
        self.assertFalse(pool.is_loaded)
        self.assertTrue(all(not w.closed for w in workers.workers), 'health getter must be read-only')
        with self.assertRaisesRegex(VectorError, 'embedding_provider_unavailable'):
            pool.encode_query('must-not-encode')
        self.assertTrue(all(w.closed for w in workers.workers))
        self.assertEqual(workers.started, [])
        with self.assertRaisesRegex(VectorError, 'embedding_provider_closed'):
            pool.initialize()

    def test_contract_access_after_idle_child_death_retires_all_workers(self):
        workers = ControlledWorkers()
        pool = self.make_pool(workers)
        pool.initialize()
        workers.workers[2].loaded = False
        with self.assertRaisesRegex(VectorError, 'embedding_provider_unavailable'):
            _ = pool.contract
        self.assertTrue(all(w.closed for w in workers.workers))
        self.assertEqual(workers.started, [])

    def test_queued_caller_detects_child_death_without_waiting_for_its_deadline(self):
        workers = ControlledWorkers(hold=True)
        pool = self.make_pool(workers)
        pool.initialize()
        with ThreadPoolExecutor(max_workers=5) as executor:
            active = [executor.submit(pool.encode_query, f'active-{i}', deadline=time.monotonic() + 3)
                      for i in range(4)]
            self.assertTrue(workers.wait_started(4))
            waiting = executor.submit(pool.encode_query, 'queued', deadline=time.monotonic() + 3)
            done = threading.Event()
            waiting.add_done_callback(lambda _:done.set())
            try:
                with pool._condition:
                    self.assertTrue(pool._condition.wait_for(lambda: len(pool._waiters) == 1, 1))
                workers.workers[0].loaded = False
                self.assertTrue(done.wait(.5), 'dead child left the queued caller waiting until request expiry')
                with self.assertRaisesRegex(VectorError, 'embedding_provider_unavailable'):
                    waiting.result()
                self.assertTrue(all(w.closed for w in workers.workers))
                self.assertEqual(len(workers.started), 4)
            finally:
                pool.close()
            for future in active:
                with self.assertRaises(VectorError):
                    future.result(1)


class OwnedProcessChecks(unittest.TestCase):
    def config(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Config(data_dir=Path(temporary.name), apply_environment=False)
        config.model_path.parent.mkdir()
        config.model_path.write_bytes(b'synthetic identity only; not a model')
        return config

    def test_native_provider_remains_single_worker_and_retires_timed_out_process(self):
        provider = encoder.ManagedEmbeddingProvider(self.config(), startup_timeout=3, encode_timeout=.06)
        self.addCleanup(provider.close)
        with patch.object(encoder, '_child', synthetic_child):
            provider.initialize()
        pid = provider._process.pid
        self.assertEqual(provider.encode_document('abc')[0], 3.)
        with self.assertRaisesRegex(VectorError, 'embedding_encode_timeout'):
            provider.encode_query('hang')
        self.assertFalse(provider.is_loaded)
        self.assertNotIn(pid, [p.pid for p in multiprocessing.active_children()])
        with self.assertRaisesRegex(VectorError, 'embedding_provider_closed'):
            provider.initialize()

    def test_concurrent_close_never_acknowledges_incomplete_child_termination(self):
        provider = encoder.ManagedEmbeddingProvider(Config(apply_environment=False))
        self.addCleanup(provider.close)
        terminating, release, second_done = threading.Event(), threading.Event(), threading.Event()
        class SlowOwnedProcess:
            alive = True
            def is_alive(self):return self.alive
            def terminate(self):
                terminating.set()
                release.wait(2)
                self.alive = False
            def join(self, timeout):pass
            def close(self):pass
        provider._process = SlowOwnedProcess()
        first = threading.Thread(target=provider.close)
        second = threading.Thread(target=lambda:(provider.close(), second_done.set()))
        first.start()
        try:
            self.assertTrue(terminating.wait(1))
            second.start()
            self.assertFalse(second_done.wait(.04), 'shutdown was acknowledged while the owned child was alive')
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertTrue(second_done.is_set())

    def test_pool_owns_four_real_synthetic_processes_and_closes_all(self):
        pool_class = getattr(encoder, 'ManagedEmbeddingPool', None)
        self.assertIsNotNone(pool_class, 'missing bounded concurrent owned encoder pool')
        pool = pool_class(self.config(), startup_timeout=5)
        self.addCleanup(pool.close)
        with patch.object(encoder, '_child', synthetic_child):
            pool.initialize()
        children = [worker._process.pid for worker in pool._workers]
        self.assertEqual(len(set(children)), 4)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(pool.encode_query, 'cold-' + str(i)) for i in range(4)]
            self.assertEqual([f.result(2)[0] for f in futures], [6.] * 4)
        pool.close()
        self.assertFalse(pool.is_loaded)
        self.assertFalse(set(children) & {p.pid for p in multiprocessing.active_children()})
        self.assertFalse(any(t.name == 'evolvmem-embedding-ipc' for t in threading.enumerate()))

    def test_worker_cpu_pairs_use_available_affinity_without_caller_configuration(self):
        select = getattr(encoder, '_worker_cpus', None)
        self.assertIsNotNone(select, 'missing bounded child affinity partition')
        self.assertEqual([select(range(8), offset) for offset in (0, 2, 4, 6)],
                         [{0, 1}, {2, 3}, {4, 5}, {6, 7}])
        self.assertEqual(select([3], 6), {3})
        self.assertEqual(select([2, 4, 6, 8], 6), {6, 8})


class StartupSelectionChecks(unittest.TestCase):
    def test_only_larger_trusted_host_configuration_selects_pool(self):
        from evolvmem import library_startup as startup
        config = Config(apply_environment=False)
        for maximum, expected in ((1, 'single'), (2, 'single'), (3, 'single'), (4, 'pool'), (20, 'pool'), (32, 'pool')):
            with self.subTest(maximum=maximum):
                selected = []
                def factory(kind):
                    def create(config):
                        selected.append(kind)
                        return SimpleNamespace(initialize=lambda:None, close=lambda:None,
                                               contract=SimpleNamespace(key='trusted'))
                    return create
                cfg = SimpleNamespace(secret_files=(), projection_enabled=False, vector_enabled=True,
                    model_contract='trusted', control_binding=None, content_bindings=(), max_requests=maximum,
                    timeout_seconds=3, acl_control_binding=None, acl_projection_targets=(), recall_enabled=True)
                value = startup.ServerStartup(SimpleNamespace(owner_data_dir=Path('/synthetic')), cfg)
                with patch.object(encoder, 'ManagedEmbeddingProvider', factory('single')), \
                     patch.object(encoder, 'ManagedEmbeddingPool', factory('pool'), create=True), \
                     patch.object(startup.Config, 'from_file', return_value=config), \
                     patch.object(startup, 'LibraryHost', return_value=SimpleNamespace(close=lambda:None)):
                    startup.build_library_host(value, connector=lambda **kw:None)
                self.assertEqual(selected, [expected])


if __name__ == '__main__':
    unittest.main()
