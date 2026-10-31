"""Fixed-namespace, shared-model runtime for the trusted-LAN MCP slice."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.embedding import EmbeddingEngine
from evolvmem.lan_config import LanSettings
from evolvmem.lan_daily_rollup import LanDailyRollup
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.recall_recovery import ContextVectorRecoveryLoop
from evolvmem.capability_recovery import CapabilityVectorRecoveryLoop
from evolvmem.workspace_identity import WorkspaceIdentityProvider
from evolvmem.vector_index import VectorIndex


_USERS = frozenset({"jiangli", "kane"})
_SPACES = frozenset({"personal", "public"})
_CAPTURE_POLL_SECONDS = 5.0
_RECOVERY_POLL_SECONDS = 5.0


class _UnavailableEmbeddingEngine:
    """Explicit optional-model boundary that keeps SQLite and FTS usable."""

    is_loaded = False

    def initialize(self) -> None:
        return None

    def close(self) -> None:
        return None


class _SerializedEmbeddingEngine:
    """One external embedding engine with serialized lifecycle and encodes."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

    @property
    def is_loaded(self):
        return bool(getattr(self._engine, "is_loaded", False))

    @property
    def dim(self):
        return self._engine.dim

    @property
    def reranker_loaded(self):
        return bool(getattr(self._engine, 'reranker_loaded', False))

    def rerank(self, query, documents):
        with self._lock:
            return self._engine.rerank(query, documents)

    @property
    def config(self):
        return self._engine.config

    def encode(self, text):
        with self._lock:
            return self._engine.encode(text)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
            self._engine.initialize()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            close = getattr(self._engine, "close", None)
            if callable(close):
                close()

    def encode_document(self, text):
        with self._lock:
            return self._engine.encode_document(text)

    def encode_query(self, text):
        with self._lock:
            return self._engine.encode_query(text)


