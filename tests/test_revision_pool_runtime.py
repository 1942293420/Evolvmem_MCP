"""Real runtime publication/close methods; native initialization tested separately."""
import ast
import concurrent.futures
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from evolvmem import lan_revision as revision

if os.environ.get('EVOLVMEM_REVISION_ISOLATED_TESTS') == '1':
    # Windows lacks application/model dependencies. Execute the actual runtime
    # class body, then supply minimal lifecycle state, without faking SQLite.
    source = Path(revision.__file__).with_name('lan_runtime.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LanRuntime')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = {'threading': threading}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    LanRuntime = namespace['LanRuntime']
else:
    from evolvmem.lan_runtime import LanRuntime


class RuntimePoolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = SimpleNamespace(owner_data_dir=root / 'owner', data_dir=root / 'lan')
        for relative, value in [('owner/memory.db', 7), ('lan/users/kane/memory.db', 12), ('lan/public/memory.db', 19)]:
            path = root / relative
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
                conn.execute('INSERT INTO lan_memory_revision VALUES(1,?)', (value,))
            conn.close()

    def runtime(self):
        runtime = LanRuntime.__new__(LanRuntime)
        runtime.settings = self.settings
        runtime._closed = False
        runtime._resources_lock = threading.RLock()
        runtime._revision_pool_lock = threading.Lock()
        runtime._revision_pool = None
        runtime._capture_stop = threading.Event()
        runtime._recovery_stop = threading.Event()
        runtime._capture_worker = runtime._recovery_worker = None
        runtime._servers = {}
        runtime._shared_engine = None
        runtime._workers_running = {'capture': False, 'recovery': False}
        runtime._resources_closed = False
        self.addCleanup(runtime.close)
        return runtime

    def test_multiple_adapters_get_same_prepared_pool(self):
        runtime = self.runtime()
        self.assertTrue(hasattr(runtime, 'prepare_revision_reader'), 'explicit startup preparation missing')
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
            pools = list(workers.map(lambda _: runtime.prepare_revision_reader(), range(3)))
        self.assertTrue(all(pool is pools[0] for pool in pools))
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('adapter made another pool')):
            self.assertIs(runtime.revision_reader(), pools[0])
            self.assertEqual(pools[0].read('kane'), '12.19')
        runtime.close()
        with self.assertRaises(RuntimeError):
            pools[0].read('kane')
        with self.assertRaises(RuntimeError):
            runtime.revision_reader()

    def test_close_rejects_revision_before_background_worker_join(self):
        runtime = self.runtime()
        self.assertTrue(hasattr(runtime, 'prepare_revision_reader'), 'explicit startup preparation missing')
        pool = runtime.prepare_revision_reader()
        case = self
        class Worker:
            def join(self, timeout):
                with case.assertRaises(RuntimeError):
                    pool.read('kane')
            def is_alive(self):
                return False
        runtime._capture_worker = Worker()
        runtime.close()

    def test_unprepared_getter_never_initializes_in_request(self):
        runtime = self.runtime()
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('online prewarm')):
            with self.assertRaises(RuntimeError):
                runtime.revision_reader()

    def test_close_during_initialization_never_publishes_a_pool(self):
        runtime = self.runtime()
        self.assertTrue(hasattr(runtime, 'prepare_revision_reader'), 'explicit startup preparation missing')
        entered, release = threading.Event(), threading.Event()
        actual = revision.RevisionPool
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test release missing')
            return actual(*args, **kwargs)
        with patch.object(revision, 'RevisionPool', side_effect=delayed):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as workers:
                pending = workers.submit(runtime.prepare_revision_reader)
                try:
                    self.assertTrue(entered.wait(1))
                    runtime.close()
                finally:
                    release.set()
                with self.assertRaises(RuntimeError):
                    pending.result(timeout=1)
        self.assertIsNone(runtime._revision_pool)
