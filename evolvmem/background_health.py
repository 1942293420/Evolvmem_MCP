"""LAN-only last-checked diagnostics; never a global semantic authorization."""
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
import sqlite3
import hashlib
import threading
import time

from evolvmem.context_store import ContextStore
from evolvmem.context_service import ContextService, _health_log_codes
from evolvmem.health_generation import observed
from evolvmem.vector_index import VectorIndex


class BackgroundHealth:
    """One coalescing worker. All SQLite and native scan handles are owned locally.

    Baselines are tied to startup database identities. Replacement requires a
    runtime restart because resident business connections still name the old file.
    Diagnostic semantic staleness does not revoke per-candidate admission.
    """
    def __init__(self, servers, *, start=True, cadence=5.0):
        self.servers = dict(servers)
        self._identities = {key: self._identity(s.config.db_path) for key, s in servers.items()}
        self._contracts = {}
        for name in self.servers:
            with closing(self._connect(name)) as conn:
                self._contracts[name] = self._observation(name, conn)[0]
        self._condition = threading.Condition()
        self._pending = set()
        self._running = None
        self._snapshots = {}
        self._physical_cache = {}
        self._errors = {}
        self._next = {}
        self._last_selected = {}
        self._selection_counter = 0
        self._closed = False
        self._epoch = 0
        self._cadence = max(0.05, cadence)
        self._worker = None
        self.active = False
        if start:
            self.start()

    def start(self):
        with self._condition:
            if self._closed or self._worker is not None:
                return
            self.active = True
            self._pending.update(self.servers)
            self._worker = threading.Thread(target=self._loop, name='evolvmem-health', daemon=True)
            self._worker.start()

    @staticmethod
    def _identity(path):
        value = path.stat()
        return str(path.resolve()), value.st_dev, value.st_ino

    def _connect(self, name):
        path = self.servers[name].config.db_path
        if self._identity(path) != self._identities[name]:
            raise RuntimeError('health_identity_changed')
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return conn

    def _observation(self, name, conn):
        server = self.servers[name]
        config = server.config
        if self._identity(config.db_path) != self._identities[name]:
            raise RuntimeError('health_identity_changed')
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        store = ContextStore(config)
        store._conn = conn
        generation = observed(store, schema)
        if generation is None:
            raise RuntimeError('health_generation_unavailable')
        schema_contract = hashlib.sha256(repr([tuple(row) for row in conn.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]).encode()).hexdigest()
        engine = server.context_service.embedding_engine
        try:
            model_stat = config.model_path.stat()
            model_stamp = (model_stat.st_dev, model_stat.st_ino, model_stat.st_size, model_stat.st_mtime_ns)
        except FileNotFoundError:
            model_stamp = None
        hard = (self._identities[name], schema,
                tuple((k, repr(v)) for k, v in vars(config).items()),
                server.context_mode, server.adapter, server.context_service._mode,
                server.context_service._adapter, model_stamp, schema_contract, id(engine), bool(getattr(engine, 'is_loaded', False)))
        return hard, generation

    def refresh(self, name):
        if name not in self.servers:
            raise ValueError('unknown namespace')
        with self._condition:
            if not self._closed:
                self._pending.add(name)
                self._condition.notify_all()

    def status(self, name):
        if name not in self.servers:
            raise ValueError('unknown namespace')
        observation = None
        try:
            with closing(self._connect(name)) as conn:
                observation = self._observation(name, conn)
        except Exception:
            pass
        now = time.monotonic()
        resident_physical = self.servers[name].context_service._integrity_snapshot
        # LAN bootstrap can recreate schema after the resident baseline. Its
        # old timer then reports an identity mismatch as quick_check_failed.
        # Only a result for the currently observed database/schema is evidence
        # against the independently checked worker baseline.
        current_identity = None if observation is None else (*observation[0][0], observation[0][1])
        known_physical_failure = bool(resident_physical and resident_physical[2]
                                      and resident_physical[0][1:] == current_identity)
        with self._condition:
            saved = self._snapshots.get(name)
            age = None if saved is None else max(0.0, now - saved['completed'])
            ttl = ContextService._integrity_ttl()
            gate = ('health_closed' if self._closed else
                    'health_boundary_unavailable' if observation is None else
                    'health_baseline_pending' if saved is None else
                    'health_boundary_changed' if observation[0] != saved['hard'] else
                    'health_physical_failed' if saved['physical_codes'] or known_physical_failure else
                    'health_contract_invalid' if saved['hard_codes'] else
                    'health_physical_expired' if not ttl or now - saved['physical_checked'] >= ttl else
                    'health_evaluation_failed' if name in self._errors else 'allowed')
            stale = saved is None or observation is None or saved['generation'] != observation[1] or saved['hard'] != observation[0]
            if not self._closed and (stale or gate != 'allowed' or age >= min(ttl * .6, 30.0)):
                self._pending.add(name)
                self._condition.notify_all()
            return deepcopy(dict(
                hard_gate=gate,
                diagnostic=None if saved is None else saved['diagnostic'],
                evaluated_generation=None if saved is None else saved['generation'],
                observed_generation=None if observation is None else observation[1],
                freshness='unavailable' if saved is None else 'stale' if stale else 'current',
                completion_age_seconds=age,
                refresh_pending=name in self._pending or self._running == name,
                error=self._errors.get(name),
            ))

    def _physical_result(self, name, path, expected):
        """Reuse only an owned, successful, current-identity physical proof.

        Semantic generations do not renew its timestamp. Near-expiry refresh
        uses the existing 60% threshold; failed attempts discard old success.
        """
        ttl = ContextService._integrity_ttl()
        now = time.monotonic()
        resident = self.servers[name].context_service._integrity_snapshot
        resident_failed = bool(resident and resident[2] and resident[0][1:] == expected)
        with self._condition:
            if self._closed:
                raise RuntimeError('health_closed')
            epoch = self._epoch
            cached = self._physical_cache.get(name)
            if (not resident_failed and cached is not None and cached[0] == expected
                    and ttl > 0 and 0 <= now - cached[1] < ttl * .6):
                return (), cached[1]
            # Remove before running: an exception/failure must not leave an old
            # success available to the next scan while a refresh has failed.
            self._physical_cache.pop(name, None)
        result = ContextService._scan_integrity_readonly(path, expected)
        checked = time.monotonic()
        with self._condition:
            if self._closed or epoch != self._epoch:
                raise RuntimeError('health_closed')
            if not result:
                self._physical_cache[name] = (expected, checked)
        return result, checked

    def _evaluate(self, name):
        server = self.servers[name]
        config = deepcopy(server.config)
        with closing(self._connect(name)) as conn:
            deadline = time.monotonic() + 60.0
            conn.set_progress_handler(lambda: self._closed or time.monotonic() >= deadline, 1000)
            conn.execute('BEGIN')
            hard, generation = self._observation(name, conn)
            expected_contract = self._contracts[name]
            if hard[:1] + hard[2:] != expected_contract[:1] + expected_contract[2:]:
                raise RuntimeError('health_boundary_changed')
            expected = (*self._identities[name], hard[1])
            physical, physical_checked = self._physical_result(name, config.db_path, expected)
            hard_codes = tuple(config.validate_runtime())
            store = ContextStore(config)
            store._conn = conn
            # A metadata-only engine cannot encode or close the runtime model.
            engine = SimpleNamespace(is_loaded=hard[-1])
            # Isolated native handles, with no shared cache refresh/writer hooks.
            index_config = deepcopy(config)
            index_config.lan_shared_vector_cache = False
            index_config.embedding_http_url = ''
            index_config.lan_mcp_client_config = ''
            index = VectorIndex(index_config, path=config.context_vector_path)
            legacy = VectorIndex(index_config)
            try:
                for owned in (index, legacy):
                    try:
                        owned.initialize(dim=config.embedding_dim)
                    except Exception:
                        owned.close()
                service = ContextService(config, store=store, vector_index=index, embedding_engine=engine)
                service._mode, service._adapter = server.context_mode, server.adapter
                service._legacy_vector = legacy
                service._integrity_diagnostics = lambda: physical
                status = service.evaluated_status()
                if self._observation(name, conn)[0] != hard:
                    raise RuntimeError('health_boundary_changed')
                return dict(hard=hard, generation=generation, completed=time.monotonic(),
                            physical_codes=physical, physical_checked=physical_checked, hard_codes=hard_codes,
                            diagnostic=dict(last_checked_ready=bool(status.ready),
                                            codes=list(_health_log_codes(status.diagnostics))))
            finally:
                index.close()
                legacy.close()

    def run_once(self):
        """Run one queued observation; also usable by an explicit lifecycle host."""
        with self._condition:
            if self._closed or self._running is not None or not self._pending:
                return False
            eligible = [name for name in self._pending if self._next.get(name, 0) <= time.monotonic()]
            if not eligible:
                return False
            self.active = True
            # Least recently selected, including never selected namespaces,
            # remains fair when a scan itself exceeds the refresh interval.
            name = min(eligible, key=lambda key: (self._last_selected.get(key, -1), key))
            self._selection_counter += 1
            self._last_selected[name] = self._selection_counter
            self._pending.remove(name)
            self._running = name
            epoch = self._epoch
        result = None
        try:
            result = self._evaluate(name)
        except Exception:
            pass
        with self._condition:
            self._running = None
            if not self._closed and epoch == self._epoch:
                if result is None:
                    self._errors[name] = 'health_evaluation_failed'
                    self._pending.add(name)
                else:
                    self._snapshots[name] = result
                    self._errors.pop(name, None)
                self._next[name] = time.monotonic() + self._cadence
            self._condition.notify_all()
        return True

    def _loop(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                now = time.monotonic()
                # Periodic observation detects committed writes without requests.
                for name in self.servers:
                    saved = self._snapshots.get(name)
                    if saved is None or now - saved['completed'] >= min(30.0, max(.05, ContextService._integrity_ttl() * .6)):
                        self._pending.add(name)
                eligible = [name for name in self._pending if self._next.get(name, 0) <= now]
                if not eligible:
                    self._condition.wait(timeout=self._cadence)
                    continue
            self.run_once()

    def close(self):
        with self._condition:
            self._closed = True
            self._epoch += 1
            self._pending.clear()
            self._physical_cache.clear()
            self._condition.notify_all()
        # Worker owns its resources until exit; a timeout never closes them.
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=1)