class LanRuntime:
    """Own the three fixed SQLite namespaces and a single optional model."""

    def __init__(self, settings: LanSettings, embedding_engine=None) -> None:
        if not isinstance(settings, LanSettings):
            raise TypeError("settings must be a LanSettings instance")
        self.settings = settings
        self._provided_engine = embedding_engine
        self._shared_engine = None
        self._base_config: Config | None = None
        self._servers: dict[tuple[str, str], MemoryMCPServer] = {}
        self._initialized = False
        self._closed = False
        self._capture_worker = None
        self._capture_stop = threading.Event()
        self._vector_recovery = ContextVectorRecoveryLoop()
        self._capability_recovery = CapabilityVectorRecoveryLoop()
        self._capability_recovery_reports: tuple = ()
        # Content-free reports from the most recent recovery rounds, for
        # operators and tests; never transcript, memory, or model content.
        self._vector_recovery_reports: tuple = ()
        self._recovery_worker = None
        self._recovery_stop = threading.Event()
        self._resources_lock = threading.RLock()
        self._workers_running = {'capture': False, 'recovery': False}
        self._resources_closed = False

    def initialize(self) -> None:
        if self._closed:
            raise RuntimeError("LAN runtime is closed")
        if self._initialized:
            return
        self._validate_namespace_databases()
        self._base_config = Config.from_file(
            self.settings.owner_data_dir / "config.json",
            data_dir=self.settings.owner_data_dir,
            apply_environment=False,
        )
        if self.settings.embedding_enabled:
            engine = self._provided_engine
            if engine is None:
                engine = EmbeddingEngine(
                    self._namespace_config(self.settings.owner_data_dir)
                )
            self._shared_engine = _SerializedEmbeddingEngine(engine)
            try:
                self._shared_engine.initialize()
            except Exception:
                # The model is optional: retained unloaded for truthful FTS-only
                # operation without retrying its initialization per namespace.
                pass
        else:
            self._shared_engine = _UnavailableEmbeddingEngine()
        for user, space in (("jiangli", "personal"), ("kane", "personal"),
                            ("jiangli", "public")):
            self._server_for(user, space)
        # A stuck Context vector cache must recover on its own, without the
        # operator restarting anything and without delaying capture work.
        self.start_recovery_worker()
        self._initialized = True

    def server_for(self, user: str, space: str = "personal") -> MemoryMCPServer:
        if self._closed:
            raise RuntimeError("LAN runtime is closed")
        if user not in _USERS:
            raise ValueError("unknown LAN user")
        if space not in _SPACES:
            raise ValueError("unknown LAN space")
        if not self._initialized:
            raise RuntimeError("LAN runtime is not initialized")
        return self._server_for(user, space)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        health = getattr(self, '_background_health', None)
        if health is not None:
            health.close()
        self._capture_stop.set()
        self._recovery_stop.set()
        if self._capture_worker is not None:
            self._capture_worker.join(timeout=5)
        if self._recovery_worker is not None:
            self._recovery_worker.join(timeout=5)
            if self._recovery_worker.is_alive():
                # A long encode is still in flight; it owns final cleanup and
                # must not race the server shutdown it has not finished with.
                return
        if self._capture_worker is not None and self._capture_worker.is_alive():
            # The worker owns final cleanup after its bounded provider call.
            return
        self._close_resources()

    def _close_resources(self):
        with self._resources_lock:
            if self._resources_closed or any(self._workers_running.values()):
                return
            for server in set(self._servers.values()):
                server.shutdown()
            self._servers.clear()
            if self._shared_engine is not None:
                self._shared_engine.close()
            self._resources_closed = True

    def _worker_finished(self, kind):
        # Thread.is_alive() is still true inside finally. Record logical exit
        # only after that worker has released everything it can still use.
        with self._resources_lock:
            self._workers_running[kind] = False
            if self._closed:
                self._close_resources()

    def start_capture_worker(self, adapter):
        # HTTP production hosting starts owned background resources explicitly.
        # Standalone test adapters do not implicitly create a diagnostic thread.
        health = getattr(self, '_background_health', None)
        if health is not None:
            health.start()
        if self._capture_worker is not None:
            return
        daily = LanDailyRollup(adapter)
        from evolvmem.archive_maintenance import LanArchiveMaintenance
        storage = LanArchiveMaintenance(adapter)

        def run():
            try:
                while not self._capture_stop.wait(_CAPTURE_POLL_SECONDS):
                    try:
                        adapter.process_backfills()
                        adapter.process_pending()
                    except Exception:
                        # Jobs remain queryable; never log transcript/provider details.
                        pass
                    try:
                        storage.check()
                    except Exception:
                        pass
                    try:
                        # Uploads and extractions win; daily summaries come after.
                        daily.check()
                    except Exception:
                        # Daily maintenance must never take the capture worker down.
                        pass
            finally:
                self._worker_finished('capture')
        self._capture_worker = threading.Thread(target=run, name='evolvmem-session-extraction', daemon=True)
        with self._resources_lock:
            self._workers_running['capture'] = True
        try:
            self._capture_worker.start()
        except Exception:
            self._worker_finished('capture')
            raise

    def start_recovery_worker(self):
        """One bounded Context-vector recovery thread, separate from capture.

        A rebuild can encode the whole active library, so it must never run on
        the request thread or delay uploads and extractions. The thread owns
        its SQLite connections and vector handles; only the already-serialized
        LAN embedding engine is shared. A request can never join or commit a
        recovery transaction through the resident store's nesting depth.
        """
        if self._recovery_worker is not None:
            return
        self._recovery_stop.clear()
        self._recovery_worker = threading.Thread(
            target=self._recovery_loop,
            name='evolvmem-context-vector-recovery',
            daemon=True,
        )
        with self._resources_lock:
            self._workers_running['recovery'] = True
        try:
            self._recovery_worker.start()
        except Exception:
            self._worker_finished('recovery')
            raise

    def _recovery_loop(self):
        services = ()
        try:
            while not self._recovery_stop.wait(_RECOVERY_POLL_SECONDS):
                try:
                    if not self._vector_recovery.due() and not self._capability_recovery.due():
                        continue
                    services = self._open_recovery_services(services)
                    # Scans and encoding hold no dispatch lock. Only the
                    # existing final writer transaction serializes publication.
                    reports = self._vector_recovery.tick(services)
                    if reports:
                        self._vector_recovery_reports = reports
                    capability_reports = self._capability_recovery.tick(services)
                    if capability_reports:
                        self._capability_recovery_reports = capability_reports
                except Exception:
                    # Recovery must never take this thread (or capture) down.
                    pass
        finally:
            self._close_recovery_services(services)
            self._worker_finished('recovery')

    def _open_recovery_services(self, previous=()):
        """Worker-owned handles, without schema migration or model loading.

        A failed namespace is reported through the normal recovery backoff and
        retried only when due. Healthy namespaces keep their own open handles.
        An unreadable vector file must reach atomic rebuilding, not stop the
        entire worker before its recovery policy can inspect the namespace.
        """
        existing = {s.config.db_path.resolve(): s for s in previous}
        services = []
        for resident in self._live_servers():
            config = deepcopy(resident.config)
            prior = existing.get(config.db_path.resolve())
            if prior is not None and prior.store is not None:
                services.append(prior)
                continue
            store = ContextStore(config)
            index = VectorIndex(config, path=config.context_vector_path)
            service = SimpleNamespace(config=config, store=store,
                                      vector_index=index,
                                      embedding_engine=self._shared_engine)
            try:
                store.initialize(create_schema=False)
                try:
                    index.initialize(dim=config.embedding_dim)
                except Exception:
                    # An uninitialized index yields context_vector_unavailable;
                    # the existing stager can replace its corrupted formal file.
                    index.close()
            except Exception:
                self._close_recovery_services((service,))
                service.store = None
                service.vector_index = None
            services.append(service)
        return tuple(services)

    @staticmethod
    def _close_recovery_services(services):
        for service in services:
            for resource in (service.vector_index, service.store):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        # Release every other handle without masking the
                        # original open/recovery failure or skipping cleanup.
                        pass

    def _live_servers(self):
        """Servers that are fully initialized and safe to touch from the worker."""
        servers = []
        for server in set(self._servers.values()):
            service = getattr(server, 'context_service', None)
            if service is None:
                continue
            config = getattr(service, 'config', None)
            store = getattr(service, 'store', None)
            index = getattr(service, 'vector_index', None)
            if config is None or store is None or index is None:
                continue
            servers.append(service)
        return servers

    def _server_for(self, user: str, space: str) -> MemoryMCPServer:
        key = ("public", "public") if space == "public" else (user, "personal")
        server = self._servers.get(key)
        if server is not None:
            return server
        namespace = self._namespace_for(user, space)
        self._bootstrap_workspace_key(namespace)
        server = MemoryMCPServer(
            config=self._namespace_config(namespace),
            embedding_engine=self._shared_engine,
            rebuild_legacy_vectors_on_start=False,
        )
        server.initialize()
        server._init_done.set()
        self._servers[key] = server
        return server

    def _namespace_for(self, user: str, space: str):
        if space == "public":
            return self.settings.data_dir / "public"
        if user == "jiangli":
            return self.settings.owner_data_dir
        return self.settings.data_dir / "users" / "kane"

    @staticmethod
    def _bootstrap_workspace_key(data_dir) -> None:
        provider = WorkspaceIdentityProvider(data_dir / "workspace.key")
        status = provider.status()
        if status.state == "missing":
            status = provider.bootstrap_key()
        if status.state != "ready":
            raise RuntimeError("workspace identity is unavailable")

    def _namespace_config(self, data_dir) -> Config:
        if self._base_config is None:
            raise RuntimeError("LAN runtime configuration is not initialized")
        config = deepcopy(self._base_config)
        config.data_dir = Path(data_dir).expanduser()
        config.apply_environment = False
        config.context_mode = "primary"
        config.adapter = "codex"
        config.context_vectors_required = False
        config.lan_shared_vector_cache = True
        # Server owns the local model; never recursively contact an HTTP provider.
        config.embedding_http_url = ""
        config.embedding_http_token_file = ""
        config.lan_mcp_client_config = ""
        return config

    def _validate_namespace_databases(self) -> None:
        paths = {
            (Path(self._namespace_for(user, space)) / "memory.db").resolve()
            for user, space in (
                ("jiangli", "personal"),
                ("kane", "personal"),
                ("jiangli", "public"),
            )
        }
        if len(paths) != 3:
            raise ValueError("LAN database directories must be distinct")
