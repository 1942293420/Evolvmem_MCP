"""Real SQLite contracts for the revision-only pool; also runnable by unittest."""
import concurrent.futures
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evolvmem import lan_revision as revision


class RevisionPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = SimpleNamespace(owner_data_dir=root / 'owner', data_dir=root / 'lan')
        self.paths = {'jiangli': root / 'owner/memory.db', 'kane': root / 'lan/users/kane/memory.db',
                      'public': root / 'lan/public/memory.db'}
        for key, path in self.paths.items():
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
                conn.execute('INSERT INTO lan_memory_revision VALUES(1,?)',
                             ({'jiangli': 7, 'kane': 12, 'public': 19}[key],))
            conn.close()

    def pool(self, **kwargs):
        # The first regression runs against the original reader too: it must
        # fail on its online connect, not on an import of an unimplemented class.
        cls = getattr(revision, 'RevisionPool', revision.RevisionReader)
        value = cls(self.settings, **kwargs)
        if hasattr(value, 'close'):
            self.addCleanup(value.close)
        return value

    def commit(self, key, value):
        with sqlite3.connect(self.paths[key]) as conn:
            conn.execute('UPDATE lan_memory_revision SET revision=? WHERE id=1', (value,))
        conn.close()

    def test_warm_reads_never_connect_or_attach_and_observe_new_commits(self):
        pool = self.pool()
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('online cold connect')):
            self.assertEqual(pool.read('kane'), '12.19')
        self.commit('kane', 24)
        self.commit('public', 31)
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('online cold connect')):
            self.assertEqual(pool.read('kane'), '24.31')
            self.assertEqual(pool.read('jiangli'), '7.31')

    def test_cold_attach_deadline_failure_is_absent_from_prewarmed_requests(self):
        state = {'delay_attach': False}
        connect = sqlite3.connect
        class SlowAttach(sqlite3.Connection):
            def set_progress_handler(self, callback, interval):
                # Deterministic progress callback on the real ATTACH VM after
                # an injected scheduling delay, instead of depending on schema size.
                return super().set_progress_handler(callback, 1 if callback else interval)
            def execute(self, sql, *args, **kwargs):
                if state['delay_attach'] and sql.startswith('ATTACH DATABASE'):
                    time.sleep(.3)
                return super().execute(sql, *args, **kwargs)
        with patch.object(revision.sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=SlowAttach, **kw)):
            pool = self.pool()
            state['delay_attach'] = True
            with self.assertRaises(sqlite3.OperationalError) as raised:
                revision.RevisionReader(self.settings).read('kane')
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
            self.assertEqual(pool.read('kane'), '12.19')

    def test_default_file_checker_opens_no_connections(self):
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('identity checker opened DB')):
            revision.RevisionReader(self.settings)._check()

    def test_uncommitted_resident_changes_are_invisible(self):
        pool = self.pool()
        writer = sqlite3.connect(self.paths['kane'])
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE lan_memory_revision SET revision=999')
        self.assertEqual(pool.read('kane'), '12.19')
        writer.rollback()
        self.commit('kane', 42)
        self.assertEqual(pool.read('kane'), '42.19')

    def test_no_idle_snapshot_and_runtime_close_closes_all_four_handles(self):
        handles = []
        connect = sqlite3.connect
        def capture(*args, **kwargs):
            conn = connect(*args, **kwargs)
            handles.append(conn)
            return conn
        with patch.object(revision.sqlite3, 'connect', side_effect=capture):
            pool = self.pool()
            self.assertEqual(pool.read('kane'), '12.19')
            self.assertEqual(pool.read('jiangli'), '7.19')
        self.assertEqual(len(handles), 4)
        for conn in handles:
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone(), (1,))
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('UPDATE lan_memory_revision SET revision=999')
        self.commit('kane', 29)
        writer = sqlite3.connect(self.paths['kane'])
        try:
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
        finally:
            writer.close()
        pool.close()
        for conn in handles:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_same_inode_schema_change_cannot_return_cached_revision(self):
        pool = self.pool()
        self.assertEqual(pool.read('kane'), '12.19')
        with sqlite3.connect(self.paths['public']) as writer:
            writer.execute('DROP TABLE lan_memory_revision')
        writer.close()
        with self.assertRaises(sqlite3.OperationalError):
            pool.read('kane')
        with sqlite3.connect(self.paths['public']) as writer:
            writer.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
            writer.execute('INSERT INTO lan_memory_revision VALUES(1,55)')
        writer.close()
        self.assertEqual(pool.read('kane'), '12.55')

    @unittest.skipUnless(os.name == 'posix', 'Linux permission semantics require native fixture')
    def test_mode_change_fails_closed_even_with_existing_open_handles(self):
        pool = self.pool()
        path = self.paths['kane']
        old = path.stat().st_mode
        path.chmod(0o400)
        self.addCleanup(path.chmod, old)
        with self.assertRaises(RuntimeError):
            pool.read('kane')

    def test_namespace_replacement_fails_closed(self):
        pool = self.pool()
        original = self.paths['kane']
        old = original.with_name('original.db')
        try:
            original.rename(old)
        except PermissionError:
            self.skipTest('Windows may deny renaming SQLite open files; native Linux test required')
        with sqlite3.connect(original) as conn:
            conn.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
            conn.execute('INSERT INTO lan_memory_revision VALUES(1,999)')
        conn.close()
        with self.assertRaises(RuntimeError):
            pool.read('kane')

    def test_query_interrupt_is_cleaned_and_does_not_poison_next_lease(self):
        state = {'slow': False}
        connect = sqlite3.connect
        class SlowConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if state['slow'] and sql.startswith('SELECT p.revision'):
                    return super().execute('WITH RECURSIVE t(x) AS (VALUES(0) UNION ALL '
                        'SELECT x+1 FROM t WHERE x<100000000) SELECT sum(x) FROM t')
                return super().execute(sql, *args, **kwargs)
        with patch.object(revision.sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=SlowConnection, **kw)):
            pool = self.pool()
        state['slow'] = True
        with self.assertRaises(sqlite3.OperationalError) as raised:
            pool.read('kane')
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        state['slow'] = False
        self.commit('kane', 35)
        self.assertEqual(pool.read('kane'), '35.19')
        self.assertEqual(pool.read('kane'), '35.19')

    def test_pool_is_bounded_and_close_wakes_waiter_without_cross_thread_close(self):
        state = {'block': False, 'targets': set()}
        entered = threading.Semaphore(0)
        release = threading.Event()
        connect = sqlite3.connect
        class BlockingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if state['block'] and self in state['targets'] and sql.startswith('SELECT p.revision'):
                    entered.release()
                    if not release.wait(2):
                        raise AssertionError('test release missing')
                return super().execute(sql, *args, **kwargs)
        with patch.object(revision.sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=BlockingConnection, **kw)):
            pool = self.pool()
        self.assertTrue(hasattr(pool, 'close'), 'revision must own a closeable bounded pool')
        state['targets'] = set(pool._idle['kane'])
        state['block'] = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
            first = workers.submit(pool.read, 'kane')
            second = workers.submit(pool.read, 'kane')
            try:
                self.assertTrue(entered.acquire(timeout=1))
                self.assertTrue(entered.acquire(timeout=1))
                self.assertEqual(pool.read('jiangli'), '7.19')
                waiting = threading.Event()
                wait = pool._condition.wait
                def observed_wait(timeout):
                    waiting.set()
                    return wait(timeout)
                with patch.object(pool._condition, 'wait', side_effect=observed_wait), patch.object(revision, '_LEASE_SECONDS', .2):
                    third = workers.submit(pool.read, 'kane')
                    self.assertTrue(waiting.wait(1))
                    pool.close()
                    # Without notify_all this would wait for the .2s lease cap.
                    with self.assertRaises(RuntimeError):
                        third.result(timeout=.1)
            finally:
                release.set()
            for pending in (first, second):
                with self.assertRaises((RuntimeError, sqlite3.OperationalError)):
                    pending.result(timeout=1)
        with self.assertRaises(RuntimeError):
            pool.read('jiangli')

    def test_partial_warmup_failure_closes_previously_opened_connections(self):
        handles = []
        connect = sqlite3.connect
        def capture(*args, **kwargs):
            if len(handles) == 2:
                raise sqlite3.OperationalError('synthetic initialization failure')
            conn = connect(*args, **kwargs)
            handles.append(conn)
            return conn
        self.assertTrue(hasattr(revision, 'RevisionPool'), 'prewarm lifecycle missing')
        with patch.object(revision.sqlite3, 'connect', side_effect=capture):
            with self.assertRaises(sqlite3.OperationalError):
                revision.RevisionPool(self.settings)
        for conn in handles:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_lease_delay_uses_existing_deadline_and_never_returns_late_success(self):
        clock = [0.0]
        pool = self.pool()
        acquire = pool._acquire
        def delayed(user, deadline):
            conn = acquire(user, deadline)
            clock[0] = .26
            return conn
        with patch.object(revision, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            with patch.object(pool, '_acquire', side_effect=delayed):
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    pool.read('kane')
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertEqual(pool.read('kane'), '12.19')

    def test_post_query_and_cleanup_time_cannot_publish_overdue_success(self):
        clock = [0.0]
        pool = self.pool()
        clean = pool._clean
        def delayed(conn):
            clean(conn)
            clock[0] = .26
        with patch.object(revision, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            with patch.object(pool, '_clean', side_effect=delayed):
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    pool.read('kane')
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertEqual(pool.read('kane'), '12.19')

    def test_cleanup_failure_retires_slot_and_other_slot_is_not_poisoned(self):
        state = {'fail': False}
        retired = []
        connect = sqlite3.connect
        class FailingRollback(sqlite3.Connection):
            def rollback(self):
                if state['fail']:
                    state['fail'] = False
                    retired.append(self)
                    raise sqlite3.OperationalError('synthetic cleanup failure')
                return super().rollback()
        with patch.object(revision.sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=FailingRollback, **kw)):
            pool = self.pool()
        state['fail'] = True
        with self.assertRaises(sqlite3.OperationalError):
            pool.read('kane')
        with self.assertRaises(sqlite3.ProgrammingError):
            retired[0].execute('SELECT 1')
        self.commit('kane', 45)
        with patch.object(revision.sqlite3, 'connect', side_effect=AssertionError('online refill')):
            self.assertEqual(pool.read('kane'), '45.19')

    @unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: 0)() != 0,
                         'non-root Linux directory-access semantics require native fixture')
    def test_directory_access_revoked_is_rejected_by_stat(self):
        pool = self.pool()
        parent = self.paths['kane'].parent
        old = parent.stat().st_mode
        self.addCleanup(parent.chmod, old)
        parent.chmod(0)
        with self.assertRaises(PermissionError):
            pool.read('kane')

    def test_unknown_namespace_never_uses_a_valid_users_slot(self):
        pool = self.pool()
        with self.assertRaises((ValueError, KeyError)):
            pool.read('other')
        self.assertEqual(pool.read('jiangli'), '7.19')

    def test_page_cache_is_bounded_for_both_attached_databases(self):
        pool = self.pool()
        with pool._condition:
            handles = [conn for values in pool._idle.values() for conn in values]
        for conn in handles:
            self.assertEqual(conn.execute('PRAGMA main.cache_size').fetchone(), (-256,))
            self.assertEqual(conn.execute('PRAGMA public.cache_size').fetchone(), (-256,))
            self.assertEqual(conn.execute('PRAGMA main.mmap_size').fetchone(), (0,))
            self.assertEqual(conn.execute('PRAGMA public.mmap_size').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
