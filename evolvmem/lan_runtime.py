"""Fixed-namespace, shared-model runtime for the trusted-LAN MCP slice."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading

from evolvmem.config import Config
from evolvmem.embedding import EmbeddingEngine
from evolvmem.lan_config import LanSettings
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.workspace_identity import WorkspaceIdentityProvider


_USERS = frozenset({"jiangli", "kane"})
_SPACES = frozenset({"personal", "public"})


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
        for server in set(self._servers.values()):
            server.shutdown()
        self._servers.clear()
        if self._shared_engine is not None:
            self._shared_engine.close()

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
